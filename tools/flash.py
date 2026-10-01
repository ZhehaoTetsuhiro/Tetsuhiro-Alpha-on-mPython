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

⚠️ 这份工具**还没在真板子上跑过**（手上没板子）。协议是照 MicroPython 的
   raw REPL 写的，和 TTL 的 tools/flash.py 一致；上了板子要是有问题，
   先跑 `--dry-run` 看它认端口、切 raw REPL 这两步对不对。
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
    if not check_exists(repl, name):
        return False
    repl.exec("import os\ntry:\n    os.remove(%r + '.bak')\nexcept OSError:\n    pass\n"
              "os.rename(%r, %r + '.bak')\nprint('BAK')" % (name, name, name))
    return True


def write_file(repl, name, data, dry_run=False):
    """分块写，写完读回来核对字节数和校验和。"""
    if dry_run:
        print('  [dry-run] %s  %d 字节' % (name, len(data)))
        return True
    repl.exec("f = open(%r, 'wb')" % name)
    done = 0
    while done < len(data):
        part = data[done:done + CHUNK]
        hexs = ''.join('%02x' % b for b in part)
        out, err = repl.exec("f.write(bytes.fromhex('%s'))" % hexs)
        if err.strip():
            repl.exec('f.close()')
            raise RuntimeError('写 %s 出错：%s' % (name, err.strip()))
        done += len(part)
    repl.exec('f.close()')
    # 读回来核对
    want_sum = sum(data) & 0xffff
    out, err = repl.exec(
        "f = open(%r, 'rb')\nd = f.read()\nf.close()\n"
        "print(len(d), sum(d) & 0xffff)" % name)
    try:
        got_len, got_sum = [int(x) for x in out.split()[:2]]
    except ValueError:
        raise RuntimeError('%s 读回来核对失败：%r' % (name, out))
    if got_len != len(data) or got_sum != want_sum:
        raise RuntimeError('%s 校验不过：写了 %d/%d，读回 %d/%d'
                           % (name, len(data), want_sum, got_len, got_sum))
    print('  %-14s %6d 字节  ✓ 读回核对过' % (name, len(data)))
    return True


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
        # 先备份同名的
        for name, _ in payloads:
            if backup(repl, name):
                print('  %-14s 原来的备份成 %s.bak' % (name, name))
        for name, data in payloads:
            write_file(repl, name, data)
        if prog is not None:
            write_file(repl, 'ta.prog', prog)
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
