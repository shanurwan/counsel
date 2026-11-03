-- Calls that were billed but produced nothing usable, e.g. an answer cut off
-- at max_tokens. The runtime retries the job, so the same prompt is often
-- billed again and cut off again.
select r.agent,
       split_part(c.error, ':', 2) as reason,
       count(*) as calls,
       count(distinct c.run_id) as runs,
       sum(c.usd)::float as usd,
       (array_agg(distinct c.run_id::text))[1:3] as sample_runs
from counsel.call_costs c
join counsel.runs r on r.id = c.run_id
where c.error is not null and c.usd > 0
  and c.created_at >= %(since)s and c.created_at < %(until)s
group by 1, 2
order by usd desc;
