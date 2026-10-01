#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TA 电脑端命令行 —— 汇编、反汇编、跑、看按键、模拟掌控板。

    python3 tools/ta.py asm   prog.s   -o prog.ta     助记符 -> 四进制文本
    python3 tools/ta.py dis   prog.ta                 四进制文本 -> 助记符
    python3 tools/ta.py run   prog.ta  --in 3,0.5     在电脑上跑
    python3 tools/ta.py keys  prog.ta                 在板上该按哪几个键
    python3 tools/ta.py sim   prog.ta                 终端里模拟掌控板
    python3 tools/ta.py push  prog.s                  汇编好写进板子的 ta.prog
    python3 tools/ta.py pull  -o 备份.ta               从板子读回来并反汇编
    python3 tools/ta.py info                          指令与 I/O 约定

⚠️ 这个 asm 是**给人写程序 / 造样例用的开发工具**，跑在电脑上。
   "在掌控板上直接输入汇编" 那件事的方案见 Plan/汇编输入方案.md —— 还没拍板。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))

from ta_core import (VM, ST_END, ST_ERR, ST_NEED_IN, ST_LIMIT,
                     text_to_digits, digits_to_text, words_of,
                     words_to_bytes, bytes_to_digits, decode)

# ── 寄存器名 ─────────────────────────────────────────────────

ABI = ('zero', 'ra', 'sp', 'gp', 'tp', 't0', 't1', 't2', 's0', 's1',
       'a0', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7',
       's2', 's3', 's4', 's5', 's6', 's7', 's8', 's9', 's10', 's11',
       't3', 't4', 't5', 't6')

REGS = {}
for _i, _n in enumerate(ABI):
    REGS[_n] = _i
    REGS['x%d' % _i] = _i
REGS['fp'] = 8

# ── 编码表 ───────────────────────────────────────────────────

OP_LOAD, OP_STORE, OP_IMM, OP_REG = 0x03, 0x23, 0x13, 0x33
OP_LUI, OP_AUIPC, OP_BRANCH, OP_JALR, OP_JAL = 0x37, 0x17, 0x63, 0x67, 0x6f
OP_SYS, OP_FENCE = 0x73, 0x0f

R_OPS = {'add': (0x00, 0), 'sub': (0x20, 0), 'sll': (0x00, 1), 'slt': (0x00, 2),
         'sltu': (0x00, 3), 'xor': (0x00, 4), 'srl': (0x00, 5), 'sra': (0x20, 5),
         'or': (0x00, 6), 'and': (0x00, 7)}
M_OPS = {'mul': 0, 'mulh': 1, 'mulhsu': 2, 'mulhu': 3,
         'div': 4, 'divu': 5, 'rem': 6, 'remu': 7}
I_OPS = {'addi': 0, 'slti': 2, 'sltiu': 3, 'xori': 4, 'ori': 6, 'andi': 7}
SH_OPS = {'slli': 0x00, 'srli': 0x00, 'srai': 0x08}
LOAD3 = {'lb': 0, 'lh': 1, 'lw': 2, 'ld': 3, 'lbu': 4, 'lhu': 5, 'lwu': 6, 'lq': 7}
STORE3 = {'sb': 0, 'sh': 1, 'sw': 2, 'sd': 3, 'sq': 4}
BR3 = {'beq': 0, 'bne': 1, 'blt': 4, 'bge': 5, 'bltu': 6, 'bgeu': 7}
SYS_IMM = {'ecall': 0x000, 'ebreak': 0x001, 'sret': 0x002,
           'dret': 0x102, 'mret': 0x202, 'tret': 0x302}


def r_type(f7, rs2, rs1, f3, rd):
    return ((f7 & 0x7f) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | OP_REG


def i_type(op, f3, rd, rs1, imm):
    return ((imm & 0xfff) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def s_type(f3, rs1, rs2, imm):
    return (((imm >> 5) & 0x7f) << 25) | (rs2 << 20) | (rs1 << 15) | \
           (f3 << 12) | ((imm & 0x1f) << 7) | OP_STORE


def b_type(f3, rs1, rs2, imm):
    return (((imm >> 12) & 1) << 31) | (((imm >> 5) & 0x3f) << 25) | (rs2 << 20) | \
           (rs1 << 15) | (f3 << 12) | (((imm >> 1) & 0xf) << 8) | \
           (((imm >> 11) & 1) << 7) | OP_BRANCH


def u_type(op, rd, imm20):
    return (imm20 << 12) | (rd << 7) | op


def j_type(rd, imm):
    return (((imm >> 20) & 1) << 31) | (((imm >> 1) & 0x3ff) << 21) | \
           (((imm >> 11) & 1) << 20) | (((imm >> 12) & 0xff) << 12) | (rd << 7) | OP_JAL


# ── 汇编器 ───────────────────────────────────────────────────

class AsmError(Exception):
    pass


def _reg(tok, line):
    t = tok.strip().lower()
    if t not in REGS:
        raise AsmError('第 %d 行：不认识的寄存器 %r' % (line, tok))
    return REGS[t]


def _imm(tok, line):
    t = tok.strip()
    neg = False
    if t.startswith('-'):
        neg, t = True, t[1:]
    try:
        if t.startswith('0x') or t.startswith('0X'):
            v = int(t, 16)
        elif t.startswith('0b'):
            v = int(t, 2)
        else:
            v = int(t, 10)
    except ValueError:
        raise AsmError('第 %d 行：不是数字 %r' % (line, tok))
    return -v if neg else v


def _mem(tok, line):
    """'12(x5)' / '(x5)' -> (imm, reg)。"""
    t = tok.strip()
    if '(' not in t or not t.endswith(')'):
        raise AsmError('第 %d 行：访存操作数要写成 偏移(寄存器)：%r' % (line, tok))
    off, rest = t.split('(', 1)
    reg = rest[:-1]
    return (_imm(off, line) if off.strip() else 0), _reg(reg, line)


PSEUDO = ('li', 'mv', 'nop', 'j', 'jr', 'ret', 'beqz', 'bnez', 'la')
DIRECTIVES = ('.text', '.data', '.globl', '.word', '.half', '.byte')


def _split(s):
    return [p.strip() for p in s.replace(',', ' ').split() if p.strip()]


def assemble(src):
    """汇编源码 -> 字表（最后不满一条的字节补 0，别悄悄丢掉）。"""
    b = assemble_bytes(src)
    words = []
    for i in range(0, len(b), 4):
        chunk = b[i:i + 4]
        v = 0
        for k in range(len(chunk)):
            v |= chunk[k] << (8 * k)
        words.append(v)
    return words


def assemble_bytes(src):
    """两遍：先定标签（**字节**地址），再发射。返回小端字节流（就是 .tthr 的格式）。

    用字节而不是字来排版，是因为 .byte 段（数据、故意写坏的指令）本来就是
    零碎的 —— `.org`/对齐那套先不做，指令之间保证 4 字节对齐就够用了。
    """
    clean = []
    for i, raw in enumerate(src.split('\n'), 1):
        s = raw.split('#', 1)[0].split(';', 1)[0].strip()
        if s:
            clean.append((i, s))

    labels = {}
    laid = []
    addr = 0
    for lineno, s in clean:
        while s and ':' in s.split()[0]:
            head, _, s = s.partition(':')
            labels[head.strip()] = addr
            s = s.strip()
        if not s:
            continue
        parts = _split(s)
        laid.append((lineno, s, addr))
        addr += _size_of(parts[0].lower(), parts, lineno)

    out = bytearray()
    for lineno, s, addr in laid:
        parts = _split(s)
        m = parts[0].lower()
        if m in DIRECTIVES:
            _emit_directive(m, parts, labels, out, lineno)
            continue
        words = []
        if m in PSEUDO:
            emit_pseudo(m, parts, addr, labels, words, lineno)
        else:
            words.append(encode(m, parts, addr, labels, lineno))
        for w in words:
            out.append(w & 0xff)
            out.append((w >> 8) & 0xff)
            out.append((w >> 16) & 0xff)
            out.append((w >> 24) & 0xff)
    return out


def _size_of(m, parts, lineno):
    if m in ('.text', '.data', '.globl'):
        return 0
    if m == '.byte':
        return len(parts) - 1
    if m == '.half':
        return 2 * (len(parts) - 1)
    if m == '.word':
        return 4 * (len(parts) - 1)
    if m == 'la':
        return 8                       # 恒定两个字，偏移布局两遍都看得见
    if m == 'li':
        try:
            v = _imm(parts[2], lineno)  # li 的操作数是 (rd, imm)
            return 4 if -2048 <= v < 2048 else 8
        except AsmError:
            return 8
    return 4


def _data_val(tok, labels, lineno):
    t = tok.strip()
    if t in labels:
        return labels[t]
    return _imm(t, lineno)


def _emit_directive(m, parts, labels, out, lineno):
    if m in ('.text', '.data', '.globl'):
        return
    if m == '.byte':
        for t in parts[1:]:
            out.append(_data_val(t, labels, lineno) & 0xff)
    elif m == '.half':
        for t in parts[1:]:
            v = _data_val(t, labels, lineno) & 0xffff
            out.append(v & 0xff)
            out.append((v >> 8) & 0xff)
    elif m == '.word':
        for t in parts[1:]:
            v = _data_val(t, labels, lineno) & 0xffffffff
            out.append(v & 0xff)
            out.append((v >> 8) & 0xff)
            out.append((v >> 16) & 0xff)
            out.append((v >> 24) & 0xff)


def _target(tok, addr, labels, lineno):
    """分支 / 跳转目标 -> 相对当前指令的字节偏移。"""
    t = tok.strip()
    if t in labels:
        return labels[t] - addr
    return _imm(t, lineno)


def emit_pseudo(m, parts, addr, labels, words, lineno):
    if m == 'nop':
        words.append(i_type(OP_IMM, 0, 0, 0, 0))
    elif m == 'mv':
        words.append(i_type(OP_IMM, 0, _reg(parts[1], lineno), _reg(parts[2], lineno), 0))
    elif m == 'ret':
        words.append(i_type(OP_JALR, 0, 0, 1, 0))
    elif m == 'jr':
        words.append(i_type(OP_JALR, 0, 0, _reg(parts[1], lineno), 0))
    elif m == 'j':
        words.append(j_type(0, _target(parts[1], addr, labels, lineno)))
    elif m in ('beqz', 'bnez'):
        f3 = 0 if m == 'beqz' else 1
        words.append(b_type(f3, _reg(parts[1], lineno), 0,
                            _target(parts[2], addr, labels, lineno)))
    elif m == 'li':
        rd = _reg(parts[1], lineno)
        v = _imm(parts[2], lineno)
        if -2048 <= v < 2048:
            words.append(i_type(OP_IMM, 0, rd, 0, v & 0xfff))
        else:
            hi = (v + 0x800) >> 12
            lo = v - (hi << 12)
            words.append(u_type(OP_LUI, rd, hi & 0xfffff))
            words.append(i_type(OP_IMM, 0, rd, rd, lo & 0xfff))
    elif m == 'la':
        rd = _reg(parts[1], lineno)
        sym = parts[2]
        if sym not in labels:
            raise AsmError('第 %d 行：没有这个标签 %r' % (lineno, sym))
        off = labels[sym] - addr
        hi = (off + 0x800) >> 12
        lo = off - (hi << 12)
        words.append(u_type(OP_AUIPC, rd, hi & 0xfffff))
        words.append(i_type(OP_IMM, 0, rd, rd, lo & 0xfff))


def encode(m, parts, addr, labels, lineno):
    if m in R_OPS:
        f7, f3 = R_OPS[m]
        return r_type(f7, _reg(parts[3], lineno), _reg(parts[2], lineno),
                      f3, _reg(parts[1], lineno))
    if m in M_OPS:
        return r_type(0x01, _reg(parts[3], lineno), _reg(parts[2], lineno),
                      M_OPS[m], _reg(parts[1], lineno))
    if m in I_OPS:
        return i_type(OP_IMM, I_OPS[m], _reg(parts[1], lineno),
                      _reg(parts[2], lineno), _imm(parts[3], lineno) & 0xfff)
    if m in SH_OPS:
        sh = _imm(parts[3], lineno)
        if not 0 <= sh <= 127:
            raise AsmError('第 %d 行：移位量要在 0..127（给的是 %d）' % (lineno, sh))
        f7 = SH_OPS[m]
        if m == 'srai':
            return ((0x08 << 27) | (sh << 20)) | (_reg(parts[2], lineno) << 15) | \
                   (5 << 12) | (_reg(parts[1], lineno) << 7) | OP_IMM
        return i_type(OP_IMM, 1 if m == 'slli' else 5, _reg(parts[1], lineno),
                      _reg(parts[2], lineno), sh)
    if m in LOAD3:
        off, rs = _mem(parts[2], lineno)
        return i_type(OP_LOAD, LOAD3[m], _reg(parts[1], lineno), rs, off & 0xfff)
    if m in STORE3:
        off, rs = _mem(parts[2], lineno)
        return s_type(STORE3[m], rs, _reg(parts[1], lineno), off & 0xfff)
    if m in BR3:
        return b_type(BR3[m], _reg(parts[1], lineno), _reg(parts[2], lineno),
                      _target(parts[3], addr, labels, lineno))
    if m == 'lui':
        return u_type(OP_LUI, _reg(parts[1], lineno), _imm(parts[2], lineno) & 0xfffff)
    if m == 'auipc':
        return u_type(OP_AUIPC, _reg(parts[1], lineno), _imm(parts[2], lineno) & 0xfffff)
    if m == 'jal':
        return j_type(_reg(parts[1], lineno), _target(parts[2], addr, labels, lineno))
    if m == 'jalr':
        off, rs = _mem(parts[3], lineno) if '(' in parts[3] else (0, 0)
        if '(' in parts[3]:
            return i_type(OP_JALR, 0, _reg(parts[1], lineno), rs, off & 0xfff)
        return i_type(OP_JALR, 0, _reg(parts[1], lineno), _reg(parts[2], lineno),
                      _imm(parts[3], lineno) & 0xfff)
    if m in SYS_IMM:
        return (SYS_IMM[m] << 20) | OP_SYS
    if m == 'fence':
        return OP_FENCE
    if m == 'fence.i':
        return (1 << 12) | OP_FENCE
    raise AsmError('第 %d 行：不认识的指令 %r' % (lineno, m))


# ── 文件往返 ─────────────────────────────────────────────────

def load_prog(path):
    f = open(path, 'r')
    try:
        return text_to_digits(f.read())
    finally:
        f.close()


def save_prog(path, ds):
    f = open(path, 'w')
    try:
        f.write(digits_to_text(ds))
        f.write('\n')
    finally:
        f.close()


# ── 运行 ─────────────────────────────────────────────────────

def run_prog(ds, inputs=(), limit=2000000, quiet=False):
    vm = VM(step_limit=limit)
    if not vm.load(ds):
        return vm, []
    vm.inq = [ord(c) & 0xff for c in inputs]
    vm.flush_at = 1 << 30
    st = vm.run(limit)
    vm.flush()
    lines = vm.take_output()
    if not quiet:
        for ln in lines:
            sys.stdout.write(render_line(ln) + '\n')
        if st == ST_END:
            sys.stderr.write('exit code %d\n' % vm.exit_code)
        elif st == ST_NEED_IN:
            sys.stderr.write('(等输入)\n')
        elif st == ST_LIMIT:
            sys.stderr.write('(步数到顶 %d)\n' % vm.steps)
        elif st == ST_ERR:
            sys.stderr.write('(错误: %s)\n' % vm.error)
    return vm, lines


def render_line(bys):
    return ''.join(chr(b) if 32 <= b < 127 else ('\\x%02x' % b) for b in bys)


# ── 按键表 ───────────────────────────────────────────────────

def keys_of(ds):
    """把这些数字翻译成'在板上按哪几个键'。"""
    out = []
    for d in ds:
        out.append(('H', 'T', 'Y', 'P')[d])
    return ''.join(out)


# ── main ─────────────────────────────────────────────────────

USAGE = __doc__


def main(argv):
    if len(argv) < 2:
        print(USAGE)
        return 1
    cmd = argv[1]
    args = argv[2:]
    if cmd == 'info':
        print('Tetsuhiro-Alpha on mPython —— 板用指令集')
        print('  16 个四进制数字 = 一条 32 位指令，P=3 Y=2 T=1 H=0')
        print('  寄存器 128 位；内存按页分配')
        print('  写 0x1000: (v&1) 打印 (v>>8)&0xff / 否则退出 v>>1')
        print('  读 0x1004: 下一个输入字符')
        return 0
    if cmd == 'asm':
        src = open(args[0], 'r', encoding='utf-8').read()
        ds = bytes_to_digits(assemble_bytes(src))
        out = None
        if '-o' in args:
            out = args[args.index('-o') + 1]
        if out:
            save_prog(out, ds)
            print('%s -> %s（%d 位 / %d 条指令）' % (args[0], out, len(ds), len(ds) // 16))
        else:
            print(digits_to_text(ds))
        return 0
    if cmd == 'dis':
        ds = load_prog(args[0])
        ws = words_of(ds)
        for i, w in enumerate(ws):
            print('%04x  %s' % (i * 4, decode(w)))
        return 0
    if cmd == 'run':
        ds = load_prog(args[0])
        inp = ''
        if '--in' in args:
            inp = args[args.index('--in') + 1]
        run_prog(ds, inp)
        return 0
    if cmd == 'keys':
        ds = load_prog(args[0])
        s = keys_of(ds)
        for i in range(0, len(s), 16):
            print(s[i:i + 16])
        return 0
    if cmd == 'sim':
        ds = load_prog(args[0])
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))
        from ta_hw import SimHW
        from ta_app import App
        hw = SimHW(interactive=True)
        App(hw, preload=digits_to_text(ds)).loop()
        return 0
    if cmd == 'push':
        # 汇编好写进板子的 ta.prog（走 flash.py 那条串口路）
        import flash
        data = flash.program_bytes(args[0])
        port = flash.pick_port(args[args.index('--port') + 1] if '--port' in args else None)
        if not port:
            print('没找到串口。--list 看一眼，或者 --port 手动指定。')
            return 1
        try:
            import serial
        except ImportError:
            print('要装 pyserial：pip install pyserial')
            return 1
        dev = port if isinstance(port, str) else port.device
        ser = serial.Serial(dev, flash.BAUD, timeout=0.2)
        repl = flash.RawRepl(ser, verbose='-v' in args)
        try:
            repl.enter()
            flash.backup(repl, 'ta.prog')
            flash.write_file(repl, 'ta.prog', data)
            flash.soft_reset(repl)
        finally:
            try:
                repl.leave()
            except Exception:
                pass
            ser.close()
        print('%s -> 板上 ta.prog（%d 字节）。回板上按 A 就能跑。' % (args[0], len(data)))
        return 0
    if cmd == 'pull':
        import flash
        port = flash.pick_port(args[args.index('--port') + 1] if '--port' in args else None)
        if not port:
            print('没找到串口。')
            return 1
        import serial
        dev = port if isinstance(port, str) else port.device
        ser = serial.Serial(dev, flash.BAUD, timeout=0.2)
        repl = flash.RawRepl(ser)
        try:
            repl.enter()
            out, err = repl.exec("f = open('ta.prog', 'rb')\nd = f.read()\nf.close()\n"
                                 "print(d.decode())")
        finally:
            try:
                repl.leave()
            except Exception:
                pass
            ser.close()
        if '-o' in args:
            f = open(args[args.index('-o') + 1], 'w')
            try:
                f.write(out)
            finally:
                f.close()
            print('存到 %s' % args[args.index('-o') + 1])
        ds = text_to_digits(out)
        for i, w in enumerate(words_of(ds)):
            print('%04x  %s' % (i * 4, decode(w)))
        return 0
    print(USAGE)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
