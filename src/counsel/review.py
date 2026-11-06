"""The human gate: a person publishes or rejects each draft.

Agents only insert drafts; they cannot update one. Publishing is done by a
person on their own connection, so `reviewed_by` and, for a support reply,
the author of the customer-visible message are that person, never an agent.
A published draft is what the value ledger counts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable

import psycopg
from psycopg.rows import dict_row

from .behaviors import ACTION_FLAG, SUPPORT_NOT_COVERED


class ReviewError(Exception):
    pass


def pending(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """select d.id::text, r.agent, d.kind, d.created_at, d.body
               from counsel.drafts d join counsel.runs r on r.id = d.run_id
               where d.status = 'pending' order by d.created_at limit %s""", (limit,)).fetchall()


def _draft(conn: psycopg.Connection, draft_id: str) -> tuple[str, str, str]:
    row = conn.execute("select kind, body, status from counsel.drafts where id = %s for update",
                       (draft_id,)).fetchone()
    if row is None:
        raise ReviewError(f"no draft {draft_id}")
    if row[2] != "pending":
        raise ReviewError(f"draft {draft_id} is already {row[2]}")
    return row


def publish(conn: psycopg.Connection, draft_id: str, body: str | None = None,
            at: datetime | None = None, deliver: Callable[[str, str], None] | None = None) -> str:
    """Accept a draft, optionally edited. A support reply is posted to the customer as you.

    `deliver(kind, text)` sends it to a system outside this database, as you. It runs
    last in the transaction: if it fails the draft stays pending, so you can retry.
    """
    at = at or datetime.now(timezone.utc)
    with conn.transaction():
        kind, draft_body, _ = _draft(conn, draft_id)
        # Unedited, the draft goes out byte for byte, so "edited" in the ledger means a person changed it.
        text = body.strip() if body is not None else draft_body
        if ACTION_FLAG in text:
            raise ReviewError("the draft still carries the reviewer flag; edit the offer and publish "
                              "with --body-file")
        if text.strip() == SUPPORT_NOT_COVERED:
            raise ReviewError("there is no reply to publish: the docs did not cover this ticket")
        if kind.startswith("carry:"):
            _consent_and_carry(conn, kind, text)
        if kind.startswith("support_reply:"):
            conn.execute("insert into support.ticket_messages (ticket_id, body) values (%s, %s)",
                         (kind.split(":", 1)[1], text))
        conn.execute(
            """update counsel.drafts set status = 'published', published_body = %s,
                      reviewed_at = %s, reviewed_by = session_user where id = %s""",
            (text, at, draft_id))
        if deliver:
            deliver(kind, text)
    return kind


def _consent_and_carry(conn: psycopg.Connection, kind: str, text: str) -> None:
    """Approving a proposed note is the person's consent to that exact text, for that task.

    The consent hashes the text; the carry job holds the text. Postgres checks
    one against the other when the agent inserts the note. Here the operator's
    CLI stands in for the person; in production this is their own approval,
    bound to their identity.
    """
    _, task_id, standup_id = kind.split(":")
    person_id = conn.execute("select person_id from work.standups where id = %s", (standup_id,)).fetchone()[0]
    consent_id = conn.execute(
        "insert into work.consents (person_id, task_id, text_sha256) values (%s, %s, %s) returning id::text",
        (person_id, task_id, hashlib.sha256(text.encode("utf-8")).hexdigest())).fetchone()[0]
    conn.execute(
        "insert into counsel.jobs (agent, idempotency_key, payload) values ('standup_carrier', %s, %s)",
        (f"carry:{consent_id}", json.dumps({"consent_id": consent_id, "task_id": task_id, "text": text})))


def reject(conn: psycopg.Connection, draft_id: str, at: datetime | None = None) -> str:
    at = at or datetime.now(timezone.utc)
    with conn.transaction():
        kind, _, _ = _draft(conn, draft_id)
        conn.execute(
            """update counsel.drafts set status = 'rejected', reviewed_at = %s,
                      reviewed_by = session_user where id = %s""", (at, draft_id))
    return kind
