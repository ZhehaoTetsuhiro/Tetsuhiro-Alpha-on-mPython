#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键烧录：把板上要的那几个文件写进掌控板，顺便能写程序进去。

    pip install pyserial
    python3 tools/flash.py                     # 自动找串口，写进去，读回校验，重启
    python3 tools/flash.py --list              # 看看有哪些串口
    python3 tools/flash.py --port COM7         # 手动指定
    python3 tools/flash.py --program prog.ta   # 顺便把程序写进板子的 ta.prog
    python3 tools/flash.py --program prog.s    # .s 也行（会先汇编）
    python3 tools/flash.py --monitor           # 传完盯着串口看输出
    python3 tools/flash.py --dry-run           # 只演练不写

做的事（照 TTL 那套成熟流程）：
  1. 把 src/ 拼成 dist/board/ 那几个文件
  2. 自动认出掌控板的串口（CH340 / CH9102 / CP210x / ESP32 原生 USB 都认）
  3. 发两下 Ctrl-C 打断板上正在跑的程序，进 raw REPL
  4. 板上原来的文件**自动备份成 *.bak**
  5. 分块写进去，然后**读回来核对字节数和校验和**
  6. 软重启

⚠️ 这份工具**已经在真掌控板上跑过了**（2026-10-02，ESP32 + 官方 mPython 固件）。
  踩到的第一颗雷：**那版 MicroPython 没有 `bytes.fromhex`**。所以"一段字节怎么写
  进去"是**到板上现问**的（`ubinascii.unhexlify` → `binascii.unhexlify` →
  `bytes.fromhex` → `int.to_bytes` → 整数列表，谁行用谁），不假设电脑上有什么
  板上也有 —— 掌控板实际用的是 `int.to_bytes` 那条。
  另外写失败会**自动退回原来的文件**（.bak 换回来、没有备份的半截文件删掉），
  所以半路断线不会把板子留在"半个程序"的状态。
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SYS_SRC = os.path.join(ROOT, 'src')
sys.path.insert(0, SYS_SRC)
sys.path.insert(0, HERE)

CHUNK = 192                      # 一次写的字节数（板上一次收太多会噎住）
BAUD = 115200

# 掌控板常见的 USB 串口芯片；ESP32 原生 USB 也在里面
VIDPID = (
    (0x1a86, 0x7523),            # CH340
    (0x1a86, 0x55d4),            # CH9102
    (0x10c4, 0xea60),            # CP210x
    (0x303a, 0x1001),            # ESP32-S2/S3 原生 USB
    (0x0403, 0x6001),            # FTDI
)


# ── 串口 ─────────────────────────────────────────────────────

def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        print('要装 pyserial：pip install pyserial')
        return []
    return list(lp.comports())


def pick_port(explicit=None):
    ports = list_ports()
    if explicit:
        for p in ports:
            if p.device == explicit:
                return p
        return explicit
    scored = []
    for p in ports:
        vid = getattr(p, 'vid', None)
        pid = getattr(p, 'pid', None)
        score = 2 if (vid, pid) in VIDPID else 0
        if p.description and 'CH34' in p.description.upper():
            score += 1
        scored.append((score, p.device, p))
    scored.sort(reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][2]
    if scored:
        return scored[0][2]
    return None


# ── raw REPL ─────────────────────────────────────────────────

class RawRepl(object):
    """MicroPython 的 raw REPL 协议：Ctrl-A 进去，Ctrl-D 执行，Ctrl-B 出来。

    读这边要小心：设备回的是一整块 `OK<标准输出>\\x04<标准错误>\\x04`，
    所以不能"读到结尾等于 OK"，得"读到出现 OK"，再往后找两个 \\x04。
    缓冲留在这个对象里，跨调用不丢。
    """

    def __init__(self, ser, verbose=False):
        self.ser = ser
        self.verbose = verbose
        self.buf = bytearray()

    # -- 底层读写 --

    def _write(self, b):
        self.ser.write(b)
        try:
            self.ser.flush()
        except Exception:
            pass

    def _fill(self, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                n = self.ser.in_waiting
            except Exception:
                n = 0
            if n:
                self.buf += self.ser.read(n)
                return True
            time.sleep(0.01)
        return False

    def _take_until(self, token, timeout=3.0):
        """读到 token 出现为止，返回 token **之前**的内容（token 被吃掉）。"""
        t0 = time.time()
        while True:
            i = self.buf.find(token)
            if i >= 0:
                out = bytes(self.buf[:i])
                del self.buf[:i + len(token)]
                return out
            if time.time() - t0 > timeout:
                out = bytes(self.buf)
                del self.buf[:]
                return out
            self._fill(0.05)

    def _drain(self, quiet=0.15, maxt=2.0):
        """把设备吐出来的东西喝干净（不返回）。Ctrl-C 之后提示符可能来两遍。"""
        t0 = time.time()
        last = time.time()
        while time.time() - t0 < maxt:
            try:
                n = self.ser.in_waiting
            except Exception:
                n = 0
            if n:
                self.ser.read(n)
                last = time.time()
            else:
                time.sleep(0.01)
                if time.time() - last > quiet:
                    break
        self.buf = bytearray()

    # -- 进 / 出 --

    def enter(self):
        self._write(b'\r\x03\x03')           # 打断正在跑的程序
        self._take_until(b'>>>', 2.0)
        self._drain()                        # 提示符的尾巴别留在缓冲里
        self._write(b'\x01')                 # Ctrl-A -> raw REPL
        got = self._take_until(b'>', 3.0)
        if b'raw REPL' not in got:
            raise RuntimeError('进不了 raw REPL，收到：%r' % got[-80:])

    def leave(self):
        try:
            self._write(b'\x02')             # Ctrl-B -> 友好 REPL
        except Exception:
            pass

    # -- 执行一小段代码 --

    def exec(self, code, timeout=6.0):
        if not code.endswith('\n'):
            code += '\n'
        self._write(code.encode('utf-8') + b'\x04')
        self._take_until(b'OK', 3.0)                       # 等它说出 OK
        out = self._take_until(b'\x04', timeout)           # 标准输出
        err = self._take_until(b'\x04', 2.0)               # 标准错误
        if self.verbose and out:
            sys.stdout.write(out.decode('utf-8', 'replace'))
        return out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


# ── 写文件 ───────────────────────────────────────────────────

def check_exists(repl, name):
    out, err = repl.exec("import os\ntry:\n    print(os.stat(%r)[6])\nexcept OSError:\n"
                         "    print('MISS')" % name)
    return 'MISS' not in out


def backup(repl, name):
    """把板上原来的同名文件挪成 *.bak。**已经有 .bak 就不动它** —— 最先备份的
    那个才是原件；要是每次都覆盖，第一次写坏之后重试就会拿半截文件把原件顶掉。"""
    if not check_exists(repl, name):
        return False
    out, err = repl.exec(
        "import os\n"
        "try:\n"
        "    os.stat(%r + '.bak')\n"
        "    print('KEEP')\n"
        "except OSError:\n"
        "    os.rename(%r, %r + '.bak')\n"
        "    print('BAK')\n" % (name, name, name))
    return 'BAK' in out


# ── 一段字节怎么写进板上 ─────────────────────────────────────
#
# 不能假设电脑上有什么板上也有：掌控板那版 MicroPython **没有 bytes.fromhex**，
# 而它在电脑上（CPython）是天生就有的 —— 第一版就是这么栽的。所以先问板子一次。

DECODER_PROBE = (
    "try:\n"
    "    from ubinascii import unhexlify\n"
    "    print('UBIN')\n"
    "except ImportError:\n"
    "    try:\n"
    "        from binascii import unhexlify\n"
    "        print('BIN')\n"
    "    except ImportError:\n"
    "        if getattr(bytes, 'fromhex', None):\n"
    "            print('HEX')\n"
    "        elif getattr(int, 'to_bytes', None):\n"
    "            print('BYTES')\n"
    "        else:\n"
    "            print('INTS')\n"
)


def pick_writer(repl):
    """问板子支持哪种解码方式，返回 'ubin' / 'bin' / 'hex' / 'bytes' / 'ints'。"""
    out, err = repl.exec(DECODER_PROBE)
    for k in ('UBIN', 'BIN', 'HEX', 'BYTES'):
        if k in out:
            return k.lower()
    return 'ints'


def chunk_code(name, kind, part):
    """生成"把这 192 字节追加进文件"的那一句，**每个字块自给自足**。

    不用跨块变量（连打开的文件都不留着），这样板子的 raw REPL 就算是
    一块只认单条语句的，也能一条一条往下走。

    编码长度（串口上越快越好）：hex / bytes 都是"每字节 2 个字符"，
    ints 那个整数列表是 4 个字符上下 —— 61 KB 的板子代码在 115200 波特下，
    前者约 20 秒，后者要一分半，所以只当最后的保底。
    """
    if kind == 'ints':
        return "open(%r, 'ab').write(bytearray((%s,)))" \
               % (name, ','.join(str(b) for b in part))
    h = ''.join('%02x' % b for b in part)
    if kind == 'ubin':
        return "from ubinascii import unhexlify\nopen(%r, 'ab').write(unhexlify('%s'))" % (name, h)
    if kind == 'bin':
        return "from binascii import unhexlify\nopen(%r, 'ab').write(unhexlify('%s'))" % (name, h)
    if kind == 'bytes':
        # 没有 unhexlify/fromhex，但 int.to_bytes 一定有：拿整数转回字节
        return "open(%r, 'ab').write(int('%s', 16).to_bytes(%d, 'big'))" % (name, h, len(part))
    return "open(%r, 'ab').write(bytes.fromhex('%s'))" % (name, h)


def write_file(repl, name, data, dry_run=False, kind=None):
    """分块写，写完读回来核对字节数和校验和。"""
    if dry_run:
        print('  [dry-run] %s  %d 字节' % (name, len(data)))
        return True
    if kind is None:
        kind = pick_writer(repl)
    # 先清空（'wb' 建文件），后面每块都用 'ab' 追加 —— 块与块之间不留状态
    repl.exec("open(%r, 'wb').close()" % name)
    done = 0
    while done < len(data):
        part = data[done:done + CHUNK]
        code = chunk_code(name, kind, part)
        err = ''
        for _attempt in (0, 1):                  # 串口偶尔噎一下，同一块重试一次
            out, err = repl.exec(code)
            if not err.strip():
                break
        if err.strip():
            raise RuntimeError('写 %s 第 %d 字节（%d 字节一块）出错：%s'
                               % (name, done, len(part), err.strip()))
        done += len(part)
    # 读回来核对（不用 sum()：板上不一定有；b 是整数，自己加）
    want_sum = sum(data) & 0xffff
    out, err = repl.exec(
        "f = open(%r, 'rb')\n"
        "d = f.read()\n"
        "f.close()\n"
        "s = 0\n"
        "for b in d:\n"
        "    s = (s + b) & 0xffff\n"
        "print(len(d), s)\n" % name)
    try:
        got_len, got_sum = [int(x) for x in out.split()[:2]]
    except ValueError:
        raise RuntimeError('%s 读回来核对失败：%r %s' % (name, out, err.strip()))
    if got_len != len(data) or got_sum != want_sum:
        raise RuntimeError('%s 校验不过：写了 %d/%d，读回 %d/%d'
                           % (name, len(data), want_sum, got_len, got_sum))
    print('  %-14s %6d 字节  ✓ 读回核对过' % (name, len(data)))
    return True


def free_space(repl):
    """板上还剩多少字节；问不出来（或报 0，有些板子就这么报）就返回 None。"""
    out, err = repl.exec(
        "import os\n"
        "try:\n"
        "    s = os.statvfs('/')\n"
        "    print(s[0] * s[3])\n"
        "except Exception:\n"
        "    print('?')\n")
    try:
        n = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return n or None


def check_space(repl, need):
    """够不够写。返回 (够不够, 剩多少)。

    板子的文件系统很小（几十 KB 余量很常见），写到一半 ENOSPC 会把文件留成
    半截 —— 宁可在动手之前先问一句。
    """
    free = free_space(repl)
    if free is None:
        return True, None
    return free >= need + 4096, free          # 留 4 KB 余量


def rollback(repl, backed, names):
    """写坏了就把板子**退回原来的样子**：有 .bak 的换回来，没有备份的删掉半截文件。"""
    print('  写坏了 —— 正在把板子退回原来的文件……')
    for name in names:
        try:
            if name in backed:
                repl.exec("import os\n"
                          "try:\n    os.remove(%r)\nexcept OSError:\n    pass\n"
                          "os.rename(%r + '.bak', %r)\n" % (name, name, name))
            else:
                repl.exec("import os\ntry:\n    os.remove(%r)\nexcept OSError:\n    pass\n" % name)
        except Exception:
            pass


def soft_reset(repl):
    try:
        repl.exec('import machine\nmachine.soft_reset()')
    except Exception:
        pass


# ── main ─────────────────────────────────────────────────────

USAGE = __doc__


def main(argv):
    dry = '--dry-run' in argv
    if '--list' in argv:
        for p in list_ports():
            print('%-16s %s  [%s]' % (p.device, p.description or '', p.hwid))
        return 0

    # 板上的文件：先打好包
    import build
    outdir = os.path.join(ROOT, 'dist', 'board')
    build.build(outdir)
    # main.py 最后传：万一半路断了，板上旧的至少还能用
    order = [p for p in build.BOARD_FILES if p != build.ENTRY]
    order.append(build.ENTRY)

    payloads = []
    for name in order:
        with open(os.path.join(outdir, name), 'rb') as f:
            payloads.append((name, f.read()))

    # 顺便要写的程序
    prog = None
    if '--program' in argv:
        f = argv[argv.index('--program') + 1]
        prog = program_bytes(f)

    if dry:
        print('会写这些文件：')
        for n, d in payloads:
            print('  %-14s %6d 字节' % (n, len(d)))
        if prog:
            print('  %-14s %6d 字节' % ('ta.prog', len(prog)))
        return 0

    port = pick_port(argv[argv.index('--port') + 1] if '--port' in argv else None)
    if not port:
        print('没找到串口。插上板子，或者 --list 看一眼、--port 手动指定。')
        return 1
    dev = port if isinstance(port, str) else port.device
    print('串口：%s' % dev)

    try:
        import serial
    except ImportError:
        print('要装 pyserial：pip install pyserial')
        return 1

    ser = serial.Serial(dev, BAUD, timeout=0.2)
    repl = RawRepl(ser, verbose='-v' in argv)
    try:
        if '--doctor' in argv:
            repl.enter()
            out, err = repl.exec("import sys\nprint('micropython', sys.version)\n"
                                 "import os\nprint(sorted(os.listdir()))")
            print(out)
            return 0
        repl.enter()
        # 先备份同名的（已经有 .bak 的留着不动 —— 那才是原件）
        names = [n for n, _ in payloads] + (['ta.prog'] if prog is not None else [])
        backed = []
        for name in names:
            if backup(repl, name):
                backed.append(name)
                print('  %-14s 原来的备份成 %s.bak' % (name, name))
        kind = pick_writer(repl)
        print('  板上解码方式：%s' % kind)
        need = sum(len(d) for _, d in payloads) + (len(prog) if prog is not None else 0)
        ok, free = check_space(repl, need)
        if free is not None:
            print('  板上剩 %d 字节，这次要写 %d 字节' % (free, need))
        if not ok:
            print('  空间不够（至少还差 %d 字节）：先在板上删掉不用的文件再来。'
                  % (need + 4096 - free))
            return 1
        try:
            for name, data in payloads:
                write_file(repl, name, data, kind=kind)
            if prog is not None:
                write_file(repl, 'ta.prog', prog, kind=kind)
        except Exception:
            # 半路炸了：把板子退回写之前的样子，别留个"半个程序"
            rollback(repl, backed, names)
            raise
        soft_reset(repl)
        print('写完了，板子重启，应该直接进 TA 编辑器。')
        if '--monitor' in argv:
            repl.leave()
            monitor(ser)
    finally:
        try:
            repl.leave()
        except Exception:
            pass
        ser.close()
    return 0


def program_bytes(path):
    """把 .ta（四进制文本）或 .s（汇编）变成要写进 ta.prog 的字节。"""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    if path.endswith('.s'):
        import ta as T
        from ta_core import bytes_to_digits, digits_to_text
        text = digits_to_text(bytes_to_digits(T.assemble_bytes(text)))
    else:
        from ta_core import text_to_digits, digits_to_text
        text = digits_to_text(text_to_digits(text))
    return (text + '\n').encode('utf-8')


def monitor(ser):
    print('盯着串口看输出，Ctrl-C 退出。')
    try:
        while True:
            n = ser.in_waiting
            if n:
                sys.stdout.write(ser.read(n).decode('utf-8', 'replace'))
                sys.stdout.flush()
            else:
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
