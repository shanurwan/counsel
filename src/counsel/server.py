"""HTTP entry point for Cloud Run.

POST /tick runs one scheduler tick. Cloud Run only admits callers with
run.invoker on the service (the scheduler's service account), so there is no
application-level auth here. One instance, one request at a time.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg

from .cli import build_worker
from .log import log


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
                summary = build_worker(conn).tick()
            self._send(200, summary)
        except Exception as e:  # noqa: BLE001 - report, let the scheduler retry
            log("ERROR", "tick failed", error=f"{type(e).__name__}: {e}")
            self._send(500, {"error": type(e).__name__})

    def log_message(self, fmt, *args):  # request logs come from Cloud Run itself
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    log("INFO", "listening", port=port)
    # Cloud Run routes to the container's port; binding all interfaces is required.
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()  # nosec B104


if __name__ == "__main__":
    main()
