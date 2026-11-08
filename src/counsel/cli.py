"""counsel validate | compile | audit | bench | eval | ask | standup | review | rollback | sync | warehouse |
report | maturity | trace | simulate | challenge | new | inbox | link-login"""

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
    from .model import FallbackModel, NvidiaModel, RecordingModel, ReplayModel, gemini_model

    mode = os.environ.get("COUNSEL_MODEL", "live")
    if mode == "replay":
        return ReplayModel(RECORDINGS)
    live = NvidiaModel(api_key=os.environ["NVIDIA_API_KEY"])
    # A second provider is optional; with a key set, an NVIDIA outage hands over to it.
    if os.environ.get("GEMINI_API_KEY"):
        live = FallbackModel((live, gemini_model(os.environ["GEMINI_API_KEY"])))
    return RecordingModel(live, RECORDINGS) if mode == "record" else live


def build_worker(conn):
    from .behaviors import IMPLEMENTATIONS
    from .runtime import Worker

    # Each Cloud Run service runs one pool, logged in as that pool's login.
    pool = os.environ.get("COUNSEL_RUNTIME_POOL", registry.CORE_POOL)
    agents = {a.name: a for a in _load() if a.pool == pool}
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


def cmd_link_login(args: argparse.Namespace) -> int:
    """Link a Supabase Auth sign-in to a person, so the hosted MCP server can show them their work.
    Run by an owner, on an admin connection; nobody links themselves."""
    import psycopg

    with psycopg.connect(_dsn(args)) as conn:
        user = conn.execute("select id from auth.users where lower(email) = lower(%s)", (args.email,)).fetchone()
        if not user:
            print(f"no Supabase Auth user with email {args.email}; create one first", file=sys.stderr)
            return 1
        person = conn.execute("select id from work.people where handle = %s", (args.handle,)).fetchone()
        if not person:
            print(f"no person with handle {args.handle}", file=sys.stderr)
            return 1
        conn.execute("""insert into counsel.logins (auth_user_id, person_id) values (%s, %s)
                        on conflict (auth_user_id) do update
                        set person_id = excluded.person_id, linked_by = current_user, linked_at = now()""",
                     (user[0], person[0]))
    print(f"linked {args.email} to {args.handle}")
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

    from . import github, review

    with psycopg.connect(_dsn(args)) as conn:
        try:
            if not args.draft_id:
                for d in review.pending(conn):
                    print(f"{d['id']}  {d['agent']}  {d['kind']}  {d['created_at']:%Y-%m-%d %H:%M}")
                    print("    " + d["body"][:300].replace("\n", "\n    ") + "\n")
                return 0
            if args.publish:
                body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else None
                kind = review.publish(conn, args.draft_id, body, deliver=_deliver)
                print(f"published {kind}" + (" (posted to the customer as you)" if kind.startswith("support_reply") else ""))
            elif args.reject:
                print(f"rejected {review.reject(conn, args.draft_id)}")
            else:
                print("pass --publish or --reject", file=sys.stderr)
                return 2
        except (review.ReviewError, github.GitHubError) as e:
            print(e, file=sys.stderr)
            return 1
    return 0


def _deliver(kind: str, text: str) -> None:
    """Post what you just approved to GitHub, with your token. Nothing else ever writes there."""
    from . import github

    client = github.GitHub(token=os.environ.get("GH_TOKEN"))
    if kind.startswith("issue_triage:"):
        repo, number = kind.split(":", 1)[1].rsplit("#", 1)
        print("posted", client.comment(repo, int(number), text))
    elif kind.startswith("fix:"):
        repo = os.environ.get("COUNSEL_FIX_REPO", "shanurwan/counsel")
        print("filed", client.create_issue(repo, f"drift_watcher: {kind.removeprefix('fix:')}", text))


def cmd_rollback(args: argparse.Namespace) -> int:
    """Retract everything a run wrote that a person has not accepted, in one transaction."""
    import psycopg

    with psycopg.connect(_dsn(args)) as conn:
        if not conn.execute("select from counsel.runs where id = %s", (args.run_id,)).fetchone():
            print("no such run", file=sys.stderr)
            return 1
        added = conn.execute("insert into counsel.run_rollbacks (run_id, reason) values (%s, %s) on conflict do nothing",
                             (args.run_id, args.reason)).rowcount
        if not added:
            print("already rolled back")
            return 0
        detail = conn.execute("""select detail from counsel.audit_log where run_id = %s and action = 'rollback'
                                 order by id desc limit 1""", (args.run_id,)).fetchone()[0]
    print(f"retracted {detail['drafts']} drafts, {detail['answers']} answers, {detail['notes']} notes and cancelled "
          f"{detail['messages']} messages; {detail['kept_published']} published drafts stand, a person accepted them")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Pull new issues from every repository an agent watches. Read-only on GitHub."""
    import json
    from datetime import datetime, timezone

    import psycopg

    from . import github

    with psycopg.connect(_dsn(args), autocommit=True) as conn:
        results = github.sync_all(conn, {a.name: a for a in _load()}, datetime.now(timezone.utc))
    print(json.dumps(results, indent=2))
    return 0


def cmd_warehouse(args: argparse.Namespace) -> int:
    """Load finished runs into BigQuery until none are left."""
    import psycopg

    from . import warehouse

    client = warehouse.gcloud_client(args.project)
    total = 0
    with psycopg.connect(_dsn(args), autocommit=True) as conn:
        while n := warehouse.export(conn, client)["exported"]:
            total += n
    print(f"exported {total} runs")
    return 0


def cmd_maturity(args: argparse.Namespace) -> int:
    """Each team's grade, and with --team the rows behind it, so a dispute has an answer."""
    from datetime import datetime, timedelta, timezone

    import psycopg
    from psycopg.rows import dict_row

    until = datetime.now(timezone.utc)
    window = {"since": until - timedelta(days=args.days), "until": until}
    query = (ROOT / "db" / "reports" / "maturity.sql").read_text(encoding="utf-8")
    with psycopg.connect(_dsn(args), row_factory=dict_row) as conn:
        rows = conn.execute(query, window).fetchall()
        if args.team:
            rows = [r for r in rows if r["team"] == args.team]
        print(f"Last {args.days} days. Definition and query: db/reports/maturity.sql\n")
        print("| Team | Grade | Weeks active | Digests | Notes | Questions | Digests rejected |")
        print("|---|--:|--:|--:|--:|--:|--:|")
        for r in rows:
            print(f"| {r['team']} | {r['grade']} | {r['active_weeks']} | {r['digests']} | {r['notes']} | "
                  f"{r['questions']} | {r['rejected']} of {r['digests_reviewed']} |")
        if args.team:
            print("\nThe outputs counted:")
            for d in conn.execute(
                    """select id, kind, reviewed_at, reviewed_by from counsel.drafts
                       where kind = %(kind)s and status = 'published'
                         and reviewed_at >= %(since)s and reviewed_at < %(until)s order by reviewed_at""",
                    {**window, "kind": f"team_digest:{args.team}"}):
                print(f"  digest {d['id']}  published {d['reviewed_at']:%Y-%m-%d} by {d['reviewed_by']}")
            for n in conn.execute(
                    """select u.id, u.created_at, u.on_behalf_of from work.task_updates u
                       join work.people p on p.handle = u.on_behalf_of
                       where p.team_id = %(team)s and u.retracted_at is null
                         and u.created_at >= %(since)s and u.created_at < %(until)s order by u.created_at""",
                    {**window, "team": args.team}):
                print(f"  note {n['id']}  {n['created_at']:%Y-%m-%d} from {n['on_behalf_of']}")
    return 0


def cmd_challenge(args: argparse.Namespace) -> int:
    """Turn a disputed output into a case in the agent's golden suite."""
    import getpass

    import psycopg

    from .challenge import ChallengeError, add_case, case_from_run

    with psycopg.connect(_dsn(args)) as conn:
        try:
            agent, case = case_from_run(conn, args.run_id, case_id=args.id, must_contain=args.must_contain,
                                        must_not_contain=args.must_not_contain, rubric=args.rubric,
                                        by=getpass.getuser())
            suite = ROOT / "evals" / agent / "suite.yaml"
            add_case(suite, case)
        except ChallengeError as e:
            print(e, file=sys.stderr)
            return 1
    print(f"added {case['id']} to {suite.relative_to(ROOT)}\n"
          f"record it: COUNSEL_MODEL=record counsel eval --agent {agent}\n"
          "If it fails, the challenge was right: the gate blocks this agent until it is fixed.")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    from .challenge import ChallengeError, scaffold

    try:
        made = scaffold(ROOT, args.name, args.owner, args.tier, args.capabilities)
    except ChallengeError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(f"wrote {p.relative_to(ROOT)}" for p in made))
    print(f"next: write the suite's cases, implement {args.name} in behaviors.py, run `counsel compile`")
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    """What agents have sent a person, and how much of their budget it spent."""
    import psycopg

    with psycopg.connect(_dsn(args)) as conn:
        person = conn.execute("select id, interruption_budget from work.people where handle = %s",
                              (args.handle,)).fetchone()
        if person is None:
            print(f"no person {args.handle!r}", file=sys.stderr)
            return 1
        rows = conn.execute(
            """select created_at, agent, body, cancelled_at from counsel.outbound_messages
               where recipient_id = %s order by created_at desc limit 20""", (person[0],)).fetchall()
        spent = conn.execute(
            """select count(*) from counsel.outbound_messages where recipient_id = %s
               and cancelled_at is null and created_at > now() - interval '7 days'""", (person[0],)).fetchone()[0]
    print(f"{args.handle}: {spent} of {person[1]} messages in the last 7 days\n")
    for at, agent, body, cancelled in rows:
        print(f"{at:%Y-%m-%d %H:%M} {agent}{' (cancelled)' if cancelled else ''}\n{body}\n")
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

    p = sub.add_parser("link-login", help="link a Supabase Auth sign-in to a person, for the hosted MCP server")
    p.add_argument("email")
    p.add_argument("handle")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_link_login)

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

    p = sub.add_parser("rollback", help="retract what a run wrote that no person has accepted")
    p.add_argument("run_id")
    p.add_argument("--reason", default="rolled back by a person")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("sync", help="pull new issues from every repository an agent watches")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("warehouse", help="load finished runs into BigQuery (Cloud Run does this every tick)")
    p.add_argument("--project", required=True)
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_warehouse)

    p = sub.add_parser("maturity", help="each team's grade from what happened; --team shows the rows behind it")
    p.add_argument("--team")
    p.add_argument("--days", type=int, default=28)
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_maturity)

    p = sub.add_parser("challenge", help="turn a disputed output into a case in the agent's golden suite")
    p.add_argument("run_id")
    p.add_argument("--id", required=True, help="the new case's id, lower_snake_case")
    p.add_argument("--must-contain", action="append", default=[])
    p.add_argument("--must-not-contain", action="append", default=[])
    p.add_argument("--rubric", help="a judge rubric from evals/judge/rubrics.yaml")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_challenge)

    p = sub.add_parser("new", help="scaffold an agent: its registry entry and an empty suite")
    p.add_argument("name")
    p.add_argument("--owner", required=True)
    p.add_argument("--tier", required=True, choices=registry.TIERS)
    p.add_argument("--capabilities", required=True, nargs="+")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("inbox", help="what agents have sent a person")
    p.add_argument("handle")
    p.add_argument("--dsn")
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("simulate", help="play a synthetic month and benchmark the cost report's fix")
    p.add_argument("--admin-url", default=os.environ.get("COUNSEL_TEST_ADMIN_URL"))
    p.add_argument("--out", default="cost")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(fn=cmd_simulate)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
