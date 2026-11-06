"""Tool calls from a model, and the only way they reach the database.

The model never writes SQL. It names an operation and a table; this module
builds the statement with sql.Identifier and bound values and runs it in the
calling step's transaction, which is already SET LOCAL ROLE to the agent.

There is deliberately no allow-list of tables or verbs here. The agent's
Postgres role, compiled from agents/*.yaml, is the whole policy, so whatever
a hijacked model asks for (read the secrets, post to the customer, drop the
table) is answered by Postgres, not by a filter in Python that someone could
loosen. Because statements are built, not written, the model cannot express
SET ROLE, set_config or a second statement: the identifiers are quoted.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql

OPS = ("select", "insert", "update", "delete", "truncate", "drop")
TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}$")
COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MAX_ROWS = 20
MAX_CELL = 500

# Refusals that end the run (recorded, never retried). Anything else, such as
# a table that does not exist, goes back to the model as an error result.
DENIALS = {"42501", "CN001", "CN002", "CN004"}


class InvalidCall(Exception):
    pass


def _table(name: object) -> sql.Identifier:
    if not isinstance(name, str) or not TABLE_RE.match(name):
        raise InvalidCall(f"table must be schema.name, got {name!r}")
    schema, table = name.split(".")
    return sql.Identifier(schema, table)


def _columns(cols: object) -> list[str]:
    if not isinstance(cols, (list, tuple)) or not all(isinstance(c, str) and COLUMN_RE.match(c) for c in cols):
        raise InvalidCall(f"columns must be a list of column names, got {cols!r}")
    return list(cols)


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "..."


def build(call: dict) -> tuple[sql.Composed, list]:
    """Turn {"tool": op, "args": {...}} into a statement and its parameters."""
    op = call.get("tool")
    args = call.get("args") or {}
    if op not in OPS:
        raise InvalidCall(f"unknown tool {op!r}; tools are {', '.join(OPS)}, draft_reply")
    if not isinstance(args, dict):
        raise InvalidCall("args must be an object")
    table = _table(args.get("table"))

    if op == "select":
        cols = args.get("columns")
        target = sql.SQL("*") if not cols else sql.SQL(", ").join(map(sql.Identifier, _columns(cols)))
        return sql.SQL("select {} from {} limit {}").format(target, table, sql.Literal(MAX_ROWS)), []
    if op == "insert":
        row = args.get("row")
        if not isinstance(row, dict) or not row:
            raise InvalidCall("insert needs args.row, an object of column: value")
        cols = _columns(list(row))
        return (sql.SQL("insert into {} ({}) values ({})").format(
            table, sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols))),
            [None if row[c] is None else str(row[c]) for c in cols])
    if op == "update":
        values = args.get("set")
        if not isinstance(values, dict) or not values:
            raise InvalidCall("update needs args.set, an object of column: value")
        cols = _columns(list(values))
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder()) for c in cols)
        params = [None if values[c] is None else str(values[c]) for c in cols]
        stmt = sql.SQL("update {} set {}").format(table, assignments)
        if args.get("id") is not None:
            stmt += sql.SQL(" where id = {}").format(sql.Placeholder())
            params.append(str(args["id"]))
        return stmt, params
    if op == "delete":
        stmt = sql.SQL("delete from {}").format(table)
        if args.get("id") is not None:
            return stmt + sql.SQL(" where id = {}").format(sql.Placeholder()), [str(args["id"])]
        return stmt, []
    if op == "truncate":
        return sql.SQL("truncate {}").format(table), []
    return sql.SQL("drop table {}").format(table), []


def execute(conn: psycopg.Connection, call: dict) -> dict:
    """Run one tool call as whatever role the transaction is in.

    A denial propagates (the runtime ends the run and audits it). Any other
    database error, or a malformed call, comes back as a result the model can
    read, inside a savepoint so the step's transaction stays usable.
    """
    try:
        stmt, params = build(call)
    except InvalidCall as e:
        return {"error": str(e)}
    try:
        with conn.transaction():
            cur = conn.execute(stmt, params)
            if cur.description is None:
                return {"rowcount": cur.rowcount}
            names = [d.name for d in cur.description]
            return {"rows": [{n: _cell(v) for n, v in zip(names, row)} for row in cur.fetchall()]}
    except psycopg.Error as e:
        if e.sqlstate in DENIALS:
            raise
        return {"error": f"{e.sqlstate}: {e.diag.message_primary}"}
