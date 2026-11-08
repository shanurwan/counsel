"""A worker for test_guarantees: journals one step, then waits inside the second step's
transaction until the test kills the process with SIGKILL.

Usage: python kill_worker.py <runtime-url> <iso-now>
"""

import sys
import time
from datetime import datetime

import psycopg

from counsel import registry
from counsel.runtime import Worker

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def doomed(ctx, payload):
    ctx.step("first", lambda c: c.execute("insert into counsel.answers (question, body) values ('kill', 'first') "
                                          "returning 1").fetchone()[0])

    def hang(c):
        c.execute("insert into counsel.answers (question, body) values ('kill', 'second, never committed')")
        print("inside the second step", flush=True)
        time.sleep(120)

    ctx.step("second", hang)


url, now = sys.argv[1], datetime.fromisoformat(sys.argv[2])
agents = {a.name: a for a in registry.load(ROOT / "agents") if a.pool == registry.CORE_POOL}
with psycopg.connect(url, autocommit=True) as conn:
    Worker(conn, agents, {"ask_handbook": doomed}, None, worker_id="doomed", clock=lambda: now).tick(budget_s=5)
