-- 由 dslc.py 自动生成 | Tier 1（集合式：窗口函数 + 常量日程表）
-- 状态变量 ['f_prev', 'f_cur'] | 日程表 31 行 | 无环 | 编译期常量折叠=True
WITH d AS (
    SELECT DISTINCT uid AS uid, DATE(in_time) AS dt
    FROM tb_user_log
    WHERE ((sign_in = 1) AND (artical_id = 0))
      AND in_time >= '2021-07-07 00:00:00' AND in_time < '2021-08-06 00:00:00'
),
g AS (   -- 连续日期分段：日期 - 行号 = 常量
    SELECT uid, dt, TO_DAYS(dt) - ROW_NUMBER() OVER (PARTITION BY uid ORDER BY dt) AS grp
    FROM d
),
n AS (   -- 段内第几天
    SELECT uid, dt, ROW_NUMBER() OVER (PARTITION BY uid, grp ORDER BY dt) AS k
    FROM g
),
-- 编译器展开的状态轨迹：31 行（无环，按窗口上界展开）
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
    UNION ALL SELECT 17, 1597
    UNION ALL SELECT 18, 2584
    UNION ALL SELECT 19, 4181
    UNION ALL SELECT 20, 6765
    UNION ALL SELECT 21, 10946
    UNION ALL SELECT 22, 17711
    UNION ALL SELECT 23, 28657
    UNION ALL SELECT 24, 46368
    UNION ALL SELECT 25, 75025
    UNION ALL SELECT 26, 121393
    UNION ALL SELECT 27, 196418
    UNION ALL SELECT 28, 317811
    UNION ALL SELECT 29, 514229
    UNION ALL SELECT 30, 832040
    UNION ALL SELECT 31, 1346269
),
daily AS (
    SELECT n.uid AS uid, n.dt, s.coin
    FROM n JOIN sched s ON s.k = n.k
)
select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
