# 规则 1：原题
# 每天 1 币；连续第 3 天 +2，第 7 天 +6；满 7 天重置
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}

state { consec := 0 }

scan {
    if day = prev(day) + 1 and consec < 7 { consec := consec + 1 }
    else { consec := 1 }

    coin := 1
    if consec = 3 { coin := coin + 2 }
    if consec = 7 { coin := coin + 6 }
    emit coin as coin
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily
    group by uid, month
    order by month, uid
}
