# -*- coding: utf-8 -*-
"""TA 掌控板上的程序：**状态机**（画面怎么画在 ta_view 里）。

屏幕上永远只有三种画面：

  编辑画面                        运行画面                     输入画面
  ┌──────────────────┐          ┌──────────────────┐        ┌──────────────────┐
  │E  48/ 112   addi │ ←状态    │R END 918         │        │I NUM 2/32        │
  │0000000100000001  │ ←一条指令 │140               │        │12                │
  │0001001100001101  │   16 位   │0.333             │        │= 6               │
  │...               │          │                  │        │O ok  A 空格      │
  └──────────────────┘          └──────────────────┘        └──────────────────┘

一排正好 16 个四进制数字 = **一条 32 位指令**，所以长按 O / N 挪 16 位
就是上下挪一整条。左边第一个数字是 bit[31:30]。

按键（编辑界面，全部照需求表）：
    P / Y / T / H   插入 3 / 2 / 1 / 0（长按 = 每 0.1s 不断插入）
    O  短按 光标左移 1      长按 左移 16（一整条）
    N  短按 光标右移 1      长按 右移 16
    A  短按 运行            长按 保存
    B  短按 退格            长按 每 0.1s 不停退格
    A+B 快点一下            重新从 flash 读（RELOAD，放弃改动）
    A+B 一起按住 1 秒       **清空程序**（CLEAR）—— 要两个手指，不容易误碰

按键（运行界面，程序卡在输入口上时）：
    P / Y / T / H   敲数字 / ASCII 码
    O   确定（把这一行送进程序）
    N   退格（长按不停退格）
    A   短按 空格    长按 退出程序
    B   短按 回车    长按 切换 数字 / 字符 模式
    A+B 同按         插入小数点（分割整数部分和小数部分）

运行中按任意键 = 停（这时候按 A+B 也只是停，不会顺手把程序清掉）。
"""

# ---8<--- BUNDLE-STRIP-START
try:
    from ta_core import *
    from ta_ui import *
    from ta_view import *
except ImportError:                      # 打包进一个文件时这些名字已经在上文了
    pass
# ---8<--- BUNDLE-STRIP-END


class App(object):
    def __init__(self, hw, filename=SAVE_FILE, step_limit=STEP_LIMIT, preload=None):
        self.hw = hw
        self.filename = filename
        self.step_limit = step_limit
        self.preload = preload
        self.ed = Editor()
        self.scan = KeyScanner(read=hw.read_key, now=hw.now)
        self.mode = MODE_EDIT
        self.vm = None
        self.line = None
        self.in_mode = MODE_NUM
        self.out_lines = []
        self.top = 0
        self.dirty = True
        self.msg = ''
        self.msg_t = 0
        self.ticks = 0
        self.quit = False
        # flash 里那份程序**读不进来**（比板子的内存还大）时立起来：
        # 这时候编辑画面是空的，可绝不能顺手把 flash 里那份抹掉。
        self.load_bad = False

    # ── 启动 / 主循环 ─────────────────────────────────────

    def boot(self):
        if self.preload is not None:
            s = self.preload
        else:
            try:
                s = self.hw.load(self.filename)
            except Exception:
                s = None
        if s:
            self.install(s)
        self.ed.saved = True
        self.hw.write('# TA ready. %d digits' % self.ed.len())
        self.dirty = True

    def install(self, s):
        # 装不下就空着起来 —— 但**绝不让异常跑出去**：main.py 一死，
        # mPython 固件会把整块板子 soft reboot 一遍。
        try:
            self.ed.set_text(s or '')
            self.load_bad = False
        except Exception:
            self.ed.clear()
            self.load_bad = True
            self.note('BIG')
        self.ed.saved = True
        return not self.load_bad

    def loop(self):
        self.boot()
        while not self.quit:
            try:
                self.tick()
            except Exception as e:
                self.recover(e)
            if self.hw.want_quit():
                self.quit = True
        self.hw.shutdown()

    def recover(self, e):
        # `tick` 里没接住的异常 —— 兜底，别让 main.py 死掉。
        # 这一层在板上**必须有**：mPython 固件看到 main.py 抛出异常退出，
        # 就打印一行 soft reboot 把整块板子重启一遍，用户看到的是"敲着敲着
        # 板子自己重启了"。
        try:
            import sys
            sys.print_exception(e)
        except Exception:
            print('TA: %s: %s' % (type(e).__name__, e))
        self.vm = None
        self.line = None
        self.out_lines = []
        self.mode = MODE_EDIT
        self.scan.set_keys(None, None)
        self.note('MERR')
        self.dirty = True
        self.collect()
        self.hw.sleep(20)

    def collect(self):
        # 板上的堆只回收、不搬家，碎一点就会 MemoryError，所以主动收。
        try:
            import gc
            gc.collect()
        except Exception:
            pass

    def tick(self):
        ev = self.scan.poll()
        if self.mode == MODE_EDIT:
            if ev:
                self.handle_edit(ev)
                if self.mode != MODE_EDIT:
                    return
            self.tick_edit()
        elif self.mode == MODE_INPUT:
            # 敲输入的时候**不能**"按任意键停"，那是运行中的规矩
            if ev:
                self.handle_input(ev)
                if self.mode != MODE_INPUT:
                    return
            self.tick_input()
        else:
            # 运行中：**按下去**就停，而且要把组合键作废，
            # 否则按 A+B 想停下来，一秒后它会把程序清空 —— 那就成事故了。
            if ev or self.scan.any_down():
                self.leave_run()
                return
            self.tick_run()

    def tick_edit(self):
        if self.scan.combo_progress():
            self.dirty = True            # 让清空进度条动起来
        if self.dirty:
            self.draw_edit()
            self.dirty = False
        self.ticks += 1
        if self.ticks % 200 == 0:
            self.collect()
        self.hw.sleep(12)

    def tick_input(self):
        if self.dirty:
            self.draw_input()
            self.dirty = False
        self.ticks += 1
        if self.ticks % 100 == 0:
            self.collect()
        self.hw.sleep(12)

    def tick_run(self):
        vm = self.vm
        if self.mode == MODE_DONE:
            # 跑完了就停在那儿等按键。**千万别每 tick 重画一遍** ——
            # 那样板子会拿 I2C 死命刷屏，纯属浪费电。
            if self.dirty:
                self.draw_run(vm.status)
                self.dirty = False
            self.hw.sleep(15)
            return
        st = vm.run(SLICE, self.step_limit)
        self.drain_output(vm)
        self.dirty = True
        if st == ST_NEED_IN:
            self.start_input()
            return
        self.ticks += 1
        if self.ticks % 200 == 0:
            self.collect()
        if st != ST_RUNNING:
            vm.flush()
            self.drain_output(vm)
            self.mode = MODE_DONE
        self.draw_run(st)
        self.dirty = False
        self.hw.sleep(2 if st == ST_RUNNING else 20)

    def drain_output(self, vm):
        """把攒好的整行取出来：上屏 + 从串口打一份。"""
        got = vm.take_output()
        if not got:
            return
        for ln in got:
            s = render_bytes(ln)
            self.out_lines.append(s)
            self.hw.write(s)
        if len(self.out_lines) > OUT_LINES_MAX:
            del self.out_lines[:len(self.out_lines) - OUT_LINES_MAX]

    def leave_run(self):
        self.scan.cancel_combos()
        self.mode = MODE_EDIT
        self.scan.set_keys(None, None)
        self.dirty = True

    # ── 编辑：按键 ────────────────────────────────────────

    def handle_edit(self, ev):
        ed = self.ed
        for name, kind in ev:
            if name in ('P', 'Y', 'T', 'H'):
                ed.insert(DIGIT_OF_KEY[name])
                self.load_bad = False          # 用户在写新程序了，解除"别动 flash"那道闸
            elif name == 'O':
                ed.move(-DIGITS_PER_ROW if kind == 'long' else -1)
            elif name == 'N':
                ed.move(DIGITS_PER_ROW if kind == 'long' else 1)
            elif name == 'B':
                ed.backspace(1)
            elif name == 'A':
                if kind == 'long':
                    self.do_save()
                else:
                    self.start_run()
                    return
            elif name == 'AB':
                if kind == 'long':
                    self.do_clear()
                else:
                    self.do_reload()
            self.dirty = True

    def note(self, s):
        self.msg = s
        self.msg_t = self.hw.now()

    def do_save(self):
        if self.load_bad:
            # 读档失败过一次、用户还没动过手：别把 flash 里那份抹了
            self.note('BIG')
            return
        ok = False
        try:
            ok = self.hw.save(self.filename, self.ed.text())
        except Exception:
            ok = False
        self.ed.saved = bool(ok)
        self.note('SAVED' if ok else 'NOFLSH')

    def do_clear(self):
        self.ed.clear()
        self.load_bad = False
        try:
            self.hw.save(self.filename, '')
        except Exception:
            pass
        self.ed.saved = True
        self.note('CLEAR')

    def do_reload(self):
        # A+B 快点一下：把 flash 里存的那份重新读回来，等于"撤销全部改动"。
        try:
            s = self.hw.load(self.filename)
        except Exception:
            s = None
        if self.install(s):
            self.note('RELOAD')

    # ── 运行 ──────────────────────────────────────────────

    def start_run(self):
        self.do_save()
        self.out_lines = []
        self.ticks = 0
        vm = VM(step_limit=self.step_limit)
        ok = vm.load(self.ed.ds)
        self.vm = vm
        self.scan.set_keys(RUN_KEYS, INPUT_COMBOS)
        self.mode = MODE_RUN if ok else MODE_DONE
        self.dirty = True

    # ── 板上输入 ──────────────────────────────────────────

    def start_input(self):
        self.line = InputLine(self.in_mode)
        self.mode = MODE_INPUT
        self.dirty = True

    def resume_run(self):
        # 给完输入要把"卡在输入口上"这个状态清掉 —— 不然 vm.run 一看状态
        # 不是 RUNNING 就立刻又退回来，界面会一直卡在输入画面。
        if self.vm is not None and self.vm.status == ST_NEED_IN:
            self.vm.status = ST_RUNNING
        self.mode = MODE_RUN
        self.dirty = True

    def handle_input(self, ev):
        ln = self.line
        vm = self.vm
        for name, kind in ev:
            if name in ('P', 'Y', 'T', 'H'):
                ln.insert(DIGIT_OF_KEY[name])
            elif name == 'O':
                bys = ln.chars()
                if bys:
                    for b in bys:
                        vm.inq.append(b)
                    self.note_input = ln.preview()
                    ln.clear()
                    self.resume_run()
                    return
            elif name == 'N':
                ln.backspace(1)
            elif name == 'A':
                if kind == 'long':
                    self.stop_run()
                    return
                vm.inq.append(32)          # 空格
                self.resume_run()
                return
            elif name == 'B':
                if kind == 'long':
                    ln.toggle_mode()
                    self.in_mode = ln.mode
                else:
                    vm.inq.append(10)      # 回车
                    self.resume_run()
                    return
            elif name == 'AB':
                ln.insert_dot()
            self.dirty = True

    def stop_run(self):
        self.scan.cancel_combos()
        if self.vm:
            self.vm.status = ST_STOP
        self.mode = MODE_EDIT
        self.scan.set_keys(None, None)
        self.note('STOP')
        self.dirty = True

    # ── 画图：实现都在 ta_view，这里只把两边接上 ──────────

    def draw_edit(self):
        draw_edit(self.hw, self)

    def draw_run(self, st):
        draw_run(self.hw, self, st)

    def draw_input(self):
        draw_input(self.hw, self)


def main(hw=None):
    # ---8<--- BUNDLE-STRIP-START
    if hw is None:
        from ta_hw import get_hw
        hw = get_hw()
    # ---8<--- BUNDLE-STRIP-END
    app = App(hw, step_limit=STEP_LIMIT)
    try:
        app.loop()
    except KeyboardInterrupt:
        # 从电脑上按 Ctrl-C 打断（也就是烧录工具发的那两下 Ctrl-C），
        # 老老实实退回 REPL，别把板子卡死。
        pass
