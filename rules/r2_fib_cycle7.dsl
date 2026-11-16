# 规则 2：每日金币 = 斐波那契数列，仍然满 7 天重置
# 人类视角：三个变量、顺序赋值，和 @用户变量 写法一模一样
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}

state { n := 0   f_prev := 0   f_cur := 1 }

scan {
    if gap() = 1 and n < 7 {
        n      := n + 1
        t      := f_cur          # t 是行内临时变量，不跨行
        f_cur  := f_prev + f_cur
        f_prev := t
    } else {
        n := 1   f_prev := 0   f_cur := 1
    }
    emit f_cur as coin
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
}
