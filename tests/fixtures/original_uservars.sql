-- 用户原始解法（@用户变量过程式），原样保留，用于对账
with t1 as(
    select *,date(date_format(in_time,"%Y-%m-%d")) as signin_date from tb_user_log where sign_in=1 and artical_id=0 and in_time >= date("2021-07-07 00:00:00") and  in_time<date("2021-11-01 00:00:00")
)
,
signin_dates AS (
    SELECT
        signin_date,
        uid,
        IF(signin_date = @prev_date + INTERVAL 1 DAY AND uid = @prev_uid
        AND @consec_days <> 7
        , @consec_days := @consec_days + 1, @consec_days := 1) AS consec_days,

        (case when uid = @prev_uid then
                (case when @consec_days=3 then @coin := @coin + 1+2
                      when @consec_days=7 then @coin := @coin + 1+6
                      else @coin := @coin + 1
                end)
              else
                @coin := 1
        end) AS coin,

        @prev_date := signin_date,
        @prev_uid := uid
    FROM
        (SELECT @prev_date := NULL, @prev_uid := NULL, @consec_days := 1 , @coin := 1) vars,
        (SELECT signin_date, uid FROM t1 ORDER BY uid, signin_date) ordered_dates
)
,
`每个用户每日金币持有数` as(
select signin_date,uid,coin,
row_number() over(partition by uid,date_format(signin_date,"%Y-%m") order by signin_date desc) as rn
from signin_dates
)
,
`每个用户每月底金币持有数` as(
select uid,
date_format(signin_date,"%Y%m") as month,
cast(coin as unsigned) as coin
from `每个用户每日金币持有数`
where rn  = 1
order by month , uid
)
,
tmptmp as(
    select *,row_number() over(partition by uid order by month asc) as my_rn
    from `每个用户每月底金币持有数`
)
select
tmp1.uid,tmp1.month,
ifnull(tmp1.coin-tmp2.coin ,tmp1.coin)
from tmptmp tmp1
left join tmptmp tmp2
on tmp1.uid=tmp2.uid and tmp2.my_rn=tmp1.my_rn-1
