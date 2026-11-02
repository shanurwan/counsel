-- The execution layer: a durable queue, runs, and a step journal.
--
-- A job is leased, not popped: if the worker dies, the lease expires and the
-- next tick resumes the same run. Every step is committed to counsel.steps in
-- the same transaction as the agent's write, so a resumed run replays finished
-- steps from the journal and never repeats their side effects.
-- Design and trade-offs: docs/adr/002-runtime.md.

create table counsel.jobs (
  id               bigint generated always as identity primary key,
  agent            text not null,
  idempotency_key  text not null unique,
  payload          jsonb not null default '{}',
  status           text not null default 'queued'
                   check (status in ('queued', 'running', 'done', 'dead')),
  attempts         int not null default 0,
  max_attempts     int not null default 3,
  not_before       timestamptz not null default now(),
  lease_until      timestamptz,
  leased_by        text,
  run_id           uuid,
  last_error       text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create index jobs_ready on counsel.jobs (not_before, id) where status in ('queued', 'running');

create table counsel.runs (
  id           uuid primary key default gen_random_uuid(),
  job_id       bigint not null references counsel.jobs (id),
  agent        text not null,
  status       text not null default 'running'
               check (status in ('running', 'succeeded', 'failed', 'denied')),
  attempt      int not null,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  error        text
);

create table counsel.steps (
  run_id       uuid not null references counsel.runs (id),
  seq          int not null,
  name         text not null,
  output       jsonb,
  finished_at  timestamptz not null default now(),
  primary key (run_id, seq)
);

-- Every model call, request and response, so any run can be replayed.
create table counsel.model_calls (
  id             bigint generated always as identity primary key,
  run_id         uuid not null references counsel.runs (id),
  step_seq       int not null,
  provider       text not null,
  model          text not null,
  request_hash   text not null,
  request        jsonb not null,
  response       jsonb,
  error          text,
  latency_ms     int not null,
  prompt_tokens  int,
  output_tokens  int,
  created_at     timestamptz not null default now()
);

create index model_calls_run on counsel.model_calls (run_id, step_seq);

-- Dead letters are recorded as 'failed'.
alter table counsel.audit_log drop constraint audit_log_decision_check;
alter table counsel.audit_log add constraint audit_log_decision_check
  check (decision in ('allowed', 'denied', 'approved', 'reverted', 'failed'));

-- Digests go out at 09:00 in each team's own time zone.
alter table work.teams add column tz text not null default 'UTC';

-- Event path: a question asked in the handbook channel enqueues ask_handbook.
create table counsel.questions (
  id          uuid primary key default gen_random_uuid(),
  asker_id    uuid references work.people (id),
  body        text not null,
  created_at  timestamptz not null default now()
);

create function counsel.enqueue_question() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into counsel.jobs (agent, idempotency_key, payload)
  values ('ask_handbook', 'question:' || new.id, jsonb_build_object('question_id', new.id))
  on conflict (idempotency_key) do nothing;
  return new;
end $$;

create trigger enqueue_question
  after insert on counsel.questions
  for each row execute function counsel.enqueue_question();

-- The orchestrator is the role the runtime uses for bookkeeping: leasing jobs
-- and writing the journal. It is a member of counsel_agents, so it is audited
-- like every agent and the commitment guard applies to it.
do $$
begin
  if not exists (select from pg_roles where rolname = 'counsel_orchestrator') then
    create role counsel_orchestrator nologin;
  end if;
end $$;

grant counsel_agents to counsel_orchestrator;
grant usage on schema counsel to counsel_orchestrator;
grant select, insert, update on counsel.jobs, counsel.runs to counsel_orchestrator;
grant select, insert on counsel.steps, counsel.model_calls to counsel_orchestrator;
grant select on counsel.questions to counsel_orchestrator;
