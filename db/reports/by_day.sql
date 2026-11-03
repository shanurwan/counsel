-- Spend per day and agent, for the trend and the month-end projection.
select (started_at at time zone 'UTC')::date as day, agent, count(*) as runs, sum(usd)::float as usd
from counsel.run_costs
where started_at >= %(since)s and started_at < %(until)s
group by 1, 2
order by 1, 2;
