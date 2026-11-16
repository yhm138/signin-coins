# 规则 5：原题 + 周末双倍
# 关键点：奖励依赖日历，但「状态转移」不依赖日历
# → 依然是 Tier 1：日程表只存状态，发放表达式在 SQL 里对着真实日期算
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}

state { consec := 0 }

scan {
    if gap() = 1 and consec < 7 { consec := consec + 1 } else { consec := 1 }
    coin := 1
    if consec = 3 { coin := coin + 2 }
    if consec = 7 { coin := coin + 6 }
    if dayofweek(day) = 1 or dayofweek(day) = 7 { coin := coin * 2 }
    emit coin as coin
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
}
