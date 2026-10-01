# -*- coding: utf-8 -*-
"""TA 的全部测试 —— 不插板子也能跑。

    python3 tests/test_all.py -v

覆盖四层：
  ① 机器核心：四进制数字 <-> 字节、译码、执行（含 M 扩展、x0 丢弃、128 位）
  ② 汇编器：assemble -> disassemble 往返、伪指令、指示字
  ③ 界面模型：编辑缓冲、按键扫描（短按/长按/连发/组合键）、板上输入规则
  ④ 整机：SimHW 剧本驱动 App —— 敲程序、跑、看输出
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import ta as T
import ta_core
from ta_core import (VM, Memory, text_to_digits, digits_to_text, words_of,
                     words_to_bytes, bytes_to_digits, digits_to_int, int_to_digits,
                     decode, ST_END, ST_ERR, ST_NEED_IN, ST_LIMIT, MASK,
                     DIGITS_PER_WORD, DIGITS_PER_ROW, TOHOST, FROMHOST)
from ta_ui import (Editor, KeyScanner, InputLine, MODE_NUM, MODE_CHR,
                   EDIT_KEYS, RUN_KEYS, EDIT_COMBOS, INPUT_COMBOS, MAX_DIGITS)
from ta_hw import SimHW
import ta_app
from ta_app import App

DATADIR = os.path.join(HERE, '.tasim')


def asm(src):
    return bytes_to_digits(T.assemble_bytes(src))


def example_src(name):
    """读 examples/<name>.s 的源码。"""
    with open(os.path.join(ROOT, 'examples', name + '.s'), encoding='utf-8') as f:
        return f.read()


def asm_file(name):
    with open(os.path.join(ROOT, 'examples', name + '.s')) as f:
        return asm(f.read())


def run_digits(ds, inputs='', limit=200000):
    vm = VM(step_limit=limit)
    vm.load(ds)
    vm.inq = [ord(c) & 0xff for c in inputs]
    vm.flush_at = 1 << 30
    st = vm.run(limit + 1)          # 多给一拍，好让「步数到顶」判得出来
    vm.flush()
    lines = [T.render_line(l) for l in vm.take_output()]
    return st, vm, lines


def run_src(src, inputs='', limit=200000):
    return run_digits(asm(src), inputs, limit)


# ══════════════════════════════════════════════════════════════
# ① 机器核心
# ══════════════════════════════════════════════════════════════

class TestDigits(unittest.TestCase):
    def test_key_digit_map(self):
        self.assertEqual(text_to_digits('PYTH'), [3, 2, 1, 0])
        self.assertEqual(digits_to_text([3, 2, 1, 0]), '3210')

    def test_text_skips_junk_and_comments(self):
        self.assertEqual(text_to_digits('01 23\n# 45\n67'), [0, 1, 2, 3])

    def test_maxlen_applies_while_reading(self):
        self.assertEqual(text_to_digits('0123456789', 4), [0, 1, 2, 3])

    def test_word_is_leftmost_digit_most_significant(self):
        ds = [0] * 16
        ds[0] = 3                                   # bit[31:30]
        self.assertEqual(words_of(ds)[0], 3 << 30)
        ds = [0] * 16
        ds[15] = 3                                  # bit[1:0]
        self.assertEqual(words_of(ds)[0], 3)

    def test_roundtrip_words_bytes_digits(self):
        words = [0x12345678, 0xffffffff, 0x00000001]
        self.assertEqual(words_of(bytes_to_digits(words_to_bytes(words))), words)

    def test_middle_word_of_a_long_program(self):
        ds = []
        for w in (0x11223344, 0xaabbccdd):
            ds.extend(int_to_digits(w, 16))
        self.assertEqual(digits_to_int(ds[16:32]), 0xaabbccdd)

    def test_digits_to_text_breaks_a_line_every_instruction(self):
        self.assertEqual(digits_to_text([0] * 32).split('\n'), ['0' * 16, '0' * 16])


class TestVM(unittest.TestCase):
    def test_hello(self):
        st, vm, lines = run_digits(asm_file('hello'))
        self.assertEqual(st, ST_END)
        self.assertEqual(vm.exit_code, 0)
        self.assertEqual(lines, ['Hi'])

    def test_countdown_loop(self):
        st, vm, lines = run_digits(asm_file('countdown'))
        self.assertEqual(lines, ['5', '4', '3', '2', '1'])

    def test_echo_reads_input(self):
        st, vm, lines = run_digits(asm_file('echo'), 'AB\n')
        self.assertEqual(lines, ['AB'])
        self.assertEqual(vm.exit_code, 0)

    def test_needs_input_when_queue_empty(self):
        st, vm, lines = run_digits(asm_file('echo'), '')
        self.assertEqual(st, ST_NEED_IN)
        self.assertEqual(vm.pc % 4, 0)

    def test_writes_to_x0_are_dropped(self):
        # `j` 展开成 jal x0, ...；只要 x0 被写坏，后面全乱
        st, vm, lines = run_src('    j  next\n    addi a0, x0, 9\nnext:\n'
                                '    addi a1, x0, 7\n    ecall\n')
        self.assertEqual(vm.x[0], 0)
        self.assertEqual(vm.x[11], 7)

    def test_m_extension(self):
        st, vm, lines = run_src(
            '    li   a0, 6\n    li   a1, 7\n    mul  a2, a0, a1\n'
            '    li   a3, 20\n    li   a4, 6\n    div  a5, a3, a4\n'
            '    rem  a6, a3, a4\n'
            '    li   a7, -7\n    divu t0, a7, a4\n')
        self.assertEqual(vm.x[12], 42)
        self.assertEqual(vm.x[15], 3)
        self.assertEqual(vm.x[16], 2)
        self.assertEqual(vm.x[5], ((1 << 128) - 7) // 6)     # -7 无符号除 6

    def test_mulh(self):
        st, vm, lines = run_src('    li   a0, -1\n    mulh a1, a0, a0\n'
                                '    ecall\n')
        self.assertEqual(vm.x[11], 0)                # (-1)*(-1) 的高位是 0

    def test_div_by_zero_semantics(self):
        st, vm, lines = run_src('    li   a0, 5\n    div  a1, a0, zero\n'
                                '    rem  a2, a0, zero\n    ecall\n')
        self.assertEqual(vm.x[11], MASK)              # DIV -> -1
        self.assertEqual(vm.x[12], 5)                 # REM -> 被除数

    def test_128_bit_registers(self):
        st, vm, lines = run_src('    li   a0, 1\n    slli a1, a0, 127\n'
                                '    addi a2, a1, 1\n    add  a3, a1, a1\n'
                                '    ecall\n')
        self.assertEqual(vm.x[11], 1 << 127)
        self.assertEqual(vm.x[12], (1 << 127) + 1)
        self.assertEqual(vm.x[13], 0)                 # 2^127 * 2 溢出，只留低 128 位

    def test_large_shift_is_legal(self):
        st, vm, lines = run_src('    li   a0, 1\n    slli a1, a0, 100\n'
                                '    srli a2, a1, 100\n    ecall\n')
        self.assertEqual(st, ST_END)
        self.assertEqual(vm.x[12], 1)

    def test_unsigned_compare(self):
        st, vm, lines = run_src('    li   a0, -1\n    li   a1, 1\n'
                                '    sltu a2, a0, a1\n    slt  a3, a0, a1\n'
                                '    ecall\n')
        self.assertEqual(vm.x[12], 0)                 # 0xFFFF.. > 1 无符号
        self.assertEqual(vm.x[13], 1)                 # 有符号

    def test_branches_signed_and_unsigned(self):
        st, vm, lines = run_src('    li   a0, -1\n    li   a1, 0\n'
                                '    bltu a0, a1, bad\n    bgeu a0, a1, ok\n'
                                'bad:\n    li   a2, 1\n    j    end\n'
                                'ok:\n    li   a2, 2\nend:\n    ecall\n')
        self.assertEqual(vm.x[12], 2)

    def test_illegal_instruction_is_reported_not_guessed(self):
        st, vm, lines = run_src('    .word 0\n')
        self.assertEqual(st, ST_ERR)
        self.assertTrue(vm.error)

    def test_csr_is_not_silently_accepted(self):
        # 板用子集里没有 CSR；碰到 csrrs 的编码必须报错，不能猜着执行
        st, vm, lines = run_src('    .word 0x3000A073\n    ecall\n')
        self.assertEqual(st, ST_ERR)
        self.assertEqual(vm.error, 'CSR')

    def test_step_limit(self):
        st, vm, lines = run_src('loop:\n    j loop\n', limit=50)
        self.assertEqual(st, ST_LIMIT)

    def test_ecall_halts(self):
        st, vm, lines = run_src('    ecall\n')
        self.assertEqual(st, ST_END)

    def test_load_store_roundtrip(self):
        st, vm, lines = run_src(
            '    li   a0, 0x2000\n    li   a1, 0x1234\n    sw   a1, 8(a0)\n'
            '    lw   a2, 8(a0)\n    lbu  a3, 8(a0)\n    sd   a1, 16(a0)\n'
            '    ld   a4, 16(a0)\n    ecall\n')
        self.assertEqual(vm.x[12], 0x1234)
        self.assertEqual(vm.x[13], 0x34)
        self.assertEqual(vm.x[14], 0x1234)

    def test_stack_usage(self):
        st, vm, lines = run_src(
            '    li   sp, 0x3000\n    li   a0, 77\n    sd   a0, -8(sp)\n'
            '    ld   a1, -8(sp)\n    ecall\n')
        self.assertEqual(vm.x[11], 77)

    def test_sparse_memory_reads_zero(self):
        m = Memory()
        self.assertEqual(m.read(0x90000, 8), 0)
        m.write(0x90001, 0xab, 1)
        self.assertEqual(m.rb(0x90000), 0)
        self.assertEqual(m.rb(0x90001), 0xab)

    def test_output_is_line_buffered(self):
        st, vm, lines = run_digits(asm_file('hello'))
        self.assertEqual(lines, ['Hi'])


# ══════════════════════════════════════════════════════════════
# ② 汇编 / 反汇编
# ══════════════════════════════════════════════════════════════

class TestAsm(unittest.TestCase):
    def test_disasm_roundtrip_of_the_examples(self):
        for name in ('hello', 'countdown', 'echo'):
            for w in T.assemble(example_src(name)):
                self.assertFalse(decode(w).startswith('???'),
                                 '%s: %08x 反汇编不出来' % (name, w))

    def test_label_offsets_are_relative_to_the_instruction(self):
        self.assertEqual(T.assemble('    j  skip\n    nop\nskip:\n    nop\n')[0],
                         T.j_type(0, 8))

    def test_li_uses_one_word_when_it_fits(self):
        self.assertEqual(len(T.assemble('    li a0, 5\n')), 1)
        self.assertEqual(len(T.assemble('    li a0, 5000\n')), 2)

    def test_li_negative(self):
        st, vm, lines = run_src('    li a0, -3\n    ecall\n')
        self.assertEqual(vm.x[10], MASK - 2)

    def test_la_points_at_the_data(self):
        st, vm, lines = run_src(
            '    la   a0, msg\n    lbu  a1, 0(a0)\n    li   a2, 0x4000\n'
            '    sb   a1, 0(a2)\n    lbu  a3, 0(a2)\n    ecall\n'
            'msg:\n    .byte 65\n')
        self.assertEqual(vm.x[13], 65)

    def test_byte_data_is_padded_to_a_word(self):
        self.assertEqual(len(T.assemble('    .byte 1,2,3,4,5\n')), 2)

    def test_word_directive(self):
        self.assertEqual(T.assemble('    .word 0x12345678\n'), [0x12345678])

    def test_bad_register_reports_line(self):
        try:
            T.assemble('    addi zz, x0, 1\n')
            self.fail('should have raised')
        except T.AsmError as e:
            self.assertIn('1', str(e))

    def test_shift_range_is_checked(self):
        try:
            T.assemble('    slli a0, a0, 200\n')
            self.fail('should have raised')
        except T.AsmError:
            pass

    def test_unknown_mnemonic(self):
        try:
            T.assemble('    frobnicate a0\n')
            self.fail('should have raised')
        except T.AsmError:
            pass

    def test_memory_operand_syntax_is_checked(self):
        try:
            T.assemble('    lw a0, 8\n')
            self.fail('should have raised')
        except T.AsmError:
            pass


# ══════════════════════════════════════════════════════════════
# ③ 界面模型
# ══════════════════════════════════════════════════════════════

class TestEditor(unittest.TestCase):
    def test_insert_and_move(self):
        ed = Editor()
        for d in (3, 2, 1, 0):
            ed.insert(d)
        self.assertEqual(ed.text(), '3210')
        ed.move(-2)
        ed.insert(1)
        self.assertEqual(ed.text(), '32110')
        self.assertEqual(ed.cur, 3)

    def test_move_clamps(self):
        ed = Editor()
        ed.set_text('012')
        ed.move(-10)
        self.assertEqual(ed.cur, 0)
        ed.move(99)
        self.assertEqual(ed.cur, 3)

    def test_backspace_at_start_does_nothing(self):
        ed = Editor()
        ed.set_text('012')
        ed.cur = 0
        self.assertEqual(ed.backspace(3), 0)
        self.assertEqual(ed.text(), '012')

    def test_maxlen(self):
        ed = Editor(maxlen=3)
        for d in (0, 0, 0, 0):
            ed.insert(d)
        self.assertEqual(ed.len(), 3)

    def test_dirty_flag(self):
        ed = Editor()
        ed.set_text('0')
        self.assertEqual(ed.state, 'LOAD')          # 读来的
        ed.insert(1)
        self.assertEqual(ed.state, 'UNSAV')         # 改过了
        ed.set_text('0')
        ed.backspace()
        self.assertEqual(ed.state, 'UNSAV')
        ed.clear()
        self.assertEqual(ed.state, 'UNSAV')

    def test_word_under_cursor(self):
        ed = Editor()
        ed.set_text('3' + '0' * 15 + '1')
        ed.cur = 0
        self.assertEqual(ed.word_under_cursor(), 3 << 30)
        ed.cur = 15
        self.assertEqual(ed.word_under_cursor(), 3 << 30)   # 同一条
        ed.cur = 16
        self.assertEqual(ed.word_under_cursor(), 1 << 30)

    def test_partial_last_instruction_reads_as_zero_padded(self):
        ed = Editor()
        ed.set_text('3')
        self.assertEqual(ed.word_under_cursor(), 3 << 30)


class FakeIO(object):
    """给 KeyScanner 用的假时钟 + 假按键电平。"""

    def __init__(self):
        self.t = 0
        self.down = set()

    def read(self, name):
        return name in self.down

    def now(self):
        return self.t

    def hold(self, ms, *keys):
        self.down = set(keys)
        self.t += ms

    def idle(self, ms=20):
        self.down = set()
        self.t += ms


class TestKeyScanner(unittest.TestCase):
    def _scan(self, keys=None, combos=None):
        io = FakeIO()
        return io, KeyScanner(keys=keys, combos=combos,
                              read=io.read, now=io.now)

    def _press_stable(self, io, sc, *names):
        """按住够两拍去抖，让它算"真的按下了"。"""
        io.hold(20, *names)
        sc.poll()
        io.hold(20, *names)
        sc.poll()

    def _release_stable(self, io, sc):
        """松手也要够两拍（去抖是双向的），返回期间攒下的事件。"""
        ev = []
        io.idle(20)
        ev.extend(sc.poll())
        io.idle(20)
        ev.extend(sc.poll())
        return ev

    def test_short_press_fires_on_release(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'P')
        self.assertEqual(sc.poll(), [])
        self.assertEqual(self._release_stable(io, sc), [('P', 'short')])

    def test_long_press_repeats_every_100ms(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'P')
        ev = []
        for _ in range(8):
            io.hold(100, 'P')
            ev.extend(sc.poll())
        self.assertGreaterEqual(ev.count(('P', 'long')), 4)
        self.assertNotIn(('P', 'short'), self._release_stable(io, sc))

    def test_a_stays_one_shot_on_long_press(self):
        """A 的长按是"保存"，按住不能反复存 —— 只有一下。"""
        io, sc = self._scan()
        self._press_stable(io, sc, 'A')
        ev = []
        for _ in range(8):
            io.hold(100, 'A')
            ev.extend(sc.poll())
        self.assertEqual(ev.count(('A', 'long')), 1)

    def test_holding_o_keeps_repeating(self):
        """按住 O 要一直往上挪（用户要求的；原来是一次性的）。"""
        io, sc = self._scan()
        self._press_stable(io, sc, 'O')
        ev = []
        for _ in range(8):
            io.hold(100, 'O')
            ev.extend(sc.poll())
        self.assertGreaterEqual(ev.count(('O', 'long')), 4)

    def test_holding_n_keeps_repeating(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'N')
        ev = []
        for _ in range(8):
            io.hold(100, 'N')
            ev.extend(sc.poll())
        self.assertGreaterEqual(ev.count(('N', 'long')), 4)

    def test_debounce_ignores_a_single_tick(self):
        io, sc = self._scan()
        io.hold(1, 'P')
        sc.poll()
        self.assertFalse(sc.any_down())

    def test_combo_short(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'A', 'B')
        self.assertEqual(self._release_stable(io, sc), [('AB', 'short')])

    def test_combo_ladder_climbs_never_skips(self):
        # 快点一下 = 换显示；0.7s = RELOAD；1.5s = CLEAR —— 一格一格往上走
        io, sc = self._scan()
        self._press_stable(io, sc, 'A', 'B')
        self.assertEqual(sc.poll(), [])
        io.hold(300, 'A', 'B')
        self.assertEqual(sc.poll(), [])
        io.hold(500, 'A', 'B')                       # 累计 ~0.8s
        self.assertEqual(sc.poll(), [('AB', 'mid')])
        io.hold(800, 'A', 'B')                       # 累计 ~1.6s
        self.assertEqual(sc.poll(), [('AB', 'long')])
        io.hold(200, 'A', 'B')
        self.assertEqual(sc.poll(), [])              # 到顶了就不再发

    def test_combo_ladder_does_not_emit_short_after_a_stage_fired(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'A', 'B')
        io.hold(800, 'A', 'B')
        self.assertIn(('AB', 'mid'), sc.poll())
        self.assertEqual(self._release_stable(io, sc), [])

    def test_combo_ladder_suppresses_the_single_keys(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'A', 'B')
        io.hold(1600, 'A', 'B')
        ev = sc.poll()
        self.assertIn(('AB', 'long'), ev)
        self.assertEqual([e for e in ev if e[0] in ('A', 'B')], [])

    def test_cancel_combos_mutes_until_release(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'A', 'B')
        sc.cancel_combos()
        io.hold(2000, 'A', 'B')
        self.assertEqual(sc.poll(), [])
        self.assertEqual(self._release_stable(io, sc), [])

    def test_combo_progress_names_the_next_action(self):
        io, sc = self._scan()
        self._press_stable(io, sc, 'A', 'B')
        io.hold(100, 'A', 'B')
        prog = sc.combo_progress()
        self.assertEqual(prog[0][0], 'RDT')          # 再按下去是 RELOAD
        self.assertEqual(prog[0][2], 700)
        io.hold(700, 'A', 'B')                       # 越过 RELOAD 那一级
        self.assertEqual(sc.poll(), [('AB', 'mid')])
        self.assertEqual(sc.combo_progress()[0][0], 'CLR')   # 下一级是 CLEAR

    def test_input_screen_combo_has_no_ladder(self):
        io, sc = self._scan(keys=RUN_KEYS, combos=INPUT_COMBOS)
        self._press_stable(io, sc, 'A', 'B')
        io.hold(2000, 'A', 'B')
        self.assertEqual(sc.poll(), [])              # 按多久都不发长按
        self.assertEqual(self._release_stable(io, sc), [('AB', 'short')])

    def test_run_keymap_reports_a_short_press(self):
        io, sc = self._scan(keys=RUN_KEYS, combos=INPUT_COMBOS)
        self._press_stable(io, sc, 'T')
        self.assertEqual(self._release_stable(io, sc), [('T', 'short')])


class TestInputLine(unittest.TestCase):
    def test_num_mode_decimal(self):
        ln = InputLine(MODE_NUM)
        ln.insert(1)
        ln.insert(2)
        self.assertEqual(ln.preview(), '6')
        self.assertEqual(ln.chars(), [ord('6')])

    def test_num_mode_leading_zero_zero_is_minus(self):
        ln = InputLine(MODE_NUM)
        for d in (0, 0, 1, 2):
            ln.insert(d)
        self.assertEqual(ln.preview(), '-6')

    def test_single_leading_zero_is_not_a_sign(self):
        ln = InputLine(MODE_NUM)
        ln.insert(0)
        ln.insert(1)
        self.assertEqual(ln.preview(), '1')

    def test_two_zeros_alone_are_a_sign(self):
        ln = InputLine(MODE_NUM)
        ln.insert(0)
        ln.insert(0)
        self.assertEqual(ln.preview(), '-0')

    def test_num_mode_with_dot(self):
        ln = InputLine(MODE_NUM)
        ln.insert(1)
        ln.insert(2)
        ln.insert_dot()
        ln.insert(3)
        self.assertEqual(ln.preview(), '6.3')
        self.assertEqual(ln.chars(), [ord(c) for c in '6.3'])

    def test_negative_with_dot(self):
        ln = InputLine(MODE_NUM)
        for d in (0, 0, 1, 0):
            ln.insert(d)
        ln.insert_dot()
        ln.insert(2)
        self.assertEqual(ln.preview(), '-4.2')

    def test_dot_only_in_num_mode_and_only_once(self):
        self.assertFalse(InputLine(MODE_CHR).insert_dot())
        ln = InputLine(MODE_NUM)
        self.assertTrue(ln.insert_dot())
        self.assertFalse(ln.insert_dot())

    def test_char_mode_is_ascii_code(self):
        ln = InputLine(MODE_CHR)
        for d in (1, 0, 0, 1):                     # 4^3 + 1 = 65
            ln.insert(d)
        self.assertEqual(ln.preview(), 'A')
        self.assertEqual(ln.chars(), [65])

    def test_char_mode_masks_to_a_byte(self):
        ln = InputLine(MODE_CHR)
        for _ in range(5):                         # 4^5-1 = 1023 -> 0xff
            ln.insert(3)
        self.assertEqual(ln.chars(), [0xff])

    def test_backspace_and_clear(self):
        ln = InputLine(MODE_NUM)
        for d in (1, 2, 3):
            ln.insert(d)
        self.assertEqual(ln.backspace(2), 2)
        self.assertEqual(ln.text(), '1')
        ln.clear()
        self.assertEqual(ln.chars(), [])

    def test_maxlen(self):
        ln = InputLine(MODE_NUM, maxlen=3)
        for d in (0, 1, 2, 3):
            ln.insert(d)
        self.assertEqual(len(ln.toks), 3)

    def test_toggle_mode_clears(self):
        ln = InputLine(MODE_NUM)
        ln.insert(1)
        ln.toggle_mode()
        self.assertEqual(ln.mode, MODE_CHR)
        self.assertEqual(ln.toks, [])

    def test_empty_line_yields_nothing(self):
        self.assertEqual(InputLine(MODE_NUM).chars(), [])


# ══════════════════════════════════════════════════════════════
# ④ 整机（SimHW 剧本驱动）
# ══════════════════════════════════════════════════════════════

class TestApp(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(DATADIR, ignore_errors=True)

    def _app(self, **kw):
        kw.setdefault('datadir', DATADIR)
        hw = SimHW(**kw)
        return hw, App(hw, filename='ta.prog')

    def _booted(self, hw, app, src):
        app.preload = digits_to_text(asm(src))
        app.boot()
        return app

    def test_editor_screen_shows_the_digits(self):
        hw, app = self._app(script='')
        app.preload = '3' * 32
        app.loop()
        self.assertTrue(hw.frames)
        self.assertIn('3' * 16, hw.frames[0])

    def test_status_line_shows_cursor_and_length(self):
        hw, app = self._app(script='')
        app.preload = '0' * 20
        app.loop()
        self.assertIn('E  20 / 20', hw.frames[0])      # 斜杠两边各一个空格
        self.assertIn('LOAD', hw.frames[0])             # 右上角那张标签

    # ---- 状态行右边那张标签：LOAD / UNSAV / SAVED（需求：启动后默认 LOAD）----

    def _status_row(self, hw):
        return hw.frames[-1].split('\n')[1][1:-1]

    def test_boot_shows_load_in_the_top_right_corner(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n')
        app.dirty = True
        app.draw_edit()
        row = self._status_row(hw)
        self.assertEqual(row[-4:], 'LOAD')          # 就在右上角
        self.assertIn('LOAD', row)
        self.assertNotIn('UNSAV', row)

    def test_label_flips_to_unsav_when_edited(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n')
        app.ed.insert(0)
        app.dirty = True
        app.draw_edit()
        self.assertEqual(self._status_row(hw)[-5:], 'UNSAV')

    def test_label_becomes_saved_after_saving(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n')
        app.ed.insert(0)
        app.do_save()
        app.msg = ''                                # 把那条一闪而过的提示收掉
        app.dirty = True
        app.draw_edit()
        self.assertEqual(self._status_row(hw)[-5:], 'SAVED')

    def test_label_goes_to_reload_after_reloading(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n')
        app.hw.save(app.filename, app.ed.text())    # 板上存一份
        app.ed.insert(0)
        app.do_reload()
        app.msg = ''
        app.dirty = True
        app.draw_edit()
        self.assertEqual(app.ed.state, 'RELOAD')
        self.assertEqual(self._status_row(hw)[-6:], 'RELOAD')   # 需求：右上角写 RELOAD

    def test_reload_without_a_file_on_the_board_keeps_the_buffer(self):
        """板上没有那份存档时，RELOAD 不能把手里这份抹了。"""
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n')
        app.ed.insert(0)
        keep = app.ed.text()
        app.do_reload()
        self.assertEqual(app.ed.text(), keep)
        self.assertEqual(app.msg, 'NOFILE')
        self.assertNotEqual(app.ed.state, 'RELOAD')

    def test_reload_of_an_empty_file_does_empty_the_editor(self):
        """档存在但是空的，那是"重读一份空程序" —— 照做。"""
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n')
        app.hw.save(app.filename, '')
        app.do_reload()
        self.assertEqual(app.ed.len(), 0)
        self.assertEqual(app.ed.state, 'RELOAD')

    def test_label_goes_to_clear_after_clearing(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n')
        app.do_clear()
        app.msg = ''
        app.dirty = True
        app.draw_edit()
        self.assertEqual(app.ed.state, 'CLEAR')
        self.assertEqual(self._status_row(hw)[-5:], 'CLEAR')    # 需求：右上角写 CLEAR
        self.assertEqual(app.ed.len(), 0)

    def test_the_progress_bar_does_not_stay_on_screen_after_release(self):
        """真板子上卡住的那条 CLR 进度条：松手后必须重画，不能留着。"""
        hw, app = self._app(script=[(('A', 'B'), 1000)])
        self._booted(hw, app, '    li a0, 5\n    ecall\n')
        app.hw.save(app.filename, app.ed.text())        # 板上先有一份（真板子上就有）
        app.loop()
        row = self._status_row(hw)
        self.assertNotIn('#', row)                       # 进度条没了
        self.assertNotIn('CLR', row)
        self.assertIn('E', row)                          # 状态行回来了
        self.assertEqual(app.msg, 'RELOAD')              # 而且告诉了你发生了什么
        self.assertEqual(app.ed.state, 'RELOAD')

    def test_the_progress_bar_is_still_there_while_holding(self):
        """按住不放的时候，进度条本来就该在（别把上一条修成"看不到进度条"）。"""
        hw, app = self._app(script=[(('A', 'B'), 900)])
        self._booted(hw, app, '    li a0, 5\n')
        held = []
        app.boot_ok = True
        # 手动走几拍：按住 A+B，看进度条上的标签从 RDT 走到 CLR
        app.scan.st['A']['stable'] = 1
        app.scan.st['B']['stable'] = 1
        app.scan.poll()
        for _ in range(2):
            app.hw.sleep(400)
            app.scan.poll()
            app.dirty = True
            app.draw_edit()
            held.append(self._status_row(hw))
        self.assertTrue(any('RDT' in r or 'CLR' in r for r in held), held)

    def test_label_fits_with_the_numbers_in_the_common_case(self):
        """常见长度下，标签和数字**同时**在屏上，而且都在各自该在的位置。"""
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n' * 15)     # 240 位
        app.ed.cur = 48
        app.dirty = True
        app.draw_edit()
        row = self._status_row(hw)
        self.assertEqual(len(row), hw.cols)
        self.assertEqual(row[-4:], 'LOAD')
        self.assertIn('E 48 / 240', row)                # 斜杠两边还是各一个空格

    def test_numbers_win_when_the_label_no_longer_fits(self):
        """程序上千位（4 位数）时右边摆不下标签 —— 数字优先，斜杠的间距保住。"""
        hw, app = self._app(script='')
        app.preload = '0' * 1000
        app.boot()
        app.ed.cur = 1000
        app.dirty = True
        app.draw_edit()
        row = self._status_row(hw)
        self.assertIn('E1000 / 1000', row)
        self.assertNotIn('LOAD', row)                    # 常态标签让位

    def test_status_line_layout_matrix(self):
        """把"数字 × 标签"这张表整个钉住：≤999 位标签都在，上下都是空格围着斜杠。"""
        for cur, n in ((0, 0), (48, 240), (241, 241), (999, 999)):
            for label in ('LOAD', 'UNSAV', 'SAVED', 'RELOAD', 'CLEAR'):
                hw, app = self._app(script='')
                app.preload = '0' * n
                app.boot()
                app.ed.cur = cur
                app.ed.state = label
                app.msg = ''
                app.dirty = True
                app.draw_edit()
                row = self._status_row(hw)
                where = 'cur=%d n=%d %s' % (cur, n, label)
                self.assertEqual(len(row), hw.cols, where)
                self.assertTrue(row.endswith(label), '%s：标签没在右上角：%r' % (where, row))
                i = row.index('/')
                self.assertEqual(row[i - 1], ' ', where)
                self.assertEqual(row[i + 1], ' ', where)
        # 4 位数（>=1000 位）时数字优先，标签让位
        hw, app = self._app(script='')
        app.preload = '0' * 1000
        app.boot()
        app.ed.state = 'SAVED'
        app.dirty = True
        app.draw_edit()
        row = self._status_row(hw)
        self.assertIn('E1000 / 1000', row)
        self.assertNotIn('SAVED', row)

    def test_a_six_letter_note_still_fits_beside_the_numbers(self):
        """最长的提示（RELOAD / NOFILE / NOFLSH，6 格）也能和数字同时上屏，
        而且斜杠两边照样是空格。"""
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n' * 15)
        app.ed.cur = 48
        app.note('RELOAD')
        app.dirty = True
        app.draw_edit()
        row = self._status_row(hw)
        self.assertEqual(row[-6:], 'RELOAD')
        self.assertIn('E48 / 240', row)
        i = row.index('/')
        self.assertEqual(row[i - 1], ' ')
        self.assertEqual(row[i + 1], ' ')

    def test_the_numbers_do_not_shift_when_the_label_changes(self):
        """LOAD -> UNSAV -> SAVED 换词时，左边的数字不许左右跳。"""
        hw, app = self._app(script='')
        self._booted(hw, app, '    li a0, 5\n' * 15)
        app.ed.cur = 48
        heads = []
        for label in ('LOAD', 'UNSAV', 'SAVED'):
            app.msg = ''
            app.ed.state = label
            app.dirty = True
            app.draw_edit()
            heads.append(self._status_row(hw)[:11])
        self.assertEqual(heads[0], heads[1])
        self.assertEqual(heads[1], heads[2])

    def test_status_line_slash_sits_between_the_two_numbers(self):
        """斜杠两边各一个空格（需求：'/' 放两个数字中央）。"""
        for cur, n in ((0, 0), (48, 224), (999, 999)):
            hw, app = self._app(script='')
            app.preload = '0' * n
            app.boot()
            app.ed.cur = cur
            app.dirty = True
            app.draw_edit()
            row = self._status_row(hw)
            i = row.index('/')
            self.assertEqual(row[i - 1], ' ', 'cur=%d n=%d：斜杠左边不是空格' % (cur, n))
            self.assertEqual(row[i + 1], ' ', 'cur=%d n=%d：斜杠右边不是空格' % (cur, n))

    def test_key_map_inserts_digits_by_short_press(self):
        hw, app = self._app(script='pyth')          # 小写 = 短按
        app.ed.set_text('')
        app.loop()
        self.assertEqual(app.ed.text().replace('\n', ''), '3210')

    def test_long_press_moves_a_whole_instruction(self):
        """长按一下（刚过 0.4s）挪一整条；按住不放才连发（另有测试）。"""
        hw, app = self._app(script=[('O', 450)])
        app.ed.set_text('0' * 40)
        app.ed.cur = 32
        app.loop()
        self.assertEqual(app.ed.cur, 16)

    def test_holding_n_keeps_stepping_down_a_row_at_a_time(self):
        """按住 N 一直往下挪：按住 0.9 秒该挪好几行（一行 = 16 位）。"""
        hw, app = self._app(script=[('N', 900)])    # 按住 N 0.9 秒
        app.ed.set_text('0' * 320)                  # 20 行
        app.ed.cur = 0
        app.loop()
        self.assertGreaterEqual(app.ed.cur, 16 * 3)     # 至少挪了三行
        self.assertEqual(app.ed.cur % DIGITS_PER_ROW, 0)  # 而且停在整行上
        self.assertLessEqual(app.ed.cur, 320)

    def test_holding_o_keeps_stepping_up_a_row_at_a_time(self):
        hw, app = self._app(script=[('O', 900)])
        app.ed.set_text('0' * 320)
        app.ed.cur = 320
        app.loop()
        self.assertLessEqual(app.ed.cur, 320 - 16 * 3)
        self.assertEqual(app.ed.cur % DIGITS_PER_ROW, 0)

    def test_holding_o_at_the_top_just_stops(self):
        """到头了就停在那儿，不许绕回末尾。"""
        hw, app = self._app(script=[('O', 900)])
        app.ed.set_text('0' * 64)
        app.ed.cur = 0
        app.loop()
        self.assertEqual(app.ed.cur, 0)

    def test_short_press_moves_one_digit(self):
        hw, app = self._app(script='n')
        app.ed.set_text('0' * 40)
        app.ed.cur = 20
        app.loop()
        self.assertEqual(app.ed.cur, 21)

    def test_run_and_see_output(self):
        hw, app = self._app(script='a')
        self._booted(hw, app, example_src('countdown'))
        app.loop()
        for want in ('5', '4', '3', '2', '1'):
            self.assertIn(want, hw.serial, hw.serial)

    def test_ab_short_toggles_the_view(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    addi a0, x0, 5\n')
        self.assertFalse(app.dis)
        app.handle_edit([('AB', 'short')])
        self.assertTrue(app.dis)
        self.assertEqual(app.msg, 'DIS')
        self.assertEqual(app.ed.len(), DIGITS_PER_WORD)      # 程序一个字没动
        app.handle_edit([('AB', 'short')])
        self.assertFalse(app.dis)
        self.assertEqual(app.msg, 'DIG')

    def test_disassembly_view_shows_mnemonics(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    addi a0, x0, 5\n    sw a0, 0(x0)\n')
        app.ed.cur = 0
        app.dis = True
        app.dirty = True
        app.draw_edit()
        screen = hw.frames[-1]
        self.assertIn('D', screen.split('\n')[1][:2])        # 状态行第一个字母
        self.assertIn('>addi x10, x0, 5', screen)
        self.assertIn('sw x10, 0(x0)', screen)

    def test_digit_view_shows_digits(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    addi a0, x0, 5\n')
        app.ed.cur = 0
        app.dis = False
        app.dirty = True
        app.draw_edit()
        self.assertIn('0000110000110103', hw.frames[-1])

    def test_disassembly_view_marks_the_instruction_under_the_cursor(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    addi a0, x0, 5\nnop\nnop\n')
        app.dis = True
        app.ed.cur = 0
        app.top = 0
        app.dirty = True
        app.draw_edit()
        rows = hw.frames[-1].split('\n')
        self.assertTrue(rows[2].startswith('|>'))
        app.ed.cur = DIGITS_PER_WORD * 2                        # 第三条
        app.dirty = True
        app.draw_edit()
        rows = hw.frames[-1].split('\n')
        self.assertTrue(rows[2].startswith('| '))
        self.assertTrue(rows[4].startswith('|>'))

    def test_ab_mid_reloads(self):
        hw, app = self._app(script='')
        app.hw.save('ta.prog', '3' * 16)
        app.ed.set_text('0' * 8)
        app.handle_edit([('AB', 'mid')])
        self.assertEqual(app.ed.text(), '3' * 16)
        self.assertEqual(app.msg, 'RELOAD')
        self.assertEqual(app.mode, ta_app.MODE_EDIT)

    def test_ab_long_clears_and_wipes_flash(self):
        hw, app = self._app(script='')
        app.hw.save('ta.prog', '3' * 16)
        app.ed.set_text('3' * 16)
        app.handle_edit([('AB', 'long')])
        self.assertEqual(app.ed.len(), 0)
        self.assertEqual(app.msg, 'CLEAR')
        self.assertEqual(app.hw.load('ta.prog'), '')

    def test_a_long_saves_without_running(self):
        hw, app = self._app(script='')
        app.ed.set_text('0123')
        app.handle_edit([('A', 'long')])
        self.assertEqual(app.mode, ta_app.MODE_EDIT)
        self.assertEqual(app.msg, 'SAVED')
        self.assertEqual(app.hw.load('ta.prog'), '0123')

    def test_a_short_runs(self):
        hw, app = self._app(script='')
        self._booted(hw, app, 'loop:\n    j loop\n')
        app.handle_edit([('A', 'short')])
        self.assertEqual(app.mode, ta_app.MODE_RUN)

    def test_b_backspaces(self):
        hw, app = self._app(script='')
        app.ed.set_text('0123')
        app.handle_edit([('B', 'short')])
        self.assertEqual(app.ed.text(), '012')

    def test_run_saves_first(self):
        hw, app = self._app(script='')
        self._booted(hw, app, 'loop:\n    j loop\n')
        app.ed.insert(0)
        want = app.ed.text()
        app.handle_edit([('A', 'short')])
        self.assertEqual(app.hw.load('ta.prog'), want)

    def test_loading_a_broken_file_does_not_wipe_flash(self):
        hw, app = self._app(script='')
        app.hw.save('ta.prog', '3' * 16)
        app.load_bad = True
        app.ed.clear()
        app.handle_edit([('A', 'long')])
        self.assertEqual(app.msg, 'BIG')
        self.assertEqual(app.hw.load('ta.prog'), '3' * 16)

    def test_input_screen_feeds_the_program(self):
        hw, app = self._app(script='')
        self._booted(hw, app, example_src('echo'))
        app.start_run()
        self.assertEqual(app.vm.run(10000, app.step_limit), ST_NEED_IN)
        app.start_input()
        self.assertEqual(app.mode, ta_app.MODE_INPUT)
        app.handle_input([('T', 'short'), ('Y', 'short'), ('O', 'short')])
        self.assertEqual(app.vm.inq, [ord('6')])
        self.assertEqual(app.mode, ta_app.MODE_RUN)
        self.assertEqual(app.vm.run(10000, app.step_limit), ST_NEED_IN)
        app.start_input()
        app.handle_input([('B', 'short')])
        self.assertEqual(app.vm.inq, [10])
        self.assertEqual(app.vm.run(10000, app.step_limit), ST_END)
        app.vm.flush()
        self.assertEqual([T.render_line(l) for l in app.vm.take_output()], ['6'])

    def test_input_char_mode(self):
        hw, app = self._app(script='')
        self._booted(hw, app, example_src('echo'))
        app.start_run()
        app.vm.run(10000, app.step_limit)
        app.start_input()
        app.line.mode = MODE_CHR
        app.handle_input([('O', 'short')])          # 空行：什么也不发
        self.assertEqual(app.mode, ta_app.MODE_INPUT)
        app.handle_input([('T', 'short'), ('Y', 'short'), ('T', 'short'),
                          ('Y', 'short'), ('O', 'short')])   # 1*64+2*16+1*4+2 = 102
        self.assertEqual(app.vm.inq, [102])

    def test_input_a_short_sends_a_space(self):
        hw, app = self._app(script='')
        self._booted(hw, app, example_src('echo'))
        app.start_run()
        app.vm.run(10000, app.step_limit)
        app.start_input()
        app.handle_input([('A', 'short')])
        self.assertEqual(app.vm.inq, [32])

    def test_input_a_long_stops_the_run(self):
        hw, app = self._app(script='')
        self._booted(hw, app, example_src('echo'))
        app.start_run()
        app.vm.run(10000, app.step_limit)
        app.start_input()
        app.handle_input([('A', 'long')])
        self.assertEqual(app.mode, ta_app.MODE_EDIT)

    def test_input_b_long_toggles_mode(self):
        hw, app = self._app(script='')
        self._booted(hw, app, example_src('echo'))
        app.start_run()
        app.vm.run(10000, app.step_limit)
        app.start_input()
        app.handle_input([('B', 'long')])
        self.assertEqual(app.in_mode, MODE_CHR)

    def test_input_ab_inserts_a_dot(self):
        hw, app = self._app(script='')
        self._booted(hw, app, example_src('echo'))
        app.start_run()
        app.vm.run(10000, app.step_limit)
        app.start_input()
        app.handle_input([('T', 'short'), ('AB', 'short'), ('Y', 'short')])
        self.assertEqual(app.line.text(), '1.2')

    def test_running_any_key_stops_without_clearing(self):
        hw, app = self._app(script='')
        self._booted(hw, app, 'loop:\n    j loop\n')
        app.start_run()
        app.tick()
        self.assertEqual(app.mode, ta_app.MODE_RUN)
        app.scan.st['A']['stable'] = 1              # 模拟"按任意键"
        app.tick()
        self.assertEqual(app.mode, ta_app.MODE_EDIT)
        self.assertEqual(app.ed.len(), DIGITS_PER_WORD)   # 程序还在

    def test_running_into_an_error_shows_err(self):
        hw, app = self._app(script='')
        self._booted(hw, app, '    .word 0\n')
        app.start_run()
        for _ in range(4):
            app.tick()
            if app.mode != ta_app.MODE_RUN:
                break
        self.assertTrue(app.vm.status == ST_ERR or app.mode == ta_app.MODE_DONE)
        self.assertTrue(app.vm.error)

    def test_editor_recovers_from_a_bad_tick(self):
        hw, app = self._app(script='')
        app.recover(ValueError('boom'))
        self.assertEqual(app.mode, ta_app.MODE_EDIT)

    def test_combo_progress_draws_a_bar(self):
        hw, app = self._app(script='')
        app.scan.st['A']['stable'] = 1
        app.scan.st['B']['stable'] = 1
        app.scan.poll()                             # 让组合键进入 active
        app.hw.sleep(350)                           # 时间走一半（目标 700ms）
        app.dirty = True
        app.draw_edit()
        bar = hw.frames[-1].split('\n')[1]
        self.assertIn('RDT', bar)                   # 再按下去是 RELOAD
        self.assertIn('#', bar)                     # 条在涨


class TestBoardFootprint(unittest.TestCase):
    """把 GC 堆压到掌控板的水平，看板上那几个文件到底起不起得来。

    MicroPython 编译一个模块时峰值**正比于单个文件的语法树大小**，所以板上能不能
    跑不看总量，看"最大的那个文件"。这条是那颗钉子：哪天有人把两个模块又并回
    一个文件，它会立刻红。

    实测（`micropython -X heapsize=N`）：6 个文件 85 KB 就能起；
    并回单文件要 100 KB 以上 —— 掌控板开机只有约 96 KB。
    """

    # 掌控板实测开机空闲 100944 字节；留一点余量给编辑器里的程序
    BUDGET = 95000
    KNOWN_MIN = 85116          # 当前实测值，变了就说明版式动了

    def _mp(self):
        try:
            from shutil import which
            return which('micropython')
        except Exception:
            return None

    def _min_heap(self, path, mods):
        import subprocess
        lo, hi = 40000, 250000
        while hi - lo > 1000:
            mid = (lo + hi) // 2
            r = subprocess.run([self._mp(), '-X', 'heapsize=%d' % mid,
                                '-c', 'import ' + mods],
                               cwd=path, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
            if r.returncode == 0:
                hi = mid
            else:
                lo = mid
        return hi

    def test_board_bundle_fits_in_the_board_heap(self):
        mp = self._mp()
        if not mp:
            self.skipTest('没装 micropython（dnf/apt install micropython）')
        import shutil
        import sys as _sys
        import tempfile
        _sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import build
        tmp = tempfile.mkdtemp(prefix='ta-board-')
        try:
            build.build(tmp)
            mods = ','.join(f[:-3] for f in build.BOARD_FILES)
            need = self._min_heap(tmp, mods)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertLess(need, self.BUDGET,
                        '板上那几个文件最小要 %d 字节堆，预算 %d' % (need, self.BUDGET))

    def test_single_file_build_does_not_fit(self):
        """单文件版**故意**起不来 —— 留着当证据，拦着想把它改回去的人。"""
        mp = self._mp()
        if not mp:
            self.skipTest('没装 micropython')
        import shutil
        import sys as _sys
        import tempfile
        _sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import build
        tmp = tempfile.mkdtemp(prefix='ta-single-')
        try:
            p = os.path.join(tmp, 'main.py')
            build.build_single(p)
            need = self._min_heap(tmp, 'main')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertGreater(need, self.BUDGET,
                           '单文件版居然只要 %d 字节堆了？那这条钉子可以拆了' % need)


class TestConfigPinned(unittest.TestCase):
    """把现在这套约定钉死的钉子：哪天有人顺手改了，这几条会拦着。"""

    def test_pin_keys_and_digits(self):
        self.assertEqual(ta_core.DIGIT_OF_KEY, {'P': 3, 'Y': 2, 'T': 1, 'H': 0})

    def test_pin_bits_per_row(self):
        # 一行 = 一条 32 位指令 = 16 个四进制数字；长按 O/N 挪 16 位
        self.assertEqual(DIGITS_PER_WORD, 16)
        self.assertEqual(DIGITS_PER_ROW, 16)

    def test_pin_io_addresses(self):
        self.assertEqual((TOHOST, FROMHOST), (0x1000, 0x1004))

    def test_pin_save_file(self):
        self.assertEqual(ta_app.SAVE_FILE, 'ta.prog')

    def test_pin_max_digits_is_a_whole_number_of_instructions(self):
        self.assertEqual(MAX_DIGITS % DIGITS_PER_WORD, 0)

    def test_pin_modes(self):
        self.assertEqual((ta_app.MODE_EDIT, ta_app.MODE_RUN,
                          ta_app.MODE_DONE, ta_app.MODE_INPUT),
                         ('edit', 'run', 'done', 'input'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
