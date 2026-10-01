# -*- coding: utf-8 -*-
"""把 src/ 整理成板上要的那几个文件，写到 dist/board/。

板上为什么是**几个文件**而不是一个大文件：MicroPython 编译一个模块时要先把
整个文件的语法树建出来，峰值正比于**单个文件**的大小。掌控板只有不到 100 KB
的堆，所以决定成败的是最大的那一个文件，不是总量。

  python3 tools/build.py            # -> dist/board/{main,ta_core,ta_ui,ta_app,ta_hw}.py
  python3 tools/build.py --single   # -> dist/single/main.py（**别拿这个烧板子**，看上面那段）
  python3 tools/build.py --check    # 只看每个文件多大
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'src')

BOARD_FILES = ('ta_bits.py', 'ta_core.py', 'ta_ui.py', 'ta_view.py',
               'ta_app.py', 'ta_hw.py')
ENTRY = 'main.py'

# 板上不要的整段：SimHW（6 KB+）。电脑端工具另有入口。
HOST_BLOCK = re.compile(r'^[ \t]*# ---8<--- HOST-ONLY-START.*?'
                        r'^[ \t]*# ---8<--- HOST-ONLY-END[ \t]*\n?',
                        re.S | re.M)

# 打包成单文件时，这几个 import 的字面得去掉（名字已经在上面了）。
BUNDLE_BLOCK = re.compile(r'^[ \t]*# ---8<--- BUNDLE-STRIP-START.*?'
                          r'^[ \t]*# ---8<--- BUNDLE-STRIP-END[ \t]*\n?',
                          re.S | re.M)


def read(name):
    with open(os.path.join(SRC, name), encoding='utf-8') as f:
        return f.read()


def strip_host(text):
    return HOST_BLOCK.sub('', text)


def strip_bundle(text):
    return BUNDLE_BLOCK.sub('', text)


def write(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return len(text.encode('utf-8'))


def build(outdir):
    total = 0
    sizes = []
    for name in BOARD_FILES + (ENTRY,):
        text = read(name)
        if name != ENTRY:
            # 板上是**分开的模块**，那几行 import 是真的要用的，只把
            # HOST-ONLY 整段（SimHW）扔掉。
            text = strip_host(text)
        n = write(os.path.join(outdir, name), text)
        sizes.append((name, n))
        total += n
    return sizes, total


def build_single(path):
    parts = []
    for name in BOARD_FILES:
        parts.append(strip_host(strip_bundle(read(name))))
    main = read(ENTRY)
    body = '#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n'
    body += '# 单文件版，由 tools/build.py --single 拼出来。**别拿这个烧板子**：\n'
    body += '# 语法树峰值正比于单个文件大小，这一个文件太大了。\n'
    body += ''.join(parts)
    body += main.replace('import ta_app\n', '').replace('ta_app.main()', 'main()')
    n = write(path, body)
    return n


def main(argv):
    if '--single' in argv:
        p = os.path.join(ROOT, 'dist', 'single', 'main.py')
        n = build_single(p)
        print('%s  %d 字节' % (os.path.relpath(p, ROOT), n))
        print('  ⚠️ 别拿它烧板子 —— 单文件太大，板上建不出语法树。')
        return 0

    outdir = os.path.join(ROOT, 'dist', 'board')
    sizes, total = build(outdir)
    print('dist/board/')
    for name, n in sizes:
        print('  %-14s %6d 字节' % (name, n))
    print('  %-14s %6d 字节' % ('合计', total))
    biggest = max(n for _, n in sizes)
    print('  最大单个文件 %d 字节（真板上 import 完还剩 65040 字节堆，'
          '卡住的是最大的那一个文件）' % biggest)
    if '--check' in argv:
        return 0 if biggest < 20000 else 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
