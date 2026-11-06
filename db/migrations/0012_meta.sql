-- The layer that watches the platform: drift_watcher reads one view of what is
-- going wrong, with counts and no prompts or tickets, and drafts a fix for a
-- person to file. The view runs as its owner, so the agent holds no grant on
-- the tables behind it.

create view counsel.platform_findings as
select 'dead_letters:' || agent as key, agent,
       format('%s job(s) dead-lettered in the last 7 days', count(*)) as evidence,
       'Read the last_error of these jobs, fix the cause, and add a test.' as action
from counsel.jobs
where status = 'dead' and updated_at > now() - interval '7 days'
group by agent
union all
select 'refused:' || agent, agent,
       format('%s run(s) refused by Postgres in the last 7 days', count(*)),
       'Each refusal is an attempted forbidden write. Replay the runs and decide whether the agent or its input is the problem.'
from counsel.runs
where status = 'denied' and started_at > now() - interval '7 days'
group by agent
union all
select 'rejections:' || r.agent, r.agent,
       format('%s of %s reviewed drafts rejected in the last 28 days',
              count(*) filter (where d.status = 'rejected'), count(*)),
       'Turn the rejected drafts into eval cases with `counsel challenge`, then fix the agent.'
from counsel.drafts d
join counsel.runs r on r.id = d.run_id
where d.status in ('published', 'rejected') and d.created_at > now() - interval '28 days'
group by r.agent
having count(*) >= 10 and count(*) filter (where d.status = 'rejected') * 4 >= count(*)
union all
select 'model_errors:' || r.agent, r.agent,
       format('%s of %s model calls failed in the last 7 days', count(*) filter (where c.error is not null), count(*)),
       'Check for a retired model or a prompt that truncates; both are billed or lost.'
from counsel.model_calls c
join counsel.runs r on r.id = c.run_id
where c.created_at > now() - interval '7 days'
group by r.agent
having count(*) >= 5 and count(*) filter (where c.error is not null) * 5 >= count(*)
union all
select 'stale_reviews:' || r.agent, r.agent,
       format('%s draft(s) waiting for a person for over 7 days', count(*)),
       'Nobody is reading this agent''s output. Ask its owner whether it should keep running.'
from counsel.drafts d
join counsel.runs r on r.id = d.run_id
where d.status = 'pending' and d.created_at < now() - interval '7 days'
group by r.agent
union all
select 'over_budget:' || s.agent, s.agent,
       format('$%s spent this month against a $%s budget', round(sum(c.usd)::numeric, 4), s.budget_usd),
       'Act on the cost report for this agent, or raise budget_usd in its YAML in a PR.'
from counsel.agent_settings s
join counsel.runs r on r.agent = s.agent and r.started_at >= date_trunc('month', now())
join counsel.call_costs c on c.run_id = r.id
where s.budget_usd is not null
group by s.agent, s.budget_usd
having sum(c.usd) > s.budget_usd;

-- What drift_watcher has already raised, so one problem is one draft. A person
-- who rejected a fix has answered; it is not raised again for 28 days.
create view counsel.raised_fixes as
select substr(kind, 5) as key
from counsel.drafts
where kind like 'fix:%' and created_at > now() - interval '28 days';

-- The runtime exports finished runs to the warehouse, and needs to read them whole.
create table counsel.warehouse_cursor (
  id           boolean primary key default true check (id),
  finished_at  timestamptz not null default '1970-01-01Z',
  run_id       uuid not null default '00000000-0000-0000-0000-000000000000',
  attempt      int not null default 0
);
insert into counsel.warehouse_cursor default values;

grant select, update on counsel.warehouse_cursor to counsel_orchestrator;
grant select on counsel.audit_log, counsel.run_costs to counsel_orchestrator;

-- counsel_mcp: the login behind the MCP server. It reads the journal and may ask
-- the handbook a question, and that is all. `counsel audit` fails on anything
-- more. It cannot log in until someone sets a password out of band:
--   alter role counsel_mcp password '...';
do $$
begin
  if not exists (select from pg_roles where rolname = 'counsel_mcp') then
    create role counsel_mcp login noinherit;
  end if;
end $$;

alter role counsel_mcp login noinherit nocreatedb nocreaterole nobypassrls connection limit 3;
alter role counsel_mcp set statement_timeout = '10s';

-- Usage on work and support lets the privilege matrix name their tables. It grants no table.
grant usage on schema counsel, work, support to counsel_mcp;
grant select on counsel.runs, counsel.steps, counsel.model_calls, counsel.audit_log, counsel.drafts,
  counsel.answers, counsel.outbound_messages, counsel.run_costs, counsel.value_ledger, counsel.agent_settings
  to counsel_mcp;
grant insert (body) on counsel.questions to counsel_mcp;

-- Asking enqueues a job. The login holds no grant on the queue, so the trigger
-- runs as its owner.
alter function counsel.enqueue_question() security definer;
