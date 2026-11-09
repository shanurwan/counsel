"""counsel guard on a database shaped like a Supabase project: roles, auth schema, default grants."""

import os

import psycopg
import pytest

from counsel import guard

ADMIN_URL = os.environ.get("COUNSEL_TEST_ADMIN_URL")

GUARD_DB = "counsel_guard_test"

SUPABASE_LIKE = """
do $$ begin
  if not exists (select from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls; end if;
end $$;
create schema auth;
create table auth.users (id uuid primary key, email text, encrypted_password text);
create table public.tickets (
  id int primary key, team text not null, subject text, body text, summary text,
  assignee text, status text not null default 'open', refund_cents int);
create table public.ticket_drafts (id serial primary key, ticket_id int references public.tickets, body text);
create table public.api_keys (id int primary key, owner text, api_key text);
alter table public.tickets enable row level security;
alter table public.ticket_drafts enable row level security;
create policy app_users on public.tickets for all to authenticated using (true);
-- What Supabase grants on public by default.
grant usage on schema public to anon, authenticated, service_role;
grant all on all tables in schema public to anon, authenticated, service_role;
grant all on all sequences in schema public to anon, authenticated, service_role;
insert into public.tickets values (1, 'support', 'Refund', 'Please refund', null, 'aiko', 'open', 0),
                                  (2, 'billing', 'Invoice', 'Wrong total', null, 'bruno', 'open', 0);
"""

CONFIG = {
    "role": "support_agent",
    "reads": {"public.tickets": ["id", "team", "subject", "body", "summary", "status"]},
    "writes": {"public.ticket_drafts": {"insert": ["ticket_id", "body"]},
               "public.tickets": {"update": ["summary"]}},
    "rows": {"public.tickets": "team = 'support'", "public.ticket_drafts": "true"},
    "never_write": {"public.tickets": ["assignee", "status", "refund_cents"]},
}


@pytest.fixture
def pg():
    if not ADMIN_URL:
        pytest.skip("set COUNSEL_TEST_ADMIN_URL to run database tests")
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"drop database if exists {GUARD_DB} with (force)")
        admin.execute(f"create database {GUARD_DB}")
        admin.execute("drop role if exists support_agent")
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=GUARD_DB)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SUPABASE_LIKE)
        yield conn
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"drop database if exists {GUARD_DB} with (force)")
        admin.execute("drop role if exists support_agent")


def whats(report, level="fail"):
    return {f.what for f in report.findings if f.level == level}


def as_agent(conn, stmt):
    """Run one statement as the agent; the error code if Postgres refused it, else the row count."""
    try:
        with conn.transaction():
            conn.execute("set local role support_agent")
            cur = conn.execute(stmt)
            n = cur.rowcount
            raise _Rollback(n)
    except _Rollback as r:
        return r.n
    except psycopg.Error as e:
        return e.sqlstate


class _Rollback(Exception):
    def __init__(self, n):
        self.n = n


def test_the_usual_shortcut_an_agent_on_service_role_fails_loudly(pg):
    pg.execute("create role support_agent nologin; grant service_role to support_agent")
    report = guard.check(pg, "support_agent")
    assert report.failed and "can become a powerful role" in whats(report)
    assert "can truncate" in whats(report)
    # The secret-looking column is named, wherever it is.
    assert any("api_key" in f.detail for f in report.findings if f.what == "reads a secret-looking column")


def test_sharing_the_app_role_is_named_and_reading_auth_fails(pg):
    pg.execute("create role support_agent nologin; grant authenticated to support_agent;"
               "grant usage on schema auth to support_agent; grant select on auth.users to support_agent")
    report = guard.check(pg, "support_agent")
    assert "shares your app's role" in whats(report, "warn")
    assert "reads a sensitive schema" in whats(report)
    assert "row-level security is off" in whats(report, "warn")  # api_keys, through authenticated


def test_compile_then_check_passes_and_the_database_holds_the_line(pg):
    cfg = guard.load(CONFIG)
    pg.execute("create role support_agent nologin; grant service_role to support_agent")
    pg.execute(guard.compile_sql(cfg))
    report = guard.check(pg, "support_agent", cfg)
    assert not report.failed, guard.render(report)
    assert {x["table"] for x in report.tables} == {"public.tickets", "public.ticket_drafts"}

    # Reads are scoped to its rows and columns.
    assert as_agent(pg, "select id, subject from public.tickets") == 1
    assert as_agent(pg, "select assignee from public.tickets") == "42501"
    assert as_agent(pg, "select * from public.api_keys") == "42501"
    assert as_agent(pg, "select * from auth.users") == "42501"
    # It may draft, and summarise its own team's ticket.
    assert as_agent(pg, "insert into public.ticket_drafts (ticket_id, body) values (1, 'draft')") == 1
    assert as_agent(pg, "update public.tickets set summary = 's' where id = 1") == 1
    assert as_agent(pg, "update public.tickets set summary = 's' where id = 2") == 0  # billing's ticket
    # It may never reassign, close, refund or delete.
    assert as_agent(pg, "update public.tickets set status = 'closed'") == "42501"
    assert as_agent(pg, "delete from public.tickets") == "42501"
    assert as_agent(pg, "truncate public.tickets") == "42501"


def test_the_trigger_still_refuses_after_someone_grants_too_much_and_check_says_so(pg):
    cfg = guard.load(CONFIG)
    pg.execute(guard.compile_sql(cfg))
    pg.execute("grant update, delete on public.tickets to support_agent")
    report = guard.check(pg, "support_agent", cfg)
    assert {"more than the config allows", "can write a never_write column"} <= whats(report)
    assert as_agent(pg, "update public.tickets set assignee = 'agent' where id = 1") == "42501"
    assert as_agent(pg, "update public.tickets set refund_cents = 9999 where id = 1") == "42501"
    # No delete policy names it, so row-level security finds nothing; on a table without
    # row-level security the trigger refuses. Either way the rows stay.
    assert as_agent(pg, "delete from public.tickets where id = 1") in ("42501", 0)
    pg.execute("alter table public.tickets disable row level security")
    assert as_agent(pg, "delete from public.tickets where id = 1") == "42501"
    assert as_agent(pg, "update public.tickets set status = 'closed' where id = 2") == "42501"
    assert pg.execute("select count(*) from public.tickets").fetchone() == (2,)
    # A person, as any other role, is untouched by the trigger.
    pg.execute("update public.tickets set status = 'closed' where id = 1")


def test_a_dropped_trigger_fails_the_check(pg):
    cfg = guard.load(CONFIG)
    pg.execute(guard.compile_sql(cfg))
    pg.execute('drop trigger "guard_support_agent" on public.tickets')
    assert "never_write trigger missing" in whats(guard.check(pg, "support_agent", cfg))


def test_a_view_that_runs_as_its_owner_is_flagged(pg):
    cfg = guard.load({**CONFIG, "reads": {**CONFIG["reads"], "public.ticket_view": "all"}})
    pg.execute("create view public.ticket_view as select id, assignee from public.tickets")
    pg.execute(guard.compile_sql(cfg))
    assert "view runs as its owner" in whats(guard.check(pg, "support_agent", cfg), "warn")
    pg.execute("alter view public.ticket_view set (security_invoker = true)")
    assert "view runs as its owner" not in whats(guard.check(pg, "support_agent", cfg), "warn")


def test_check_never_writes(pg):
    pg.execute("create role support_agent nologin")
    before = pg.execute("select count(*) from pg_class").fetchone()
    guard.check(pg, "support_agent")
    assert pg.execute("select count(*) from pg_class").fetchone() == before


def test_a_missing_role_fails_and_says_how_to_fix_it(pg):
    report = guard.check(pg, "support_agent")
    assert report.failed and not report.exists and "compile" in report.findings[0].detail


@pytest.mark.parametrize("bad, message", [
    ({"role": "service_role", "reads": {"public.t": "all"}}, "built-in role"),
    ({"role": "Agent", "reads": {"public.t": "all"}}, "lower-case role"),
    ({"role": "a_agent"}, "reads and writes nothing"),
    ({"role": "a_agent", "reads": {"tickets": "all"}}, "schema.table"),
    ({"role": "a_agent", "writes": {"public.t": {"upsert": "all"}}}, "insert, update"),
    ({"role": "a_agent", "writes": {"public.t": {"update": "all"}}, "never_write": {"public.t": ["status"]}},
     "never_write forbids"),
    ({"role": "a_agent", "reads": {"public.t": "all"}, "rows": {"public.t": "true; drop table x"}},
     "one SQL condition"),
    ({"role": "a_agent", "reads": {"public.t": ["Robert'); drop"]}}, "column name"),
])
def test_a_bad_config_is_refused_before_any_sql_is_written(bad, message):
    with pytest.raises(guard.GuardError, match=message):
        guard.load(bad)


def test_the_example_config_in_the_repo_compiles():
    from pathlib import Path

    cfg = guard.load(Path(__file__).resolve().parents[1] / "guard" / "guard.example.yaml")
    sql = guard.compile_sql(cfg)
    assert cfg.role == "support_agent" and "counsel_guard.refuse_columns('support_agent', 'assignee'" in sql


def test_the_cli_exit_code_gates_ci(pg, monkeypatch, tmp_path, capsys):
    import yaml

    from counsel import cli

    path = tmp_path / "guard.yaml"
    path.write_text(yaml.safe_dump(CONFIG))
    assert cli.main(["guard", "compile", str(path)]) == 0
    sql = capsys.readouterr().out
    assert "counsel guard compile guard.yaml" in sql
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=GUARD_DB)
    monkeypatch.setenv("DATABASE_URL", url)
    assert cli.main(["guard", "check", "--config", str(path)]) == 1  # not applied yet
    pg.execute(sql)
    assert cli.main(["guard", "check", "--config", str(path)]) == 0
    assert "0 failing" in capsys.readouterr().out
