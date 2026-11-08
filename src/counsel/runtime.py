"""The headless runtime: lease a job, run its agent step by step, never twice.

Connection model: the runtime logs in as `counsel_runtime`, which holds no
privileges. Every transaction starts by SET LOCAL ROLE to either the
orchestrator (jobs, runs, journal) or exactly one agent (that agent's reads
and writes). A step that writes as an agent records itself in the journal in
the same transaction, so a write and its journal entry commit together or not
at all. Design: docs/adr/002-runtime.md.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg
from croniter import croniter
from psycopg.rows import dict_row

from .compiler import ORCHESTRATOR_ROLE
from .log import log
from .model import Completion, Model, ModelError, request_hash
from .registry import Agent

LEASE = timedelta(seconds=150)  # longer than Cloud Run's 120 s request timeout
SCHEDULE_WINDOW = timedelta(minutes=35)  # one 30-minute tick plus slack
TICK_BUDGET_S = 90.0  # stop leasing new jobs after this; the request times out at 120
DENIALS = {"42501", "CN001", "CN002", "CN004"}
# Set by the server on SIGTERM: Cloud Run is replacing or removing this instance.
STOPPING = threading.Event()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Denied(Exception):
    """Postgres refused an agent's action. Recorded and never retried."""

    def __init__(self, sqlstate: str, message: str):
        super().__init__(message)
        self.sqlstate = sqlstate


class LeaseLost(Exception):
    """This worker no longer holds the job's lease, so another may be running it.
    It stops and writes nothing more; the lease holder finishes the run."""


LOST_CALL = "lost: the worker stopped before this answer was recorded; the provider may have billed it"


@dataclass
class RunContext:
    conn: psycopg.Connection
    run_id: str
    agent: Agent
    model: Model
    journal: dict[int, Any]
    seq: int = 0
    replayed: list[str] = field(default_factory=list)
    # Row-level scope for the agent's reads, e.g. {"counsel.ticket": <id>}. Set
    # by the behavior from the job payload, applied in every step's
    # transaction; RLS policies read it. The model never writes SQL, so it
    # cannot widen it.
    scope: dict[str, str] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(default=lambda: utcnow())
    # The lease this run holds. None only for callers outside a Worker (the bench).
    job_id: int | None = None
    worker_id: str | None = None

    def _role(self, role: str) -> None:
        self.conn.execute(f'set local role "{role}"')

    def _hold(self) -> None:
        """Renew this worker's lease, or refuse: a fence in the caller's transaction.

        In production a worker committed steps after its lease had expired, twice
        (docs/red-team.md, R1). Nobody else had taken the job, so no harm was done,
        but nothing would have stopped two workers writing one run.
        """
        if self.job_id is None:
            return
        now = self.clock()
        held = self.conn.execute(
            """update counsel.jobs set lease_until = %s
               where id = %s and leased_by = %s and status = 'running' and lease_until >= %s
               returning id""",
            (now + LEASE, self.job_id, self.worker_id, now)).fetchone()
        if held is None:
            raise LeaseLost(f"job {self.job_id}: lease expired or taken by another worker")

    def step(self, name: str, fn: Callable[[psycopg.Connection], Any], *, as_agent: bool = True) -> Any:
        """Run `fn` once per run. On resume, return the journaled output instead."""
        self.seq += 1
        if self.seq in self.journal:
            self.replayed.append(name)
            return self.journal[self.seq]
        try:
            with self.conn.transaction():
                self._role(ORCHESTRATOR_ROLE)
                self._hold()
                for key, value in self.scope.items():
                    self.conn.execute("select set_config(%s, %s, true)", (key, value))
                self._role(self.agent.role if as_agent else ORCHESTRATOR_ROLE)
                output = fn(self.conn)
                self._role(ORCHESTRATOR_ROLE)
                self.conn.execute(
                    "insert into counsel.steps (run_id, seq, name, output, finished_at) "
                    "values (%s, %s, %s, %s, %s)",
                    (self.run_id, self.seq, name, json.dumps(output), self.clock()),
                )
        except psycopg.Error as e:
            if e.sqlstate in DENIALS:
                raise Denied(e.sqlstate, f"{name}: {e.diag.message_primary}") from e
            raise
        return output

    def complete(self, name: str, messages: list[dict], *, max_tokens: int = 800) -> str:
        """A journaled model call. The request and response go to counsel.model_calls."""
        self.seq += 1
        if self.seq in self.journal:
            self.replayed.append(name)
            return self.journal[self.seq]["text"]

        digest = request_hash(messages, max_tokens)
        # Answered and recorded, but the worker stopped before journaling it: use it, don't pay twice.
        answered = self._answered(digest)
        if answered is not None:
            self.replayed.append(name)
            self._journal(name, answered)
            return answered["text"]
        self._record_lost(messages, max_tokens)

        cached = self._cached(digest)
        if cached:
            source_id, model, text = cached
            self._record(digest, messages, max_tokens, provider="cache", model=model, text=text,
                         latency_ms=0, prompt_tokens=0, output_tokens=0, cached_from=source_id)
            self._journal(name, {"text": text, "model": model})
            return text

        intent = self._intend(digest)
        started = time.monotonic()
        try:
            result = self.model.complete(messages, max_tokens=max_tokens)
        except ModelError as e:
            # Recorded with whatever usage the provider reported: a truncated
            # answer was billed even though it is never used.
            self._record(digest, messages, max_tokens, provider="unknown", model=e.model or "unknown",
                         error=str(e), latency_ms=int((time.monotonic() - started) * 1000),
                         prompt_tokens=e.prompt_tokens, output_tokens=e.output_tokens, intent_id=intent)
            raise
        # Wall time, not the last attempt's: retries and fallbacks are part of the call.
        elapsed = max(result.latency_ms, int((time.monotonic() - started) * 1000))
        # Recorded before it is journaled and outside the fence, so a call that
        # was paid for is never lost from the cost log, even if the lease was.
        self._record(digest, messages, max_tokens, provider=result.provider, model=result.model,
                     text=result.text, latency_ms=elapsed, prompt_tokens=result.prompt_tokens,
                     output_tokens=result.output_tokens, intent_id=intent,
                     failed_attempts=result.failed_attempts)
        self._journal(name, {"text": result.text, "model": result.model})
        return result.text

    def _answered(self, digest: str) -> dict | None:
        with self.conn.transaction():
            self._role(ORCHESTRATOR_ROLE)
            row = self.conn.execute(
                """select response ->> 'text', model from counsel.model_calls
                   where run_id = %s and step_seq = %s and request_hash = %s
                     and error is null and response is not null
                   order by id desc limit 1""", (self.run_id, self.seq, digest)).fetchone()
        return {"text": row[0], "model": row[1]} if row else None

    def _record_lost(self, messages: list[dict], max_tokens: int) -> None:
        """Calls this step started and never recorded: the worker died mid-call."""
        with self.conn.transaction():
            self._role(ORCHESTRATOR_ROLE)
            lost = self.conn.execute(
                """select i.id, i.request_hash from counsel.model_call_intents i
                   where i.run_id = %s and i.step_seq = %s
                     and not exists (select from counsel.model_calls c where c.intent_id = i.id)""",
                (self.run_id, self.seq)).fetchall()
        for intent_id, digest in lost:
            self._record(digest, messages, max_tokens, provider="unknown", model="unknown", error=LOST_CALL,
                         latency_ms=0, intent_id=intent_id)

    def _intend(self, digest: str) -> int:
        """Written, under the fence, before the request goes out."""
        with self.conn.transaction():
            self._role(ORCHESTRATOR_ROLE)
            self._hold()
            return self.conn.execute(
                """insert into counsel.model_call_intents (run_id, step_seq, request_hash, started_at)
                   values (%s, %s, %s, %s) returning id""",
                (self.run_id, self.seq, digest, self.clock())).fetchone()[0]

    def _journal(self, step: str, output: dict) -> None:
        with self.conn.transaction():
            self._role(ORCHESTRATOR_ROLE)
            self._hold()
            self.conn.execute(
                "insert into counsel.steps (run_id, seq, name, output, finished_at) values (%s, %s, %s, %s, %s)",
                (self.run_id, self.seq, step, json.dumps(output), self.clock()))

    def _cached(self, digest: str) -> tuple[int, str, str] | None:
        """An earlier answer to the identical request, if this agent opted in (T0 only).

        The hash covers the whole prompt, handbook excerpts included, so an
        edited document is a different request and is never served stale.
        """
        if not self.agent.cache_hours:
            return None
        with self.conn.transaction():
            self._role(ORCHESTRATOR_ROLE)
            return self.conn.execute(
                """select c.id, c.model, c.response ->> 'text'
                   from counsel.model_calls c join counsel.runs r on r.id = c.run_id
                   where c.request_hash = %s and r.agent = %s and c.cached_from is null
                     and c.error is null and c.response is not null and c.created_at > %s
                   order by c.id desc limit 1""",
                (digest, self.agent.name, self.clock() - timedelta(hours=self.agent.cache_hours)),
            ).fetchone()

    def _record(self, digest: str, messages: list[dict], max_tokens: int, *, provider: str, model: str,
                latency_ms: int, text: str | None = None, error: str | None = None,
                prompt_tokens: int | None = None, output_tokens: int | None = None,
                cached_from: int | None = None, intent_id: int | None = None,
                failed_attempts: tuple[str, ...] = ()) -> None:
        """The model call row: what was asked, what came back, and what it cost."""
        response = None
        if text is not None:
            response = {"text": text, **({"failed_attempts": list(failed_attempts)} if failed_attempts else {})}
        with self.conn.transaction():
            self._role(ORCHESTRATOR_ROLE)
            self.conn.execute(
                """insert into counsel.model_calls
                   (run_id, step_seq, provider, model, request_hash, request, response, error,
                    latency_ms, prompt_tokens, output_tokens, cached_from, intent_id, created_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.run_id, self.seq, provider, model, digest,
                 json.dumps({"messages": messages, "max_tokens": max_tokens}),
                 json.dumps(response) if response is not None else None, error,
                 latency_ms, prompt_tokens, output_tokens, cached_from, intent_id, self.clock()),
            )


Implementation = Callable[[RunContext, dict], None]


@dataclass
class Worker:
    conn: psycopg.Connection  # autocommit, logged in as counsel_runtime
    agents: dict[str, Agent]
    implementations: dict[str, Implementation]
    model: Model
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{id(object())}")
    clock: Callable[[], datetime] = utcnow
    stopping: threading.Event = field(default_factory=lambda: STOPPING)

    # --- scheduling ------------------------------------------------------------

    def enqueue_due(self, now: datetime) -> int:
        """Enqueue every scheduled fire time in the last window. Idempotent per fire time."""
        added = 0
        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            for agent in self.agents.values():
                # Registered but not yet implemented agents are not scheduled,
                # rather than dead-lettered every hour.
                if not agent.schedule or agent.name not in self.implementations:
                    continue
                it = croniter(agent.schedule, now - SCHEDULE_WINDOW)
                while (fire := it.get_next(datetime)) <= now:
                    cur = self.conn.execute(
                        """insert into counsel.jobs (agent, idempotency_key, payload, not_before)
                           values (%s, %s, %s, %s) on conflict (idempotency_key) do nothing""",
                        (agent.name, f"schedule:{agent.name}:{fire.isoformat()}",
                         json.dumps({"scheduled_for": fire.isoformat()}), fire),
                    )
                    added += cur.rowcount
        return added

    def reap(self, now: datetime) -> int:
        """Jobs whose final attempt died mid-run become dead letters."""
        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            cur = self.conn.execute(
                """update counsel.jobs set status = 'dead', updated_at = %s,
                          last_error = coalesce(last_error, 'lease expired on final attempt')
                   where status = 'running' and lease_until < %s and attempts >= max_attempts
                   returning id, agent, run_id""",
                (now, now),
            )
            dead = cur.fetchall()
            for job_id, agent, run_id in dead:
                self._audit(agent, "dead_letter", f"job:{job_id}", "failed",
                            {"reason": "lease expired on final attempt"})
                if run_id:
                    self._rollback(run_id, "dead letter")
        return len(dead)

    # --- leasing and running ------------------------------------------------------

    def lease(self, now: datetime) -> dict | None:
        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """update counsel.jobs j
                       set status = 'running', attempts = j.attempts + 1,
                           lease_until = %(until)s, leased_by = %(me)s, updated_at = %(now)s
                       where j.id = (
                         select id from counsel.jobs
                         where not_before <= %(now)s and attempts < max_attempts
                           and (status = 'queued' or (status = 'running' and lease_until < %(now)s))
                         order by id
                         for update skip locked
                         limit 1)
                       returning j.*""",
                    {"until": now + LEASE, "me": self.worker_id, "now": now},
                )
                return cur.fetchone()

    def run(self, job: dict) -> str:
        try:
            return self._run(job)
        except LeaseLost as e:
            # Someone else may own the job now: touch neither the job nor the run.
            self._audit(job["agent"], "lease_lost", f"job:{job['id']}", "failed",
                        {"worker": self.worker_id, "error": str(e)}, run_id=job.get("run_id"))
            log("WARNING", "lease lost; stopped without writing", agent=job["agent"], job_id=job["id"])
            return "lease lost"

    def _run(self, job: dict) -> str:
        agent = self.agents.get(job["agent"])
        impl = self.implementations.get(job["agent"])
        now = self.clock()

        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            run_id = job["run_id"]
            if run_id is None:
                run_id = self.conn.execute(
                    "insert into counsel.runs (job_id, agent, attempt, started_at) "
                    "values (%s, %s, %s, %s) returning id",
                    (job["id"], job["agent"], job["attempts"], now),
                ).fetchone()[0]
                self.conn.execute("update counsel.jobs set run_id = %s where id = %s", (run_id, job["id"]))
            else:
                self.conn.execute("update counsel.runs set attempt = %s where id = %s",
                                  (job["attempts"], run_id))
            journal = {seq: out for seq, out in self.conn.execute(
                "select seq, output from counsel.steps where run_id = %s", (run_id,))}

        if agent is None or impl is None:
            self._finish(job, run_id, "failed", "dead", f"no implementation for agent {job['agent']!r}")
            return "dead"

        ctx = RunContext(self.conn, str(run_id), agent, self.model, journal, clock=self.clock,
                         job_id=job["id"], worker_id=self.worker_id)
        try:
            impl(ctx, job["payload"])
        except LeaseLost:
            raise
        except Denied as e:
            self._audit(agent.name, "denied_write", f"run:{run_id}", "denied",
                        {"sqlstate": e.sqlstate, "message": str(e)}, run_id=run_id)
            self._finish(job, run_id, "denied", "done", str(e))
            self._rollback(run_id, f"refused: {e.sqlstate}")
            log("WARNING", "agent write denied", agent=agent.name, run_id=str(run_id), sqlstate=e.sqlstate)
            return "denied"
        except Exception as e:  # noqa: BLE001 - any failure is retried, then dead-lettered
            message = f"{type(e).__name__}: {e}"[:1000]
            if job["attempts"] >= job["max_attempts"]:
                self._audit(agent.name, "dead_letter", f"run:{run_id}", "failed",
                            {"error": message}, run_id=run_id)
                self._finish(job, run_id, "failed", "dead", message)
                self._rollback(run_id, "dead letter")
                log("ERROR", "job dead-lettered", agent=agent.name, job_id=job["id"], error=message)
                return "dead"
            backoff = timedelta(seconds=30 * 2 ** (job["attempts"] - 1))
            with self.conn.transaction():
                self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
                requeued = self.conn.execute(
                    """update counsel.jobs set status = 'queued', not_before = %s, last_error = %s,
                              lease_until = null, updated_at = %s
                       where id = %s and leased_by = %s and status = 'running' and lease_until >= %s""",
                    (now + backoff, message, now, job["id"], self.worker_id, self.clock()),
                )
                if requeued.rowcount == 0:
                    raise LeaseLost(f"job {job['id']}: lease lost before the retry was scheduled")
            log("WARNING", "job will retry", agent=agent.name, job_id=job["id"],
                attempt=job["attempts"], error=message)
            return "retry"

        self._finish(job, run_id, "succeeded", "done", None)
        log("INFO", "run succeeded", agent=agent.name, run_id=str(run_id),
            steps=ctx.seq, replayed=ctx.replayed)
        return "succeeded"

    def _finish(self, job: dict, run_id, run_status: str, job_status: str, error: str | None) -> None:
        now = self.clock()
        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            # Only the lease holder may close the run (docs/red-team.md, R1).
            closed = self.conn.execute(
                """update counsel.jobs set status = %s, lease_until = null, last_error = %s, updated_at = %s
                   where id = %s and leased_by = %s and status = 'running' and lease_until >= %s""",
                (job_status, error, now, job["id"], self.worker_id, now),
            )
            if closed.rowcount == 0:
                raise LeaseLost(f"job {job['id']}: lease lost before the run was closed")
            self.conn.execute(
                "update counsel.runs set status = %s, finished_at = %s, error = %s where id = %s",
                (run_status, now, error, run_id),
            )

    def _rollback(self, run_id, reason: str) -> None:
        """A run that did not finish takes back what it wrote (0010_contact_rollback.sql)."""
        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            self.conn.execute(
                "insert into counsel.run_rollbacks (run_id, reason) values (%s, %s) on conflict do nothing",
                (run_id, reason))

    def _audit(self, agent: str, action: str, target: str, decision: str, detail: dict,
               run_id=None) -> None:
        # A fresh transaction, so the record survives the failed one (ADR-001).
        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            self.conn.execute(
                """insert into counsel.audit_log (run_id, action, target, decision, detail)
                   values (%s, %s, %s, %s, %s)""",
                (run_id, f"{agent}:{action}", target, decision, json.dumps(detail)),
            )

    # --- one tick ------------------------------------------------------------------

    def tick(self, budget_s: float = TICK_BUDGET_S) -> dict:
        started = time.monotonic()
        now = self.clock()
        summary = {"enqueued": self.enqueue_due(now), "reaped": self.reap(now), "runs": []}
        while time.monotonic() - started < budget_s:
            if self.stopping.is_set():
                # Shutting down (a deploy, scale-in): take nothing new. A run cut
                # off mid-way resumes from its journal once its lease expires.
                summary["stopped"] = "shutting down"
                break
            job = self.lease(self.clock())
            if job is None:
                break
            result = self.run(job)
            summary["runs"].append({"job": job["id"], "agent": job["agent"], "result": result})
            if result == "lease lost":
                # This worker is running late (a slow call, a throttled instance).
                # Leasing again would hand it back the same expired job: leave it.
                summary["stopped"] = "lease lost"
                break
        log("INFO", "tick", **summary)
        return summary
