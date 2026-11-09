"""A replay of issue_triage on issues maintainers have already handled.

The live field run (field.py) can only score issues opened since watching began, and a
quiet repository gives a handful a week. A replay goes back: for every issue opened in a
window by someone outside the repository's team, it rebuilds what the live agent would
have seen at the moment the issue opened, asks the same model with the same prompt and
the same code-checked duplicate references, and scores the note against what the
maintainers went on to do (field.score).

What the agent sees, and what it does not:

- the issue's title, body and author, as GitHub serves them today (a later edit by the
  reporter would show; GitHub does not keep the original body);
- no labels: on an old issue they were mostly added by maintainers afterwards, which is
  the answer, not the question;
- as duplicate candidates, only issues opened before it and still open at that moment,
  from the `pool_days` before the window, ranked by the live agent's own similarity
  (behaviors.similar_issues).

Nothing is posted to GitHub and nothing is stored in Counsel's database. Only GETs.
`counsel field-replay --repo supabase/auth --repo ... --since ... --until ...`
writes field/replay.json and docs/field-replay.md.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import field
from .behaviors import TRIAGE_SYSTEM, check_references, similar_issues
from .evals import wilson

API = field.API


def _time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch(repo: str, since: datetime, until: datetime, pool_days: int, get: field.Get) -> list[dict]:
    """Every issue (not PR) created from pool start to `until`, newest first, paged by creation."""
    start = since - timedelta(days=pool_days)
    out: list[dict] = []
    page = 1
    while True:
        batch = get(f"{API}/repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=100&page={page}") or []
        if not batch:
            break
        for i in batch:
            if "pull_request" in i:
                continue
            created = _time(i["created_at"])
            if start <= created < until:
                out.append(i)
        if _time(batch[-1]["created_at"]) < start or len(batch) < 100:
            break
        page += 1
    return out


def open_at(issue: dict, moment: datetime) -> bool:
    if _time(issue["created_at"]) >= moment:
        return False
    closed = issue.get("closed_at")
    return closed is None or _time(closed) > moment


def as_seen(issue: dict, pool: list[dict]) -> dict:
    """The live agent's input (behaviors._issue) as it would have been when `issue` opened."""
    moment = _time(issue["created_at"])
    others = [(o["number"], o["title"], o.get("body") or "") for o in pool
              if o["number"] != issue["number"] and open_at(o, moment)]
    return {"number": issue["number"], "title": issue["title"], "body": (issue.get("body") or "")[:8000],
            "author": issue["user"]["login"], "labels": [],
            "similar": similar_issues(issue["title"], issue.get("body") or "", others)}


def candidates(issues: list[dict], since: datetime) -> list[dict]:
    """Issues in the window from outside the repository's team. A maintainer's own issue is
    their to-do list, not a report to triage; a bot's is not a person's."""
    return [i for i in issues if _time(i["created_at"]) >= since
            and i.get("author_association") not in field.MAINTAINERS
            and i["user"].get("type") != "Bot"]


def run(repos: list[str], since: datetime, until: datetime, model, out: Path, doc: Path, *,
        pool_days: int = 90, get: field.Get = field._get, pause: float = 0.0,
        now: Callable[[], datetime] | None = None, log: Callable[[str], None] = print) -> dict:
    rows: list[field.Scored] = []
    for repo in repos:
        pool = fetch(repo, since, until, pool_days, get)
        todo = sorted(candidates(pool, since), key=lambda i: i["number"])
        log(f"{repo}: {len(todo)} issues to replay, {len(pool)} in the pool")
        for issue in todo:
            seen = as_seen(issue, pool)
            text = model.complete([{"role": "system", "content": TRIAGE_SYSTEM},
                                   {"role": "user", "content": json.dumps(seen, sort_keys=True)}], max_tokens=300).text
            note = check_references(text, seen)
            rows.append(field.score({"repo": repo, "number": issue["number"], "title": issue["title"],
                                     "url": issue["html_url"], "note": note, "drafted_at": issue["created_at"]}, get))
            if pause:
                time.sleep(pause)
    summary = summarise(rows)
    out.mkdir(parents=True, exist_ok=True)
    stamp = (now or (lambda: datetime.now(since.tzinfo)))()
    (out / "replay.json").write_text(json.dumps(
        {"repos": repos, "since": since.isoformat(), "until": until.isoformat(), "pool_days": pool_days,
         "generated": stamp.isoformat(), "summary": summary,
         "issues": [{**asdict(r), "truly_duplicate": r.truly_duplicate} for r in rows]},
        indent=2, default=str) + "\n", encoding="utf-8")
    doc.write_text(render(rows, summary, repos, since, until, pool_days, stamp), encoding="utf-8", newline="\n")
    return summary


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "lower": round(wilson(k, n)[0], 3) if n else None}


def summarise(rows: list[field.Scored]) -> dict:
    base = field.summarise(rows)
    ok = [r for r in rows if r.error is None]
    claimed = [r for r in ok if r.claimed_duplicates]
    truly = [r for r in ok if r.truly_duplicate]
    asked = [r for r in ok if r.maintainer_asked_for_more]
    return {**base,
            # Of the duplicates the notes named, how many maintainers confirmed.
            "duplicate_precision": _rate(len([r for r in claimed if r.truly_duplicate]), len(claimed)),
            # Of the issues maintainers closed as duplicates, how many notes had named one.
            "duplicate_recall": _rate(len([r for r in truly if r.claimed_duplicates]), len(truly)),
            # Of the issues where a maintainer asked the reporter for more, how many notes had said so first.
            "missing_recall": _rate(len([r for r in asked if r.says_missing]), len(asked))}


def _pct(rate: dict) -> str:
    if not rate["n"]:
        return "none to score"
    return f"**{rate['k']} of {rate['n']}** (lower bound {rate['lower']:.0%})"


def render(rows: list[field.Scored], s: dict, repos: list[str], since: datetime, until: datetime,
           pool_days: int, stamp: datetime) -> str:
    cell = field._cell
    lines = [
        "# Replay: issue triage against what maintainers did",
        "",
        f"Every issue opened from {since:%d %b} to {until:%d %b %Y} on {', '.join(f'`{r}`' for r in repos)} by "
        "someone outside the repository's team, replayed through Counsel's `issue_triage` as if it had just "
        "opened, then compared with what the maintainers did afterwards. Nothing was posted. Generated by "
        f"`counsel field replay` on {stamp:%d %b %Y %H:%M} UTC.",
        "",
        "## What it found",
        "",
        f"- **{s['triaged']} issues replayed**; maintainers had acted on {s['maintainers_acted']} (a reply, a "
        "label or closing it).",
        f"- **Duplicates named:** {_pct(s['duplicate_precision'])} confirmed by maintainers.",
        f"- **Duplicates caught:** {_pct(s['duplicate_recall'])} of the issues maintainers closed as duplicates "
        "had one named in the note.",
        f"- **Missing information:** where a maintainer asked the reporter for more, the note had already said "
        f"something was missing on {_pct(s['missing_recall'])}.",
        "",
        "Lower bounds are Wilson, 95%: with this few issues, the bound is the honest number. A duplicate counts "
        "when GitHub records it as closed or marked as one. \"Asked for more\" is read from maintainers' comments "
        "and labels with a pattern, so read the table rather than trusting the counts alone.",
        "",
        "## How the replay avoids hindsight",
        "",
        "- The agent sees the title, body and author, not the labels, which maintainers mostly added later.",
        f"- Duplicate candidates are only issues opened before it and still open at that moment, from the "
        f"{pool_days} days before the window, ranked by the live agent's own similarity code.",
        "- The prompt, the model and the code check on issue numbers are the live agent's.",
        "",
        "## Every issue",
        "",
        "| Issue | The note said | What maintainers did |",
        "|---|---|---|",
    ]
    for r in rows:
        did = []
        if r.error:
            did.append(f"could not read: {r.error}")
        if r.labels:
            did.append("labels " + ", ".join(f"`{label}`" for label in r.labels))
        if r.truly_duplicate:
            did.append("closed as duplicate" + (f" of #{r.marked_duplicate_of[0]}" if r.marked_duplicate_of else ""))
        elif r.state == "closed":
            did.append(f"closed ({r.state_reason or 'no reason'})")
        if r.maintainer_replied:
            did.append(f"replied after {r.first_reply_hours} h" + (", asked for more" if r.maintainer_asked_for_more
                                                                     else ""))
        lines.append(f"| [{r.repo}#{r.number}]({r.url}) {cell(r.title, 70)} | {cell(r.note, 260)} | "
                     f"{'; '.join(did) or 'nothing yet'} |")
    lines += ["", "## Limits", "",
              "- A replay, not a live run: maintainers never saw these notes, so this measures agreement with what "
              "they did anyway, not whether the notes would have helped them.",
              "- The body is as GitHub serves it today; an edit after the issue opened would show.",
              "- One model run per issue. A rerun can word a note differently.", ""]
    return "\n".join(lines)
