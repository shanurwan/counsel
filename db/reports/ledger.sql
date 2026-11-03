-- The value ledger: outputs people accepted, the minutes each one saves by
-- its owner's estimate (minutes_per_output in agents/*.yaml), and what the
-- agent cost. Drafts people rejected are counted too: a high rejection rate
-- means the agent is making work, not saving it.
with accepted as (
  select agent, count(*) as accepted
  from counsel.accepted_outputs
  where accepted_at >= %(since)s and accepted_at < %(until)s
  group by agent
), reviewed as (
  select r.agent,
         count(*) filter (where d.status = 'rejected') as rejected,
         count(*) filter (where d.status in ('published', 'rejected')) as reviewed,
         count(*) filter (where d.status = 'published' and d.published_body is distinct from d.body) as edited
  from counsel.drafts d
  join counsel.runs r on r.id = d.run_id
  where r.started_at >= %(since)s and r.started_at < %(until)s
  group by r.agent
), spend as (
  select agent, count(*) as runs, sum(usd) as usd
  from counsel.run_costs
  where started_at >= %(since)s and started_at < %(until)s
  group by agent
)
select agent, g.owner,
       coalesce(s.runs, 0) as runs,
       coalesce(a.accepted, 0) as accepted,
       coalesce(v.reviewed, 0) as reviewed,
       coalesce(v.rejected, 0) as rejected,
       coalesce(v.edited, 0) as edited,
       g.minutes_per_output,
       (coalesce(a.accepted, 0) * g.minutes_per_output / 60.0)::float as hours_absorbed,
       coalesce(s.usd, 0)::float as usd
from spend s
full join accepted a using (agent)
left join reviewed v using (agent)
left join counsel.agent_settings g using (agent)
order by hours_absorbed desc nulls last, agent;
