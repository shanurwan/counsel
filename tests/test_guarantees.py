"""docs/guarantees.md, tested with a real kill: a worker process is SIGKILLed inside a
step's transaction, and another worker resumes the run after the lease expires."""

import os
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from counsel.runtime import LEASE, Worker

from test_runtime import NOW, Clock, FakeModel, admin, ask, one, runtime_url  # noqa: F401

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL needs a POSIX system")
def test_a_worker_killed_mid_step_loses_that_step_and_the_next_worker_finishes_once(admin, runtime_url, agents):
    ask(admin, "kill test")
    doomed = subprocess.Popen([sys.executable, str(HERE / "kill_worker.py"), runtime_url, NOW.isoformat()],
                              stdout=subprocess.PIPE, text=True, cwd=HERE)
    try:
        assert doomed.stdout.readline().strip() == "inside the second step"
        os.kill(doomed.pid, signal.SIGKILL)      # no cleanup, no except, no finally
    finally:
        doomed.wait(timeout=30)
    assert doomed.returncode == -signal.SIGKILL

    # The killed step's write and its journal entry went together: neither is there.
    assert [r[0] for r in admin.execute("select body from counsel.answers where question = 'kill' order by body")] \
        == ["first"]
    assert [r[0] for r in admin.execute("select name from counsel.steps")] == ["first"]
    assert one(admin, "select status, leased_by from counsel.jobs") == ("running", "doomed")

    ran = []

    def resumed(ctx, payload):
        ctx.step("first", lambda c: ran.append("first") or 1)
        ctx.step("second", lambda c: ran.append("second") or c.execute(
            "insert into counsel.answers (question, body) values ('kill', 'second') returning 1").fetchone()[0])

    # Too early: the dead worker's lease still stands.
    with psycopg.connect(runtime_url, autocommit=True) as conn:
        early = Worker(conn, {a.name: a for a in agents if a.pool == "core"}, {"ask_handbook": resumed},
                       FakeModel(), clock=Clock(NOW + timedelta(seconds=10))).tick(budget_s=5)
        assert early["runs"] == []
        late = Worker(conn, {a.name: a for a in agents if a.pool == "core"}, {"ask_handbook": resumed},
                      FakeModel(), clock=Clock(NOW + LEASE + timedelta(seconds=1))).tick(budget_s=5)

    assert [r["result"] for r in late["runs"]] == ["succeeded"]
    assert ran == ["second"], "the journaled step is replayed, not run again"
    assert [r[0] for r in admin.execute("select body from counsel.answers where question = 'kill' order by body")] \
        == ["first", "second"]
    assert one(admin, "select status, attempt from counsel.runs") == ("succeeded", 2)
