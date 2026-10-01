# hello.s —— 打印 "Hi"，然后正常退出。
#
# 主机 I/O（沿用 tthr 的约定）：
#   往 0x1000 写 (ch<<8)|1  = 打印这个字符
#   往 0x1000 写 0          = 以退出码 0 结束

    li   t0, 0x1000         # tohost
    li   t1, 72             # 'H'
    slli t1, t1, 8
    ori  t1, t1, 1
    sw   t1, 0(t0)

    li   t1, 105            # 'i'
    slli t1, t1, 8
    ori  t1, t1, 1
    sw   t1, 0(t0)

    li   t1, 10             # 换行
    slli t1, t1, 8
    ori  t1, t1, 1
    sw   t1, 0(t0)

    sw   zero, 0(t0)        # exit(0)
