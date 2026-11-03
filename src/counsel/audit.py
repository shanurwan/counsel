"""Audit every agent credential against the live database.

The question is not "did we write the right grants" but "what can this role
actually do right now". Each check asks Postgres directly, so a grant added by
hand, a role attribute, a membership or a SECURITY DEFINER function shows up
whether or not the registry knows about it.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql

from .compiler import GROUP_ROLE, ORCHESTRATOR_ROLE, RUNTIME_ROLE
from .registry import Agent

COMMITMENT_COLUMNS = ("owner_id", "due_date", "status")

# Every role that can act as an agent: the group, the runtime, and anything
# that reaches them through a membership that passes privileges on (INHERIT or
# SET). pg_has_role(..., 'MEMBER') is not used because it also counts
# superusers and Supabase's admin-only creator grants.
#
# Each check below is a constant query that reads from `subjects`. Role names
# are bound as parameters; nothing is formatted into SQL text.
SUBJECTS = sql.SQL("""
  with recursive s(oid) as (
    select oid from pg_roles where rolname in (%(group)s, %(runtime)s)
    union
    select am.member from pg_auth_members am
    join s on am.roleid = s.oid
    where am.inherit_option or am.set_option
  ),
  subjects as (select rolname from pg_roles where oid in (select oid from s))
""")
# The dashboard login (db/migrations/0006_reader.sql): the cost views, read-only, nothing else.
READER_ROLE = "counsel_reader"
READER_ALLOWED = ("counsel.call_costs", "counsel.run_costs", "counsel.model_prices")

PARAMS = {"group": GROUP_ROLE, "runtime": RUNTIME_ROLE, "orchestrator": ORCHESTRATOR_ROLE,
          "reader": READER_ROLE, "reader_allowed": list(READER_ALLOWED)}

CHECKS: dict[str, str] = {
    "commitment column writable": """
        select s.rolname, format('%%s %%s on work.tasks', p.priv, c.col)
        from subjects s
        cross join (values ('owner_id'), ('due_date'), ('status')) c(col)
        cross join (values ('INSERT'), ('UPDATE')) p(priv)
        where has_column_privilege(s.rolname, 'work.tasks', c.col, p.priv)
    """,
    "forbidden table privilege": """
        select s.rolname, format('%%s on %%s', t.priv, t.tbl)
        from subjects s
        cross join (values
            ('work.tasks', 'INSERT'), ('work.tasks', 'DELETE'),
            ('work.tasks', 'TRUNCATE'), ('work.tasks', 'TRIGGER'),
            ('work.consents', 'INSERT'), ('work.consents', 'UPDATE'),
            ('work.consents', 'DELETE'), ('work.consents', 'TRUNCATE'),
            ('counsel.audit_log', 'UPDATE'), ('counsel.audit_log', 'DELETE'),
            ('counsel.audit_log', 'TRUNCATE'),
            -- the customer-visible thread: only people post there
            ('support.ticket_messages', 'INSERT'), ('support.ticket_messages', 'UPDATE'),
            ('support.ticket_messages', 'DELETE'), ('support.ticket_messages', 'TRUNCATE'),
            ('support.tickets', 'UPDATE'), ('support.tickets', 'DELETE'),
            ('support.tickets', 'TRUNCATE')
        ) t(tbl, priv)
        where has_table_privilege(s.rolname, t.tbl, t.priv)
    """,
    "secret reachable": """
        select s.rolname, format('%%s on support.integration_tokens', p.priv)
        from subjects s
        cross join (values ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')) p(priv)
        where has_any_column_privilege(s.rolname, 'support.integration_tokens', p.priv)
        union all
        select s.rolname, format('%%s on support.integration_tokens', p.priv)
        from subjects s
        cross join (values ('DELETE'), ('TRUNCATE'), ('TRIGGER')) p(priv)
        where has_table_privilege(s.rolname, 'support.integration_tokens', p.priv)
    """,
    "dangerous role attribute": """
        select r.rolname, a.attr
        from pg_roles r
        cross join lateral (values
            ('superuser', r.rolsuper), ('createrole', r.rolcreaterole),
            ('createdb', r.rolcreatedb), ('bypassrls', r.rolbypassrls),
            ('replication', r.rolreplication),
            ('login', r.rolcanlogin and r.rolname <> %(runtime)s),
            ('inherit', r.rolinherit and r.rolname = %(runtime)s)
        ) a(attr, bad)
        where r.rolname in (select rolname from subjects) and a.bad
    """,
    "unexpected membership": """
        select m.rolname, format('member of %%s', g.rolname)
        from pg_auth_members am
        join pg_roles m on m.oid = am.member
        join pg_roles g on g.oid = am.roleid
        where m.rolname in (select rolname from subjects)
          and g.rolname not in (select rolname from subjects)
          and (am.inherit_option or am.set_option)
    """,
    "non-agent role can act as an agent": """
        select rolname, 'holds an agent membership'
        from subjects
        where rolname not in (%(group)s, %(runtime)s, %(orchestrator)s)
          and left(rolname, 6) <> 'agent_'
    """,
    "owns objects": """
        select r.rolname, format('owns %%s %%s', o.kind, o.name)
        from pg_roles r
        join (
          select relowner as owner, 'relation' as kind, oid::regclass::text as name from pg_class
          union all select proowner, 'function', oid::regprocedure::text from pg_proc
          union all select nspowner, 'schema', nspname::text from pg_namespace
        ) o on o.owner = r.oid
        where r.rolname in (select rolname from subjects)
    """,
    "can run SECURITY DEFINER function": """
        select s.rolname, p.oid::regprocedure::text
        from subjects s
        cross join pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
        where p.prosecdef
          -- trigger and event trigger functions cannot be called directly
          and p.prorettype not in ('trigger'::regtype, 'event_trigger'::regtype)
          and n.nspname not in ('pg_catalog', 'information_schema')
          and has_function_privilege(s.rolname, p.oid, 'EXECUTE')
    """,
    "reader reaches beyond the cost views": """
        select %(reader)s, format('%%s on %%s', p.priv, c.oid::regclass)
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        cross join (values ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'),
                           ('REFERENCES'), ('TRIGGER')) p(priv)
        where exists (select from pg_roles where rolname = %(reader)s)
          and c.relkind in ('r', 'v', 'm', 'p', 'f')
          and n.nspname not in ('pg_catalog', 'information_schema')
          and has_schema_privilege(%(reader)s, n.oid, 'USAGE')
          and (has_table_privilege(%(reader)s, c.oid, p.priv)
               or (p.priv in ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES')
                   and has_any_column_privilege(%(reader)s, c.oid, p.priv)))
          and not (p.priv = 'SELECT' and c.oid::regclass::text = any(%(reader_allowed)s))
        union all
        select r.rolname, a.attr
        from pg_roles r
        cross join lateral (values
            ('superuser', r.rolsuper), ('createrole', r.rolcreaterole), ('createdb', r.rolcreatedb),
            ('bypassrls', r.rolbypassrls), ('replication', r.rolreplication), ('inherit', r.rolinherit)
        ) a(attr, bad)
        where r.rolname = %(reader)s and a.bad
        union all
        select m.rolname, format('member of %%s', g.rolname)
        from pg_auth_members am
        join pg_roles m on m.oid = am.member
        join pg_roles g on g.oid = am.roleid
        where m.rolname = %(reader)s
    """,
}

LIVE_AGENTS = """
    select rolname from subjects
    where rolname not in (%(group)s, %(runtime)s, %(orchestrator)s)
"""


def _query(body: str) -> sql.Composed:
    return SUBJECTS + sql.SQL(body)


@dataclass(frozen=True)
class Violation:
    check: str
    role: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.role}: {self.detail}"


def run(conn: psycopg.Connection, agents: list[Agent] | None = None) -> list[Violation]:
    found: list[Violation] = []
    with conn.cursor() as cur:
        for name, body in CHECKS.items():
            cur.execute(_query(body), PARAMS)
            found += [Violation(name, role, detail) for role, detail in cur.fetchall()]

        if agents is not None:
            cur.execute(_query(LIVE_AGENTS), PARAMS)
            live = {row[0] for row in cur.fetchall()}
            for role in sorted(live - {a.role for a in agents}):
                found.append(Violation("unregistered agent role", role, "not in agents/*.yaml"))
            for role in sorted({a.role for a in agents} - live):
                found.append(Violation("registered agent missing", role, "run `counsel compile` and apply"))
    return found


def matrix(conn: psycopg.Connection, agents: list[Agent]) -> str:
    """The table a reviewer reads first, built from has_*_privilege on the live database."""
    head = ["Agent role", "Tier", "read tasks", "add update note", *COMMITMENT_COLUMNS,
            "read secrets", "post to customer"]
    rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    roles = [(a.role, a.tier) for a in sorted(agents, key=lambda a: a.name)]
    roles += [(ORCHESTRATOR_ROLE, "platform"), (RUNTIME_ROLE, "platform")]
    with conn.cursor() as cur:
        for role, tier in roles:
            cur.execute(
                """
                select has_table_privilege(%(r)s, 'work.tasks', 'SELECT'),
                       has_column_privilege(%(r)s, 'work.task_updates', 'body', 'INSERT'),
                       has_column_privilege(%(r)s, 'work.tasks', 'owner_id', 'UPDATE')
                         or has_column_privilege(%(r)s, 'work.tasks', 'owner_id', 'INSERT'),
                       has_column_privilege(%(r)s, 'work.tasks', 'due_date', 'UPDATE')
                         or has_column_privilege(%(r)s, 'work.tasks', 'due_date', 'INSERT'),
                       has_column_privilege(%(r)s, 'work.tasks', 'status', 'UPDATE')
                         or has_column_privilege(%(r)s, 'work.tasks', 'status', 'INSERT'),
                       has_any_column_privilege(%(r)s, 'support.integration_tokens', 'SELECT'),
                       has_table_privilege(%(r)s, 'support.ticket_messages', 'INSERT')
                """,
                {"r": role},
            )
            read, note, *commit = cur.fetchone()
            cells = ["yes" if read else "no", "with consent" if note else "no"]
            cells += ["**YES**" if c else "no" for c in commit]
            rows.append(f"| `{role}` | {tier} | " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"
