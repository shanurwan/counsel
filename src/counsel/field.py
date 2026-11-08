"""A field run: issue_triage on real repositories, scored against what maintainers did.

Shadow mode. Counsel reads public issues and writes triage drafts inside its own
database; nothing is posted to GitHub. Days later, this module asks GitHub what the
maintainers actually did with each issue (labels, closing as a duplicate, asking
the reporter for more) and compares it with the draft. Only GETs, with a read-only
token if one is set.

`counsel field --since <date>` writes docs/field-week.md and field/results.json.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psycopg

from .behaviors import REFERENCE, UNCHECKED_REFERENCE

API = "https://api.github.com"
MAINTAINERS = {"OWNER", "MEMBER", "COLLABORATOR"}
# A draft says something is missing unless it says nothing is.
NOTHING_MISSING = re.compile(r"\b(no|nothing|none)\b[^.\n]{0,40}\bmissing\b|\bmissing\b[^.\n]{0,15}\bnone\b|"
                             r"\bnothing (else )?(is )?(needed|required)\b", re.I)
SAYS_MISSING = re.compile(r"\bmissing\b|\bnot (provided|included|given|stated|specified)\b|\bwithout\b[^.\n]{0,30}"
                          r"\b(version|steps|logs?|reproduction|repro|error)\b", re.I)
# A maintainer asking the reporter for more.
ASKS_FOR_MORE = re.compile(r"\b(can|could|would) you (please )?(share|provide|post|send|add|include|confirm|try)\b|"
                           r"\bplease (share|provide|post|send|add|include)\b|\breproduc|\bminimal (repro|example)\b|"
                           r"\bwhich version\b|\bwhat version\b|\bmore (details|info|information|context)\b|"
                           r"\bsteps to\b|\blogs?\b", re.I)
NEEDS_INFO_LABEL = re.compile(r"need|info|repro|clarif|question|waiting", re.I)

Get = Callable[[str], object]


def _get(url: str) -> object:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "counsel-field-run"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("COUNSEL_GITHUB_READ_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    # Fixed https host in this module, never caller input.
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310
        return json.loads(resp.read() or b"null")


@dataclass
class Scored:
    repo: str
    number: int
    title: str
    url: str
    note: str
    drafted_at: str
    # What the draft said
    claimed_duplicates: list[int]
    says_missing: bool
    # What maintainers did
    labels: list[str] = field(default_factory=list)
    state: str = "open"
    state_reason: str | None = None
    marked_duplicate_of: list[int] = field(default_factory=list)
    maintainer_replied: bool = False
    maintainer_asked_for_more: bool = False
    first_reply_hours: float | None = None
    error: str | None = None

    @property
    def truly_duplicate(self) -> bool:
        return self.state_reason == "duplicate" or bool(self.marked_duplicate_of)


def read_note(note: str, number: int) -> tuple[list[int], bool]:
    """The duplicate the draft names (code-checked ones only) and whether it says something is missing."""
    unchecked = UNCHECKED_REFERENCE.split("{")[0] in note
    claimed = [] if unchecked else sorted({int(n) for n in REFERENCE.findall(note)} - {number})
    missing = bool(SAYS_MISSING.search(note)) and not NOTHING_MISSING.search(note)
    return claimed, missing


def drafts(conn: psycopg.Connection, since: datetime) -> list[dict]:
    with conn.transaction():
        conn.execute("set transaction read only")
        return [dict(zip(("repo", "number", "title", "url", "opened_at", "note", "drafted_at"), r)) for r in conn.execute(
            """select i.repo, i.number, i.title, i.url, i.opened_at, d.body, d.created_at
               from counsel.drafts d
               join work.issues i on d.kind = 'issue_triage:' || i.repo || '#' || i.number
               where d.created_at >= %s
               order by i.repo, i.number""", (since,))]


def score(draft: dict, get: Get = _get) -> Scored:
    claimed, missing = read_note(draft["note"], draft["number"])
    s = Scored(repo=draft["repo"], number=draft["number"], title=draft["title"], url=draft["url"],
               note=draft["note"], drafted_at=str(draft["drafted_at"]), claimed_duplicates=claimed,
               says_missing=missing)
    base = f"{API}/repos/{draft['repo']}/issues/{draft['number']}"
    try:
        issue = get(base)
        s.labels = [label["name"] for label in issue.get("labels") or []]
        s.state, s.state_reason = issue.get("state", "open"), issue.get("state_reason")
        opened = datetime.fromisoformat(str(issue["created_at"]).replace("Z", "+00:00"))
        for comment in get(f"{base}/comments?per_page=100") or []:
            if comment.get("author_association") not in MAINTAINERS:
                continue
            if not s.maintainer_replied:
                at = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
                s.first_reply_hours = round((at - opened).total_seconds() / 3600, 1)
            s.maintainer_replied = True
            s.maintainer_asked_for_more |= bool(ASKS_FOR_MORE.search(comment.get("body") or ""))
        if any(NEEDS_INFO_LABEL.search(label) for label in s.labels):
            s.maintainer_asked_for_more = True
        for event in get(f"{base}/timeline?per_page=100") or []:
            if event.get("event") == "marked_as_duplicate":
                canonical = (event.get("source") or {}).get("issue", {}).get("number")
                if canonical:
                    s.marked_duplicate_of.append(int(canonical))
    except (urllib.error.URLError, OSError, KeyError, ValueError, AttributeError) as e:
        s.error = f"{type(e).__name__}: {e}"[:200]
    return s


def summarise(rows: list[Scored]) -> dict:
    ok = [r for r in rows if r.error is None]
    acted = [r for r in ok if r.maintainer_replied or r.labels or r.state == "closed"]
    claimed = [r for r in ok if r.claimed_duplicates]
    truly = [r for r in ok if r.truly_duplicate]
    confirmed = [r for r in claimed if r.truly_duplicate]
    asked = [r for r in ok if r.maintainer_asked_for_more]
    flagged = [r for r in ok if r.says_missing]
    return {
        "triaged": len(rows), "scored": len(ok), "maintainers_acted": len(acted),
        "duplicates": {"claimed": len(claimed), "confirmed": len(confirmed), "maintainers_closed_as_duplicate": len(truly),
                       "missed": len([r for r in truly if not r.claimed_duplicates])},
        "missing_info": {
            "maintainer_asked": len(asked),
            "draft_flagged": len(flagged),
            "both": len([r for r in asked if r.says_missing]),
            "draft_only": len([r for r in flagged if not r.maintainer_asked_for_more and r in acted]),
            "maintainer_only": len([r for r in asked if not r.says_missing]),
        },
    }


def _cell(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return (text if len(text) <= limit else text[:limit - 1] + "…").replace("|", "\\|")


def render(rows: list[Scored], summary: dict, since: datetime, until: datetime) -> str:
    d, m = summary["duplicates"], summary["missing_info"]
    repos = sorted({r.repo for r in rows})
    lines = [
        "# Field run: issue triage on real repositories",
        "",
        f"From {since:%d %b %Y} to {until:%d %b %Y %H:%M} UTC, Counsel's `issue_triage` agent read every new issue "
        f"on {', '.join(f'`{r}`' for r in repos)} and wrote a triage draft. **Shadow mode:** nothing was posted to "
        "GitHub, labelled or assigned; the drafts stayed in Counsel's database. Then each draft was compared with "
        "what the maintainers actually did. Generated by `counsel field`.",
        "",
        "## What it found",
        "",
        f"- **{summary['triaged']} issues triaged**, {summary['maintainers_acted']} of them acted on by a maintainer "
        "so far (a reply, a label or closing it).",
        f"- **Duplicates:** the drafts named a duplicate on {d['claimed']} issues; maintainers agreed on "
        f"{d['confirmed']}. Maintainers closed {d['maintainers_closed_as_duplicate']} as duplicates; the drafts "
        f"missed {d['missed']} of those.",
        f"- **Missing information:** maintainers asked a reporter for more on {m['maintainer_asked']} issues; the "
        f"draft had already flagged something missing on {m['both']} of them. On {m['draft_only']} issues a "
        "maintainer acted without asking for more although the draft said something was missing.",
        "",
        "How the comparison is made: a duplicate counts when GitHub records the issue as closed as a duplicate or "
        "marked as one. A maintainer is the owner, a member or a collaborator. \"Asked for more\" is read from their "
        "comments and labels with a pattern, so read the table below rather than trusting the counts alone.",
        "",
        "## Every issue",
        "",
        "| Issue | The draft said | What maintainers did |",
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
        lines.append(f"| [{r.repo}#{r.number}]({r.url}) {_cell(r.title, 80)} | {_cell(r.note)} | "
                     f"{'; '.join(did) or 'nothing yet'} |")
    lines += ["", "## Limits", "",
              "- One week and a handful of repositories: a field check, not a benchmark.",
              "- Maintainers had not seen the drafts, so this measures agreement with what they did anyway, not "
              "whether the drafts would have helped them.",
              "- Issues still untouched when the report ran count as \"nothing yet\", not as a miss.", ""]
    return "\n".join(lines)


def run(conn: psycopg.Connection, since: datetime, out: Path, doc: Path, get: Get = _get,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    rows = [score(d, get) for d in drafts(conn, since)]
    summary = summarise(rows)
    until = now()
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(
        {"since": since.isoformat(), "until": until.isoformat(), "summary": summary,
         "issues": [{**asdict(r), "truly_duplicate": r.truly_duplicate} for r in rows]},
        indent=2, default=str) + "\n", encoding="utf-8")
    doc.write_text(render(rows, summary, since, until), encoding="utf-8", newline="\n")
    return summary
