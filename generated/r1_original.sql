-- 由 dslc.py 自动生成 | Tier 1（集合式：窗口函数 + 常量日程表）
-- 状态变量 ['consec'] | 日程表 8 行 | 前缀 1 + 周期 7 | 编译期常量折叠=True
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
-- 编译器展开的状态轨迹：8 行（前缀 1 + 周期 7）
sched AS (
    SELECT 1 AS k, 1 AS coin
    UNION ALL SELECT 2, 1
    UNION ALL SELECT 3, 3
    UNION ALL SELECT 4, 1
    UNION ALL SELECT 5, 1
    UNION ALL SELECT 6, 1
    UNION ALL SELECT 7, 7
    UNION ALL SELECT 8, 1
),
daily AS (
    SELECT n.uid AS uid, n.dt, s.coin
    FROM n JOIN sched s ON s.k = IF(n.k < 2, n.k, 2 + MOD(n.k - 2, 7))
)
select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily
    group by uid, month
    order by month, uid
