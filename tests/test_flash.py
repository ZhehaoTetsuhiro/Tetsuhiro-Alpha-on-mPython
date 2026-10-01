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
import sys
import tempfile
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import flash


class FakeSerial(object):
    """一块假掌控板。"""

    def __init__(self, root):
        self.root = root
        self.out = bytearray()
        self.line = bytearray()
        self.ns = {}
        self.running = False
        self.transform_code = None          # 测试可以借它动手脚

    # pyserial 的那几个接口
    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n):
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
            return code.replace('sum(d) & 0xffff', '0')
        self.ser.transform_code = tamper
        try:
            flash.write_file(r, 'ta.prog', b'abcdef')
            self.fail('校验不过必须报错')
        except RuntimeError as e:
            self.assertIn('校验不过', str(e))

    def test_write_file_pads_odd_lengths(self):
        r = self._repl()
        flash.write_file(r, 'x.bin', b'a')          # 1 字节，肯定不是块大小的整数倍
        with open(os.path.join(self.tmp, 'x.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'a')

    def test_backup_renames_the_old_file(self):
        with open(os.path.join(self.tmp, 'ta.prog'), 'w') as f:
            f.write('old')
        r = self._repl()
        self.assertTrue(flash.backup(r, 'ta.prog'))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'ta.prog.bak')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'ta.prog')))

    def test_backup_is_a_noop_when_there_is_nothing(self):
        r = self._repl()
        self.assertFalse(flash.backup(r, 'nope.prog'))

    def test_chunking_covers_exactly_the_payload(self):
        r = self._repl()
        n = flash.CHUNK * 5 + 7
        data = bytes((i * 7) & 0xff for i in range(n))
        flash.write_file(r, 'big.bin', data)
        with open(os.path.join(self.tmp, 'big.bin'), 'rb') as f:
            self.assertEqual(len(f.read()), n)


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
