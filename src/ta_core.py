# -*- coding: utf-8 -*-
"""TA 的机器：TH128I 板用子集的译码 / 执行，以及反汇编。

数字、字节、内存那些约定在 ta_bits 里；这里只放"机器本身"。
指令集是 ../Tetsuhiro-Alpha（TH128I）的**板用子集**：基础整数 + M 乘除。
寄存器仍是 128 位 —— MicroPython 的整数本来就是任意精度，不费一分钱。

碰到不认识的编码一律报错，绝不猜着执行。
"""

# ---8<--- BUNDLE-STRIP-START
from ta_bits import *
# ---8<--- BUNDLE-STRIP-END

# ── 机器 ─────────────────────────────────────────────────────

OP_LOAD = 0x03
OP_STORE = 0x23
OP_IMM = 0x13
OP_REG = 0x33
OP_LUI = 0x37
OP_AUIPC = 0x17
OP_BRANCH = 0x63
OP_JALR = 0x67
OP_JAL = 0x6f
OP_SYSTEM = 0x73
OP_FENCE = 0x0f


class VM(object):
    """TH128I 板用子集。

    没有 CSR、没有陷阱、没有浮点、没有向量 —— 那几样在板上用不上，
    也给不了它们该有的语义。**碰到不认识的编码一律 ST_ERR**，
    绝不猜着执行（猜错的机器码比报错难查得多）。
    """

    MAX_PAGES = 64                    # 64 页 = 16 KiB，板上够用（见 docs/设计.md）

    def __init__(self, hw=None, step_limit=200000):
        self.hw = hw
        self.step_limit = step_limit
        self.mem = Memory()
        self.x = [0] * 32
        self.pc = 0
        self.steps = 0
        self.status = ST_RUNNING
        self.error = ''
        self.exit_code = 0
        self.out_lines = []           # 已经成行的输出（给屏幕用）
        self.inq = []                 # 待取走的输入字节
        self._line = []
        self.flush_at = 64
        self.code_end = 0

    # ── 装程序 ────────────────────────────────────────────

    def load(self, digits, base=0):
        try:
            b = words_to_bytes(words_of(digits))
        except Exception as e:
            self.status = ST_ERR
            self.error = '%s' % type(e).__name__
            return False
        if not b:
            self.status = ST_ERR
            self.error = 'EMPTY'
            return False
        self.mem.load_bytes(base, b)
        self.pc = base
        self.code_end = base + len(b)      # 跑过最后一条指令就算正常跑完（见 docs/设计.md）
        return True

    # ── 输出 ──────────────────────────────────────────────

    def _emit(self, byte):
        if 10 == byte:
            self.out_lines.append(self._line)
            self._line = []
            return
        self._line.append(byte)

    def take_output(self):
        """把攒好的整行取走（屏幕上只能一行一行画）。"""
        got = self.out_lines
        self.out_lines = []
        return got

    def flush(self):
        if self._line:
            self.out_lines.append(self._line)
            self._line = []

    # ── 跑 ────────────────────────────────────────────────

    def run(self, budget, step_limit=None):
        limit = self.step_limit if step_limit is None else step_limit
        while budget > 0:
            if self.status != ST_RUNNING:
                return self.status
            if self.steps >= limit:
                self.status = ST_LIMIT
                return self.status
            try:
                self.step()
            except _NeedIn:
                self.status = ST_NEED_IN
                return self.status
            except Exception as e:
                self.status = ST_ERR
                self.error = '%s' % e
                return self.status
            self.steps += 1
            budget -= 1
            if len(self.out_lines) >= self.flush_at:
                return self.status
        return self.status

    # ── 取指译码 ──────────────────────────────────────────

    def step(self):
        pc = self.pc
        if pc & 3:
            raise ValueError('MISALIGN')
        if pc >= self.code_end:
            # 跑过最后一条指令 = 正常跑完。板上"忘了写停机"太常见了，
            # 与其扔一个 ERR，不如当 END。（这是**有意**偏离 tthr 的一处，
            # 写进 docs/设计.md 了。）
            self.status = ST_END
            return
        inst = self.mem.read(pc, 4)
        self.pc = (pc + 4) & MASK
        self.exec(inst)

    def exec(self, inst):
        self._exec(inst)
        self.x[0] = 0        # x0 硬连线为 0：任何写 x0 的指令都当没写

    def _exec(self, inst):
        op = inst & 0x7f
        x = self.x
        rd = (inst >> 7) & 0x1f
        f3 = (inst >> 12) & 7
        rs1 = (inst >> 15) & 0x1f
        rs2 = (inst >> 20) & 0x1f

        if op == OP_LUI:
            x[rd] = inst & 0xfffff000
            return
        if op == OP_AUIPC:
            x[rd] = (self.pc - 4 + (inst & 0xfffff000)) & MASK
            return
        if op == OP_IMM:
            a = x[rs1]
            imm = sx(inst >> 20, 12)
            if f3 == 0:
                x[rd] = (a + imm) & MASK
            elif f3 == 2:
                x[rd] = 1 if s128(a) < imm else 0
            elif f3 == 3:
                x[rd] = 1 if (a & MASK) < (imm & MASK) else 0
            elif f3 == 4:
                x[rd] = (a ^ imm) & MASK
            elif f3 == 6:
                x[rd] = (a | (imm & MASK)) & MASK
            elif f3 == 7:
                x[rd] = (a & imm) & MASK
            elif f3 == 1:
                # shamt 是 7 位（占 [26:20]），所以 [31:27] 必须全 0
                if (inst >> 27) != 0:
                    raise ValueError('ILLEGAL')
                x[rd] = (a << ((inst >> 20) & 0x7f)) & MASK
            elif f3 == 5:
                sh = (inst >> 20) & 0x7f
                if (inst >> 27) == 0:
                    x[rd] = a >> sh
                elif (inst >> 27) == 0x08:
                    x[rd] = (s128(a) >> sh) & MASK
                else:
                    raise ValueError('ILLEGAL')
            else:
                raise ValueError('ILLEGAL')
            return
        if op == OP_REG:
            a = x[rs1]
            b = x[rs2]
            f7 = inst >> 25
            if f7 == 0x01:                           # M 扩展
                x[rd] = self._muldiv(a, b, f3) & MASK
                return
            if f3 == 0:
                if f7 == 0:
                    x[rd] = (a + b) & MASK
                elif f7 == 0x20:
                    x[rd] = (a - b) & MASK
                else:
                    raise ValueError('ILLEGAL')
            elif f3 == 1:
                x[rd] = (a << (b & 0x7f)) & MASK
            elif f3 == 2:
                x[rd] = 1 if s128(a) < s128(b) else 0
            elif f3 == 3:
                x[rd] = 1 if (a & MASK) < (b & MASK) else 0
            elif f3 == 4:
                x[rd] = (a ^ b) & MASK
            elif f3 == 5:
                sh = b & 0x7f
                if f7 == 0:
                    x[rd] = a >> sh
                elif f7 == 0x20:
                    x[rd] = (s128(a) >> sh) & MASK
                else:
                    raise ValueError('ILLEGAL')
            elif f3 == 6:
                x[rd] = (a | b) & MASK
            elif f3 == 7:
                x[rd] = (a & b) & MASK
            return
        if op == OP_BRANCH:
            imm = sx(((inst >> 31) << 12) | (((inst >> 7) & 1) << 11) |
                     (((inst >> 25) & 0x3f) << 5) | (((inst >> 8) & 0xf) << 1), 13)
            a = x[rs1]
            b = x[rs2]
            take = False
            if f3 == 0:
                take = a == b
            elif f3 == 1:
                take = a != b
            elif f3 == 4:
                take = s128(a) < s128(b)
            elif f3 == 5:
                take = s128(a) >= s128(b)
            elif f3 == 6:
                take = (a & MASK) < (b & MASK)
            elif f3 == 7:
                take = (a & MASK) >= (b & MASK)
            else:
                raise ValueError('ILLEGAL')
            if take:
                self.pc = (self.pc - 4 + imm) & MASK
            return
        if op == OP_JAL:
            imm = sx(((inst >> 31) << 20) | (((inst >> 12) & 0xff) << 12) |
                     (((inst >> 20) & 1) << 11) | (((inst >> 21) & 0x3ff) << 1), 21)
            x[rd] = self.pc
            self.pc = (self.pc - 4 + imm) & MASK
            return
        if op == OP_JALR:
            if f3 != 0:
                raise ValueError('ILLEGAL')
            target = (x[rs1] + sx(inst >> 20, 12)) & MASK & ~1
            x[rd] = self.pc
            self.pc = target
            return
        if op == OP_LOAD:
            addr = (x[rs1] + sx(inst >> 20, 12)) & MASK
            if addr == FROMHOST:
                if not self.inq:
                    self.pc = (self.pc - 4) & MASK     # 退回这条，等输入
                    raise _NeedIn()
                x[rd] = self.inq.pop(0) & MASK
                return
            x[rd] = self._load(addr, f3) & MASK
            return
        if op == OP_STORE:
            addr = (x[rs1] + sx(((inst >> 25) << 5) | ((inst >> 7) & 0x1f), 12)) & MASK
            if addr == TOHOST:
                v = x[rs2]
                if v & 1:
                    self._emit((v >> 8) & 0xff)
                else:
                    self.status = ST_END
                    self.exit_code = (v >> 1) & MASK
                return
            self._store(addr, x[rs2], f3)
            return
        if op == OP_FENCE:
            return                       # 板上没有别的 hart，屏障就是空操作
        if op == OP_SYSTEM:
            if f3 != 0:
                raise ValueError('CSR')
            imm = inst >> 20
            if imm == 0x000 or imm == 0x001:     # ECALL / EBREAK -> 停机
                self.status = ST_END
                return
            raise ValueError('SYSTEM')
        raise ValueError('ILLEGAL')

    def _muldiv(self, a, b, f3):
        """M 扩展（128 位语义，除零与溢出按规范）。"""
        ua = a & MASK
        ub = b & MASK
        if f3 == 0:                                   # MUL
            return ua * ub
        if f3 == 1:                                   # MULH（有符号）
            return (s128(ua) * s128(ub)) >> 128
        if f3 == 2:                                   # MULHSU
            return (s128(ua) * ub) >> 128
        if f3 == 3:                                   # MULHU
            return (ua * ub) >> 128
        if f3 == 4 or f3 == 6:                        # DIV / REM（有符号）
            sa = s128(ua)
            sb = s128(ub)
            if sb == 0:
                return MASK if f3 == 4 else ua
            if sa == -SIGN and sb == -1:              # 溢出
                return ua if f3 == 4 else 0
            q = abs(sa) // abs(sb)
            if (sa < 0) != (sb < 0):
                q = -q
            return q if f3 == 4 else (sa - q * sb)
        if f3 == 5 or f3 == 7:                        # DIVU / REMU
            if ub == 0:
                return MASK if f3 == 5 else ua
            return ua // ub if f3 == 5 else ua % ub
        raise ValueError('ILLEGAL')

    def _load(self, addr, f3):
        if f3 == 0:                                  # LB
            return sx(self.mem.rb(addr), 8)
        if f3 == 1:                                  # LH
            return sx(self.mem.read(addr, 2), 16)
        if f3 == 2:                                  # LW
            return sx(self.mem.read(addr, 4), 32)
        if f3 == 3:                                  # LD
            return sx(self.mem.read(addr, 8), 64)
        if f3 in (4, 5, 6, 7):                       # LBU / LHU / LWU / LQ
            n = (1, 2, 4, 16)[f3 - 4]
            return self.mem.read(addr, n)
        raise ValueError('ILLEGAL')

    def _store(self, addr, v, f3):
        n = (1, 2, 4, 8, 16)[f3] if f3 < 5 else None
        if n is None:
            raise ValueError('ILLEGAL')
        self.mem.write(addr, v, n)


class _NeedIn(Exception):
    """内部用：这条 load 在等用户敲键盘。"""
    pass


# ── 反汇编（屏幕上那一列"这行是什么"用得上） ─────────────────

_R_OPS = {0x0: 'add', 0x1: 'sll', 0x2: 'slt', 0x3: 'sltu', 0x4: 'xor',
          0x5: 'srl', 0x6: 'or', 0x7: 'and'}
_R_SUB = {0x0: 'sub', 0x5: 'sra'}
_I_OPS = {0x0: 'addi', 0x2: 'slti', 0x3: 'sltiu', 0x4: 'xori',
          0x6: 'ori', 0x7: 'andi'}
_I_SHT = {0x1: 'slli', 0x5: 'srli'}
_XRET = {0x002: 'sret', 0x102: 'dret', 0x202: 'mret', 0x302: 'tret'}
_LOAD = ('lb', 'lh', 'lw', 'ld', 'lbu', 'lhu', 'lwu', 'lq')
_STORE = ('sb', 'sh', 'sw', 'sd', 'sq')
_BR = ('beq', 'bne', '?', '?', 'blt', 'bge', 'bltu', 'bgeu')


def _sx(v, w):
    return sx(v, w)


def reg_name(r):
    return 'x%d' % r


def decode(inst):
    """32 位指令 -> 一行助记符。认不出来就返回 '??? <hex>'。"""
    op = inst & 0x7f
    rd = (inst >> 7) & 0x1f
    f3 = (inst >> 12) & 7
    rs1 = (inst >> 15) & 0x1f
    rs2 = (inst >> 20) & 0x1f
    try:
        if op == OP_LUI:
            return 'lui %s, 0x%x' % (reg_name(rd), inst >> 12)
        if op == OP_AUIPC:
            return 'auipc %s, 0x%x' % (reg_name(rd), inst >> 12)
        if op == OP_IMM:
            imm = _sx(inst >> 20, 12)
            if f3 in _I_OPS:
                return '%s %s, %s, %d' % (_I_OPS[f3], reg_name(rd), reg_name(rs1), imm)
            if f3 in _I_SHT:
                sh = (inst >> 20) & 0x7f
                if f3 == 5 and (inst >> 27) == 0x08:
                    return 'srai %s, %s, %d' % (reg_name(rd), reg_name(rs1), sh)
                return '%s %s, %s, %d' % (_I_SHT[f3], reg_name(rd), reg_name(rs1), sh)
            return '??? %08x' % inst
        if op == OP_REG:
            sub = (inst >> 25) == 0x20
            if f3 in _R_OPS:
                nm = _R_SUB[f3] if sub else _R_OPS[f3]
                return '%s %s, %s, %s' % (nm, reg_name(rd), reg_name(rs1), reg_name(rs2))
            return '??? %08x' % inst
        if op == OP_BRANCH:
            imm = _sx(((inst >> 31) << 12) | (((inst >> 7) & 1) << 11) |
                      (((inst >> 25) & 0x3f) << 5) | (((inst >> 8) & 0xf) << 1), 13)
            if f3 in (0, 1, 4, 5, 6, 7):
                return '%s %s, %s, %+d' % (_BR[f3], reg_name(rs1), reg_name(rs2), imm)
            return '??? %08x' % inst
        if op == OP_JAL:
            imm = _sx(((inst >> 31) << 20) | (((inst >> 12) & 0xff) << 12) |
                      (((inst >> 20) & 1) << 11) | (((inst >> 21) & 0x3ff) << 1), 21)
            return 'jal %s, %+d' % (reg_name(rd), imm)
        if op == OP_JALR and f3 == 0:
            return 'jalr %s, %s, %d' % (reg_name(rd), reg_name(rs1), _sx(inst >> 20, 12))
        if op == OP_LOAD:
            return '%s %s, %d(%s)' % (_LOAD[f3], reg_name(rd),
                                      _sx(inst >> 20, 12), reg_name(rs1))
        if op == OP_STORE:
            imm = _sx(((inst >> 25) << 5) | ((inst >> 7) & 0x1f), 12)
            return '%s %s, %d(%s)' % (_STORE[f3], reg_name(rs2), imm, reg_name(rs1))
        if op == OP_FENCE:
            return 'fence' if f3 == 0 else ('fence.i' if f3 == 1 else '???')
        if op == OP_SYSTEM and f3 == 0:
            imm = inst >> 20
            if imm == 0:
                return 'ecall'
            if imm == 1:
                return 'ebreak'
            if imm in _XRET:
                return _XRET[imm]
    except Exception:
        pass
    return '??? %08x' % inst


def disasm_words(words):
    return [decode(w) for w in words]
