# -*- coding: utf-8 -*-
"""TA 的底层约定：四进制数字 <-> 文本 / 整数 / 字节，以及稀疏内存。

单独一个文件**不是审美问题**：MicroPython 编译一个模块时要先把整个文件的语法树
建出来，峰值正比于**单个文件**的大小；掌控板跑起来只剩 ~96 KB 堆，把这一块
和 VM 挤在一个文件里就要多要几 KB —— 实测差一点点就起不来。

板上的一份程序就是一串**四进制数字**（0 1 2 3），16 个数字 = 一条 32 位指令，
最左边那个数字是 bit[31:30]。键位：P = 3，Y = 2，T = 1，H = 0。
"""

# ── 数字、键位、文本 ─────────────────────────────────────────

DIGIT_OF_KEY = {'P': 3, 'Y': 2, 'T': 1, 'H': 0}
KEY_OF_DIGIT = ('H', 'T', 'Y', 'P')   # 0->H  1->T  2->Y  3->P

DIGITS_PER_WORD = 16          # 一条 32 位指令 = 16 个四进制数字
DIGITS_PER_ROW = 16           # 一屏一行 = 16 位 = 一条指令
DIGITS_PER_BYTE = 4           # 4^4 = 256，四个数字正好一个字节

# 存进 flash 的文本用 '0'-'3'；读的时候两种写法都收（'H' 'T' 'Y' 'P' 也认）
_CHAR_TO_DIGIT = {
    '0': 0, '1': 1, '2': 2, '3': 3,
    'H': 0, 'T': 1, 'Y': 2, 'P': 3,
    'h': 0, 't': 1, 'y': 2, 'p': 3,
}
_CHAR_ALIAS = {
    'P': '3', 'Y': '2', 'T': '1', 'H': '0',
    'p': '3', 'y': '2', 't': '1', 'h': '0',
}


def text_to_digits(s, maxlen=0):
    """把 flash 里的文本拆成数字表。

    只认 0-3、P/Y/T/H；别的字符一律跳过，`#` 到行尾算注释 ——
    这样在电脑上手写的程序也能直接塞进板子。
    上限在**读的时候**就生效，别先整串拆完再切一刀（板上堆很紧）。
    """
    out = []
    if not s:
        return out
    comment = False
    for ch in s:
        if ch == '\n':
            comment = False
            continue
        if comment:
            continue
        if ch == '#':
            comment = True
            continue
        v = _CHAR_TO_DIGIT.get(ch)
        if v is None:
            continue
        out.append(v)
        if maxlen and len(out) >= maxlen:
            break
    return out


def digits_to_text(ds):
    """数字表 -> 一串 '0'-'3'（存 flash 用这个，一行 16 位）。"""
    out = []
    for i in range(len(ds)):
        if i and i % DIGITS_PER_ROW == 0:
            out.append('\n')
        out.append(chr(48 + ds[i]))
    return ''.join(out)


def digits_to_int(ds):
    """把一串四进制数字按**从左到右高位在前**读成一个整数。"""
    v = 0
    for d in ds:
        v = v * 4 + d
    return v


def int_to_digits(v, width=0):
    """整数 -> 数字表（高位在前）。width 给定时高位补 0。"""
    if v < 0:
        v = 0
    ds = []
    while v:
        ds.append(v & 3)
        v >>= 2
    ds.reverse()
    if width and len(ds) < width:
        ds = [0] * (width - len(ds)) + ds
    return ds


def words_of(ds):
    """数字表 -> 32 位字表（最后不满一条的，右边补 0）。"""
    out = []
    n = len(ds) - len(ds) % DIGITS_PER_WORD
    for i in range(0, n, DIGITS_PER_WORD):
        v = 0
        for d in ds[i:i + DIGITS_PER_WORD]:
            v = (v << 2) | d
        out.append(v)
    return out


def words_to_bytes(words):
    """字表 -> 小端字节流（就是 .tthr 那个格式）。"""
    b = bytearray()
    for w in words:
        b.append(w & 0xff)
        b.append((w >> 8) & 0xff)
        b.append((w >> 16) & 0xff)
        b.append((w >> 24) & 0xff)
    return b


def bytes_to_digits(b):
    """小端字节流 -> 数字表（words_of 的反操作）。尾巴不满一条的补 0。"""
    ds = []
    n = len(b)
    if n % 4:
        n += 4 - n % 4
    for i in range(0, n, 4):
        w = 0
        for k in range(4):
            j = i + k
            if j < len(b):
                w |= b[j] << (8 * k)
        ds.extend(int_to_digits(w, DIGITS_PER_WORD))
    return ds


# ── 主机 I/O 地址 ────────────────────────────────────────────

TOHOST = 0x1000          # 写：打印 / 退出
FROMHOST = 0x1004        # 读：下一个输入字符

# ── 运行状态 ─────────────────────────────────────────────────

ST_RUNNING = 'run'
ST_NEED_IN = 'need'      # 卡在输入口上，等用户敲
ST_END = 'end'           # 正常退出
ST_ERR = 'err'
ST_LIMIT = 'limit'       # 步数到顶
ST_STOP = 'stop'         # 用户按停

MASK = (1 << 128) - 1
SIGN = 1 << 127


def s128(v):
    """按 128 位补码解释一个整数。"""
    v &= MASK
    return v - (1 << 128) if v & SIGN else v


def sx(v, w):
    """把 w 位数按补码符号扩展到任意精度。"""
    v &= (1 << w) - 1
    return v - (1 << w) if v & (1 << (w - 1)) else v


# ── 内存：稀疏页（4 KiB 一页，页里 256 字节） ────────────────

PAGE_BITS = 8
PAGE_SIZE = 1 << PAGE_BITS


class Memory(object):
    def __init__(self):
        self.pages = {}

    def _page(self, a, make):
        p = a >> PAGE_BITS
        pg = self.pages.get(p)
        if pg is None:
            if not make:
                return None
            pg = bytearray(PAGE_SIZE)
            self.pages[p] = pg
        return pg

    def rb(self, a):
        pg = self._page(a, False)
        if pg is None:
            return 0
        return pg[a & (PAGE_SIZE - 1)]

    def wb(self, a, v):
        pg = self._page(a, True)
        pg[a & (PAGE_SIZE - 1)] = v & 0xff

    def read(self, a, nbytes):
        """小端读 nbytes 字节，零扩展成整数。"""
        v = 0
        for i in range(nbytes):
            v |= self.rb(a + i) << (8 * i)
        return v

    def write(self, a, v, nbytes):
        for i in range(nbytes):
            self.wb(a + i, (v >> (8 * i)) & 0xff)

    def load_bytes(self, base, b):
        for i in range(len(b)):
            self.wb(base + i, b[i])


