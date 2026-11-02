# ADR-002: A leased job table, a step journal, and one clock

Status: accepted, 2026-11-03

## Context

The platform should be boring: agents run on a schedule and on events, state
survives restarts, failed runs end cleanly, and every run can be reconstructed
from its log. It must also stay inside the GCP and Supabase free tiers, which
allow 3 Cloud Scheduler jobs and one small Cloud Run instance.

## Decision

**Queue: a Postgres table with leases, not pgmq.** `counsel.jobs` has a unique
`idempotency_key`, so a schedule fire time or an event enqueues at most once
(`on conflict do nothing`). A worker leases a job with
`FOR UPDATE SKIP LOCKED` and a 150 s `lease_until`, longer than Cloud Run's
120 s request timeout. A job whose worker died is leased again after the lease
expires, as the same run. Supabase Queues (pgmq) was the first plan; it does
not deduplicate messages, and enqueueing from a trigger in the same
transaction as the event row is simpler with a plain table that also runs on
any Postgres in CI.

**Steps: journaled in the same transaction as the write.** Agents are written
as a sequence of steps. A step that writes as an agent `SET LOCAL ROLE`s to
that agent, does the write, switches to `counsel_orchestrator`, and records
`(run_id, seq, output)` in `counsel.steps`, all in one transaction. On resume
the runtime replays finished steps from the journal instead of running them.
A model call is a step too, so a run killed after the model answered does not
call the model again or write twice (`test_killed_run_resumes_...`).

**Failures: three attempts, then a dead letter.** Retries back off 30 s, 60 s.
After the last attempt the job is `dead`, the run `failed`, and an audit entry
records why. A job whose final attempt died mid-run is reaped on the next tick.

**Denials: recorded, never retried.** A Postgres refusal (`42501`, `CN001`,
`CN002`) ends the run as `denied` and writes `denied_write` to the audit log in
a fresh transaction, as ADR-001 requires. Retrying a forbidden write cannot
make it allowed.

**Clock: one Scheduler job.** Cloud Scheduler calls `POST /tick` every 30
minutes. Agent schedules live in `agents/*.yaml`; each tick enqueues every fire
time in the last 35 minutes, idempotently, then drains the queue for up to 90
s. Agents without an implementation are not scheduled, rather than
dead-lettered every hour.

**Model: an adapter with a fallback list and replay.** The NVIDIA API retires
models with a 410; on 2026-11-03 the two Llama models in the original plan were
already gone. The adapter falls through an ordered list on 404/410, backs off on
429/5xx, keeps under the free tier's ~40 requests a minute, disables Nemotron's
thinking (it consumed the token budget and truncated a digest), and treats a
`length` finish as an error so a truncated answer is never written. Every call
is stored in `counsel.model_calls`; CI replays recordings and never calls the
API.

**Identity: one login with no privileges.** Cloud Run connects as
`counsel_runtime` (`NOINHERIT`, no grants). Every transaction picks its role
explicitly: the orchestrator for bookkeeping, or one agent for that agent's
work. The database URL in Secret Manager holds this login, not `postgres`.

## Consequences

- `counsel replay <run>` rebuilds any run: steps, prompts, responses, model,
  latency and audit entries.
- Cloud Run is capped at 1 instance, 1 request, 120 s, so ticks never overlap
  and the worst case stays inside the free tier (docs/free-tier.md).
- The 30-minute tick bounds event latency: a question waits up to 30 minutes.
  A database webhook to `/tick` would cut that, at the cost of more requests.
- Steps must be deterministic given the journal: anything random (ids, the
  current time) is produced inside a step so it is journaled with it.
