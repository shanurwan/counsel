"""Every finished run, whole, in BigQuery: one row per run.

Postgres is the system of record and keeps the agents running; the warehouse is
where a run stays reconstructable and joins other company data. A row holds the
run's metadata, its cost, and its whole journal (steps, prompts, responses, audit
entries) as JSON.

Free tier by construction: batch load jobs are free, where streaming inserts are
billed, and partitions expire after 90 days (infra/main.tf). Nothing here runs a
query, so the 1 TiB of query allowance is left for people.

Exactly once: the job id is derived from the batch and an attempt counter. A job
that was accepted but whose answer was lost (a timeout, a killed tick) comes back
as a 409 on retry, and the existing job is read instead of loading twice. The
cursor moves only after BigQuery reports the job done.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404 - only a fixed gcloud command, for local runs
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

import psycopg

from .compiler import ORCHESTRATOR_ROLE

DATASET = "counsel_runs"
TABLE = "runs"
BATCH = 200
METADATA = "http://metadata.google.internal/computeMetadata/v1"
UPLOAD = "https://bigquery.googleapis.com/upload/bigquery/v2"
JOBS = "https://bigquery.googleapis.com/bigquery/v2"
SCHEMA = [
    {"name": "run_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "agent", "type": "STRING", "mode": "REQUIRED"},
    {"name": "status", "type": "STRING", "mode": "REQUIRED"},
    {"name": "attempt", "type": "INT64"},
    {"name": "started_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
    {"name": "finished_at", "type": "TIMESTAMP"},
    {"name": "error", "type": "STRING"},
    {"name": "job_key", "type": "STRING"},
    {"name": "requester", "type": "STRING"},
    {"name": "usd", "type": "FLOAT64"},
    {"name": "input_tokens", "type": "INT64"},
    {"name": "output_tokens", "type": "INT64"},
    {"name": "journal", "type": "JSON"},
]


class WarehouseError(Exception):
    pass


class JobFailed(WarehouseError):
    """BigQuery ran the job and refused the data. Retrying needs a new job id."""


# (method, url, headers, body, timeout) -> (status, parsed json)
Transport = Callable[[str, str, dict, bytes | None, float], tuple[int, dict]]


def _urllib(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        # Fixed URLs in this module: the metadata server and the BigQuery API.
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read()
            return resp.status, json.loads(raw) if raw[:1] in (b"{", b"[") else {"text": raw.decode()}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError) as e:
        raise WarehouseError(f"{method} {url}: {e}") from e


def on_cloud_run() -> bool:
    return "K_SERVICE" in os.environ


@dataclass
class BigQuery:
    project: str
    token: Callable[[], str]
    transport: Transport = _urllib
    sleep: Callable[[float], None] = time.sleep
    wait_s: float = 15.0

    def load(self, job_id: str, rows: list[dict]) -> None:
        """Load the rows and wait for the job. Raises unless BigQuery says it worked."""
        headers = {"Authorization": f"Bearer {self.token()}"}
        config = {
            "jobReference": {"projectId": self.project, "jobId": job_id},
            "configuration": {"load": {
                "destinationTable": {"projectId": self.project, "datasetId": DATASET, "tableId": TABLE},
                "sourceFormat": "NEWLINE_DELIMITED_JSON",
                "writeDisposition": "WRITE_APPEND",
                "createDisposition": "CREATE_IF_NEEDED",
                "schema": {"fields": SCHEMA},
                "timePartitioning": {"type": "DAY", "field": "started_at"},
            }},
        }
        boundary = "counsel" + hashlib.sha256(job_id.encode()).hexdigest()[:24]
        ndjson = "\n".join(json.dumps(r, default=str) for r in rows)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(config)}\r\n"
                f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n{ndjson}\r\n"
                f"--{boundary}--\r\n").encode()
        status, data = self.transport(
            "POST", f"{UPLOAD}/projects/{self.project}/jobs?uploadType=multipart",
            {**headers, "Content-Type": f"multipart/related; boundary={boundary}"}, body, 30.0)
        if status not in (200, 201, 409):
            raise WarehouseError(f"load rejected: {status} {str(data)[:300]}")
        self._wait(job_id, headers)

    def _wait(self, job_id: str, headers: dict) -> None:
        deadline = time.monotonic() + self.wait_s
        while True:
            status, job = self.transport("GET", f"{JOBS}/projects/{self.project}/jobs/{job_id}", headers, None, 10.0)
            if status != 200:
                raise WarehouseError(f"cannot read job {job_id}: {status}")
            state = job.get("status", {})
            if state.get("state") == "DONE":
                if state.get("errorResult"):
                    raise JobFailed(str(state["errorResult"])[:300])
                return
            if time.monotonic() > deadline:
                raise WarehouseError(f"job {job_id} still running; the next tick reads it again")
            self.sleep(1.0)


def _batch(conn: psycopg.Connection, limit: int = BATCH) -> tuple[list[dict], int]:
    with conn.transaction():
        conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
        done_at, done_id, attempt = conn.execute(
            "select finished_at, run_id, attempt from counsel.warehouse_cursor").fetchone()
        runs = conn.execute(
            """select r.id, r.agent, r.status, r.attempt, r.started_at, r.finished_at, r.error,
                      j.idempotency_key, c.requester, c.usd, c.input_tokens, c.output_tokens
               from counsel.runs r
               join counsel.jobs j on j.id = r.job_id
               join counsel.run_costs c on c.run_id = r.id
               where r.finished_at is not null and (r.finished_at, r.id) > (%s, %s)
               order by r.finished_at, r.id limit %s""", (done_at, done_id, limit)).fetchall()
        rows = []
        for r in runs:
            steps = conn.execute(
                "select seq, name, output, finished_at from counsel.steps where run_id = %s order by seq",
                (r[0],)).fetchall()
            calls = conn.execute(
                """select step_seq, provider, model, request, response, error, latency_ms, prompt_tokens,
                          output_tokens, cached_from, created_at
                   from counsel.model_calls where run_id = %s order by id""", (r[0],)).fetchall()
            audit = conn.execute(
                "select at, actor, action, target, decision, detail from counsel.audit_log where run_id = %s order by id",
                (r[0],)).fetchall()
            journal = {
                "steps": [dict(zip(("seq", "name", "output", "at"), s)) for s in steps],
                "model_calls": [dict(zip(("step", "provider", "model", "request", "response", "error", "latency_ms",
                                          "prompt_tokens", "output_tokens", "cached_from", "at"), c)) for c in calls],
                "audit": [dict(zip(("at", "actor", "action", "target", "decision", "detail"), a)) for a in audit],
            }
            rows.append({
                "run_id": str(r[0]), "agent": r[1], "status": r[2], "attempt": r[3],
                "started_at": r[4].isoformat(), "finished_at": r[5].isoformat(), "error": r[6], "job_key": r[7],
                "requester": r[8], "usd": float(r[9]), "input_tokens": int(r[10]), "output_tokens": int(r[11]),
                "journal": json.dumps(journal, default=str),
            })
    return rows, attempt


def export(conn: psycopg.Connection, bq: BigQuery) -> dict:
    """Load one batch of finished runs and move the cursor. Safe to call any number of times."""
    rows, attempt = _batch(conn)
    if not rows:
        return {"exported": 0}
    digest = hashlib.sha256("".join(r["run_id"] for r in rows).encode()).hexdigest()[:32]
    try:
        bq.load(f"counsel_{digest}_{attempt}", rows)
    except JobFailed:
        with conn.transaction():
            conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
            conn.execute("update counsel.warehouse_cursor set attempt = attempt + 1")
        raise
    with conn.transaction():
        conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
        conn.execute("update counsel.warehouse_cursor set finished_at = %s, run_id = %s, attempt = 0",
                     (rows[-1]["finished_at"], rows[-1]["run_id"]))
    return {"exported": len(rows)}


def metadata(path: str, transport: Transport = _urllib) -> str:
    status, data = transport("GET", f"{METADATA}/{path}", {"Metadata-Flavor": "Google"}, None, 5.0)
    if status != 200:
        raise WarehouseError(f"metadata {path}: {status}")
    return data.get("access_token") or data["text"]


def cloud_run_client() -> BigQuery:
    """On Cloud Run, the runtime's own service account, with no key anywhere."""
    return BigQuery(metadata("project/project-id"),
                    lambda: metadata("instance/service-accounts/default/token"))


def gcloud_client(project: str) -> BigQuery:
    """From a person's machine, as that person."""
    token = subprocess.run(["gcloud", "auth", "print-access-token"],  # nosec B603 B607
                           capture_output=True, text=True, check=True, shell=False).stdout.strip()
    return BigQuery(project, lambda: token)
