# 规则 8：产品又改需求了 —— 一轮 9 天，第 2 天 +1、第 5 天 +3、第 9 天 +8
# 和 r1 相比，人类只动了 scan 块里的 4 个数字；SQL 由编译器重新生成
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}

state { consec := 0 }

scan {
    if day = prev(day) + 1 and consec < 9 { consec := consec + 1 }
    else { consec := 1 }

    coin := 1
    if consec = 2 { coin := coin + 1 }
    if consec = 5 { coin := coin + 3 }
    if consec = 9 { coin := coin + 8 }
    emit coin as coin
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
}
