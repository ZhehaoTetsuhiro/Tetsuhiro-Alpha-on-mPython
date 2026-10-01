# echo.s —— 把输入原样回显，直到遇上换行。
#
# 读 0x1004 就是"从板上拿一个字符"：没有输入时 VM 会停下来等用户敲
# （运行界面上那套 数字 / 字符 模式）。

    li   t0, 0x1000         # tohost
    li   s0, 0x1004         # fromhost
    li   s1, 10             # '\n'
echo:
    lw   t1, 0(s0)          # 取一个输入字符
    beq  t1, s1, done
    beqz t1, done
    slli t1, t1, 8
    ori  t1, t1, 1
    sw   t1, 0(t0)
    j    echo
done:
    li   t1, 10
    slli t1, t1, 8
    ori  t1, t1, 1
    sw   t1, 0(t0)
    sw   zero, 0(t0)        # exit(0)
