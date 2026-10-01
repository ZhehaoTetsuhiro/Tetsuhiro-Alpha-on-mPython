# -*- coding: utf-8 -*-
"""板上程序的入口。**只做一件事**：把界面跑起来。

（固件看到 main.py 退出就会 soft reboot 整块板子，所以 ta_app.loop() 里面
把异常都兜住了，宁可退回编辑画面也不让 main 死掉。）
"""

import ta_app

ta_app.main()
