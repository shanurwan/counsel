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
DENIALS = {"42501", "CN001", "CN002"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Denied(Exception):
    """Postgres refused an agent's action. Recorded and never retried."""

    def __init__(self, sqlstate: str, message: str):
        super().__init__(message)
        self.sqlstate = sqlstate


@dataclass
class RunContext:
    conn: psycopg.Connection
    run_id: str
    agent: Agent
    model: Model
    journal: dict[int, Any]
    seq: int = 0
    replayed: list[str] = field(default_factory=list)

    def _role(self, role: str) -> None:
        self.conn.execute(f'set local role "{role}"')

    def step(self, name: str, fn: Callable[[psycopg.Connection], Any], *, as_agent: bool = True) -> Any:
        """Run `fn` once per run. On resume, return the journaled output instead."""
        self.seq += 1
        if self.seq in self.journal:
            self.replayed.append(name)
            return self.journal[self.seq]
        try:
            with self.conn.transaction():
                self._role(self.agent.role if as_agent else ORCHESTRATOR_ROLE)
                output = fn(self.conn)
                self._role(ORCHESTRATOR_ROLE)
                self.conn.execute(
                    "insert into counsel.steps (run_id, seq, name, output) values (%s, %s, %s, %s)",
                    (self.run_id, self.seq, name, json.dumps(output)),
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

        started = time.monotonic()
        result: Completion | None = None
        error = None
        try:
            result = self.model.complete(messages, max_tokens=max_tokens)
        except ModelError as e:
            error = str(e)

        with self.conn.transaction():
            self._role(ORCHESTRATOR_ROLE)
            self.conn.execute(
                """insert into counsel.model_calls
                   (run_id, step_seq, provider, model, request_hash, request, response, error,
                    latency_ms, prompt_tokens, output_tokens)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    self.run_id, self.seq,
                    result.provider if result else "unknown",
                    result.model if result else "unknown",
                    request_hash(messages, max_tokens),
                    json.dumps({"messages": messages, "max_tokens": max_tokens}),
                    json.dumps({"text": result.text}) if result else None,
                    error,
                    result.latency_ms if result else int((time.monotonic() - started) * 1000),
                    result.prompt_tokens if result else None,
                    result.output_tokens if result else None,
                ),
            )
            if result:
                self.conn.execute(
                    "insert into counsel.steps (run_id, seq, name, output) values (%s, %s, %s, %s)",
                    (self.run_id, self.seq, name, json.dumps({"text": result.text, "model": result.model})),
                )
        if error:
            raise ModelError(error)
        return result.text


Implementation = Callable[[RunContext, dict], None]


@dataclass
class Worker:
    conn: psycopg.Connection  # autocommit, logged in as counsel_runtime
    agents: dict[str, Agent]
    implementations: dict[str, Implementation]
    model: Model
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{id(object())}")
    clock: Callable[[], datetime] = utcnow

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
                        """insert into counsel.jobs (agent, idempotency_key, payload)
                           values (%s, %s, %s) on conflict (idempotency_key) do nothing""",
                        (agent.name, f"schedule:{agent.name}:{fire.isoformat()}",
                         json.dumps({"scheduled_for": fire.isoformat()})),
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
                   returning id, agent""",
                (now, now),
            )
            dead = cur.fetchall()
            for job_id, agent in dead:
                self._audit(agent, "dead_letter", f"job:{job_id}", "failed",
                            {"reason": "lease expired on final attempt"})
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
        agent = self.agents.get(job["agent"])
        impl = self.implementations.get(job["agent"])
        now = self.clock()

        with self.conn.transaction():
            self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            run_id = job["run_id"]
            if run_id is None:
                run_id = self.conn.execute(
                    "insert into counsel.runs (job_id, agent, attempt) values (%s, %s, %s) returning id",
                    (job["id"], job["agent"], job["attempts"]),
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

        ctx = RunContext(self.conn, str(run_id), agent, self.model, journal)
        try:
            impl(ctx, job["payload"])
        except Denied as e:
            self._audit(agent.name, "denied_write", f"run:{run_id}", "denied",
                        {"sqlstate": e.sqlstate, "message": str(e)}, run_id=run_id)
            self._finish(job, run_id, "denied", "done", str(e))
            log("WARNING", "agent write denied", agent=agent.name, run_id=str(run_id), sqlstate=e.sqlstate)
            return "denied"
        except Exception as e:  # noqa: BLE001 - any failure is retried, then dead-lettered
            message = f"{type(e).__name__}: {e}"[:1000]
            if job["attempts"] >= job["max_attempts"]:
                self._audit(agent.name, "dead_letter", f"run:{run_id}", "failed",
                            {"error": message}, run_id=run_id)
                self._finish(job, run_id, "failed", "dead", message)
                log("ERROR", "job dead-lettered", agent=agent.name, job_id=job["id"], error=message)
                return "dead"
            backoff = timedelta(seconds=30 * 2 ** (job["attempts"] - 1))
            with self.conn.transaction():
                self.conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
                self.conn.execute(
                    """update counsel.jobs set status = 'queued', not_before = %s, last_error = %s,
                              lease_until = null, updated_at = %s where id = %s""",
                    (now + backoff, message, now, job["id"]),
                )
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
            self.conn.execute(
                "update counsel.runs set status = %s, finished_at = %s, error = %s where id = %s",
                (run_status, now, error, run_id),
            )
            self.conn.execute(
                """update counsel.jobs set status = %s, lease_until = null, last_error = %s,
                          updated_at = %s where id = %s""",
                (job_status, error, now, job["id"]),
            )

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
            job = self.lease(self.clock())
            if job is None:
                break
            summary["runs"].append({"job": job["id"], "agent": job["agent"], "result": self.run(job)})
        log("INFO", "tick", **summary)
        return summary
