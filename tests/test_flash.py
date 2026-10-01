# -*- coding: utf-8 -*-
"""烧录工具的测试 —— 拿一块**假板子**跑，不插线。

`FakeSerial` 就是一块假的掌控板：它实现了 MicroPython 的 raw REPL 协议
（Ctrl-A 进去、Ctrl-D 执行、Ctrl-B 出来），并且**真的**在临时目录里执行
那些 `open/write/read` 语句。所以"分块写 → 读回核对字节数和校验和"这条路
是**从头到尾真跑了一遍**，不是打桩。

    python3 tests/test_flash.py -v
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import flash


class _NoFromhex(bytes):
    """一块**没有** unhexlify 也没有 bytes.fromhex 的板子上的 bytes。"""
    fromhex = None


class _NoToBytes(int):
    """连 int.to_bytes 都没有的板子上的 int。"""
    to_bytes = None


class _BoardFile(object):
    """板上那种"不 close 就不落盘"的文件对象。

    MicroPython 没有引用计数：`open(f,'ab').write(x)` 不 close 就**丢数据**
    （真板子上量过：文件一直是 0 字节）。CPython 会立刻回收并 flush，所以
    在电脑上怎么测都发现不了 —— 这个类专门把那层"电脑和板的差别"补上，
    谁再写出没 close 的写法，测试当场红。
    """

    def __init__(self, path, mode):
        self.path = path
        self.binary = 'b' in mode
        self.append = 'a' in mode
        self.buf = bytearray()
        self.closed = False
        if not self.append:
            with open(path, 'wb' if self.binary else 'w') as f:   # 打开就截空
                pass

    def write(self, data):
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.buf += data
        return len(data)

    def close(self):
        if not self.closed:
            self.closed = True
            with open(self.path, 'ab' if self.binary else 'a') as f:
                f.write(bytes(self.buf))
            self.buf = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __del__(self):
        pass          # 不 close 就丢 —— 板上就是这样


class _BoardReadFile(object):
    """板上读文件：`read()` **不带长度**要一口气要到整块内存，文件一大就 MemoryError。

    真板子上就是这样炸的（16 KB 的 ta_core.py）：
      MemoryError: memory allocation failed, allocating 13312 bytes
    所以读回来核对必须分块读；这个类负责把"电脑上随便读、板上不行"的差别补上。
    """

    BIG = 4096

    def __init__(self, path):
        self.f = open(path, 'rb')
        self.size = os.path.getsize(path)

    def read(self, n=None):
        if n is None:
            if self.size > self.BIG:
                raise MemoryError('memory allocation failed, allocating %d bytes'
                                  % self.size)
            return self.f.read()
        return self.f.read(n)

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _board_open(path, mode='r'):
    if 'w' in mode or 'a' in mode or '+' in mode:
        return _BoardFile(path, mode)
    return _BoardReadFile(path)


class FakeSerial(object):
    """一块假掌控板。"""

    def __init__(self, root, board_hex=True, board_bytes=True, lazy_files=True,
                 slow=0.0):
        self.root = root
        self.out = bytearray()
        self.line = bytearray()
        self.ns = {}
        self.running = False
        self.transform_code = None          # 测试可以借它动手脚
        self.slow = slow                    # >0：假装板子要这么久才回话
        self.pending = []                   # [(什么时候能回话, 要跑的代码)]
        # board_hex=False：假装是掌控板那版 MicroPython —— 没有 ubinascii.unhexlify，
        # 也没有 bytes.fromhex（真板子上就是这么栽的）
        self.board_hex = board_hex
        # board_bytes=False：连 int.to_bytes 都没有（只留整数列表那条保底路）
        self.board_bytes = board_bytes
        # lazy_files=True：文件对象不 close 就不落盘（真板子的脾气，见 _BoardFile）
        self.lazy_files = lazy_files

    # pyserial 的那几个接口
    @property
    def in_waiting(self):
        self._deliver_due()
        return len(self.out)

    def _deliver_due(self):
        """到点了才把回话吐出来 —— 板子慢是**墙上时间**上的慢，不是写的时候就慢。"""
        now = time.time()
        while self.pending and self.pending[0][0] <= now:
            _, code = self.pending.pop(0)
            self._run(code)

    def read(self, n):
        self._deliver_due()
        b = bytes(self.out[:n])
        del self.out[:n]
        return b

    def flush(self):
        pass

    def write(self, b):
        for ch in b:
            if ch == 4:                     # Ctrl-D：执行到这儿为止的那段代码
                code = bytes(self.line[:-1]).decode('utf-8')
                self.line = bytearray()
                if self.slow:
                    # 板子要过一会儿才回话（编译一个 15 KB 模块就是这样）
                    self.pending.append((time.time() + self.slow, code))
                else:
                    self._run(code)
            elif ch == 1:                   # Ctrl-A：进 raw REPL
                self.line = bytearray()
                self.out += b'raw REPL; CTRL-B to exit\r\n>'
            elif ch == 2:                   # Ctrl-B：回友好 REPL
                self.line = bytearray()
                self.out += b'\r\n>>> '
            elif ch == 3:                   # Ctrl-C：打断
                self.line = bytearray()
                self.out += b'>>> '
            else:
                self.line.append(ch)

    def _run(self, code):
        if self.transform_code:
            code = self.transform_code(code)
        if not self.board_hex:
            code = code.replace('ubinascii', 'ubinascii_missing')
            code = code.replace('binascii', 'binascii_missing')
            self.ns['bytes'] = _NoFromhex       # 把内建 bytes 换成没有 fromhex 的
        if not self.board_bytes:
            self.ns['int'] = _NoToBytes         # int 也没有 to_bytes
        if self.lazy_files:
            self.ns['open'] = _board_open       # 不 close 就不落盘
        buf = io.StringIO()
        err = ''
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with contextlib.redirect_stdout(buf):
                exec(code, self.ns)
        except Exception:
            err = traceback.format_exc()
        finally:
            os.chdir(cwd)
        self.out += b'OK' + buf.getvalue().encode('utf-8') + b'\x04' + err.encode('utf-8') + b'\x04'


class TestRawRepl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ta-flash-')
        self.ser = FakeSerial(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _repl(self):
        r = flash.RawRepl(self.ser)
        r.enter()
        return r

    def test_enter_raw_repl(self):
        r = self._repl()
        self.assertEqual(r.buf, bytearray())

    def test_exec_returns_stdout(self):
        r = self._repl()
        out, err = r.exec("print('hello', 1 + 1)")
        self.assertEqual(out.strip(), 'hello 2')
        self.assertEqual(err.strip(), '')

    def test_exec_reports_errors_separately(self):
        r = self._repl()
        out, err = r.exec("print('x')\n1 / 0")
        self.assertEqual(out.strip(), 'x')
        self.assertIn('ZeroDivisionError', err)

    def test_exec_waits_for_a_slow_board(self):
        """板子慢（编译 15 KB 要好几秒）时，输出不能被切错位置。

        老代码等 'OK' 只等 3 秒：超时之后把 'OK' 和后面的输出当成 out 交回来，
        还把剩下的内容顶到下一次读 —— 真板子上 --doctor 就是这么把一个
        `ok ta_ui` 塞进 stderr 的。
        """
        ser = FakeSerial(self.tmp, slow=3.5)
        r = flash.RawRepl(ser)
        r.enter()
        out, err = r.exec("print('goodbye')")
        self.assertEqual(out.strip(), 'goodbye')
        self.assertEqual(err.strip(), '')
        # 而且流没被搅乱：下一段还读得对
        out2, err2 = r.exec("print('second')")
        self.assertEqual(out2.strip(), 'second')

    def test_exec_complains_when_the_board_says_nothing(self):
        ser = FakeSerial(self.tmp)
        r = flash.RawRepl(ser)
        r._write = lambda b: None                 # 代码根本发不出去 = 板子不理人
        try:
            r.exec('print(1)', ok_timeout=0.4)
            self.fail('没回应就该报错，别硬撑着读')
        except RuntimeError as e:
            self.assertIn('没回应', str(e))

    def test_write_file_and_verify(self):
        r = self._repl()
        data = bytes(range(256)) * 3 + b'tail'      # 比一个块大，且不平整
        flash.write_file(r, 'ta.prog', data)
        with open(os.path.join(self.tmp, 'ta.prog'), 'rb') as f:
            got = f.read()
        self.assertEqual(got, data)

    def test_write_file_detects_a_bad_readback(self):
        r = self._repl()
        # 让"读回核对"那一步报一个假校验和 —— 必须当场炸，不能蒙混过去
        def tamper(code):
            return code.replace('s = (s + c) & 0xffff', 's = 0')
        self.ser.transform_code = tamper
        try:
            flash.write_file(r, 'ta.prog', b'abcdef')
            self.fail('校验不过必须报错')
        except RuntimeError as e:
            self.assertIn('校验不过', str(e))

    def test_readback_reads_in_pieces(self):
        """读回来核对必须**分块读**：一口气 f.read() 在板上会 MemoryError。"""
        code = flash.readback_code('ta_core.py')
        self.assertIn('f.read(256)', code)
        self.assertNotIn('d = f.read()', code)
        self.assertIn('gc.collect()', code)

    def test_the_fake_board_blows_up_on_a_whole_file_read(self):
        """先钉住假板子的这条脾气，上一条测试才有意义。"""
        r = self._repl()
        r.exec("f = open('b.bin', 'wb')\nf.write(b'x' * 5000)\nf.close()")
        out, err = r.exec("f = open('b.bin', 'rb')\nd = f.read()\nf.close()")
        self.assertIn('MemoryError', err)

    def test_big_file_verifies_without_holding_it_in_memory(self):
        r = self._repl()
        data = bytes((i * 7 + 1) & 0xff for i in range(5000))   # 比假板子"一口气读"的上限大
        flash.write_file(r, 'ta_core.py', data)
        with open(os.path.join(self.tmp, 'ta_core.py'), 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_write_file_pads_odd_lengths(self):
        r = self._repl()
        flash.write_file(r, 'x.bin', b'a')          # 1 字节，肯定不是块大小的整数倍
        with open(os.path.join(self.tmp, 'x.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'a')

    # ---- 板上没有 unhexlify / bytes.fromhex 的那档事（真板子踩过）----

    def test_the_fake_board_loses_unclosed_writes(self):
        """先把假板子的脾气钉住：**不 close 的写就该丢**（真板子就是这样）。

        这条要是红了，说明 _BoardFile 不再模拟那块板子，后面那些"写得对"的
        测试就都失去意义了。
        """
        r = self._repl()
        r.exec("open('z.bin', 'wb').close()")
        r.exec("open('z.bin', 'ab').write(b'hi')")      # 故意不 close
        out, err = r.exec("f = open('z.bin', 'rb')\nd = f.read()\nf.close()\nprint(len(d))")
        self.assertEqual(out.strip(), '0')

    def test_chunk_code_closes_the_file(self):
        # 掌控板上不 close 就丢数据 —— 五种编码都得有 close
        for kind in ('ubin', 'bin', 'hex', 'bytes', 'ints'):
            self.assertIn('f.close()', flash.chunk_code('ta.prog', kind, b'AB'))

    def test_write_file_through_a_board_that_drops_unclosed_writes(self):
        r = self._repl()
        data = bytes((i * 11 + 5) & 0xff for i in range(flash.CHUNK * 3 + 7))
        flash.write_file(r, 'ta_app.py', data)
        with open(os.path.join(self.tmp, 'ta_app.py'), 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_pick_writer_asks_the_board(self):
        r = self._repl()
        # 电脑上（CPython）没有 ubinascii，但有 binascii —— 问出来什么就是什么
        self.assertIn(flash.pick_writer(r), ('ubin', 'bin', 'hex'))

    def test_pick_writer_falls_back_to_bytes_on_a_stripped_board(self):
        """真板子的样子：没有 unhexlify、没有 fromhex，但有 int.to_bytes。"""
        ser = FakeSerial(self.tmp, board_hex=False)
        r = flash.RawRepl(ser)
        r.enter()
        self.assertEqual(flash.pick_writer(r), 'bytes')

    def test_pick_writer_falls_back_to_ints_on_a_bare_board(self):
        ser = FakeSerial(self.tmp, board_hex=False, board_bytes=False)
        r = flash.RawRepl(ser)
        r.enter()
        self.assertEqual(flash.pick_writer(r), 'ints')

    def test_write_file_on_a_board_without_unhexlify(self):
        """掌控板那版 MicroPython 没有 bytes.fromhex —— 也得能写进去。"""
        ser = FakeSerial(self.tmp, board_hex=False)
        r = flash.RawRepl(ser)
        r.enter()
        data = bytes((i * 13 + 7) & 0xff for i in range(flash.CHUNK * 2 + 5))
        flash.write_file(r, 'ta_bits.py', data)
        with open(os.path.join(self.tmp, 'ta_bits.py'), 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_write_file_on_a_board_with_nothing_at_all(self):
        """连 int.to_bytes 都没有：整数列表也得写对。"""
        ser = FakeSerial(self.tmp, board_hex=False, board_bytes=False)
        r = flash.RawRepl(ser)
        r.enter()
        data = bytes((i * 13 + 7) & 0xff for i in range(flash.CHUNK + 3))
        flash.write_file(r, 'ta_core.py', data)
        with open(os.path.join(self.tmp, 'ta_core.py'), 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_chunk_code_opens_and_closes_every_time(self):
        # 每块自己 open、自己 close —— 不依赖上一块留下的句柄（板上不 close 就丢数据）
        for kind in ('ubin', 'bin', 'hex', 'bytes', 'ints'):
            code = flash.chunk_code('ta.prog', kind, b'AB')
            self.assertIn("f = open('ta.prog', 'ab')", code)
            self.assertIn('f.close()', code)

    def test_chunk_code_bytes_puts_the_length_in(self):
        code = flash.chunk_code('x.bin', 'bytes', b'\x00\x00\xff')
        self.assertIn("int('0000ff', 16).to_bytes(3, 'big')", code)

    def test_chunk_code_ints_uses_a_literal(self):
        code = flash.chunk_code('x.bin', 'ints', b'\x00\xff')
        self.assertIn('bytearray((0,255,))', code)

    def test_rollback_puts_the_original_back(self):
        with open(os.path.join(self.tmp, 'ta_bits.py'), 'w') as f:
            f.write('old')
        r = self._repl()
        self.assertTrue(flash.backup(r, 'ta_bits.py'))
        with open(os.path.join(self.tmp, 'ta_bits.py'), 'w') as f:
            f.write('half')                       # 写了一半就断了
        flash.rollback(r, ['ta_bits.py'], ['ta_bits.py', 'ta.prog'])
        with open(os.path.join(self.tmp, 'ta_bits.py')) as f:
            self.assertEqual(f.read(), 'old')     # 原件回来了
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'ta_bits.py.bak')))

    def test_rollback_drops_files_that_never_existed(self):
        r = self._repl()
        with open(os.path.join(self.tmp, 'ta_app.py'), 'w') as f:
            f.write('half')
        flash.rollback(r, [], ['ta_app.py', 'ta.prog'])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'ta_app.py')))

    def test_backup_renames_the_old_file(self):
        with open(os.path.join(self.tmp, 'ta.prog'), 'w') as f:
            f.write('old')
        r = self._repl()
        self.assertTrue(flash.backup(r, 'ta.prog'))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'ta.prog.bak')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'ta.prog')))

    def test_second_backup_keeps_the_first_one(self):
        """第一次写坏之后重试，不能拿半截文件把原件顶掉。"""
        with open(os.path.join(self.tmp, 'ta.bin'), 'w') as f:
            f.write('original')
        r = self._repl()
        self.assertTrue(flash.backup(r, 'ta.bin'))
        with open(os.path.join(self.tmp, 'ta.bin'), 'w') as f:
            f.write('')                           # 重试时板上是空的半截文件
        self.assertFalse(flash.backup(r, 'ta.bin'))
        with open(os.path.join(self.tmp, 'ta.bin.bak')) as f:
            self.assertEqual(f.read(), 'original')

    def test_backup_is_a_noop_when_there_is_nothing(self):
        r = self._repl()
        self.assertFalse(flash.backup(r, 'nope.prog'))

    def test_space_check_refuses_when_the_board_is_full(self):
        r = self._repl()
        ok, free = flash.check_space(r, 1024)          # 电脑的盘大得很
        self.assertTrue(ok)
        if free is not None:
            self.assertGreater(free, 1024)

        def pretend_full(code):
            return code.replace("os.statvfs('/')", "((1, 1, 1, 4, 0, 0, 0, 0, 0, 0))")
        self.ser.transform_code = pretend_full
        ok, free = flash.check_space(r, 1024)          # 只剩 4 字节
        self.assertFalse(ok)
        self.assertEqual(free, 4)

    def test_chunking_covers_exactly_the_payload(self):
        r = self._repl()
        n = flash.CHUNK * 5 + 7
        data = bytes((i * 7) & 0xff for i in range(n))
        flash.write_file(r, 'big.bin', data)
        with open(os.path.join(self.tmp, 'big.bin'), 'rb') as f:
            self.assertEqual(len(f.read()), n)


class _Recorder(object):
    """假的 RawRepl：只把发给板子的那几段代码记下来。"""

    def __init__(self):
        self.codes = []

    def exec(self, code, timeout=6.0):
        self.codes.append(code)
        return '', ''


class TestOnBoardSyntax(unittest.TestCase):
    """发给板子的每一段代码，都得**真 MicroPython** 认。

    CPython 跑得过不算数：`bytes.fromhex` 在电脑上天生就有、在掌控板上没有，
    第一版就是栽在这上面。这些语句在真板上跑之前，先让 micropython 解析一遍。
    没装 micropython 就跳过（`make size` 也用得上它）。
    """

    def _micropython(self):
        path = shutil.which('micropython')
        if not path:
            raise unittest.SkipTest('没装 micropython，跳过板上语法检查')
        return path

    def _collect(self):
        r = _Recorder()
        flash.backup(r, 'ta_bits.py')
        flash.free_space(r)
        try:
            flash.write_file(r, 'ta_bits.py', bytes(range(256)) * 2)
        except Exception:
            pass
        flash.rollback(r, ['ta_bits.py'], ['ta_bits.py', 'ta.prog'])
        flash.rollback(r, [], ['ta_app.py'])
        r.codes.append(flash.DECODER_PROBE)
        for kind in ('ubin', 'bin', 'hex', 'bytes', 'ints'):
            r.codes.append(flash.chunk_code('ta_bits.py', kind, bytes(range(32))))
        return r.codes

    def test_every_snippet_parses_on_micropython(self):
        mpy = self._micropython()
        codes = self._collect()
        self.assertGreaterEqual(len(codes), 10)
        lines = []
        for i, c in enumerate(codes):
            lines.append('def _snip_%d():' % i)
            lines.append(textwrap.indent(c.rstrip('\n'), '    '))
            lines.append('    pass')
        tmp = tempfile.mkdtemp(prefix='ta-parse-')
        try:
            p = os.path.join(tmp, 'snips.py')
            with open(p, 'w') as f:
                f.write('\n'.join(lines) + '\n')
            res = subprocess.run([mpy, p], capture_output=True, text=True, timeout=120)
            self.assertEqual(res.returncode, 0,
                             '有语句板子解析不了：%s' % res.stderr[:800])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_decoder_probe_is_valid_micropython(self):
        """板上第一句就是这个探测；它要是在板上解析不了，后面全白搭。"""
        mpy = self._micropython()
        tmp = tempfile.mkdtemp(prefix='ta-probe-')
        try:
            p = os.path.join(tmp, 'probe.py')
            with open(p, 'w') as f:
                f.write(flash.DECODER_PROBE)
            res = subprocess.run([mpy, p], capture_output=True, text=True, timeout=120)
            self.assertEqual(res.returncode, 0, res.stderr[:400])
            self.assertIn(res.stdout.strip(), ('UBIN', 'BIN', 'HEX', 'INTS'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestProgramBytes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ta-prog-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ta_text_is_normalised(self):
        p = os.path.join(self.tmp, 'a.ta')
        with open(p, 'w') as f:
            f.write('0 1 2 3\n# 注释\nPYTH\n')
        got = flash.program_bytes(p).decode()
        self.assertEqual(got, '0123' + '3210' + '\n')

    def test_s_file_is_assembled(self):
        p = os.path.join(self.tmp, 'a.s')
        with open(p, 'w') as f:
            f.write('    li a0, 5\n    ecall\n')
        got = flash.program_bytes(p).decode().replace('\n', '')
        from ta_core import text_to_digits, words_of
        self.assertEqual(words_of(text_to_digits(got))[0], 0x00500513)


class TestFlashMain(unittest.TestCase):
    def test_dry_run_needs_no_hardware(self):
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = flash.main(['--dry-run'])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        for name in ('ta_bits.py', 'ta_core.py', 'ta_ui.py', 'ta_view.py',
                     'ta_app.py', 'ta_hw.py', 'main.py'):
            self.assertIn(name, out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
