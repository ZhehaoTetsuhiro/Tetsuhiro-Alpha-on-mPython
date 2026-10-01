# -*- coding: utf-8 -*-
"""TA 的界面模型：编辑缓冲、按键扫描、板上输入的解读规则。

和硬件无关，所以电脑上不插板子就能整套测。（单独一个文件不是审美问题：
MicroPython 编译模块时要先建整棵语法树，峰值正比于**单个文件**的大小，
板子那不到 100 KB 的堆经不起把这一层塞进 ta_app。）
"""

# ---8<--- BUNDLE-STRIP-START
try:
    from ta_core import *
except ImportError:
    pass
# ---8<--- BUNDLE-STRIP-END

# ── 编辑缓冲 ─────────────────────────────────────────────────
#
# 板上一份程序就是一串四进制数字。一排 16 位 = 一条 32 位指令，
# 所以长按 O / N 挪 16 位就是上下挪一整条指令。

MAX_DIGITS = 4096          # 256 条指令；板上堆也放得下


class Editor(object):
    def __init__(self, maxlen=MAX_DIGITS):
        self.maxlen = maxlen
        self.ds = []
        self.cur = 0
        # 右上角那张小标签（state）：LOAD / UNSAV / SAVED。
        # 一份真相放在这儿，别再多一个布尔跟着跑 ——
        # 开机、RELOAD、装进新程序都是 LOAD；改一下就 UNSAV；在板上存过才是 SAVED。
        self.state = 'LOAD'

    def set_text(self, s):
        # 上限在**读的时候**就交给 text_to_digits：flash 里那份可能比板子
        # 内存还大，先整串拆成表再切一刀，中间那块表就能把板子读爆。
        self.ds = text_to_digits(s, self.maxlen)
        self.cur = len(self.ds)
        self.state = 'LOAD'

    def text(self):
        return digits_to_text(self.ds)

    def len(self):
        return len(self.ds)

    def at(self, i):
        if 0 <= i < len(self.ds):
            return self.ds[i]
        return None

    def insert(self, d):
        if len(self.ds) >= self.maxlen:
            return False
        self.ds.insert(self.cur, d)
        self.cur += 1
        self.state = 'UNSAV'
        return True

    def backspace(self, k=1):
        n = 0
        while n < k and self.cur > 0:
            self.cur -= 1
            del self.ds[self.cur]
            n += 1
        if n:
            self.state = 'UNSAV'
        return n

    def move(self, d):
        c = self.cur + d
        if c < 0:
            c = 0
        elif c > len(self.ds):
            c = len(self.ds)
        self.cur = c
        return self.cur

    def clear(self):
        self.ds = []
        self.cur = 0
        self.state = 'UNSAV'

    def word_at(self, i):
        """第 i 个数字所在的那条指令的 32 位字（不够 16 位就右边补 0）。"""
        base = (i // DIGITS_PER_WORD) * DIGITS_PER_WORD
        v = 0
        for k in range(DIGITS_PER_WORD):
            v = (v << 2) | (self.ds[base + k] if base + k < len(self.ds) else 0)
        return v

    def word_under_cursor(self):
        """光标所在那一条指令。"""
        return self.word_at(self.cur)


# ── 按键：短按 / 长按 / 长按连发 / 组合键 ─────────────────────
#
# 需求表里 "不断输入 3，间隔 0.1s" 就是长按连发；O / N 也一样 ——
# **按住就一直上/下挪**（一行一条指令），松手才停。A 的"长按 = 保存"是
# 一次性动作，按住不能反复存。
# A+B 是组合键，三件事排在一条时间轴上（见下面 AB_STAGES）。

LONG_MS = 400
REPEAT_MS = 100            # 0.1s

# (名字, 长按判定毫秒, 长按连发间隔毫秒；0 表示没有长按/不连发)
EDIT_KEYS = (
    ('P', LONG_MS, REPEAT_MS),
    ('Y', LONG_MS, REPEAT_MS),
    ('T', LONG_MS, REPEAT_MS),
    ('H', LONG_MS, REPEAT_MS),
    ('O', LONG_MS, REPEAT_MS),      # 按住一直往上挪（每 0.1s 一行）
    ('N', LONG_MS, REPEAT_MS),      # 按住一直往下挪
    ('A', 500, 0),                  # 长按 = 保存，只能一下
    ('B', LONG_MS, REPEAT_MS),
)

# 运行界面（程序卡在输入口上）用的是另一套：A 空格 / 长按退出，
# B 回车 / 长按切换 数字·字符 模式，N 退格 / 长按连退。
RUN_KEYS = (
    ('P', 0, 0),
    ('Y', 0, 0),
    ('T', 0, 0),
    ('H', 0, 0),
    ('O', 0, 0),
    ('N', LONG_MS, REPEAT_MS),
    ('A', 600, 0),
    ('B', 600, 0),
)

# 组合键：(名字, 要一起按住的键, 分级)
#
#   松手时还没到第一级  ->  ('名字', 'short')
#   按住到了第 N 级     ->  ('名字', 那一级写着的动作名)
#
# A+B 上有三件事，按"破坏性从小到大"排在一条时间轴上：
#
#     快点一下        换显示（数字 <-> 反汇编）   ← 无损，随便点
#     按住 0.7 秒     RELOAD（放弃改动，重读 flash）
#     按住 2.5 秒     CLEAR（清空程序）—— 离 RELOAD 留 1.8 秒，手抖按不出来
#
# 为什么这么排：原需求表把八个键的短按/长按**排满了**，A+B 快点一下原来是 RELOAD，
# 现在让给"换显示"；RELOAD 没地方去，就接到这条时间轴上。好处是最破坏性的那个
# 要按住最久，按错的机会更小（以前快点一下就会把改动丢掉）。
AB_STAGES = ((700, 'mid'), (2500, 'long'))
EDIT_COMBOS = (('AB', ('A', 'B'), AB_STAGES),)
# 输入界面里的 A+B 只管插小数点（需求：同按 AB 分割整数部分和小数部分），
# 没有分级 —— 松手就算。
INPUT_COMBOS = (('AB', ('A', 'B'), ()),)

# 进度条上写什么
AB_LABEL = {'mid': 'RDT', 'long': 'CLR'}


class KeyScanner(object):
    DEBOUNCE = 2

    def __init__(self, keys=None, combos=None, read=None, now=None):
        self.read = read
        self.now = now
        self.set_keys(keys, combos)

    def set_keys(self, keys, combos):
        self.spec = EDIT_KEYS if keys is None else keys
        self.combo_spec = EDIT_COMBOS if combos is None else combos
        self.st = {}
        for item in self.spec:
            self.st[item[0]] = {'stable': 0, 'cnt': 0, 't0': 0, 'longed': False,
                                'trep': 0, 'muted': False, 'edge': 0}
        self.combos = []
        for item in self.combo_spec:
            self.combos.append({'name': item[0], 'keys': item[1],
                                'stages': tuple(item[2]), 'active': False,
                                'fired': -1, 'cancel': False, 't0': 0})

    def any_down(self):
        for item in self.spec:
            if self.st[item[0]]['stable']:
                return True
        return False

    def cancel_combos(self):
        """跑的正欢时按 A+B 想停下来 —— 那是"停"，不是"清空"。掐掉它。"""
        for c in self.combos:
            if c['active'] or c['fired'] >= 0:
                c['cancel'] = True
            c['active'] = False
            c['fired'] = -1
            for m in c['keys']:
                self.st[m]['muted'] = True

    def combo_progress(self):
        """正按着的组合键 [(标签, 已按了多久, 到下一级要多久), ...]，给进度条用。"""
        out = []
        for c in self.combos:
            if c['active'] and not c['cancel']:
                i = c['fired'] + 1
                if i < len(c['stages']):
                    ms, kind = c['stages'][i]
                    out.append((AB_LABEL.get(kind, c['name']),
                                self.now() - c['t0'], ms))
        return out

    def poll(self):
        ev = []
        t = self.now()

        for item in self.spec:
            k = self.st[item[0]]
            k['edge'] = 0
            raw = 1 if self.read(item[0]) else 0
            if raw == k['stable']:
                k['cnt'] = 0
            else:
                k['cnt'] += 1
                if k['cnt'] >= self.DEBOUNCE:
                    k['stable'] = raw
                    k['cnt'] = 0
                    k['edge'] = 1 if raw else -1
                    if raw:
                        k['t0'] = t
                        k['longed'] = False

        for c in self.combos:
            down = True
            for m in c['keys']:
                if not self.st[m]['stable']:
                    down = False
                    break
            if down:
                if c['cancel']:
                    pass
                elif not c['active']:
                    c['active'] = True
                    c['t0'] = t
                    c['fired'] = -1
                    for m in c['keys']:
                        self.st[m]['muted'] = True
                        self.st[m]['longed'] = True
                else:
                    # 一格一格往上走：按得越久，动作越重
                    stages = c['stages']
                    for i in range(c['fired'] + 1, len(stages)):
                        if t - c['t0'] >= stages[i][0]:
                            c['fired'] = i
                            ev.append((c['name'], stages[i][1]))
            else:
                if c['active'] and c['fired'] < 0 and not c['cancel']:
                    ev.append((c['name'], 'short'))
                c['active'] = False
                c['fired'] = -1
                c['cancel'] = False

        for item in self.spec:
            name, long_ms, repeat_ms = item
            k = self.st[name]
            if k['muted']:
                continue
            if k['edge'] == -1 and not k['longed']:
                ev.append((name, 'short'))
            if k['stable'] and long_ms:
                if not k['longed']:
                    if t - k['t0'] >= long_ms:
                        k['longed'] = True
                        k['trep'] = t
                        ev.append((name, 'long'))
                elif repeat_ms and t - k['trep'] >= repeat_ms:
                    k['trep'] = t
                    ev.append((name, 'long'))

        for item in self.spec:
            k = self.st[item[0]]
            if not k['stable']:
                k['muted'] = False
        return ev


# ── 板上输入（运行界面）：数字模式 / 字符模式 ─────────────────
#
# 需求原文：
#   字符模式：P、Y、T、H 输入 ASCII 码后 O 确定转为字符
#   数字模式：P、Y、T、H 输入四进制数字后 O 转化为十进制数字字符输入，
#             开头 HH 以输入负号，同按 AB 分割整数部分和小数部分
#
# 所以这一行缓冲区里装的是**四进制数字**（0-3）和一个小数点标记（DOT）。
# 光标在这里没要求，一律"往尾巴上追加"，N 退格。

DOT = -1
MAX_INPUT_DIGITS = 32

# ── 板上的几种画面与运行参数 ─────────────────────────────────
#
# 放在这里（不在 ta_app 里）是有原因的：MicroPython 的内存账算的是**单个文件**
# 的语法树峰值。ta_app（状态机）和 ta_view（画面）都要用这几个名字，谁 import 谁
# 都会成环，索性沉到最下面这一层。

MODE_EDIT = 'edit'
MODE_RUN = 'run'
MODE_DONE = 'done'
MODE_INPUT = 'input'

SAVE_FILE = 'ta.prog'       # 板上的程序存这儿（就是四进制数字的文本）

STEP_LIMIT = 300000         # 死循环兜底
SLICE = 800                 # 一次跑多少步就回去扫一遍按键
OUT_LINES_MAX = 120         # 屏幕上只留最后几行；板上堆很紧

MODE_NUM = 'num'
MODE_CHR = 'chr'


class InputLine(object):
    def __init__(self, mode=MODE_NUM, maxlen=MAX_INPUT_DIGITS):
        self.mode = mode
        self.maxlen = maxlen
        self.toks = []          # 0..3 或 DOT

    def insert(self, d):
        if len(self.toks) >= self.maxlen:
            return False
        self.toks.append(d)
        return True

    def insert_dot(self):
        # 只有数字模式有小数点；每行最多一个
        if self.mode != MODE_NUM or DOT in self.toks:
            return False
        return self.insert(DOT)

    def backspace(self, k=1):
        n = 0
        while n < k and self.toks:
            self.toks.pop()
            n += 1
        return n

    def clear(self):
        self.toks = []

    def toggle_mode(self):
        self.mode = MODE_CHR if self.mode == MODE_NUM else MODE_NUM
        self.clear()

    def text(self):
        out = []
        for t in self.toks:
            out.append('.' if t == DOT else chr(48 + t))
        return ''.join(out)

    # ── 解读 ────────────────────────────────────────────

    def _parts(self):
        """拆成 (负号, 整数数字表, 小数数字表)。"""
        toks = self.toks
        neg = False
        # "开头 HH" —— 两个 0。四进制里前导 0 本来没意义，所以拿来当负号。
        if len(toks) >= 2 and toks[0] == 0 and toks[1] == 0:
            neg = True
            toks = toks[2:]
        if DOT in toks:
            i = toks.index(DOT)
            return neg, toks[:i], toks[i + 1:]
        return neg, toks, []

    def preview(self):
        """这一行会被解读成什么（给屏幕看）。"""
        if not self.toks:
            return ''
        if self.mode == MODE_CHR:
            v = digits_to_int([t for t in self.toks if t != DOT])
            return chr(v & 0xff)
        neg, ip, fp = self._parts()
        s = ('-' if neg else '') + str(digits_to_int(ip))
        if fp:
            s += '.' + str(digits_to_int(fp))
        return s

    def chars(self):
        """O 确定：这一行到底往程序嘴里塞哪几个字节。"""
        if not self.toks:
            return []
        if self.mode == MODE_CHR:
            v = digits_to_int([t for t in self.toks if t != DOT])
            return [v & 0xff]
        s = self.preview()
        return [ord(c) & 0xff for c in s]
