"""The platform is boring: scheduled and event runs, restarts resume, failures
dead-letter, denials are recorded, and every run can be reconstructed.

These run against a real Postgres, logged in as counsel_runtime exactly as
Cloud Run is.
"""

import contextlib
import io
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from counsel import cli
from counsel.behaviors import IMPLEMENTATIONS, NOT_COVERED, ask_handbook
from counsel.model import Completion, ModelError
from counsel.runtime import LEASE, Worker

HANDBOOK_ONLY = {"ask_handbook": ask_handbook}  # keeps scheduled digests out of question tests

# 00:05 UTC is 09:05 in Tokyo (team core) and 21:05 in Sao Paulo (team growth).
# Tomorrow, so it is always after jobs that triggers enqueue at the database's now().
NOW = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)


class FakeModel:
    def __init__(self, text="Everyone gets 18 days of paid annual leave [leave.md].", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def complete(self, messages, *, max_tokens=800):
        self.calls.append(messages)
        if self.fail:
            raise ModelError("nvidia/nemotron: 503 upstream unavailable")
        return Completion(text=self.text, provider="fake", model="fake-1", latency_ms=1)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def admin(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("""truncate counsel.run_rollbacks, counsel.steps, counsel.model_calls,
                                 counsel.model_call_intents, counsel.runs, counsel.jobs,
                                 counsel.questions, counsel.answers, counsel.drafts""")
        yield conn


@pytest.fixture
def runtime_url(database_url):
    return psycopg.conninfo.make_conninfo(database_url, user="counsel_runtime", password="counsel-test-only")


@pytest.fixture
def make_worker(runtime_url, agents):
    conns = []

    def make(model=None, implementations=IMPLEMENTATIONS, clock=None):
        conn = psycopg.connect(runtime_url, autocommit=True)
        conns.append(conn)
        return Worker(conn, {a.name: a for a in agents}, implementations,
                      model or FakeModel(), clock=clock or Clock())

    yield make
    for c in conns:
        c.close()


def ask(admin, text):
    return admin.execute("insert into counsel.questions (body) values (%s) returning id", (text,)).fetchone()[0]


def one(admin, sql, *params):
    return admin.execute(sql, params).fetchone()


# --- scheduling and events ------------------------------------------------------

def test_schedules_enqueue_once_per_fire_time_and_only_implemented_agents(admin, make_worker):
    w = make_worker()
    assert w.enqueue_due(NOW) == 1  # team_digest at 00:00; nudge etc. have no implementation yet
    assert w.enqueue_due(NOW) == 0
    assert one(admin, "select agent, payload->>'scheduled_for' from counsel.jobs") == (
        "team_digest", NOW.replace(minute=0).isoformat())


def test_question_event_is_answered_once_with_citation_and_agent_identity(admin, make_worker):
    model = FakeModel()
    ask(admin, "How many days of annual leave do I get?")
    summary = make_worker(model, HANDBOOK_ONLY).tick()

    assert [(r["agent"], r["result"]) for r in summary["runs"]] == [("ask_handbook", "succeeded")]
    assert len(model.calls) == 1
    assert "[leave.md]" in model.calls[0][1]["content"]
    body, agent = one(admin, "select body, agent from counsel.answers")
    assert "[leave.md]" in body and agent == "agent_ask_handbook"


def test_question_the_handbook_does_not_cover_never_reaches_the_model(admin, make_worker):
    model = FakeModel()
    ask(admin, "Can I bring my dog to the office?")
    make_worker(model, HANDBOOK_ONLY).tick()
    assert model.calls == []
    assert one(admin, "select body from counsel.answers") == (NOT_COVERED,)


def test_team_digest_drafts_only_for_teams_at_nine_local(admin, make_worker):
    make_worker().tick()
    kinds = [r[0] for r in admin.execute("select kind from counsel.drafts")]
    assert kinds == ["team_digest:core"]


# --- state survives restarts ------------------------------------------------------

def test_killed_run_resumes_without_repeating_the_model_call_or_the_write(admin, make_worker):
    model = FakeModel()

    def killed_before_write(ctx, payload):
        real_step = ctx.step

        def step(name, fn, **kw):
            if name == "write answer":
                raise KeyboardInterrupt("simulated kill -9 between model call and write")
            return real_step(name, fn, **kw)

        ctx.step = step
        ask_handbook(ctx, payload)

    ask(admin, "How many days of annual leave do I get?")
    clock = Clock()
    with pytest.raises(KeyboardInterrupt):
        make_worker(model, {"ask_handbook": killed_before_write}, clock).tick()
    assert one(admin, "select status from counsel.jobs") == ("running",)
    assert one(admin, "select count(*) from counsel.answers") == (0,)

    # Too early: the lease still belongs to the dead worker.
    assert make_worker(model, HANDBOOK_ONLY, clock).lease(clock()) is None

    clock.now += LEASE + timedelta(seconds=1)
    summary = make_worker(model, HANDBOOK_ONLY, clock).tick()

    assert summary["runs"][0]["result"] == "succeeded"
    assert len(model.calls) == 1, "the finished model call was replayed from the journal"
    assert one(admin, "select count(*) from counsel.answers") == (1,)
    assert one(admin, "select count(*) from counsel.runs") == (1,)
    assert one(admin, "select attempt from counsel.runs") == (2,)


def test_failures_back_off_then_dead_letter_with_an_audit_record(admin, make_worker):
    model = FakeModel(fail=True)
    ask(admin, "How many days of annual leave do I get?")
    clock = Clock()
    results = []
    for _ in range(3):
        results += [r["result"] for r in make_worker(model, HANDBOOK_ONLY, clock).tick()["runs"]]
        clock.now += timedelta(minutes=5)

    assert results == ["retry", "retry", "dead"]
    assert one(admin, "select status, attempts from counsel.jobs") == ("dead", 3)
    assert one(admin, "select status from counsel.runs") == ("failed",)
    action, decision = one(admin, "select action, decision from counsel.audit_log "
                                  "where action <> 'rollback' order by id desc limit 1")
    assert (action, decision) == ("ask_handbook:dead_letter", "failed")
    errors = [r[0] for r in admin.execute("select error from counsel.model_calls")]
    assert len(errors) == 3 and all("503" in e for e in errors)


def test_a_denied_write_is_recorded_and_never_retried(admin, make_worker):
    def sneaky(ctx, payload):
        ctx.step("mark done", lambda c: c.execute("update work.tasks set status = 'done'"))

    admin.execute("""insert into counsel.jobs (agent, idempotency_key) values ('team_digest', 'test:sneaky')""")
    # 00:40: no hourly schedule falls in the window, so only this job runs.
    summary = make_worker(implementations={"team_digest": sneaky},
                          clock=Clock(NOW + timedelta(minutes=35))).tick()

    assert [r["result"] for r in summary["runs"]] == ["denied"]
    assert one(admin, "select status from counsel.jobs where idempotency_key = 'test:sneaky'") == ("done",)
    assert one(admin, "select status from counsel.runs") == ("denied",)
    action, decision, sqlstate = one(
        admin, "select action, decision, detail->>'sqlstate' from counsel.audit_log "
               "where action <> 'rollback' order by id desc limit 1")
    assert (action, decision, sqlstate) == ("team_digest:denied_write", "denied", "42501")
    assert one(admin, "select count(*) from work.tasks where status = 'done'") == (0,)


def test_a_leased_job_is_not_handed_to_a_second_worker(admin, make_worker):
    ask(admin, "How many days of annual leave do I get?")
    first, second = make_worker(implementations=HANDBOOK_ONLY), make_worker(implementations=HANDBOOK_ONLY)
    assert first.lease(NOW) is not None
    assert second.lease(NOW) is None


# --- every run can be reconstructed -------------------------------------------------

def test_replay_reconstructs_a_run_from_the_journal(admin, make_worker, database_url):
    ask(admin, "How many days of annual leave do I get?")
    make_worker(implementations=HANDBOOK_ONLY).tick()
    run_id = one(admin, "select id from counsel.runs")[0]

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["replay", str(run_id), "--dsn", database_url]) == 0
    text = out.getvalue()
    for expected in ["agent ask_handbook", "status succeeded", "[1] load question",
                     "[2] search handbook", "[3] answer", "model fake-1", "> user:",
                     "< Everyone gets 18 days", "[4] write answer"]:
        assert expected in text, expected
