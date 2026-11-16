-- 由 dslc.py 自动生成 | Tier 1（集合式：窗口函数 + 常量日程表）
-- 状态变量 ['f_prev', 'f_cur'] | 日程表 19 行 | 前缀 18 + 周期 1 | 编译期常量折叠=True
WITH d AS (
    SELECT DISTINCT uid AS uid, DATE(in_time) AS dt
    FROM tb_user_log
    WHERE ((sign_in = 1) AND (artical_id = 0))
      AND in_time >= '2021-07-07 00:00:00' AND in_time < '2021-11-01 00:00:00'
),
g AS (   -- 连续日期分段：日期 - 行号 = 常量
    SELECT uid, dt, TO_DAYS(dt) - ROW_NUMBER() OVER (PARTITION BY uid ORDER BY dt) AS grp
    FROM d
),
n AS (   -- 段内第几天
    SELECT uid, dt, ROW_NUMBER() OVER (PARTITION BY uid, grp ORDER BY dt) AS k
    FROM g
),
-- 编译器展开的状态轨迹：19 行（前缀 18 + 周期 1）
sched AS (
    SELECT 1 AS k, 1 AS coin
    UNION ALL SELECT 2, 1
    UNION ALL SELECT 3, 2
    UNION ALL SELECT 4, 3
    UNION ALL SELECT 5, 5
    UNION ALL SELECT 6, 8
    UNION ALL SELECT 7, 13
    UNION ALL SELECT 8, 21
    UNION ALL SELECT 9, 34
    UNION ALL SELECT 10, 55
    UNION ALL SELECT 11, 89
    UNION ALL SELECT 12, 144
    UNION ALL SELECT 13, 233
    UNION ALL SELECT 14, 377
    UNION ALL SELECT 15, 610
    UNION ALL SELECT 16, 987
    UNION ALL SELECT 17, 1000
    UNION ALL SELECT 18, 1000
    UNION ALL SELECT 19, 1000
),
daily AS (
    SELECT n.uid AS uid, n.dt, s.coin
    FROM n JOIN sched s ON s.k = IF(n.k < 19, n.k, 19 + MOD(n.k - 19, 1))
)
select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
