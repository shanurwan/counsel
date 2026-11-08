"""Send each finished run to an OpenTelemetry collector, if one is configured.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (the standard OTLP variable; headers
from OTEL_EXPORTER_OTLP_HEADERS, as `key=value,key=value`). The runtime posts one
OTLP/JSON trace per run to <endpoint>/v1/traces: an invoke_agent span with chat and
execute_tool children (cost.trace). It reads only the journal, as the orchestrator,
and never fails a run: a collector that is down costs a warning.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import unquote

import psycopg

from .compiler import ORCHESTRATOR_ROLE
from .log import log

Post = Callable[[str, bytes, dict], int]


def _post(url: str, body: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # The endpoint is the operator's own setting, never caller input.
    with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
        return resp.status


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    for pair in filter(None, os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(",")):
        key, _, value = pair.partition("=")
        if key.strip():
            headers[key.strip()] = unquote(value.strip())
    return headers


def export_run(conn: psycopg.Connection, run_id: str, post: Post = _post) -> bool:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return False
    from .cost import trace

    try:
        with conn.transaction():
            conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            payload = trace(conn, str(run_id), with_cost=False)
        post(f"{endpoint}/v1/traces", json.dumps(payload).encode(), _headers())
        return True
    except (psycopg.Error, urllib.error.URLError, OSError, LookupError, ValueError) as e:
        log("WARNING", "trace export failed; the run is unaffected", run_id=str(run_id), error=str(e)[:200])
        return False
