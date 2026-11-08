"""HTTP entry point for Cloud Run.

POST /tick runs one scheduler tick. Cloud Run only admits callers with
run.invoker on the service (the scheduler's service account), so there is no
application-level auth here. One instance, one request at a time.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg

from . import github, githubapp, warehouse
from .cli import build_worker
from .log import log
from .registry import CORE_POOL
from .runtime import STOPPING

WAREHOUSE_CUTOFF_S = 85.0  # leave the export room inside Cloud Run's 120 s request timeout


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/tick":
            self._send(404, {"error": "not found"})
            return
        try:
            # A fresh connection per tick: nothing to go stale between ticks.
            with psycopg.connect(os.environ["COUNSEL_DATABASE_URL"], autocommit=True,
                                 connect_timeout=10) as conn:
                started = time.monotonic()
                worker = build_worker(conn)
                # GitHub sync and the warehouse export belong to the core pool's service;
                # another pool's service only runs its own agents.
                core = os.environ.get("COUNSEL_RUNTIME_POOL", CORE_POOL) == CORE_POOL
                now = datetime.now(timezone.utc)
                app = githubapp.App.from_env() if core else None
                installs = githubapp.tick(conn, app, now) if core else None
                readers = githubapp.readers(conn, app) if app else {}
                synced = github.sync_all(conn, worker.agents, now, installed=readers) if core else None
                summary = {**worker.tick(), "synced": synced, "installs": installs}
                if app:
                    # After the agents ran, so this tick's drafts go out on this tick.
                    summary["delivered"] = len(githubapp.deliver(conn, app, datetime.now(timezone.utc)))
                if core and warehouse.on_cloud_run() and time.monotonic() - started < WAREHOUSE_CUTOFF_S:
                    summary["warehouse"] = _export(conn)
            self._send(200, summary)
        except Exception as e:  # noqa: BLE001 - report, let the scheduler retry
            log("ERROR", "tick failed", error=f"{type(e).__name__}: {e}")
            self._send(500, {"error": type(e).__name__})

    def log_message(self, fmt, *args):  # request logs come from Cloud Run itself
        pass


def _export(conn) -> dict:
    """The warehouse is a copy: when it is down the agents carry on, and the cursor waits."""
    try:
        return warehouse.export(conn, warehouse.cloud_run_client())
    except warehouse.WarehouseError as e:
        log("WARNING", "warehouse export failed; the next tick retries", error=str(e))
        return {"error": str(e)[:200]}


def _stop(signum, _frame) -> None:
    # Cloud Run sends SIGTERM before replacing or removing an instance, then waits
    # 10 s. Leasing stops; a run already going finishes or resumes from its journal.
    STOPPING.set()
    log("WARNING", "SIGTERM: no new jobs from this instance", signal=signum)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    signal.signal(signal.SIGTERM, _stop)
    log("INFO", "listening", port=port)
    # Cloud Run routes to the container's port; binding all interfaces is required.
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()  # nosec B104


if __name__ == "__main__":
    main()
