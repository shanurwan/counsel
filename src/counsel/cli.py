"""counsel validate | compile | audit"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import audit, registry
from .compiler import compile_sql

ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = ROOT / "agents"
COMPILED = ROOT / "db" / "generated" / "agents.sql"


def _load() -> list[registry.Agent]:
    try:
        return registry.load(AGENTS_DIR)
    except registry.RegistryError as e:
        for p in e.problems:
            print(f"invalid: {p}", file=sys.stderr)
        sys.exit(1)


def cmd_validate(args: argparse.Namespace) -> int:
    agents = _load()
    print(f"ok: {len(agents)} agents")
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    sql = compile_sql(_load())
    if args.check:
        current = COMPILED.read_text(encoding="utf-8") if COMPILED.exists() else ""
        if current != sql:
            print(f"drift: {COMPILED.relative_to(ROOT)} is out of date; run `counsel compile`", file=sys.stderr)
            return 1
        print("ok: compiled grants match the registry")
        return 0
    COMPILED.parent.mkdir(parents=True, exist_ok=True)
    COMPILED.write_text(sql, encoding="utf-8", newline="\n")
    print(f"wrote {COMPILED.relative_to(ROOT)}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    import psycopg

    dsn = args.dsn or os.environ.get("COUNSEL_DATABASE_URL")
    if not dsn:
        print("set COUNSEL_DATABASE_URL or pass --dsn", file=sys.stderr)
        return 2
    agents = _load()
    with psycopg.connect(dsn) as conn:
        violations = audit.run(conn, agents)
        table = audit.matrix(conn, agents)

    print(table)
    if args.matrix:
        Path(args.matrix).write_text(table, encoding="utf-8")
    for v in violations:
        print(f"VIOLATION {v}", file=sys.stderr)
    if violations:
        print(f"fail: {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print(f"ok: {len(agents)} agents, 0 can write a commitment")
    return 0


def migrate(conn, out=print) -> None:
    """Apply db/migrations/*.sql once each, in order, then the compiled grants (always)."""
    conn.execute("create schema if not exists counsel_meta")
    conn.execute("""create table if not exists counsel_meta.migrations (
        name text primary key, applied_at timestamptz not null default now())""")
    conn.commit()
    done = {r[0] for r in conn.execute("select name from counsel_meta.migrations")}
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into counsel_meta.migrations (name) values (%s)", (path.name,))
        out(f"applied {path.name}")
    conn.execute(COMPILED.read_text(encoding="utf-8"))
    conn.commit()
    out(f"applied {COMPILED.relative_to(ROOT).as_posix()}")


def cmd_migrate(args: argparse.Namespace) -> int:
    import psycopg

    dsn = args.dsn or os.environ.get("COUNSEL_DATABASE_URL")
    if not dsn:
        print("set COUNSEL_DATABASE_URL or pass --dsn", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn:
        migrate(conn)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="counsel")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="validate agents/*.yaml").set_defaults(fn=cmd_validate)

    p = sub.add_parser("compile", help="compile the registry to db/generated/agents.sql")
    p.add_argument("--check", action="store_true", help="fail if the compiled file is out of date")
    p.set_defaults(fn=cmd_compile)

    p = sub.add_parser("migrate", help="apply migrations and compiled grants")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("audit", help="audit every agent credential against a live database")
    p.add_argument("--dsn")
    p.add_argument("--matrix", help="also write the privilege matrix (markdown) to this file")
    p.set_defaults(fn=cmd_audit)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
