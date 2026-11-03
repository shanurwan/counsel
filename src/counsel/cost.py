"""Spend per run, and the report that turns it into decisions.

Every model call is already a row in counsel.model_calls (tokens, latency,
model, run). db/migrations/0005_cost.sql prices them; db/reports/*.sql ask
the questions an owner would; this module runs those queries and writes
findings, each with evidence, an owner, an action and what it is worth per
month. The queries are plain SQL so anyone can run one by hand.

`trace` exports one run as OpenTelemetry spans (GenAI semantic conventions),
so a run can be read in any tracing backend as well as with `counsel replay`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .registry import Agent

ROOT = Path(__file__).resolve().parents[2]
QUERIES = ROOT / "db" / "reports"
MONTH_DAYS = 30
CACHE_HOURS = 168  # what the report suggests; owners pick their own
# One requester with at least this share, and twice a fair share, of an agent's spend is worth a look.
OUTSIZED_SHARE = 0.25
MIN_RUNS_FOR_SHARE = 20  # below this an agent's split by requester is noise


@dataclass
class Finding:
    kind: str  # cost | waste | security | budget | data
    title: str
    evidence: str
    action: str
    owner: str
    agent: str | None = None
    monthly_usd: float = 0.0  # what acting on it is worth per month, at this window's rate
    runs: list[str] = field(default_factory=list)
    share: float = 0.0  # the monthly figure as a share of the agent's spend


def query(conn: psycopg.Connection, name: str, since: datetime, until: datetime) -> list[dict]:
    sql = (QUERIES / f"{name}.sql").read_text(encoding="utf-8")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, {"since": since, "until": until})  # nosec B608 - fixed files, bound params
        return cur.fetchall()


def window(conn: psycopg.Connection) -> tuple[datetime, datetime]:
    """The span of recorded runs, whole UTC days."""
    lo, hi = conn.execute("select min(started_at), max(started_at) from counsel.runs").fetchone()
    if lo is None:
        now = datetime.now(timezone.utc)
        return now - timedelta(days=MONTH_DAYS), now
    day = lambda t: t.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)  # noqa: E731
    return day(lo), day(hi) + timedelta(days=1)


def plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def usd(x: float) -> str:
    return f"${x:,.2f}" if abs(x) >= 1 else f"${x:,.4f}"


def build(conn: psycopg.Connection, agents: list[Agent], since: datetime, until: datetime) -> dict:
    by_name = {a.name: a for a in agents}
    per_month = MONTH_DAYS / max((until - since).total_seconds() / 86400, 1)
    owner = lambda agent: by_name[agent].owner if agent in by_name else "platform"  # noqa: E731

    rows = {name: query(conn, name, since, until) for name in (
        "by_agent", "by_day", "repeats", "tool_loops", "billed_errors", "refused_runs",
        "dead_runs", "outliers", "requesters", "data_quality")}
    findings: list[Finding] = []

    for r in rows["by_agent"]:
        a = by_name.get(r["agent"])
        r["owner"] = owner(r["agent"])
        r["projected_month_usd"] = r["usd"] * per_month
        r["budget_usd"] = a.budget_usd if a else None
        if a and a.budget_usd and r["projected_month_usd"] > a.budget_usd:
            findings.append(Finding(
                "budget", f"{a.name} is on course to spend {usd(r['projected_month_usd'])} this month "
                          f"against a {usd(a.budget_usd)} budget",
                f"{r['runs']} runs in the window, p95 run {usd(r['p95_run_usd'])}.",
                f"Act on the {a.name} findings below, or raise budget_usd in agents/{a.name}.yaml "
                "in a PR the owner approves.",
                a.owner, a.name, r["projected_month_usd"] - a.budget_usd))

    for r in rows["repeats"]:
        a = by_name.get(r["agent"])
        share = r["repeat_calls"] / r["paid_calls"]
        evidence = (f"{r['repeat_calls']} of {r['paid_calls']} paid calls ({share:.0%}) repeated a request "
                    f"already answered in the window ({plural(r['repeated_requests'], 'distinct request')}), "
                    f"{usd(r['repeat_usd'])}. Same prompt, same documents, so the same answer.")
        if a and a.cache_hours:
            action = (f"{a.name} already caches for {a.cache_hours} h; these repeats fell outside it. "
                      "Lengthen cache_hours if answers can be that old.")
        elif a and a.tier == "T0":
            action = (f"Add `cache_hours: {CACHE_HOURS}` to agents/{a.name}.yaml. The cache key is the whole "
                      "prompt, so an edited document is a new request and is never served stale.")
        elif a and a.schedule:
            action = ("The same input produced the same output again (a quiet day resends identical "
                      f"activity). Skip the run when nothing changed since the last one; caching is "
                      f"T0 only and {a.name} is {a.tier}.")
        else:
            action = ("Caching is T0 only. Check for duplicate events: the same ticket or question "
                      "enqueued twice.")
        findings.append(Finding("cost", f"{r['agent']} pays for the same answer again",
                                evidence, action, owner(r["agent"]), r["agent"], r["repeat_usd"] * per_month))

    for r in rows["tool_loops"]:
        share = r["looped_runs"] / r["runs_with_calls"]
        evidence = (f"{r['looped_runs']} of {r['runs_with_calls']} runs ({share:.0%}) called a tool before "
                    f"drafting. Each later turn re-sends the whole conversation, "
                    f"{r['later_turn_input_tokens']:,} input tokens against {r['first_turn_input_tokens']:,} "
                    f"for the first. Later turns cost {usd(r['later_turn_usd'])} of {usd(r['usd'])}.")
        findings.append(Finding(
            "cost", f"{r['agent']} makes a second model call in {share:.0%} of runs", evidence,
            "Give the model what it keeps asking for in the first prompt (for the support drafter, the "
            "ticket thread), so most runs draft in one call. Upper bound on the saving shown.",
            owner(r["agent"]), r["agent"], r["later_turn_usd"] * per_month))

    dead = {r["agent"]: r for r in rows["dead_runs"]}
    for r in rows["billed_errors"]:
        d = dead.pop(r["agent"], None)
        lost = f" {d['runs']} runs failed every attempt and wrote nothing." if d else ""
        findings.append(Finding(
            "waste", f"{r['agent']}: {r['calls']} billed calls produced nothing ({r['reason'].strip()})",
            f"{r['calls']} calls across {r['runs']} runs were cut off and discarded, {usd(r['usd'])}. "
            f"A retry sends the same prompt with the same limit, so it is cut off again.{lost}",
            "Raise max_tokens for this call, or shorten its input; do not retry a truncation unchanged.",
            owner(r["agent"]), r["agent"], r["usd"] * per_month, r["sample_runs"]))

    for r in dead.values():
        findings.append(Finding(
            "waste", f"{r['agent']}: {r['runs']} runs failed every attempt",
            f"{usd(r['usd'])} spent with nothing written. Example: {(r['example_error'] or '')[:160]}",
            "Open the dead letters (`counsel replay <run>`) and fix the cause before it repeats.",
            owner(r["agent"]), r["agent"], r["usd"] * per_month, r["sample_runs"]))

    refused = rows["refused_runs"]
    if refused:
        by_req: dict[str, int] = {}
        for r in refused:
            by_req[r["requester"]] = by_req.get(r["requester"], 0) + 1
        who = ", ".join(f"{k} ({v})" for k, v in sorted(by_req.items(), key=lambda kv: -kv[1]))
        findings.append(Finding(
            "security", f"{len(refused)} runs tried something their role does not allow, and Postgres refused",
            f"From: {who}. First refusal: {refused[0]['error'][:160]}. Nothing was read or written; "
            f"each run stopped at the refusal ({usd(sum(r['usd'] for r in refused))} spent).",
            "No rotation needed: nothing was exposed. Read one with `counsel replay <run>` to see the "
            "instruction that tried it, and tell the customer's account owner.",
            "security", None, 0.0, [r["run_id"] for r in refused[:3]]))

    shares: dict[str, list[dict]] = {}
    for r in rows["requesters"]:
        shares.setdefault(r["agent"], []).append(r)
    for agent, rs in shares.items():
        top = rs[0]
        share = top["share"] or 0
        if (top["requester"] == "schedule" or len(rs) < 3 or sum(r["runs"] for r in rs) < MIN_RUNS_FOR_SHARE
                or share < max(OUTSIZED_SHARE, 2 / len(rs))):
            continue
        findings.append(Finding(
            "cost", f"{top['requester']} drives {top['share']:.0%} of {agent} spend",
            f"{top['runs']} runs, {usd(top['usd'])}, against {len(rs) - 1} other requesters.",
            "Look at what they ask: a docs page or a product fix may remove the questions entirely. "
            "Not a saving the platform can make on its own.", owner(agent), agent))

    for r in rows["data_quality"]:
        findings.append(Finding(
            "data", f"{r['model']} ({r['provider']}): spend is estimated or unpriced",
            f"{r['estimated_calls']} calls had no usage from the provider (estimated at 4 characters a "
            f"token); {r['unpriced_calls']} have no price in counsel.model_prices.",
            "Add the model to counsel.model_prices; ask the provider adapter to return usage.",
            "platform"))

    agent_month = {r["agent"]: r["projected_month_usd"] for r in rows["by_agent"]}
    for f in findings:
        if f.monthly_usd and agent_month.get(f.agent):
            f.share = f.monthly_usd / agent_month[f.agent]

    order = {"security": 0, "budget": 1, "cost": 2, "waste": 2, "data": 3}
    findings.sort(key=lambda f: (order[f.kind], -f.monthly_usd))
    total = sum(r["usd"] for r in rows["by_agent"])
    return {
        "since": since.isoformat(), "until": until.isoformat(),
        "total_usd": total, "projected_month_usd": total * per_month,
        "runs": sum(r["runs"] for r in rows["by_agent"]),
        "saved_usd": sum(r["saved_usd"] for r in rows["by_agent"]),
        "agents": rows["by_agent"], "by_day": rows["by_day"], "outliers": rows["outliers"],
        "repeats": rows["repeats"],
        "findings": [asdict(f) for f in findings],
    }


def render(report: dict, title: str = "Agent spend report", note: str | None = None) -> str:
    since, until = report["since"][:10], report["until"][:10]
    out = [f"# {title}", "", f"Window: {since} to {until} (UTC).  "
           f"Runs: {report['runs']:,}.  Spend: **{usd(report['total_usd'])}** "
           f"(projected month: {usd(report['projected_month_usd'])}).  "
           f"Saved by cache: {usd(report['saved_usd'])}.", ""]
    if note:
        out += [f"> {note}", ""]

    out += ["## What to do", ""]
    actionable = [f for f in report["findings"] if f["kind"] != "data"]
    if not actionable:
        out += ["Nothing needs action.", ""]
    for i, f in enumerate(actionable, 1):
        worth = f" · worth ~{usd(f['monthly_usd'])}/month" if f["monthly_usd"] > 0 else ""
        if f["share"] and f["kind"] != "budget":
            worth += f" ({f['share']:.0%} of the agent's spend)"
        out += [f"### {i}. {f['title']}", "", f"*{f['kind']} · owner: {f['owner']}{worth}*", "",
                f"- **Evidence:** {f['evidence']}", f"- **Action:** {f['action']}"]
        if f["runs"]:
            out.append("- **Runs:** " + ", ".join(f"`{r}`" for r in f["runs"]))
        out.append("")

    out += ["## Spend by agent", "",
            "| Agent | Owner | Runs | Calls | Cache hits | Tokens in / out | Spend | Per 1k runs "
            "| p95 run | Month (proj.) | Budget |",
            "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for a in report["agents"]:
        budget = usd(a["budget_usd"]) if a["budget_usd"] else "none"
        flag = " **over**" if a["budget_usd"] and a["projected_month_usd"] > a["budget_usd"] else ""
        out.append(f"| {a['agent']} | {a['owner']} | {a['runs']:,} | {a['model_calls']:,} | {a['cache_hits']:,} | "
                   f"{a['input_tokens']:,} / {a['output_tokens']:,} | {usd(a['usd'])} | "
                   f"{usd(a['usd'] / a['runs'] * 1000)} | {usd(a['p95_run_usd'])} | "
                   f"{usd(a['projected_month_usd'])}{flag} | {budget} |")
    out.append("")

    if report["outliers"]:
        out += ["## Most expensive runs", "", "| Run | Agent | For | Status | Calls | Spend | × median |",
                "|---|---|---|---|--:|--:|--:|"]
        for o in report["outliers"]:
            out.append(f"| `{o['run_id']}` | {o['agent']} | {o['requester']} | {o['status']} | "
                       f"{o['model_calls']} | {usd(o['usd'])} | {o['times_median']:.1f} |")
        out += ["", "`counsel replay <run>` shows every step and prompt of a run.", ""]

    data = [f for f in report["findings"] if f["kind"] == "data"]
    if data:
        out += ["## Data quality", ""] + [f"- {f['title']}: {f['evidence']}" for f in data] + [""]
    out += ["---", "Generated by `counsel report` from counsel.model_calls; queries in `db/reports/`. "
            "Prices: counsel.model_prices."]
    return "\n".join(out) + "\n"


# --- tracing -----------------------------------------------------------------------


def _ns(t: datetime) -> str:
    return str(int(t.timestamp() * 1_000_000_000))


def trace(conn: psycopg.Connection, run_id: str) -> dict:
    """One run as OTLP/JSON: a root span for the run, a child per step.

    Model steps carry the GenAI semantic-convention attributes (model, token
    usage) plus the cost and, for a cache hit, the call it was served from.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        run = cur.execute(
            """select r.id::text, r.agent, r.status, r.started_at, r.finished_at, rc.usd::float, rc.requester
               from counsel.runs r join counsel.run_costs rc on rc.run_id = r.id where r.id = %s""",
            (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"no run {run_id}")
        steps = cur.execute("select seq, name, finished_at from counsel.steps where run_id = %s order by seq",
                            (run_id,)).fetchall()
        calls = {c["step_seq"]: c for c in cur.execute(
            """select step_seq, provider, model, input_tokens, output_tokens, usd::float, latency_ms,
                      cached_from, error from counsel.call_costs where run_id = %s and error is null""",
            (run_id,))}
    trace_id = run_id.replace("-", "")
    root = trace_id[:16]

    def attrs(d: dict) -> list[dict]:
        def val(v):
            if isinstance(v, bool):
                return {"boolValue": v}
            if isinstance(v, int):
                return {"intValue": str(v)}
            if isinstance(v, float):
                return {"doubleValue": v}
            return {"stringValue": str(v)}
        return [{"key": k, "value": val(v)} for k, v in d.items() if v is not None]

    end = run["finished_at"] or (steps[-1]["finished_at"] if steps else run["started_at"])
    spans = [{"traceId": trace_id, "spanId": root, "name": f"run {run['agent']}",
              "startTimeUnixNano": _ns(run["started_at"]), "endTimeUnixNano": _ns(end),
              "status": {"code": 1 if run["status"] == "succeeded" else 2},
              "attributes": attrs({"counsel.agent": run["agent"], "counsel.run_id": run_id,
                                   "counsel.status": run["status"],
                                   "counsel.requester": run["requester"], "counsel.cost_usd": run["usd"]})}]
    prev = run["started_at"]
    for s in steps:
        call = calls.get(s["seq"])
        a = {"counsel.step": s["name"]}
        start = prev
        if call:
            start = s["finished_at"] - timedelta(milliseconds=call["latency_ms"])
            a.update({"gen_ai.operation.name": "chat", "gen_ai.system": call["provider"],
                      "gen_ai.request.model": call["model"],
                      "gen_ai.usage.input_tokens": call["input_tokens"],
                      "gen_ai.usage.output_tokens": call["output_tokens"],
                      "counsel.cost_usd": call["usd"], "counsel.cached_from": call["cached_from"]})
        spans.append({"traceId": trace_id, "spanId": f"{root[:12]}{s['seq']:04x}", "parentSpanId": root,
                      "name": ("chat " + call["model"]) if call else s["name"],
                      "startTimeUnixNano": _ns(min(start, s["finished_at"])),
                      "endTimeUnixNano": _ns(s["finished_at"]), "attributes": attrs(a)})
        prev = s["finished_at"]
    return {"resourceSpans": [{
        "resource": {"attributes": attrs({"service.name": "counsel"})},
        "scopeSpans": [{"scope": {"name": "counsel.cost"}, "spans": spans}]}]}


def write(report: dict, out: Path, **render_args) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text(render(report, **render_args), encoding="utf-8")
