# 规则 4：纯斐波那契，永不重置、不封顶（30 天窗口，避免 BIGINT 溢出）
# 状态永不重复 → 编译器按窗口上界展开日程表（无环）
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-08-05
}

state { f_prev := 0   f_cur := 1 }

scan {
    if gap() = 1 {
        t      := f_cur
        f_cur  := f_prev + f_cur
        f_prev := t
    } else {
        f_prev := 0   f_cur := 1
    }
    emit f_cur as coin
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
}
