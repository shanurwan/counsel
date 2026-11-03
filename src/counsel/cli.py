"""counsel validate | compile | audit | bench | eval | ask | standup | review | report | trace | simulate"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import audit, registry
from .compiler import compile_sql

ROOT = Path(os.environ.get("COUNSEL_ROOT") or Path(__file__).resolve().parents[2])
AGENTS_DIR = ROOT / "agents"
COMPILED = ROOT / "db" / "generated" / "agents.sql"
RECORDINGS = ROOT / "tests" / "recordings"


def build_model():
    """COUNSEL_MODEL=live (default) | replay | record."""
    from .model import NvidiaModel, RecordingModel, ReplayModel

    mode = os.environ.get("COUNSEL_MODEL", "live")
    if mode == "replay":
        return ReplayModel(RECORDINGS)
    live = NvidiaModel(api_key=os.environ["NVIDIA_API_KEY"])
    return RecordingModel(live, RECORDINGS) if mode == "record" else live


def build_worker(conn):
    from .behaviors import IMPLEMENTATIONS
    from .runtime import Worker

    agents = {a.name: a for a in _load()}
    return Worker(conn, agents, IMPLEMENTATIONS, build_model())


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
    done = {r[0] for r in conn.execute("select name from counsel_meta.migrations")}
    # Close the implicit transaction so each block below is a real commit,
    # not a savepoint that a later failure would silently roll back.
    conn.commit()
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into counsel_meta.migrations (name) values (%s)", (path.name,))
        out(f"applied {path.name}")
    with conn.transaction():
        conn.execute(COMPILED.read_text(encoding="utf-8"))
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


def _dsn(args) -> str:
    dsn = getattr(args, "dsn", None) or os.environ.get("COUNSEL_DATABASE_URL")
    if not dsn:
        print("set COUNSEL_DATABASE_URL or pass --dsn", file=sys.stderr)
        sys.exit(2)
    return dsn


def cmd_tick(args: argparse.Namespace) -> int:
    """One scheduler tick, as Cloud Run runs it. Connect as counsel_runtime."""
    import json

    import psycopg

    with psycopg.connect(_dsn(args), autocommit=True) as conn:
        print(json.dumps(build_worker(conn).tick(), indent=2, default=str))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    import psycopg

    with psycopg.connect(_dsn(args)) as conn:
        qid = conn.execute("insert into counsel.questions (body) values (%s) returning id",
                           (args.question,)).fetchone()[0]
    print(f"queued question {qid}; the next tick answers it")
    return 0


def cmd_standup(args: argparse.Namespace) -> int:
    """Post a standup for a person; the next tick proposes notes for them to approve."""
    import psycopg

    with psycopg.connect(_dsn(args)) as conn:
        person = conn.execute("select id from work.people where handle = %s", (args.handle,)).fetchone()
        if person is None:
            print(f"no person {args.handle!r}", file=sys.stderr)
            return 1
        sid = conn.execute("insert into work.standups (person_id, body) values (%s, %s) returning id",
                           (person[0], args.text)).fetchone()[0]
    print(f"queued standup {sid}; after the next tick, `counsel review` lists the proposed notes")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import run

    return run(args.admin_url, Path(args.out))


def cmd_replay(args: argparse.Namespace) -> int:
    """Reconstruct a run from the journal: every step, model call and outcome."""
    import psycopg

    with psycopg.connect(_dsn(args)) as conn:
        run = conn.execute(
            """select r.agent, r.status, r.attempt, r.started_at, r.finished_at, r.error, j.idempotency_key
               from counsel.runs r join counsel.jobs j on j.id = r.job_id where r.id = %s""",
            (args.run_id,)).fetchone()
        if not run:
            print("no such run", file=sys.stderr)
            return 1
        agent, status, attempt, started, finished, error, key = run
        print(f"run {args.run_id}\n  agent {agent}  status {status}  attempt {attempt}  job {key}")
        print(f"  {started} -> {finished}" + (f"\n  error: {error}" if error else ""))
        calls = {seq: (model, latency, req, resp) for seq, model, latency, req, resp in conn.execute(
            """select step_seq, model, latency_ms, request, response from counsel.model_calls
               where run_id = %s and error is null""", (args.run_id,))}
        for seq, name, output, at in conn.execute(
                "select seq, name, output, finished_at from counsel.steps where run_id = %s order by seq",
                (args.run_id,)):
            print(f"\n[{seq}] {name}  ({at})")
            if seq in calls:
                model, latency, req, resp = calls[seq]
                print(f"    model {model}, {latency} ms")
                for m in req["messages"]:
                    print(f"    > {m['role']}: {m['content'][:300]}")
                print(f"    < {resp['text'][:600]}")
            else:
                print(f"    {str(output)[:600]}")
        for at, action, decision, detail in conn.execute(
                """select at, action, decision, detail from counsel.audit_log
                   where run_id = %s order by id""", (args.run_id,)):
            print(f"\naudit {at} {action} {decision} {detail}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evals import run

    return run(args.admin_url, Path(args.out), args.repeat, args.agent)


def cmd_review(args: argparse.Namespace) -> int:
    """List drafts waiting for a person, or publish or reject one, as you."""
    import psycopg

    from . import review

    with psycopg.connect(_dsn(args)) as conn:
        try:
            if not args.draft_id:
                for d in review.pending(conn):
                    print(f"{d['id']}  {d['agent']}  {d['kind']}  {d['created_at']:%Y-%m-%d %H:%M}")
                    print("    " + d["body"][:300].replace("\n", "\n    ") + "\n")
                return 0
            if args.publish:
                body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else None
                kind = review.publish(conn, args.draft_id, body)
                print(f"published {kind}" + (" (posted to the customer as you)" if kind.startswith("support_reply") else ""))
            elif args.reject:
                print(f"rejected {review.reject(conn, args.draft_id)}")
            else:
                print("pass --publish or --reject", file=sys.stderr)
                return 2
        except review.ReviewError as e:
            print(e, file=sys.stderr)
            return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Spend per run, agent and requester, and the findings an owner can act on."""
    from datetime import datetime, timezone

    import psycopg

    from . import cost

    with psycopg.connect(_dsn(args)) as conn:
        since, until = cost.window(conn)
        if args.since:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.until:
            until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        report = cost.build(conn, _load(), since, until)
    if args.out:
        cost.write(report, Path(args.out))
    print(cost.render(report))
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    """One run as OpenTelemetry (OTLP/JSON) spans."""
    import json

    import psycopg

    from . import cost

    with psycopg.connect(_dsn(args)) as conn:
        print(json.dumps(cost.trace(conn, args.run_id), indent=2))
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .simulate import run

    return run(args.admin_url, Path(args.out), args.days)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # reports print · and × on Windows consoles too
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

    p = sub.add_parser("tick", help="run one scheduler tick (as counsel_runtime)")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("standup", help="post a person's standup; proposed notes need their approval")
    p.add_argument("handle")
    p.add_argument("text")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_standup)

    p = sub.add_parser("ask", help="ask the handbook agent a question (as an admin)")
    p.add_argument("question")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("replay", help="reconstruct a run from its journal")
    p.add_argument("run_id")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("bench", help="run the incident bench against a throwaway database")
    p.add_argument("--admin-url", default=os.environ.get("COUNSEL_TEST_ADMIN_URL"))
    p.add_argument("--out", default="bench")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("eval", help="run every shipped agent's golden suite; exit 1 if any gate fails")
    p.add_argument("--admin-url", default=os.environ.get("COUNSEL_TEST_ADMIN_URL"))
    p.add_argument("--out", default="evals")
    p.add_argument("--repeat", type=int, default=1, help="run each case N times (live, to measure variance)")
    p.add_argument("--agent", help="only this agent's suite")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("review", help="list pending drafts, or publish or reject one (as you)")
    p.add_argument("draft_id", nargs="?")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--publish", action="store_true")
    g.add_argument("--reject", action="store_true")
    p.add_argument("--body-file", help="publish this edited text instead of the draft")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("report", help="spend per agent, run and requester, with findings to act on")
    p.add_argument("--dsn")
    p.add_argument("--since", help="UTC date, default: first recorded run")
    p.add_argument("--until", help="UTC date, default: the day after the last run")
    p.add_argument("--out", help="also write REPORT.md and report.json to this directory")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("trace", help="export one run as OpenTelemetry spans (OTLP/JSON)")
    p.add_argument("run_id")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_trace)

    p = sub.add_parser("simulate", help="play a synthetic month and benchmark the cost report's fix")
    p.add_argument("--admin-url", default=os.environ.get("COUNSEL_TEST_ADMIN_URL"))
    p.add_argument("--out", default="cost")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(fn=cmd_simulate)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
