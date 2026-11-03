-- Runs that failed every attempt, and what they spent on the way.
select rc.agent, count(*) as runs, sum(rc.usd)::float as usd,
       (array_agg(rc.run_id::text order by rc.usd desc))[1:3] as sample_runs,
       min(r.error) as example_error
from counsel.run_costs rc
join counsel.runs r on r.id = rc.run_id
where rc.status = 'failed'
  and rc.started_at >= %(since)s and rc.started_at < %(until)s
group by rc.agent
order by usd desc;
