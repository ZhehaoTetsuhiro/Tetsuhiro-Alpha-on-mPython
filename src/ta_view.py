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
    """状态行右边那几格：先是"刚刚发生了什么"，再是那张存档标签。

    标签（需求：开机默认 LOAD，RELOAD 读进时写 RELOAD，清空后写 CLEAR）：
      LOAD   —— 开机读进来的
      RELOAD —— A+B 按住 0.7s，从板上重读了一份
      CLEAR  —— A+B 按住 2.5s，清空了（板上那份也清空了）
      UNSAV  —— 改过了，还没存
      SAVED  —— 在板上存过了
    """
    if app.msg and hw.now() - app.msg_t < 1800:
        return app.msg
    return app.ed.state


# 状态行左边那几格，**从松到紧**（斜杠两边**永远是空格**，这是需求）：
#   '%s%4d / %d' → 'E  48 / 240'   （光标位固定 4 格，斜杠永远在第 7 格）
#   '%s%3d / %d' → 'E 48 / 240'    （少占一格 —— 常态走这条）
#   '%s%d / %d'  → 'E48 / 240'     （光标位不再占格）
#   '%d / %d'    → '241 / 241'     （实在挤不下，把 E/D 视图标记让出去）
HEAD_FORMS = (('%s%4d / %d', True), ('%s%3d / %d', True),
              ('%s%d / %d', True), ('%d / %d', False))

# 存档标签最宽 5 格（SAVED / UNSAV / CLEAR）；6 格的提示（RELOAD / NOFILE /
# NOFLSH）按**实际长度**留位 —— 它挤不进去就让位，不为了它把常态排得松松垮垮。
TAIL_W = 5


def status_fields(app, cols, tail, spaced_only):
    """状态行怎么排：返回 (左边那段, 右边那段或 None)。

    * 左边按**常态标签的宽度**（5 格）留位，所以 LOAD → UNSAV → SAVED → CLEAR
      换词的时候数字不会左右跳。
    * 光标位那几格是**先花后省**：'E 48 / 240' 排不下就 'E48 / 240'，
      再排不下就连 E/D 也先让出去 —— 但斜杠两边的空格不动。
    * 数字上到 4 位（≥1000 位）时怎么排都摆不下标签，这时**数字优先**。
    """
    mark = 'D' if app.dis else 'E'
    cur = app.ed.cur
    n = app.ed.len()
    need = max(len(tail), TAIL_W) if tail else 0
    for fmt, with_mark in HEAD_FORMS:
        head = (fmt % (mark, cur, n)) if with_mark else (fmt % (cur, n))
        if len(head) + (1 if tail else 0) + need <= cols:
            return head, tail
    head = HEAD_FORMS[1][0] % (mark, cur, n)
    return head[:cols], None


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
        # 状态行：左边 `E 光标 / 长度`（斜杠两边各一个空格），右边那张存档标签
        # （LOAD / UNSAV / SAVED）或者刚刚发生的那件事（DIG / DIS / RELOAD...）。
        tail = status_extra(hw, app)
        head, tail = status_fields(app, hw.cols, tail, True)
        hw.text(head[:hw.cols], 0, 0)
        if tail:
            hw.text(tail, hw.cols - len(tail), 0)

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
