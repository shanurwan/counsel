import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg
import pytest

from counsel import registry
from counsel.cli import migrate

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("COUNSEL_TEST_ADMIN_URL")
TEST_DB = "counsel_test"


@pytest.fixture(scope="session")
def agents():
    return registry.load(ROOT / "agents")


@pytest.fixture(scope="session")
def database_url():
    if not ADMIN_URL:
        pytest.skip("set COUNSEL_TEST_ADMIN_URL to a Postgres 16+ superuser URL to run database tests")
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"drop database if exists {TEST_DB} with (force)")
        admin.execute(f"create database {TEST_DB}")
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=TEST_DB)
    with psycopg.connect(url) as conn:
        migrate(conn, out=lambda _: None)
        conn.execute("""
            insert into work.teams values ('core', 'Core'), ('growth', 'Growth');
            insert into work.people (id, handle, team_id, tz) values
              ('00000000-0000-0000-0000-00000000000a', 'aiko',  'core',   'Asia/Tokyo'),
              ('00000000-0000-0000-0000-00000000000b', 'bruno', 'growth', 'America/Sao_Paulo');
            insert into work.tasks (id, team_id, title, owner_id, due_date, status) values
              ('00000000-0000-0000-0000-0000000000a1', 'core', 'Rotate staging keys',
               '00000000-0000-0000-0000-00000000000a', '2026-11-20', 'in_progress'),
              ('00000000-0000-0000-0000-0000000000b1', 'growth', 'Pricing page copy',
               null, null, 'open');
        """)
        conn.execute("""
            update work.teams set tz = 'Asia/Tokyo' where id = 'core';
            update work.teams set tz = 'America/Sao_Paulo' where id = 'growth';
            insert into kb.documents (path, body) values
              ('leave.md', 'Annual leave: everyone gets 18 days of paid annual leave per year. '
                           'Request leave in the HR portal at least two weeks ahead.'),
              ('expenses.md', 'Expenses: submit receipts within 30 days. Meals while travelling '
                              'are covered up to 40 USD per day.');
        """)
        conn.execute("alter role counsel_runtime password 'counsel-test-only'")
        conn.commit()
    return url


@pytest.fixture
def runtime(database_url):
    """A session logged in as the runtime, exactly as Cloud Run connects."""
    url = psycopg.conninfo.make_conninfo(database_url, user="counsel_runtime", password="counsel-test-only")
    with psycopg.connect(url) as conn:
        conn.execute("select 1")
        yield conn
        conn.rollback()


@pytest.fixture
def db(database_url):
    """One connection per test inside a transaction that is always rolled back."""
    with psycopg.connect(database_url) as conn:
        # Open the outer transaction now so every conn.transaction() below is a
        # savepoint inside it, never a commit.
        conn.execute("select 1")
        yield conn
        conn.rollback()


TASK_A = "00000000-0000-0000-0000-0000000000a1"
TASK_B = "00000000-0000-0000-0000-0000000000b1"
AIKO = "00000000-0000-0000-0000-00000000000a"
BRUNO = "00000000-0000-0000-0000-00000000000b"
DUE = date(2026, 11, 20)


@contextmanager
def acting_as(conn, role):
    """Run statements as `role` inside a savepoint, then return to the test's own role."""
    try:
        with conn.transaction():
            conn.execute(f"set local role {role}")
            yield conn
    finally:
        conn.execute("reset role")


def denied(conn, role, sql, params=None):
    """Run `sql` as `role` and return the SQLSTATE it was refused with, or None."""
    try:
        with acting_as(conn, role):
            conn.execute(sql, params)
    except psycopg.Error as e:
        return e.sqlstate
    return None
