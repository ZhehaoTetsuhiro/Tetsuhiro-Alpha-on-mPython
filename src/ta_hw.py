# -*- coding: utf-8 -*-
"""硬件层：同一份 App 跑在两种"掌控板"上。

  * MpythonHW —— 真的掌控板（mpython 固件）。128x64 OLED + P Y T H O N 六个触摸金指 + A/B。
  * SimHW     —— 电脑终端。16x8 字符网格直接画在终端里，键盘当触摸键，时间是可以推的
                 虚拟时钟，所以整套编辑器 / 运行逻辑不用插板子就能测。

模拟器按键约定（终端里）：
    小写 p y t h o n a b  = 短按
    大写 P Y T H O N A B  = 长按（模拟器替你按住）
    q / Ctrl-C            = 退出
"""

# ---8<--- BUNDLE-STRIP-START
try:
    from ta_core import *
    from ta_ui import *
except ImportError:
    pass
# ---8<--- BUNDLE-STRIP-END

import sys

KEY_LETTERS = 'pythondbaPYTHONDBA'
TOUCH_TH = 300          # 触摸键 read() 小于这个值就算摸到了


# ══════════════════════════════════════════════════════════════
#  真的掌控板
# ══════════════════════════════════════════════════════════════

class MpythonHW(object):
    def __init__(self):
        import mpython
        self.mp = mpython
        self.oled = mpython.oled
        try:
            self.oled.contrast(255)
        except Exception:
            pass

        # mPython 的 OLED 是 machine.framebuf 的子类，一般都有 8x8 的 text()。
        # 万一这版固件没有，就退回 16 像素高的中文字库。
        self.use_text = hasattr(self.oled, 'text')
        self.cell_w = 8
        self.cell_h = 8 if self.use_text else 16
        self.cols = 128 // self.cell_w
        self.rows = 64 // self.cell_h

        self.pads = {}
        for n in ('P', 'Y', 'T', 'H', 'O', 'N'):
            self.pads[n] = getattr(mpython, 'touchPad_' + n, None)
        self.btn_a = getattr(mpython, 'button_a', None)
        self.btn_b = getattr(mpython, 'button_b', None)
        self.mod = _mp_time()

    # ---- 屏幕 ----

    def clear(self):
        try:
            self.oled.fill(0)
        except Exception:
            pass

    def text(self, s, col, row):
        if not s:
            return
        try:
            if self.use_text:
                self.oled.text(s, col * self.cell_w, row * self.cell_h, 1)
            else:
                self.oled.DispChar(s, col * self.cell_w, row * self.cell_h)
        except Exception:
            pass

    def cursor(self, col, row):
        x = col * self.cell_w
        y = row * self.cell_h + self.cell_h - 2
        try:
            self.oled.fill_rect(x, y, self.cell_w - 1, 2, 1)
            return
        except Exception:
            pass
        try:
            self.oled.hline(x, y, self.cell_w - 1, 1)
        except Exception:
            pass

    def show(self):
        try:
            self.oled.show()
        except Exception:
            pass

    # ---- 串口（程序打印出来的东西也从这儿走一份） ----

    def write(self, s):
        try:
            print(s)
        except Exception:
            pass

    # ---- flash 存档 ----

    def save(self, name, text):
        f = open(name, 'w')
        try:
            f.write(text)
        finally:
            f.close()
        return True

    def load(self, name):
        try:
            f = open(name, 'r')
        except OSError:
            return None
        try:
            return f.read()
        except Exception:
            return None
        finally:
            f.close()

    # ---- 按键 ----

    def _pad(self, name):
        pad = self.pads.get(name)
        if pad is None:
            return False
        v = None
        try:
            v = pad.read()
        except Exception:
            try:
                v = pad.value()
            except Exception:
                return False
        try:
            return v < TOUCH_TH
        except TypeError:
            return bool(v)

    def _btn(self, obj):
        if obj is None:
            return False
        try:
            return obj.value() == 0
        except Exception:
            pass
        try:
            return bool(obj.is_pressed())
        except Exception:
            return False

    def read_key(self, name):
        if name == 'A':
            return self._btn(self.btn_a)
        if name == 'B':
            return self._btn(self.btn_b)
        return self._pad(name)

    # ---- 时间 ----

    def now(self):
        try:
            return self.mod.ticks_ms()
        except Exception:
            return int(self.mod.time() * 1000)

    def sleep(self, ms):
        try:
            self.mod.sleep_ms(ms)
            return
        except Exception:
            pass
        try:
            self.mod.time.sleep(ms / 1000.0)
        except Exception:
            import time as _t
            _t.sleep(ms / 1000.0)

    def want_quit(self):
        return False

    def shutdown(self):
        self.clear()
        self.show()


def _mp_time():
    try:
        import utime
        return utime
    except ImportError:
        import time
        return time


# ---8<--- HOST-ONLY-START
# ══════════════════════════════════════════════════════════════
#  电脑上的模拟器（板上用不着，打包时整段扔掉）
# ══════════════════════════════════════════════════════════════

class SimHW(object):
    def __init__(self, script=None, verbose=False, interactive=None,
                 datadir='.tasim', cols=16, rows=8, tail_ms=6000):
        self.cols = cols
        self.rows = rows
        self.cell_w = 8
        self.cell_h = 8
        self.buf = [[' '] * cols for _ in range(rows)]
        self.cur_pos = None
        self.serial = []
        self.frames = []
        self.verbose = verbose
        self.datadir = datadir
        self.t = 0
        self.quit = False
        self.down = {}
        self.script = None
        self.script_end = 0
        self.tail_ms = tail_ms
        self.realtime = False

        if script is None:
            self.realtime = True if interactive is None else interactive
            self.interactive = self.realtime
        else:
            self.interactive = False
            self._load_script(script)

        if self.interactive:
            self._raw_on()

    # ---- 剧本模式 ----
    #
    #   'pyT'                    字符串：小写=短按，大写=长按
    #   [('T', 70), (('A','B'), 1100)]
    #                            列表：每一项是 (键名, 按住毫秒)；
    #                            键名写成元组就是**同时按住好几个键**

    def _load_script(self, script):
        plan = []
        if isinstance(script, str):
            for ch in script:
                lo = ch.lower()
                if lo not in 'pythondab':
                    continue
                plan.append(((lo.upper(),), 1400 if ch.isupper() else 70))
        else:
            for item in script:
                names = item[0]
                if isinstance(names, str):
                    names = (names.upper(),)
                else:
                    names = tuple(n.upper() for n in names)
                plan.append((names, item[1]))
        self.script = plan
        t = 0
        spans = []
        for names, hold in plan:
            spans.append((t, t + hold, names))
            t += hold + 60
        self.spans = spans
        self.script_end = t

    # ---- 屏幕 ----

    def clear(self):
        self.buf = [[' '] * self.cols for _ in range(self.rows)]
        self.cur_pos = None

    def text(self, s, col, row):
        if row < 0 or row >= self.rows:
            return
        for i in range(len(s)):
            c = col + i
            if 0 <= c < self.cols:
                self.buf[row][c] = s[i]

    def cursor(self, col, row):
        self.cur_pos = (col, row)

    def show(self):
        frame = self.render()
        self.frames.append(frame)
        if len(self.frames) > 300:
            del self.frames[0]
        if self.verbose or self.interactive:
            self._paint(frame)

    def render(self):
        out = []
        out.append('+' + '-' * self.cols + '+')
        for r in range(self.rows):
            out.append('|' + ''.join(self.buf[r]) + '|')
            if self.cur_pos and self.cur_pos[1] == r:
                c = self.cur_pos[0]
                if 0 <= c < self.cols:
                    out.append('|' + ' ' * c + '^' + ' ' * (self.cols - c - 1) + '|')
        out.append('+' + '-' * self.cols + '+')
        return '\n'.join(out)

    def _paint(self, frame):
        if self.interactive:
            sys.stdout.write('\x1b[2J\x1b[H')
        sys.stdout.write(frame + '\n')
        for s in self.serial[-6:]:
            sys.stdout.write('  > ' + s + '\n')
        if self.interactive:
            sys.stdout.write('\n小写=短按  大写=长按  q=退出\n')
        sys.stdout.flush()

    # ---- 串口 ----

    def write(self, s):
        self.serial.append(s)

    # ---- 存档 ----

    def _path(self, name):
        import os
        try:
            os.makedirs(self.datadir)
        except OSError:
            pass
        return os.path.join(self.datadir, name)

    def save(self, name, text):
        f = open(self._path(name), 'w')
        try:
            f.write(text)
        finally:
            f.close()
        return True

    def load(self, name):
        try:
            f = open(self._path(name))
        except IOError:
            return None
        try:
            return f.read()
        except Exception:
            return None
        finally:
            f.close()

    # ---- 按键 ----

    def read_key(self, name):
        name = name.upper()
        if self.script is not None:
            for a, b, names in self.spans:
                if name in names and a <= self.t < b:
                    return True
            return False
        return self.down.get(name, 0) > self.t

    def _raw_on(self):
        try:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self.interactive = False

    def _raw_off(self):
        if not getattr(self, '_old', None):
            return
        try:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
        except Exception:
            pass
        self._old = None

    def _poll_kbd(self):
        try:
            import select
        except ImportError:
            return
        while True:
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0)
            except Exception:
                return
            if not r:
                return
            ch = sys.stdin.read(1)
            if not ch:
                return
            if ch in ('\x03', '\x04', 'q', 'Q'):
                self.quit = True
                return
            if ch in KEY_LETTERS:
                up = ch.upper()
                hold = 1400 if ch.isupper() else 90
                self.down[up] = self.t + hold

    # ---- 时间 ----

    def now(self):
        return self.t

    def sleep(self, ms):
        self.t += ms
        if self.interactive:
            import time as _t
            _t.sleep(ms / 1000.0)
            self._poll_kbd()

    def want_quit(self):
        if self.quit:
            return True
        if self.script is not None and self.t > self.script_end + self.tail_ms:
            return True
        return False

    def shutdown(self):
        self._raw_off()
        frame = self.render()
        if self.verbose or self.interactive:
            sys.stdout.write(frame + '\n')
        for s in self.serial:
            sys.stdout.write('  > ' + s + '\n')
        if self.interactive:
            sys.stdout.write('bye.\n')
        sys.stdout.flush()


# ---8<--- HOST-ONLY-END

# ══════════════════════════════════════════════════════════════

def get_hw(**kw):
    """板上用 MpythonHW，电脑上用 SimHW。"""
    why = ''
    if not kw.pop('force_sim', False):
        try:
            return MpythonHW()
        except Exception as e:
            why = '%s: %s' % (type(e).__name__, e)
    # ---8<--- HOST-ONLY-START
    return SimHW(**kw)
    # ---8<--- HOST-ONLY-END
    print('')
    print('!! TA 起不来：这块板子上 import 不到 mpython 库。')
    print('   原因：%s' % why)
    print('   十有八九是它装的不是 mPython/掌控板官方固件')
    print('   （比如 Mind+ 的"实时模式"固件，那个没有 REPL 也没有 mpython）。')
    raise RuntimeError('no mpython: %s' % why)
