# 规则 3：斐波那契，永不重置，但单日封顶 1000
# 状态轨迹是「前缀 + 不动点」：1,1,2,3,...,987,1000,1000,...
# 编译器会自动识别 ρ 形轨迹，把 118 天压成十几行日程表
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}

state { f_prev := 0   f_cur := 1 }

scan {
    if gap() = 1 {
        t      := f_cur
        f_cur  := least(f_prev + f_cur, 1000)
        f_prev := least(t, 1000)
    } else {
        f_prev := 0   f_cur := 1
    }
    emit f_cur as coin
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
}
