# Tetsuhiro-Alpha on mPython
#
#   make test      跑全部测试（不插板子）
#   make check     提交前的门禁：测试 + 打包检查 + 例子跑一遍
#   make samples   把 examples/ 全汇编一遍并跑起来
#   make build     拼出板上要传的那几个文件（dist/board/）
#   make size      跟一声冷笑：看板上那份版式要多少堆
#   make flash     一键烧录
#   make clean

PYTHON ?= python3

.PHONY: all test testall samples build size flash check clean

all: check

test:
	$(PYTHON) tests/test_all.py
	$(PYTHON) tests/test_flash.py

# 别名叫 test —— 就一个测试入口，省得记
testall: test

# 例子：汇编 + 在电脑上跑一遍（跑不出来就是坏了）
samples:
	@for f in examples/*.s; do \
		printf '%-28s ' "$$f"; \
		$(PYTHON) tools/ta.py asm "$$f" -o "$${f%.s}.ta" >/dev/null || exit 1; \
		$(PYTHON) tools/ta.py run "$${f%.s}.ta" 2>&1 | tr '\n' '|'; \
		echo; \
	done

build:
	$(PYTHON) tools/build.py

size:
	$(PYTHON) tools/build.py --check

flash:
	$(PYTHON) tools/flash.py

check: test samples size

clean:
	rm -rf dist .tasim .scratch
	find . -name '__pycache__' -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
