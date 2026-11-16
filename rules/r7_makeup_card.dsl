# 规则 7：每月 2 张补签卡，断签 1 天自动补签（消耗 1 张，连续天数不断）
# 完整自然语言口径见 docs/RULES.md §1（含边界裁定与逐日算例）
# 注意口径 B：补签卡只豁免断签，漏签那天不计入连续天数、也不发币
# 状态里有「本月剩余卡数」，且转移依赖 month(day) 与 gap 的具体值
# → 真正的运行时状态机，Tier 2；这是纯窗口函数写不出来的那类规则
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}

state { consec := 0   cards := 2   m := 0 }

scan {
    if month(day) <> m { m := month(day)   cards := 2 }

    if gap() = 1 {
        if consec < 7 { consec := consec + 1 } else { consec := 1 }
    } else {
        if gap() = 2 and cards > 0 {
            cards := cards - 1
            if consec < 7 { consec := consec + 1 } else { consec := 1 }
        } else {
            consec := 1
        }
    }

    coin := 1
    if consec = 3 { coin := coin + 2 }
    if consec = 7 { coin := coin + 6 }
    emit coin as coin, cards as cards_left
}

report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin, min(cards_left) as min_cards
    from daily group by uid, month order by month, uid
}
