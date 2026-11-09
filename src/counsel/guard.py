"""counsel guard: what can the AI agent on your Postgres or Supabase database write?

Counsel's own agents are database roles that can never write a commitment. This is the
same idea for anyone's database, in two commands:

    counsel guard check --role my_agent [--config guard.yaml]
    counsel guard compile guard.yaml > agent.sql

`check` asks the live database what the role can actually do: every table it can read
or write, roles it inherits or can become (`service_role`, `authenticated`), attributes
that switch row-level security off for it, SECURITY DEFINER functions it can call,
objects it owns. It connects as an admin inside a read-only transaction, never as the
agent, and changes nothing. With a config it also fails on anything the config does not
allow, so it can gate CI.

`compile` writes the SQL that makes the role match the config: a role with no
attributes and no memberships, grants down to the column, a row-level policy for each
table it touches, and a trigger that refuses changes to the columns it must never
change, even if someone grants too much later. It never turns row-level security on or
off for a table: that would change what your app's own users see.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg
import yaml

NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}$")
WRITE_KINDS = ("insert", "update", "delete")
PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
# Roles that, if the agent inherits them or can become them, hand it far more than it needs.
SUPABASE_POWER = {"postgres", "service_role", "supabase_admin", "supabase_auth_admin", "supabase_storage_admin",
                  "dashboard_user", "pg_write_all_data", "pg_read_all_data", "pg_database_owner"}
SUPABASE_APP = {"authenticated", "anon", "authenticator"}
# Where Supabase keeps users, sessions and secrets.
SENSITIVE_SCHEMAS = {"auth", "vault", "pgsodium", "supabase_functions", "net", "cron"}
SENSITIVE_COLUMN = re.compile(r"(^|_)(pass|passwd|password|secrets?|tokens?|api_?key|apikey|private_?key|encrypted|"
                              r"credentials?|otp|hash|hashed|salt)(_|$)", re.IGNORECASE)
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


class GuardError(Exception):
    pass


def _q(name: str) -> str:
    """Quote an identifier already matched against NAME."""
    return '"' + name.replace('"', '""') + '"'


def _qt(table: str) -> str:
    schema, rel = table.split(".")
    return f"{_q(schema)}.{_q(rel)}"


@dataclass
class Config:
    role: str
    login: bool = False
    reads: dict[str, list[str] | None] = field(default_factory=dict)  # None: every column
    writes: dict[str, dict[str, list[str] | bool | None]] = field(default_factory=dict)
    rows: dict[str, str] = field(default_factory=dict)
    never_write: dict[str, list[str]] = field(default_factory=dict)

    @property
    def tables(self) -> list[str]:
        return sorted({*self.reads, *self.writes, *self.never_write})


def _columns(value, where: str) -> list[str] | None:
    if value in (None, "all", "*"):
        return None
    if not isinstance(value, list) or not value:
        raise GuardError(f"{where}: give a list of columns, or all")
    for col in value:
        if not isinstance(col, str) or not NAME.match(col):
            raise GuardError(f"{where}: {col!r} is not a lower-case column name")
    return list(dict.fromkeys(value))


def _table(name, where: str) -> str:
    if not isinstance(name, str) or not TABLE.match(name):
        raise GuardError(f"{where}: {name!r} must be schema.table in lower case, like public.tickets")
    return name


def load(source: str | Path | dict) -> Config:
    raw = source if isinstance(source, dict) else yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise GuardError("the config must be a mapping")
    unknown = set(raw) - {"role", "login", "reads", "writes", "rows", "never_write"}
    if unknown:
        raise GuardError(f"unknown keys: {', '.join(sorted(unknown))}")
    role = raw.get("role")
    if not isinstance(role, str) or not NAME.match(role):
        raise GuardError("role: a lower-case role name, like support_agent")
    if role in SUPABASE_POWER | SUPABASE_APP or role.startswith("pg_"):
        raise GuardError(f"role: {role} is a built-in role; give the agent a role of its own")
    cfg = Config(role=role, login=bool(raw.get("login", False)))
    for t, cols in (raw.get("reads") or {}).items():
        cfg.reads[_table(t, "reads")] = _columns(cols, f"reads.{t}")
    for t, kinds in (raw.get("writes") or {}).items():
        _table(t, "writes")
        if not isinstance(kinds, dict) or not kinds or set(kinds) - set(WRITE_KINDS):
            raise GuardError(f"writes.{t}: a mapping of insert, update and/or delete")
        entry: dict = {}
        for kind, cols in kinds.items():
            if kind == "delete":
                if cols is not True:
                    raise GuardError(f"writes.{t}.delete: true, or leave it out")
                entry[kind] = True
            else:
                entry[kind] = _columns(cols, f"writes.{t}.{kind}")
        cfg.writes[t] = entry
    for t, pred in (raw.get("rows") or {}).items():
        _table(t, "rows")
        if not isinstance(pred, str) or not pred.strip() or ";" in pred or "$" in pred:
            raise GuardError(f"rows.{t}: one SQL condition, like \"team_id = 'support'\"")
        if t not in cfg.reads and t not in cfg.writes:
            raise GuardError(f"rows.{t}: the agent neither reads nor writes this table")
        cfg.rows[t] = pred.strip()
    for t, cols in (raw.get("never_write") or {}).items():
        cols = _columns(cols, f"never_write.{_table(t, 'never_write')}")
        if cols is None:
            raise GuardError(f"never_write.{t}: name the columns")
        cfg.never_write[t] = cols
        for kind in ("insert", "update"):
            allowed = cfg.writes.get(t, {}).get(kind, False)
            clash = cols if allowed is None else [c for c in cols if c in (allowed or [])]
            if clash:
                raise GuardError(f"writes.{t}.{kind} allows {', '.join(clash)}, which never_write forbids")
    if not (cfg.reads or cfg.writes):
        raise GuardError("the agent reads and writes nothing: add reads or writes")
    return cfg


# ---------------------------------------------------------------------------- compile

TRIGGER_FN = """
-- Shared by every guarded role: refuses an UPDATE that changes a protected column, and
-- any DELETE, when the statement runs as the guarded role. Columns come as arguments.
create schema if not exists counsel_guard;
revoke all on schema counsel_guard from public;
create or replace function counsel_guard.refuse_columns() returns trigger
language plpgsql set search_path = '' as $$
declare
  agent text := tg_argv[0];
  col text;
begin
  if current_user <> agent then
    return coalesce(new, old);
  end if;
  if tg_op = 'DELETE' then
    raise exception 'counsel guard: % may not delete from %.%', agent, tg_table_schema, tg_table_name
      using errcode = '42501';
  end if;
  foreach col in array tg_argv[1:] loop
    if (to_jsonb(new) -> col) is distinct from (to_jsonb(old) -> col) then
      raise exception 'counsel guard: % may not change %.%.%', agent, tg_table_schema, tg_table_name, col
        using errcode = '42501';
    end if;
  end loop;
  return new;
end $$;
revoke all on function counsel_guard.refuse_columns() from public;
"""


def compile_sql(cfg: Config, source: str = "guard.yaml") -> str:
    r = _q(cfg.role)
    schemas = sorted({t.split(".")[0] for t in cfg.tables} | {"public"})
    out = [
        f"-- GENERATED by `counsel guard compile {source}`. Review it, then apply it as an admin.",
        "-- Run `counsel guard check` after, and in CI, to prove the live database matches.",
        "begin;",
        "",
        f"-- {cfg.role}: no attributes, no memberships, nothing but the grants below.",
        "do $$",
        "begin",
        f"  if not exists (select from pg_roles where rolname = '{cfg.role}') then",
        f"    create role {r} {'login' if cfg.login else 'nologin'};",
        "  end if;",
        "end $$;",
        f"alter role {r} {'login' if cfg.login else 'nologin'};",
        # Only when one is set: on Supabase, postgres is not a superuser and may not touch these.
        "do $$",
        "begin",
        f"  if exists (select from pg_roles where rolname = '{cfg.role}'",
        "             and (rolsuper or rolbypassrls or rolreplication or rolcreaterole or rolcreatedb)) then",
        f"    alter role {r} nosuperuser nobypassrls noreplication nocreaterole nocreatedb;",
        "  end if;",
        "end $$;",
        "do $$",
        "declare g text;",
        "begin",
        "  for g in select b.rolname from pg_auth_members m join pg_roles a on a.oid = m.member",
        f"           join pg_roles b on b.oid = m.roleid where a.rolname = '{cfg.role}' loop",
        f"    execute format('revoke %I from {r}', g);",
        "  end loop;",
        "end $$;",
    ]
    if cfg.login:
        out.append(f"-- Set its password yourself: alter role {r} password '...';")
    out += ["", "-- Take back everything in the schemas it touches, then grant exactly what the config says."]
    for s in schemas:
        out += [f"revoke all on all tables in schema {_q(s)} from {r};",
                f"revoke all on all sequences in schema {_q(s)} from {r};",
                f"revoke all on all functions in schema {_q(s)} from {r};",
                f"revoke create on schema {_q(s)} from {r};"]
    for s in sorted({t.split(".")[0] for t in cfg.reads} | {t.split(".")[0] for t in cfg.writes}):
        out.append(f"grant usage on schema {_q(s)} to {r};")

    def cols(c):
        return "" if c is None else " (" + ", ".join(_q(x) for x in c) + ")"

    out.append("")
    for t in sorted(cfg.reads):
        out.append(f"grant select{cols(cfg.reads[t])} on {_qt(t)} to {r};")
    for t in sorted(cfg.writes):
        for kind in WRITE_KINDS:
            if kind in cfg.writes[t]:
                c = None if kind == "delete" else cfg.writes[t][kind]
                out.append(f"grant {kind}{cols(c)} on {_qt(t)} to {r};")
        if "insert" in cfg.writes[t]:
            # A serial id's default calls nextval as the inserting role.
            out += ["do $$",
                    "declare s text;",
                    "begin",
                    "  for s in select d.objid::regclass::text from pg_depend d join pg_class c on c.oid = d.objid",
                    f"           where c.relkind = 'S' and d.refobjid = '{_qt(t)}'::regclass and d.deptype = 'a' loop",
                    f"    execute format('grant usage on sequence %s to {r}', s);",
                    "  end loop;",
                    "end $$;"]

    out += ["", "-- Which rows it sees and writes. These policies apply where row-level security is on;",
            "-- `counsel guard check` warns on a table where it is off. Default: every row. Views",
            "-- cannot hold policies, so a view is skipped."]
    for t in [t for t in cfg.tables if t in cfg.reads or t in cfg.writes]:
        pred = cfg.rows.get(t, "true")
        kinds = (["select"] if t in cfg.reads else []) + [k for k in WRITE_KINDS if k in cfg.writes.get(t, {})]
        out += ["do $guard$",
                "begin",
                f"  if (select relkind from pg_class where oid = '{_qt(t)}'::regclass) in ('r', 'p') then"]
        for kind in kinds:
            name = _q(f"guard_{cfg.role}_{kind}"[:63])
            clause = {"select": f"using ({pred})", "insert": f"with check ({pred})",
                      "update": f"using ({pred}) with check ({pred})", "delete": f"using ({pred})"}[kind]
            out += [f"    drop policy if exists {name} on {_qt(t)};",
                    f"    create policy {name} on {_qt(t)} for {kind} to {r} {clause};"]
        out += ["  end if;", "end $guard$;"]

    if cfg.never_write:
        out.append(TRIGGER_FN)
        for t, never in sorted(cfg.never_write.items()):
            trig = _q(f"guard_{cfg.role}"[:63])
            args = ", ".join(f"'{x}'" for x in [cfg.role, *never])
            may_delete = bool(cfg.writes.get(t, {}).get("delete"))
            out += [f"-- {t}: {cfg.role} may never change {', '.join(never)}"
                    + ("" if may_delete else ", nor delete a row") + ".",
                    f"drop trigger if exists {trig} on {_qt(t)};",
                    f"create trigger {trig} before update{'' if may_delete else ' or delete'} on {_qt(t)}",
                    f"  for each row execute function counsel_guard.refuse_columns({args});"]
    out += ["", "commit;", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------- check

@dataclass
class Finding:
    level: str  # fail, warn or info
    what: str
    detail: str


@dataclass
class Report:
    role: str
    exists: bool
    findings: list[Finding] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(f.level == "fail" for f in self.findings)

    def add(self, level: str, what: str, detail: str) -> None:
        self.findings.append(Finding(level, what, detail))


# Every role the agent inherits from or can SET ROLE to, through any chain of memberships.
# Postgres 16 records inherit and set per membership; 15 (still common on Supabase) per role.
REACHABLE_16 = """
with recursive up(oid, inherits) as (
  select m.roleid, m.inherit_option from pg_auth_members m
  join pg_roles r on r.oid = m.member where r.rolname = %(role)s and (m.inherit_option or m.set_option)
  union
  select m.roleid, up.inherits and m.inherit_option from pg_auth_members m
  join up on m.member = up.oid where m.inherit_option or m.set_option
)
select r.rolname, bool_or(up.inherits), r.rolsuper or r.rolbypassrls
from up join pg_roles r on r.oid = up.oid group by r.rolname, r.rolsuper, r.rolbypassrls order by 1
"""
REACHABLE_15 = """
with recursive up(oid, inherits) as (
  select m.roleid, r.rolinherit from pg_auth_members m
  join pg_roles r on r.oid = m.member where r.rolname = %(role)s
  union
  select m.roleid, up.inherits and r.rolinherit from pg_auth_members m
  join up on m.member = up.oid join pg_roles r on r.oid = m.member
)
select r.rolname, bool_or(up.inherits), r.rolsuper or r.rolbypassrls
from up join pg_roles r on r.oid = up.oid group by r.rolname, r.rolsuper, r.rolbypassrls order by 1
"""

TABLES = """
select n.nspname, c.relname, c.relkind::text, c.relrowsecurity, c.relforcerowsecurity,
       array(select p.priv from unnest(%(privs)s::text[]) p(priv)
             where has_table_privilege(%(role)s, c.oid, p.priv)) as table_privs,
       array(select format('%%s %%s', p.priv, a.attname) from pg_attribute a
             cross join unnest(array['SELECT', 'INSERT', 'UPDATE']) p(priv)
             where a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
               and not has_table_privilege(%(role)s, c.oid, p.priv)
               and has_column_privilege(%(role)s, c.oid, a.attnum, p.priv)) as column_privs,
       array(select a.attname::text from pg_attribute a where a.attrelid = c.oid and a.attnum > 0
             and not a.attisdropped and a.attname ~* %(sensitive)s
             and has_column_privilege(%(role)s, c.oid, a.attnum, 'SELECT')) as sensitive_read,
       exists (select from pg_policy p where p.polrelid = c.oid
               and (0 = any(p.polroles) or p.polroles && array(select oid from pg_roles
                    where pg_has_role(%(role)s, oid, 'USAGE') and not rolsuper)::oid[])) as has_policy,
       pg_get_userbyid(c.relowner) = %(role)s as owns,
       -- The arguments of each guard trigger: the role, then the columns it protects.
       array(select encode(t.tgargs, 'escape') from pg_trigger t join pg_proc f on f.oid = t.tgfoid
             join pg_namespace fn on fn.oid = f.pronamespace
             where t.tgrelid = c.oid and t.tgenabled <> 'D'
               and fn.nspname = 'counsel_guard' and f.proname = 'refuse_columns') as guard_triggers,
       coalesce(c.reloptions::text[] && array['security_invoker=true', 'security_invoker=on',
                                             'security_invoker=1'], false) as invoker,
       exists (select from pg_depend d where d.classid = 'pg_class'::regclass and d.objid = c.oid
               and d.deptype = 'e') as from_extension
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p', 'v', 'm', 'f')
  and n.nspname <> all(%(system)s) and n.nspname not like 'pg\\_temp%%'
  and has_schema_privilege(%(role)s, n.oid, 'USAGE')
order by 1, 2
"""

DEFINER = """
select format('%%s.%%s(%%s)', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where p.prosecdef and n.nspname <> all(%(system)s)
  and p.prorettype not in ('trigger'::regtype, 'event_trigger'::regtype)
  and has_schema_privilege(%(role)s, n.oid, 'USAGE')
  and has_function_privilege(%(role)s, p.oid, 'EXECUTE')
  and not exists (select from pg_depend d where d.classid = 'pg_proc'::regclass and d.objid = p.oid
                  and d.deptype = 'e')
order by 1
"""

CREATE_ON = """
select nspname from pg_namespace where nspname <> all(%(system)s) and nspname not like 'pg\\_temp%%'
  and has_schema_privilege(%(role)s, oid, 'CREATE') order by 1
"""


def check(conn: psycopg.Connection, role: str, cfg: Config | None = None) -> Report:
    if not NAME.match(role):
        raise GuardError(f"{role!r} is not a role name")
    if cfg is not None and cfg.role != role:
        raise GuardError(f"the config is for {cfg.role}, not {role}")
    params = {"role": role, "privs": list(PRIVS), "system": list(SYSTEM_SCHEMAS),
              "sensitive": SENSITIVE_COLUMN.pattern}
    with conn.transaction():
        conn.execute("set transaction read only")
        attrs = conn.execute("""select rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin
                                from pg_roles where rolname = %s""", (role,)).fetchone()
        rep = Report(role=role, exists=attrs is not None)
        if attrs is None:
            rep.add("fail", "no such role", f"{role} does not exist: compile a config and apply it")
            return rep
        sup, bypass, createrole, createdb, repl, login = attrs
        if sup:
            rep.add("fail", "superuser", "it can do anything, and no grant, policy or trigger applies to it")
        if bypass:
            rep.add("fail", "bypasses row-level security", "BYPASSRLS: every policy is ignored for it")
        for on, name in ((createrole, "CREATEROLE"), (createdb, "CREATEDB"), (repl, "REPLICATION")):
            if on:
                rep.add("fail", "role attribute", f"{name}: more than an agent needs")
        reachable = REACHABLE_16 if conn.info.server_version >= 160000 else REACHABLE_15
        # Policies say nothing about a role that skips them, itself or through what it inherits.
        bypasses = bool(sup or bypass)
        for name, inherits, power in conn.execute(reachable, params).fetchall():
            bypasses |= bool(power and inherits)
            how = "inherits every privilege of" if inherits else "can SET ROLE to"
            if power or name in SUPABASE_POWER:
                rep.add("fail", "can become a powerful role",
                        f"{how} {name}" + (", which bypasses row-level security" if power else ""))
            elif name in SUPABASE_APP:
                rep.add("warn", "shares your app's role",
                        f"{how} {name}: every grant and policy for your app's users applies to the agent too")
            else:
                rep.add("info", "membership", f"{how} {name}")
        if not login:
            rep.add("info", "no login", "it cannot sign in itself; something must SET ROLE to it")

        for (schema, rel, kind, rls, forced, tprivs, cprivs, sensitive, has_policy, owns,
             trigger_args, invoker, from_extension) in conn.execute(TABLES, params).fetchall():
            if not (tprivs or cprivs or owns):
                continue
            t = f"{schema}.{rel}"
            # encode(tgargs, 'escape') separates arguments with a literal \000.
            guards = [a.split("\\000")[:-1] for a in trigger_args]
            guarded = sorted({c for g in guards if g and g[0] == role for c in g[1:]})
            writes = [p for p in tprivs if p != "SELECT"] + sorted({c.split()[0] for c in cprivs} - {"SELECT"})
            reads = "SELECT" in tprivs or any(c.startswith("SELECT ") for c in cprivs)
            rep.tables.append({"table": t, "kind": {"r": "table", "p": "table", "v": "view", "m": "view",
                                                    "f": "foreign"}.get(kind, kind),
                               "privileges": tprivs, "columns": cprivs, "rls": rls, "has_policy": has_policy,
                               "owns": owns})
            if owns:
                rep.add("fail", "owns a table", f"{t}: an owner can grant itself anything and skips row-level "
                        "security" + ("" if forced else " (not forced)"))
            if reads and schema in SENSITIVE_SCHEMAS:
                rep.add("fail", "reads a sensitive schema", f"{t}: {schema} holds users, sessions or secrets")
            elif sensitive:
                rep.add("warn", "reads a secret-looking column", f"{t}: {', '.join(sensitive)}")
            if writes and schema in SENSITIVE_SCHEMAS:
                rep.add("fail", "writes a sensitive schema", f"{t}: {', '.join(writes)}")
            if "TRUNCATE" in tprivs:
                rep.add("fail", "can truncate", f"{t}: TRUNCATE skips row-level security and row triggers")
            # An extension's own views (pg_stat_statements) are the extension's design, not a policy gap.
            if kind == "v" and (reads or writes) and not invoker and not from_extension:
                rep.add("warn", "view runs as its owner", f"{t}: the policies on the tables under it are checked "
                        "for the view's owner, not the agent (set security_invoker = true)")
            if kind in ("r", "p") and (reads or writes) and not rls:
                rep.add("warn", "row-level security is off", f"{t}: the agent reaches every row it is granted")
            elif kind in ("r", "p") and rls and not has_policy and not bypasses:
                rep.add("info", "no policy for it", f"{t}: row-level security is on and no policy names the agent "
                        "or PUBLIC, so it sees and changes no rows")
            if cfg is not None:
                _against_config(rep, cfg, t, tprivs, cprivs, guarded)
        if cfg is not None:
            seen = {x["table"] for x in rep.tables}
            for t in cfg.tables:
                if t in cfg.never_write and t not in seen:
                    rep.add("info", "protected table not reachable", f"{t}: the agent cannot reach it at all")

        for fn, in conn.execute(DEFINER, params).fetchall():
            rep.add("warn", "can call a SECURITY DEFINER function",
                    f"{fn}: it runs with its owner's rights, whatever the agent's own grants")
        for schema, in conn.execute(CREATE_ON, params).fetchall():
            rep.add("warn", "can create objects", f"schema {schema}: it could add a function or view of its own")
    return rep


def _against_config(rep: Report, cfg: Config, t: str, tprivs: list[str], cprivs: list[str],
                    guarded: list[str]) -> None:
    def allowed(priv: str, col: str | None) -> bool:
        if priv == "SELECT":
            if t not in cfg.reads:
                return False
            cols = cfg.reads[t]
        else:
            kind = priv.lower()
            if kind not in cfg.writes.get(t, {}):
                return False
            if kind == "delete":
                return True
            cols = cfg.writes[t][kind]
        return cols is None or (col is not None and col in cols)

    extra = [p for p in tprivs if not allowed(p, None)]
    extra += [c for c in cprivs if not allowed(*c.split(" ", 1))]
    if extra:
        rep.add("fail", "more than the config allows", f"{t}: {', '.join(extra)}")
    never = cfg.never_write.get(t, [])
    hit = [c for c in never if any(p in tprivs for p in ("INSERT", "UPDATE"))
           or f"INSERT {c}" in cprivs or f"UPDATE {c}" in cprivs]
    if hit:
        rep.add("fail", "can write a never_write column", f"{t}: {', '.join(hit)}")
    unguarded = [c for c in never if c not in guarded]
    if unguarded:
        rep.add("fail", "never_write trigger missing", f"{t}: no trigger refuses {cfg.role} changing "
                + ", ".join(unguarded) + "; apply `counsel guard compile`")


def render(rep: Report) -> str:
    icon = {"fail": "FAIL", "warn": "warn", "info": "info"}
    lines = [f"counsel guard: {rep.role}", ""]
    if rep.tables:
        lines += ["What it can reach (live database):", ""]
        width = max(len(x["table"]) for x in rep.tables)
        for x in rep.tables:
            privs = ", ".join(x["privileges"] + x["columns"]) or "-"
            rls = "rls on" if x["rls"] else ("rls OFF" if x["kind"] == "table" else x["kind"])
            lines.append(f"  {x['table']:<{width}}  {rls:<7}  {privs}")
        lines.append("")
    order = {"fail": 0, "warn": 1, "info": 2}
    for f in sorted(rep.findings, key=lambda f: order[f.level]):
        lines.append(f"  {icon[f.level]:<4}  {f.what}: {f.detail}")
    n = {k: sum(f.level == k for f in rep.findings) for k in order}
    lines += ["", f"{n['fail']} failing, {n['warn']} warning{'' if n['warn'] == 1 else 's'}."
              + (" Exit 1." if rep.failed else "")]
    return "\n".join(lines) + "\n"


def to_json(rep: Report) -> str:
    return json.dumps({**asdict(rep), "failed": rep.failed}, indent=2)
