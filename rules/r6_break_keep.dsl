# 规则 6：断签不清零，保留一半进度（向下取整，至少 1）
# 断签后的状态依赖「断签前的状态」→ 不是常量重置
# → 编译器自动降级 Tier 2（递归 CTE 状态机），DSL 一个字都不用改写法
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}

state { consec := 0 }

scan {
    if gap() = 1 {
        if consec < 7 { consec := consec + 1 } else { consec := 1 }
    } else {
        consec := greatest(1, floor(consec / 2))
    }
    coin := 1
    if consec = 3 { coin := coin + 2 }
    if consec = 7 { coin := coin + 6 }
    emit coin as coin
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
}
