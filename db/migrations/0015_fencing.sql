-- Red-team fixes for the runtime (docs/red-team.md, R1 and R2).
--
-- R1: a worker kept committing steps after its lease expired (production,
-- 4 and 5 Nov). The runtime now renews the lease inside every step's
-- transaction and refuses the step when it no longer holds it. No schema
-- change: counsel_orchestrator already updates counsel.jobs.
--
-- R2: a model call is made before it is recorded, so a worker that dies in
-- between leaves no trace and the retry pays again. Each call now writes an
-- intent first. An intent with no model_calls row is a call that was lost:
-- the next attempt records it, so it shows up in the cost report as a failure
-- instead of disappearing.

create table counsel.model_call_intents (
  id            bigint generated always as identity primary key,
  run_id        uuid not null references counsel.runs (id),
  step_seq      int not null,
  request_hash  text not null,
  started_at    timestamptz not null default now()
);

create index model_call_intents_run on counsel.model_call_intents (run_id, step_seq);

alter table counsel.model_calls add column intent_id bigint references counsel.model_call_intents (id);

grant select, insert on counsel.model_call_intents to counsel_orchestrator;
