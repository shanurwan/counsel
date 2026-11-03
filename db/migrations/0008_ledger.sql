-- The value ledger: work the agents took off people's plates, against what it
-- cost. An output counts only once a person has accepted it:
--   * a handbook answer that answered (not "the handbook does not cover this");
--   * a draft a reviewer published (`counsel review <draft> --publish`).
-- Each accepted output is worth the minutes its owner estimates a person
-- would spend doing it by hand (minutes_per_output in agents/*.yaml, compiled
-- into counsel.agent_settings). The estimate is the owner's, in a PR, and the
-- ledger says so; it is not a measurement.

-- Filled by the compiled registry (db/generated/agents.sql) on every migrate.
create table counsel.agent_settings (
  agent               text primary key,
  owner               text not null,
  minutes_per_output  int check (minutes_per_output between 1 and 480),
  budget_usd          numeric(10, 2)
);

-- Who reviewed a draft, when, and what they published. The agent's body is
-- kept as written, so how much a person had to edit stays visible.
-- Agents cannot update drafts at all; people can.
alter table counsel.drafts
  add column reviewed_at timestamptz,
  add column reviewed_by text,
  add column published_body text;

create view counsel.accepted_outputs as
select r.agent, a.run_id, r.started_at as accepted_at, 'answer' as kind
from counsel.answers a
join counsel.runs r on r.id = a.run_id
where a.body not like 'The handbook does not cover this.%'
union all
select r.agent, d.run_id, d.reviewed_at, split_part(d.kind, ':', 1)
from counsel.drafts d
join counsel.runs r on r.id = d.run_id
where d.status = 'published';

create view counsel.value_ledger as
with accepted as (
  select agent, date_trunc('month', accepted_at) as month, count(*) as accepted
  from counsel.accepted_outputs group by 1, 2
), spend as (
  select agent, date_trunc('month', started_at) as month, count(*) as runs, sum(usd) as usd
  from counsel.run_costs group by 1, 2
)
select agent, month,
       coalesce(s.runs, 0) as runs,
       coalesce(a.accepted, 0) as accepted,
       g.owner, g.minutes_per_output,
       coalesce(a.accepted, 0) * g.minutes_per_output / 60.0 as hours_absorbed,
       coalesce(s.usd, 0) as usd,
       coalesce(s.usd, 0) / nullif(coalesce(a.accepted, 0) * g.minutes_per_output / 60.0, 0)
         as usd_per_hour_absorbed
from accepted a
full join spend s using (agent, month)
left join counsel.agent_settings g using (agent);

-- A question asked with no asker (e.g. `counsel ask`) is unattributed, not scheduled.
create or replace view counsel.run_costs as
select r.id as run_id, r.agent, r.status, r.attempt, r.started_at, r.finished_at,
       coalesce('customer:' || tk.customer_id, 'team:' || pe.team_id,
                case when j.payload ? 'question_id' or j.payload ? 'ticket_id' then 'unattributed'
                     else 'schedule' end) as requester,
       count(c.id) filter (where c.cached_from is null and c.error is null) as model_calls,
       count(c.id) filter (where c.cached_from is not null) as cache_hits,
       count(c.id) filter (where c.error is not null) as model_errors,
       coalesce(sum(c.input_tokens), 0) as input_tokens,
       coalesce(sum(c.output_tokens), 0) as output_tokens,
       coalesce(sum(c.usd), 0) as usd,
       coalesce(sum(c.saved_usd), 0) as saved_usd,
       coalesce(sum(c.latency_ms), 0) as model_ms,
       bool_or(c.estimated) as estimated
from counsel.runs r
join counsel.jobs j on j.id = r.job_id
left join counsel.questions q on q.id = (j.payload ->> 'question_id')::uuid
left join work.people pe on pe.id = q.asker_id
left join support.tickets tk on tk.id = (j.payload ->> 'ticket_id')::uuid
left join counsel.call_costs c on c.run_id = r.id
group by r.id, j.payload, tk.customer_id, pe.team_id;

-- The dashboard login reads the ledger too: counts and dollars, no text.
grant select on counsel.value_ledger to counsel_reader;
