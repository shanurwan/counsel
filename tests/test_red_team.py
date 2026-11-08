"""Holes found by red-teaming Counsel's own runtime (docs/red-team.md), each with
the test that reproduces it. R1 and R2 were seen in production."""

import re
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from counsel.behaviors import ask_handbook
from counsel.model import ChatModel, Completion, ModelError
from counsel.runtime import LEASE, LOST_CALL, RunContext

from test_runtime import (HANDBOOK_ONLY, NOW, Clock, FakeModel, admin, ask, make_worker,  # noqa: F401
                          one, runtime_url)

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "How many days of annual leave do I get?"


# --- R1: a worker past its lease kept writing (production, 4 and 5 Nov) ----------------------


# 00:40: no scheduled digest falls in the window, so only the test's own job runs.
QUIET = NOW + timedelta(minutes=35)


def test_a_worker_whose_lease_ran_out_cannot_commit_the_next_step(admin, make_worker):
    clock = Clock(QUIET)

    def slow(ctx, payload):
        ctx.step("first", lambda c: 1)
        clock.now += LEASE + timedelta(seconds=5)   # a model call that took longer than the lease
        ctx.step("second", lambda c: 2)

    admin.execute("insert into counsel.jobs (agent, idempotency_key) values ('team_digest', 'test:slow')")
    summary = make_worker(implementations={"team_digest": slow}, clock=clock).tick(budget_s=5)

    results = {r["agent"]: r["result"] for r in summary["runs"]}
    assert results["team_digest"] == "lease lost"
    assert [s for (s,) in admin.execute("select name from counsel.steps order by seq")] == ["first"]
    # Left as it was, for whoever leases it next: not closed, not retried, not dead.
    assert one(admin, "select status from counsel.jobs where idempotency_key = 'test:slow'") == ("running",)
    assert one(admin, "select count(*) from counsel.audit_log where action = 'team_digest:lease_lost'") == (1,)


def test_a_run_that_keeps_making_progress_keeps_its_lease(admin, make_worker):
    clock = Clock(QUIET)

    def steady(ctx, payload):
        for i in range(4):   # 4 x 100 s: far longer than one lease, never 150 s between steps
            clock.now += timedelta(seconds=100)
            ctx.step(f"step {i}", lambda c: i)

    admin.execute("insert into counsel.jobs (agent, idempotency_key) values ('team_digest', 'test:steady')")
    summary = make_worker(implementations={"team_digest": steady}, clock=clock).tick(budget_s=5)
    assert {r["agent"]: r["result"] for r in summary["runs"]}["team_digest"] == "succeeded"


def test_after_a_takeover_only_the_new_holder_writes_and_closes_the_run(admin, make_worker):
    clock = Clock(QUIET)
    workers = {}

    def taken_over(ctx, payload):
        ctx.step("first's step", lambda c: "a")      # the second worker replays this from the journal
        if ctx.worker_id == workers["first"].worker_id:
            clock.now += LEASE + timedelta(seconds=1)
            workers["second"].tick(budget_s=5)       # the job is leased again and finished
            ctx.step("first's late step", lambda c: "b")
        else:
            ctx.step("second's step", lambda c: "c")

    admin.execute("insert into counsel.jobs (agent, idempotency_key) values ('team_digest', 'test:takeover')")
    workers["first"] = make_worker(implementations={"team_digest": taken_over}, clock=clock)
    workers["second"] = make_worker(implementations={"team_digest": taken_over}, clock=clock)
    first = {r["agent"]: r["result"] for r in workers["first"].tick(budget_s=5)["runs"]}

    assert first["team_digest"] == "lease lost"
    assert [s for (s,) in admin.execute("select name from counsel.steps order by seq")] == \
        ["first's step", "second's step"]
    assert one(admin, "select status from counsel.jobs where idempotency_key = 'test:takeover'") == ("done",)
    assert one(admin, "select status, attempt from counsel.runs") == ("succeeded", 2)


# --- R2: a model call can be paid for twice, and its real duration was hidden ------------------


def test_an_answer_recorded_but_not_journaled_is_reused_not_bought_again(admin, make_worker, monkeypatch):
    model = FakeModel()
    real = RunContext._journal

    def die_once(self, step, output):
        if step == "answer":
            monkeypatch.setattr(RunContext, "_journal", real)
            raise KeyboardInterrupt("killed after the provider answered, before the journal")
        return real(self, step, output)

    monkeypatch.setattr(RunContext, "_journal", die_once)
    ask(admin, QUESTION)
    clock = Clock()
    with pytest.raises(KeyboardInterrupt):
        make_worker(model, HANDBOOK_ONLY, clock).tick()
    clock.now += LEASE + timedelta(seconds=1)
    summary = make_worker(model, HANDBOOK_ONLY, clock).tick()

    assert summary["runs"][0]["result"] == "succeeded"
    assert len(model.calls) == 1
    assert one(admin, "select count(*) from counsel.model_calls") == (1,)
    assert one(admin, "select count(*) from counsel.answers") == (1,)


def test_a_call_lost_mid_flight_is_recorded_as_lost_before_it_is_retried(admin, make_worker):
    class DiesOnce(FakeModel):
        def complete(self, messages, *, max_tokens=800):
            if not self.calls:
                self.calls.append(messages)
                raise KeyboardInterrupt("killed while waiting for the provider")
            return super().complete(messages, max_tokens=max_tokens)

    model = DiesOnce()
    ask(admin, QUESTION)
    clock = Clock()
    with pytest.raises(KeyboardInterrupt):
        make_worker(model, HANDBOOK_ONLY, clock).tick()
    assert one(admin, "select count(*) from counsel.model_call_intents") == (1,)
    assert one(admin, "select count(*) from counsel.model_calls") == (0,)   # before: no trace at all

    clock.now += LEASE + timedelta(seconds=1)
    make_worker(model, HANDBOOK_ONLY, clock).tick()
    assert len(model.calls) == 2
    calls = admin.execute("select error, response is not null from counsel.model_calls order by id").fetchall()
    assert calls == [(LOST_CALL, False), (None, True)]


def test_retries_and_fallbacks_are_recorded_with_the_calls_real_duration(admin, make_worker):
    class SlowWithRetries(FakeModel):
        def complete(self, messages, *, max_tokens=800):
            self.calls.append(messages)
            time.sleep(0.2)
            return Completion(text=self.text, provider="fake", model="fake-2", latency_ms=1,
                              failed_attempts=("fake-1: 599 timed out after 45000 ms",))

    ask(admin, QUESTION)
    make_worker(SlowWithRetries(), HANDBOOK_ONLY).tick()
    latency, failed = one(admin, "select latency_ms, response -> 'failed_attempts' from counsel.model_calls")
    assert latency >= 200, "the journal said 1 ms for a call that took 200"
    assert failed == ["fake-1: 599 timed out after 45000 ms"]


def test_one_call_cannot_outlast_its_budget_whatever_the_retries():
    timeouts = []

    def hanging(url, headers, body, timeout):
        timeouts.append(timeout)
        time.sleep(min(timeout, 0.4))
        return 599, {"error": "timed out"}

    model = ChatModel(api_key="k", models=("a", "b"), transport=hanging, sleep=lambda s: None,
                      min_interval_s=0, budget_s=1.5)
    started = time.monotonic()
    with pytest.raises(ModelError, match="no answer within"):
        model.complete([{"role": "user", "content": "hi"}])
    assert time.monotonic() - started < 2.5
    assert all(t <= 1.5 for t in timeouts)


def test_the_default_budget_ends_inside_the_lease_even_with_a_second_provider():
    assert 2 * ChatModel(api_key="k").budget_s < LEASE.total_seconds()


# --- R3: the runtime login chooses which ticket an agent sees ------------------------------------


def test_the_runtime_login_can_still_choose_any_ticket_known_boundary(admin, runtime_url):
    """Pins today's boundary (docs/red-team.md, R3): RLS keeps the model to one ticket, but the
    runtime login sets the scope itself. When R3's planned fix lands, this test must change."""
    ticket = str(uuid.uuid4())
    admin.execute("insert into support.customers values ('r3', 'R3') on conflict do nothing")
    admin.execute("insert into support.tickets (id, customer_id, subject, body) values (%s, 'r3', 's', 'b')",
                  (ticket,))
    try:
        with psycopg.connect(runtime_url) as conn:
            conn.execute("set role agent_support_drafter")
            assert conn.execute("select count(*) from support.tickets").fetchone() == (0,)
            conn.execute("select set_config('counsel.ticket', %s, false)", (ticket,))
            assert conn.execute("select count(*) from support.tickets").fetchone() == (1,)
    finally:
        admin.execute("delete from counsel.jobs where payload ->> 'ticket_id' = %s", (ticket,))
        admin.execute("delete from support.tickets where id = %s", (ticket,))


# --- R4: deploys and migrations while a run is in flight -----------------------------------------


def test_an_instance_shutting_down_takes_no_new_jobs(admin, make_worker):
    ask(admin, QUESTION)
    worker = make_worker(implementations=HANDBOOK_ONLY)
    worker.stopping = threading.Event()
    worker.stopping.set()
    summary = worker.tick()
    assert summary["runs"] == [] and summary["stopped"] == "shutting down"
    assert one(admin, "select status from counsel.jobs") == ("queued",)


DESTRUCTIVE = re.compile(
    r"^\s*(drop\s+(table|schema|column|view|type|function|materialized)\b"
    r"|truncate\b"
    r"|alter\s+table\b[^;]*\b(drop\s+column|rename\b|alter\s+column\s+\S+\s+(set\s+data\s+)?type)\b)",
    re.IGNORECASE | re.MULTILINE)


def test_migrations_only_add_so_a_run_on_the_old_code_survives_a_deploy():
    """CI migrates before it deploys, so the old revision's runs meet the new schema. That is
    safe only while migrations add. A destructive statement needs `-- destructive: <why>` and a
    two-step plan on the line above it."""
    found = []
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for m in DESTRUCTIVE.finditer(path.read_text(encoding="utf-8")):
            line = path.read_text(encoding="utf-8").count("\n", 0, m.start())
            if not (line and lines[line - 1].strip().lower().startswith("-- destructive:")):
                found.append(f"{path.name}:{line + 1}: {lines[line].strip()}")
    assert found == []


def test_the_destructive_check_catches_what_it_should():
    for statement in ("drop table counsel.steps;", "alter table counsel.runs drop column error;",
                      "alter table work.tasks rename column status to state;",
                      "alter table counsel.jobs alter column payload type text;", "truncate counsel.jobs;"):
        assert DESTRUCTIVE.search(statement), statement
    for statement in ("alter table counsel.audit_log drop constraint audit_log_decision_check;",
                      "create trigger t before truncate on work.tasks", "drop policy p on t;"):
        assert not DESTRUCTIVE.search(statement), statement
