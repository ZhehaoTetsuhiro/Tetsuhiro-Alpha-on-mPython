# -*- coding: utf-8 -*-
"""TA 的画面：编辑 / 运行 / 输入三种屏幕怎么画。

单独一个文件**不是审美问题**：MicroPython 编译一个模块时要先建整棵语法树，
峰值正比于**单个文件**的大小 —— 把这一块和 App 的状态机挤在一起，实测
最小堆要从 101.5 KB 降到 85 KB 上下。真掌控板上（2026-10-02 实测）六个模块
全部 import 完还剩 65040 字节空闲堆，跑得动，但余量也就是这个数了 ——
谁要是再往一个大文件里塞东西，先想想板上那 63 KB。
（数字见 `tools/build.py`、README「为什么板上是 6 个文件」一节。）

这里全是**纯画图**：拿到 hw + app，往上画，不碰状态机的转移。
"""

# ---8<--- BUNDLE-STRIP-START
try:
    from ta_core import *
    from ta_ui import *
except ImportError:
    pass
# ---8<--- BUNDLE-STRIP-END


def render_bytes(bys):
    """把程序打印出来的字节变成一行能上屏的字符。"""
    out = []
    for b in bys:
        if 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append('\\x%02x' % b)
    return ''.join(out)


def status_extra(hw, app):
    """状态行右边那几格：先是"刚刚发生了什么"，再是存盘状态。"""
    if app.msg and hw.now() - app.msg_t < 1800:
        return app.msg
    if not app.ed.saved:
        return 'UNSAV'
    return ''


def mnemonic_here(app):
    """光标所在这条指令是什么（状态行右边那几格）。"""
    if not app.ed.len():
        return ''
    s = decode(app.ed.word_under_cursor())
    if s.startswith('???') or s.startswith('fence'):
        return ''
    i = s.find(' ')
    return s[:i] if i > 0 else s


def draw_edit(hw, app):
    ed = app.ed
    n = ed.len()
    drows = hw.rows - 1

    line = ed.cur // DIGITS_PER_ROW
    if line < app.top:
        app.top = line
    elif line >= app.top + drows:
        app.top = line - drows + 1
    if app.top < 0:
        app.top = 0

    hw.clear()
    prog = app.scan.combo_progress()
    if prog:
        # 正在按 A+B：只把第一行让给进度条，下面的程序照常画 ——
        # 不然一按就整个消失，吓人。标签写着**再按下去会发生什么**
        # （RDT = RELOAD，CLR = CLEAR）。
        label, elapsed, need = prog[0]
        label = label[:3]
        room = hw.cols - len(label)
        done = int(room * float(elapsed) / float(need)) if need else room
        if done > room:
            done = room
        if done < 0:
            done = 0
        hw.text(label, 0, 0)
        hw.text('#' * done + ' ' * (room - done), len(label), 0)
    else:
        # 状态行：E/D + 4 格光标位 + " / " + 长度。
        # 斜杠**两边各一个空格**，看着像"48 / 224"而不是"48/ 224"；
        # 左边固定 4 格，所以斜杠永远在第 7 格（列号不随数字长短跳）。
        head = '%s%4d / %d' % ('D' if app.dis else 'E', ed.cur, n)
        hw.text(head[:hw.cols], 0, 0)
        if not app.dis:
            ex = status_extra(hw, app) or mnemonic_here(app)
            if ex:
                x = hw.cols - len(ex)
                if x > len(head):
                    hw.text(ex, x, 0)

    cur_line = ed.cur // DIGITS_PER_WORD
    for r in range(drows):
        idx = app.top + r
        base = idx * DIGITS_PER_WORD
        if base > n:
            break
        y = r + 1
        if app.dis:
            # 反汇编视图：一行一条指令，左边一个 '>' 指着光标所在的那条
            hw.text('>' if idx == cur_line else ' ', 0, y)
            s = decode(ed.word_at(base))
            if s.startswith('??? '):
                s = '???'
            hw.text(s[:hw.cols - 1], 1, y)
        else:
            hw.text(digits_to_text(ed.ds[base:base + DIGITS_PER_ROW]), 0, y)
            if base <= ed.cur < base + DIGITS_PER_ROW:
                hw.cursor(ed.cur - base, y)
    hw.show()


def run_headline(app, st):
    vm = app.vm
    if st == ST_NEED_IN:
        return 'IN ?'
    if st == ST_STOP:
        return 'STOP %d' % vm.steps
    if st == ST_ERR:
        return 'ERR ' + vm.error
    if st == ST_LIMIT:
        return 'LIMIT %d' % vm.steps
    if st == ST_END:
        return 'END %d' % vm.steps
    return 'RUN %d' % vm.steps


def draw_run(hw, app, st):
    drows = hw.rows - 1
    hw.clear()
    head = 'R ' + run_headline(app, st)
    last = getattr(app, 'note_input', '')
    if last and len(head) + len(last) + 3 <= hw.cols:
        head = head + ' <' + last + '>'
    hw.text(head[:hw.cols], 0, 0)
    shown = app.out_lines[-drows:]
    for i in range(len(shown)):
        hw.text(shown[i][:hw.cols], 0, i + 1)
    hw.show()


def draw_input(hw, app):
    ln = app.line
    hw.clear()
    hw.text('I %s %d' % ('CHR' if ln.mode == MODE_CHR else 'NUM', len(ln.toks)), 0, 0)
    hw.text(ln.text()[:hw.cols], 0, 1)
    hw.text(('= ' + ln.preview())[:hw.cols], 0, 2)
    hw.text('O ok  A sp', 0, 3)
    hw.text('B ent Bhold mode', 0, 4)
    hw.text('N del  AB .', 0, 5)
    hw.show()
