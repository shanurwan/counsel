-- The most expensive runs relative to their agent's median: where to look first.
with runs as (
  select * from counsel.run_costs
  where started_at >= %(since)s and started_at < %(until)s
), medians as (
  select agent, percentile_cont(0.5) within group (order by usd) as p50
  from runs group by agent
)
select r.agent, r.run_id::text, r.requester, r.status, r.model_calls, r.input_tokens, r.usd::float,
       (r.usd / m.p50)::float as times_median
from runs r join medians m using (agent)
where m.p50 > 0 and r.usd >= 3 * m.p50
order by times_median desc, r.usd desc
limit 10;
