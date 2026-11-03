-- Identical requests paid for more than once. The request hash covers the
-- whole prompt (system text, documents, question), so a repeat would have got
-- the same answer: this is what a response cache would have saved.
with paid as (
  select r.agent, c.request_hash, c.usd,
         row_number() over (partition by c.request_hash order by c.id) as nth
  from counsel.call_costs c
  join counsel.runs r on r.id = c.run_id
  where c.cached_from is null and c.error is null
    and c.created_at >= %(since)s and c.created_at < %(until)s
)
select agent,
       count(*) as paid_calls,
       count(*) filter (where nth > 1) as repeat_calls,
       count(distinct request_hash) filter (where nth > 1) as repeated_requests,
       coalesce(sum(usd) filter (where nth > 1), 0)::float as repeat_usd
from paid
group by agent
having count(*) filter (where nth > 1) > 0
order by repeat_usd desc, agent;
