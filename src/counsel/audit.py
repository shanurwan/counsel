"""Audit every agent credential against the live database.

The question is not "did we write the right grants" but "what can this role
actually do right now". Each check asks Postgres directly, so a grant added by
hand, a role attribute, a membership or a SECURITY DEFINER function shows up
whether or not the registry knows about it.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .compiler import GROUP_ROLE, RUNTIME_ROLE
from .registry import Agent

COMMITMENT_COLUMNS = ("owner_id", "due_date", "status")

# Every role that can act as an agent: the group, the runtime, and anything
# that reaches them through a membership that passes privileges on (INHERIT or
# SET). pg_has_role(..., 'MEMBER') is not used because it also counts
# superusers and Supabase's admin-only creator grants.
SUBJECTS = f"""
  with recursive s(oid) as (
    select oid from pg_roles where rolname in ('{GROUP_ROLE}', '{RUNTIME_ROLE}')
    union
    select am.member from pg_auth_members am
    join s on am.roleid = s.oid
    where am.inherit_option or am.set_option
  )
  select rolname from pg_roles where oid in (select oid from s)
"""

CHECKS: dict[str, str] = {
    "commitment column writable": f"""
        select s.rolname, format('%s %s on work.tasks', p.priv, c.col)
        from ({SUBJECTS}) s
        cross join (values ('owner_id'), ('due_date'), ('status')) c(col)
        cross join (values ('INSERT'), ('UPDATE')) p(priv)
        where has_column_privilege(s.rolname, 'work.tasks', c.col, p.priv)
    """,
    "forbidden table privilege": f"""
        select s.rolname, format('%s on %s', t.priv, t.tbl)
        from ({SUBJECTS}) s
        cross join (values
            ('work.tasks', 'INSERT'), ('work.tasks', 'DELETE'),
            ('work.tasks', 'TRUNCATE'), ('work.tasks', 'TRIGGER'),
            ('work.consents', 'INSERT'), ('work.consents', 'UPDATE'),
            ('work.consents', 'DELETE'), ('work.consents', 'TRUNCATE'),
            ('counsel.audit_log', 'UPDATE'), ('counsel.audit_log', 'DELETE'),
            ('counsel.audit_log', 'TRUNCATE')
        ) t(tbl, priv)
        where has_table_privilege(s.rolname, t.tbl, t.priv)
    """,
    "dangerous role attribute": f"""
        select r.rolname, a.attr
        from pg_roles r
        cross join lateral (values
            ('superuser', r.rolsuper), ('createrole', r.rolcreaterole),
            ('createdb', r.rolcreatedb), ('bypassrls', r.rolbypassrls),
            ('replication', r.rolreplication),
            ('login', r.rolcanlogin and r.rolname <> '{RUNTIME_ROLE}'),
            ('inherit', r.rolinherit and r.rolname = '{RUNTIME_ROLE}')
        ) a(attr, bad)
        where r.rolname in ({SUBJECTS}) and a.bad
    """,
    "unexpected membership": f"""
        select m.rolname, format('member of %s', g.rolname)
        from pg_auth_members am
        join pg_roles m on m.oid = am.member
        join pg_roles g on g.oid = am.roleid
        where m.rolname in ({SUBJECTS})
          and not (g.rolname = '{GROUP_ROLE}' or pg_has_role(g.oid, '{GROUP_ROLE}', 'MEMBER'))
          and (am.inherit_option or am.set_option)
    """,
    "non-agent role can act as an agent": f"""
        select rolname, 'holds an agent membership'
        from ({SUBJECTS}) s
        where rolname not in ('{GROUP_ROLE}', '{RUNTIME_ROLE}')
          and left(rolname, 6) <> 'agent_'
    """,
    "owns objects": f"""
        select r.rolname, format('owns %s %s', o.kind, o.name)
        from pg_roles r
        join (
          select relowner as owner, 'relation' as kind, oid::regclass::text as name from pg_class
          union all select proowner, 'function', oid::regprocedure::text from pg_proc
          union all select nspowner, 'schema', nspname::text from pg_namespace
        ) o on o.owner = r.oid
        where r.rolname in ({SUBJECTS})
    """,
    "can run SECURITY DEFINER function": f"""
        select s.rolname, p.oid::regprocedure::text
        from ({SUBJECTS}) s
        cross join pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
        where p.prosecdef
          -- trigger and event trigger functions cannot be called directly
          and p.prorettype not in ('trigger'::regtype, 'event_trigger'::regtype)
          and n.nspname not in ('pg_catalog', 'information_schema')
          and has_function_privilege(s.rolname, p.oid, 'EXECUTE')
    """,
}


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
        for name, sql in CHECKS.items():
            cur.execute(sql)
            found += [Violation(name, role, detail) for role, detail in cur.fetchall()]

        if agents is not None:
            cur.execute(f"""
                select rolname from ({SUBJECTS}) s
                where rolname not in ('{GROUP_ROLE}', '{RUNTIME_ROLE}')
            """)
            live = {row[0] for row in cur.fetchall()}
            for role in sorted(live - {a.role for a in agents}):
                found.append(Violation("unregistered agent role", role, "not in agents/*.yaml"))
            for role in sorted({a.role for a in agents} - live):
                found.append(Violation("registered agent missing", role, "run `counsel compile` and apply"))
    return found


def matrix(conn: psycopg.Connection, agents: list[Agent]) -> str:
    """The table a reviewer reads first, built from has_*_privilege on the live database."""
    head = ["Agent role", "Tier", "read tasks", "add update note", *COMMITMENT_COLUMNS]
    rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    with conn.cursor() as cur:
        for a in sorted(agents, key=lambda a: a.name):
            cur.execute(
                """
                select has_table_privilege(%(r)s, 'work.tasks', 'SELECT'),
                       has_column_privilege(%(r)s, 'work.task_updates', 'body', 'INSERT'),
                       has_column_privilege(%(r)s, 'work.tasks', 'owner_id', 'UPDATE')
                         or has_column_privilege(%(r)s, 'work.tasks', 'owner_id', 'INSERT'),
                       has_column_privilege(%(r)s, 'work.tasks', 'due_date', 'UPDATE')
                         or has_column_privilege(%(r)s, 'work.tasks', 'due_date', 'INSERT'),
                       has_column_privilege(%(r)s, 'work.tasks', 'status', 'UPDATE')
                         or has_column_privilege(%(r)s, 'work.tasks', 'status', 'INSERT')
                """,
                {"r": a.role},
            )
            read, note, *commit = cur.fetchone()
            cells = ["yes" if read else "no", "with consent" if note else "no"]
            cells += ["**YES**" if c else "no" for c in commit]
            rows.append(f"| `{a.role}` | {a.tier} | " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"
