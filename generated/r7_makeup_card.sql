-- 由 dslc.py 自动生成 | Tier 2（递归 CTE 状态机）
-- 降级原因: 首行后状态 ['cards', 'm'] 仍依赖旧状态/日历（不是常量重置）; 断签后状态 ['consec', 'cards', 'm'] 仍依赖旧状态/日历（不是常量重置）; 连续签到的状态转移依赖绝对日期 ['day']
-- MySQL 8.0 请先放开递归深度: SET SESSION cte_max_recursion_depth = 1000000;
-- MariaDB 对应参数: SET SESSION max_recursive_iterations = 1000000;
WITH RECURSIVE d AS (
    SELECT DISTINCT uid AS uid, DATE(in_time) AS dt
    FROM tb_user_log
    WHERE ((sign_in = 1) AND (artical_id = 0))
      AND in_time >= '2021-07-07 00:00:00' AND in_time < '2021-11-01 00:00:00'
),
seq AS (
    SELECT uid, dt, ROW_NUMBER() OVER (PARTITION BY uid ORDER BY dt) AS i
    FROM d
),
walk AS (
    SELECT s.uid,
           s.dt,
           s.i,
           CAST(IF((NULL = 1), (0 + 1), IF(((NULL = 2) AND (2 > 0)), (0 + 1), 1)) AS SIGNED) AS st_consec,
           CAST(IF((NULL = 1), 2, IF(((NULL = 2) AND (2 > 0)), (2 - 1), 2)) AS SIGNED) AS st_cards,
           CAST(IF((MONTH(s.dt) <> 0), MONTH(s.dt), 0) AS SIGNED) AS st_m,
           CAST(IF((IF((NULL = 1), (0 + 1), IF(((NULL = 2) AND (2 > 0)), (0 + 1), 1)) = 7), (IF((IF((NULL = 1), (0 + 1), IF(((NULL = 2) AND (2 > 0)), (0 + 1), 1)) = 3), (1 + 2), 1) + 6), IF((IF((NULL = 1), (0 + 1), IF(((NULL = 2) AND (2 > 0)), (0 + 1), 1)) = 3), (1 + 2), 1)) AS SIGNED) AS coin,
           CAST(IF((NULL = 1), 2, IF(((NULL = 2) AND (2 > 0)), (2 - 1), 2)) AS SIGNED) AS cards_left
    FROM seq s WHERE s.i = 1
    UNION ALL
    SELECT s.uid,
           s.dt,
           s.i,
           IF((DATEDIFF(s.dt, w.dt) = 1), IF((w.st_consec < 7), (w.st_consec + 1), 1), IF(((DATEDIFF(s.dt, w.dt) = 2) AND (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) > 0)), IF((w.st_consec < 7), (w.st_consec + 1), 1), 1)),
           IF((DATEDIFF(s.dt, w.dt) = 1), IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards), IF(((DATEDIFF(s.dt, w.dt) = 2) AND (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) > 0)), (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) - 1), IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards))),
           IF((MONTH(s.dt) <> w.st_m), MONTH(s.dt), w.st_m),
           IF((IF((DATEDIFF(s.dt, w.dt) = 1), IF((w.st_consec < 7), (w.st_consec + 1), 1), IF(((DATEDIFF(s.dt, w.dt) = 2) AND (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) > 0)), IF((w.st_consec < 7), (w.st_consec + 1), 1), 1)) = 7), (IF((IF((DATEDIFF(s.dt, w.dt) = 1), IF((w.st_consec < 7), (w.st_consec + 1), 1), IF(((DATEDIFF(s.dt, w.dt) = 2) AND (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) > 0)), IF((w.st_consec < 7), (w.st_consec + 1), 1), 1)) = 3), (1 + 2), 1) + 6), IF((IF((DATEDIFF(s.dt, w.dt) = 1), IF((w.st_consec < 7), (w.st_consec + 1), 1), IF(((DATEDIFF(s.dt, w.dt) = 2) AND (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) > 0)), IF((w.st_consec < 7), (w.st_consec + 1), 1), 1)) = 3), (1 + 2), 1)),
           IF((DATEDIFF(s.dt, w.dt) = 1), IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards), IF(((DATEDIFF(s.dt, w.dt) = 2) AND (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) > 0)), (IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards) - 1), IF((MONTH(s.dt) <> w.st_m), 2, w.st_cards)))
    FROM walk w JOIN seq s ON s.uid = w.uid AND s.i = w.i + 1
),
daily AS (SELECT uid AS uid, dt, coin, cards_left FROM walk)
select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin, min(cards_left) as min_cards
    from daily group by uid, month order by month, uid
