# ADR-003: Build a small runtime instead of adopting one

Status: accepted, 2026-11-06

## Context

Counsel needs a headless, model-agnostic runtime: durable runs, a human gate,
rollback, and a full log. Workflow engines and agent frameworks offer parts of this.
What is special here is the rule in ADR-001: an agent's Postgres role must be the
whole policy, with each step's write committed in one transaction with its journal
entry, as that role.

This is a judgement from reading how each option works. No option was run under
load against Counsel's workload.

## Options

| Option | Durable | Model-agnostic | Fits the role rule | Cost to run |
|---|---|---|---|---|
| Temporal | Excellent | Yes | No: its history is in its own store, so a write and its journal entry are two systems and need a compensation path | A cluster or a paid cloud; not on this free tier |
| LangGraph with a Postgres checkpointer | Good | Yes | Partly: checkpoints are framework-owned, and role switching per step would fight its connection handling | A library, so cheap |
| A vendor's agent SDK | Varies | Often tied to the vendor | No: its tool loop is the centre, and Counsel's agents mostly never choose an action | A library, so cheap |
| pgmq with a small worker | Good | Yes | Yes for the queue, but it does not deduplicate (ADR-002) | Cheap |
| Build: a leased job table and a step journal | Good enough | Yes | Yes, by construction | About 340 lines to own |

## Decision

Build it. The transaction boundary is the design: `SET LOCAL ROLE`, the write, and
the journal entry commit together or not at all. Everything else (retries, leases,
resume) is small once that holds, and any engine that keeps its state elsewhere would
have to reinvent it with compensation.

## What this gives up, and when to reverse it

- **Long-running work.** A step must finish inside a 120 s request. Agents that wait
  on a person for days do so through a draft and a new job, which has been enough so
  far. Multi-hour agents would need a different worker.
- **Fan-out.** One instance, one request at a time is a free-tier choice. The lease
  uses `FOR UPDATE SKIP LOCKED`, so more workers should need no redesign, but that
  has only been tested with two workers on one queue.
- **Owning it.** There are no visual tools, and someone must maintain it.

Adopt an engine if agents start needing long waits with timers across many systems,
or if more than one team writes runtime code. The agent interface (`ctx.step`,
`ctx.complete`) is small enough that behaviours could move without being rewritten.

## Model-agnostic

The runtime only knows `complete(messages) -> Completion`. `ChatModel` speaks the
common chat-completions format, and `FallbackModel` chains providers, so an outage
at one hands over to the next. A truncated answer does not hand over: it was billed,
and the fix is a larger `max_tokens`. Each call records which provider answered. The
second provider (Gemini) is tested against a fake transport and is off unless
`GEMINI_API_KEY` is set. Its free tier may use prompts to improve its products, which
is acceptable for synthetic data and would not be for real company data.
