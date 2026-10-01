# countdown.s —— 5 4 3 2 1，一个数一行。
# 一条分支指令就能成环，这是"程序"和"一堆数字"的分界线。

    li   t0, 0x1000         # tohost
    li   s0, 5
loop:
    addi t1, s0, 48         # '0' + n
    slli t1, t1, 8
    ori  t1, t1, 1
    sw   t1, 0(t0)

    li   t1, 10             # 换行
    slli t1, t1, 8
    ori  t1, t1, 1
    sw   t1, 0(t0)

    addi s0, s0, -1
    bnez s0, loop
    sw   zero, 0(t0)        # exit(0)
