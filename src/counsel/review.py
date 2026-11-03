"""The human gate: a person publishes or rejects each draft.

Agents only insert drafts; they cannot update one. Publishing is done by a
person on their own connection, so `reviewed_by` and, for a support reply,
the author of the customer-visible message are that person, never an agent.
A published draft is what the value ledger counts.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
            at: datetime | None = None) -> str:
    """Accept a draft, optionally edited. A support reply is posted to the customer as you."""
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
        if kind.startswith("support_reply:"):
            conn.execute("insert into support.ticket_messages (ticket_id, body) values (%s, %s)",
                         (kind.split(":", 1)[1], text))
        conn.execute(
            """update counsel.drafts set status = 'published', published_body = %s,
                      reviewed_at = %s, reviewed_by = session_user where id = %s""",
            (text, at, draft_id))
    return kind


def reject(conn: psycopg.Connection, draft_id: str, at: datetime | None = None) -> str:
    at = at or datetime.now(timezone.utc)
    with conn.transaction():
        kind, _, _ = _draft(conn, draft_id)
        conn.execute(
            """update counsel.drafts set status = 'rejected', reviewed_at = %s,
                      reviewed_by = session_user where id = %s""", (at, draft_id))
    return kind
