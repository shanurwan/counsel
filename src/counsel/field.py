"""A field run: issue_triage on real repositories, scored against what maintainers did.

Shadow mode. Counsel reads public issues and writes triage drafts inside its own
database; nothing is posted to GitHub. Days later, this module asks GitHub what the
maintainers actually did with each issue (labels, closing as a duplicate, asking
the reporter for more) and compares it with the draft. Only GETs, with a read-only
token if one is set.

On repositories that installed the Counsel Triage app, the note was delivered to the
maintainers (githubapp.deliver). There the report also reads their verdict on it:
the 👍 and 👎 on the note's comment and their replies on the notes issue. The
author's own reactions are shown but never counted.

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

from .behaviors import UNCHECKED_REFERENCE, duplicate_references  # noqa: F401  (re-exported)
from .evals import wilson

API = "https://api.github.com"
MAINTAINERS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Counsel's author: their reactions and replies show the loop works, but never count.
AUTHOR = os.environ.get("COUNSEL_AUTHOR", "shanurwan").lower()
ACTIONS = {"labeled", "closed", "marked_as_duplicate", "assigned"}
COMMENT_ID = re.compile(r"#issuecomment-(\d+)$")
MENTION = re.compile(r"#(\d+)\b")
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
    # pending, published or rejected: what the repository's owner did with the draft,
    # on repositories where they review drafts (their own, or one that installed the app).
    review: str = "pending"
    # Who, other than the author, replied, labelled, closed or assigned on the issue.
    acted_by: list[str] = field(default_factory=list)
    # Delivered through the triage app: the note's comment and the maintainers' verdict on it.
    run_id: str | None = None
    delivered_to: str | None = None
    thumbs_up: list[str] = field(default_factory=list)
    thumbs_down: list[str] = field(default_factory=list)
    author_reactions: list[str] = field(default_factory=list)
    replies: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def verdict(self) -> str | None:
        """helpful, not helpful, or None while nobody but the author has rated it."""
        if not (self.thumbs_up or self.thumbs_down):
            return None
        return "helpful" if len(self.thumbs_up) > len(self.thumbs_down) else "not helpful"

    @property
    def truly_duplicate(self) -> bool:
        return self.state_reason == "duplicate" or bool(self.marked_duplicate_of)


def read_note(note: str, number: int) -> tuple[list[int], bool]:
    """The duplicates the draft names, read as a maintainer reads them, and whether it says
    something is missing.

    Only a reference a clause calls a duplicate counts: 'Related: #12' points the reader at
    something, it does not claim the issue is the same problem. A reviewer flag at the top of
    the note does not hide a claim below it: what the maintainer reads is what is scored."""
    claimed = duplicate_references(note, number)
    missing = bool(SAYS_MISSING.search(note)) and not NOTHING_MISSING.search(note)
    return claimed, missing


def drafts(conn: psycopg.Connection, since: datetime) -> list[dict]:
    with conn.transaction():
        conn.execute("set transaction read only")
        return [dict(zip(("repo", "number", "title", "url", "opened_at", "note", "drafted_at", "review",
                          "run_id", "delivered_to", "notes_issue"), r))
                for r in conn.execute(
            """select i.repo, i.number, i.title, i.url, i.opened_at, d.body, d.created_at, d.status,
                      d.run_id::text, x.comment_url, n.notes_issue
               from counsel.drafts d
               join work.issues i on d.kind = 'issue_triage:' || i.repo || '#' || i.number
               left join counsel.deliveries x on x.draft_id = d.id
               left join counsel.installations n on n.repo = i.repo
               where d.created_at >= %s
               order by i.repo, i.number""", (since,))]


def score(draft: dict, get: Get = _get) -> Scored:
    claimed, missing = read_note(draft["note"], draft["number"])
    s = Scored(repo=draft["repo"], number=draft["number"], title=draft["title"], url=draft["url"],
               note=draft["note"], drafted_at=str(draft["drafted_at"]), claimed_duplicates=claimed,
               says_missing=missing, review=draft.get("review") or "pending",
               run_id=draft.get("run_id"), delivered_to=draft.get("delivered_to"))
    note_start = " ".join(draft["note"].split())[:80]
    base = f"{API}/repos/{draft['repo']}/issues/{draft['number']}"
    try:
        issue = get(base)
        s.labels = [label["name"] for label in issue.get("labels") or []]
        s.state, s.state_reason = issue.get("state", "open"), issue.get("state_reason")
        opened = datetime.fromisoformat(str(issue["created_at"]).replace("Z", "+00:00"))
        for comment in get(f"{base}/comments?per_page=100") or []:
            if comment.get("author_association") not in MAINTAINERS:
                continue
            if note_start and note_start in " ".join((comment.get("body") or "").split()):
                continue  # the draft itself, published: not a maintainer acting on the issue
            if not s.maintainer_replied:
                at = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))
                s.first_reply_hours = round((at - opened).total_seconds() / 3600, 1)
            s.maintainer_replied = True
            s.maintainer_asked_for_more |= bool(ASKS_FOR_MORE.search(comment.get("body") or ""))
            _actor(s.acted_by, comment.get("user"))
        if any(NEEDS_INFO_LABEL.search(label) for label in s.labels):
            s.maintainer_asked_for_more = True
        for event in get(f"{base}/timeline?per_page=100") or []:
            if event.get("event") in ACTIONS:
                _actor(s.acted_by, event.get("actor"))
            if event.get("event") == "marked_as_duplicate":
                canonical = (event.get("source") or {}).get("issue", {}).get("number")
                if canonical:
                    s.marked_duplicate_of.append(int(canonical))
    except (urllib.error.URLError, OSError, KeyError, ValueError, AttributeError) as e:
        s.error = f"{type(e).__name__}: {e}"[:200]
    return s


def _comment_id(url: str) -> int:
    m = COMMENT_ID.search(url)
    return int(m.group(1)) if m else 0


def _person(user: dict | None) -> str | None:
    """A person's login, or None for a bot (the app itself included)."""
    if not user or user.get("type") == "Bot" or not user.get("login"):
        return None
    return user["login"]


def _actor(into: list[str], user: dict | None) -> None:
    login = _person(user)
    if login and login.lower() != AUTHOR and login not in into:
        into.append(login)


def verdicts(rows: list[Scored], notes_issues: dict[str, int], get: Get = _get) -> None:
    """Read the maintainers' verdict on each delivered note: reactions on its comment,
    and replies on the notes issue. A reply goes to the note whose issue it mentions
    (#123), else to the latest note delivered before it."""
    delivered = [r for r in rows if r.delivered_to and _comment_id(r.delivered_to)]
    for r in delivered:
        try:
            for reaction in get(f"{API}/repos/{r.repo}/issues/comments/{_comment_id(r.delivered_to)}/reactions"
                                "?per_page=100") or []:
                login, kind = _person(reaction.get("user")), reaction.get("content")
                if not login or kind not in ("+1", "-1"):
                    continue
                if login.lower() == AUTHOR:
                    r.author_reactions.append("👍" if kind == "+1" else "👎")
                else:
                    (r.thumbs_up if kind == "+1" else r.thumbs_down).append(login)
        except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
            r.error = r.error or f"reactions: {type(e).__name__}: {e}"[:200]
    for repo, notes in notes_issues.items():
        mine = sorted((r for r in delivered if r.repo == repo), key=lambda r: _comment_id(r.delivered_to))
        if not mine:
            continue
        try:
            comments = get(f"{API}/repos/{repo}/issues/{notes}/comments?per_page=100") or []
        except (urllib.error.URLError, OSError, ValueError) as e:
            for r in mine:
                r.error = r.error or f"notes issue: {type(e).__name__}: {e}"[:200]
            continue
        for c in comments:
            login = _person(c.get("user"))
            if not login or login.lower() == AUTHOR:
                continue
            body = c.get("body") or ""
            mentioned = {int(n) for n in MENTION.findall(body)}
            target = next((r for r in mine if r.number in mentioned), None)
            if target is None:
                earlier = [r for r in mine if _comment_id(r.delivered_to) < int(c.get("id") or 0)]
                target = earlier[-1] if earlier else None
            if target is not None:
                target.replies.append({"by": login, "association": c.get("author_association"),
                                       "body": " ".join(body.split())[:300], "url": c.get("html_url")})


def summarise(rows: list[Scored]) -> dict:
    ok = [r for r in rows if r.error is None]
    acted = [r for r in ok if r.maintainer_replied or r.labels or r.state == "closed"]
    claimed = [r for r in ok if r.claimed_duplicates]
    truly = [r for r in ok if r.truly_duplicate]
    confirmed = [r for r in claimed if r.truly_duplicate]
    asked = [r for r in ok if r.maintainer_asked_for_more]
    flagged = [r for r in ok if r.says_missing]
    reviewed = [r for r in rows if r.review in ("published", "rejected")]
    delivered = [r for r in rows if r.delivered_to]
    rated = [r for r in delivered if r.verdict]
    helpful = [r for r in rated if r.verdict == "helpful"]
    # A user: someone other than the author who rated, answered or acted on a note delivered to them.
    users: dict[str, set[str]] = {}
    for r in delivered:
        for login in [*r.thumbs_up, *r.thumbs_down, *(x["by"] for x in r.replies), *r.acted_by]:
            users.setdefault(login, set()).add(r.repo)
    return {
        "delivered": {"notes": len(delivered), "repos": sorted({r.repo for r in delivered}), "rated": len(rated),
                      "helpful": len(helpful), "not_helpful": len(rated) - len(helpful),
                      "helpful_lower_bound": round(wilson(len(helpful), len(rated))[0], 3) if rated else None,
                      "replies": sum(len(r.replies) for r in delivered),
                      "author_reactions": sum(len(r.author_reactions) for r in delivered),
                      "users": {login: sorted(repos) for login, repos in sorted(users.items())}},
        "triaged": len(rows), "scored": len(ok), "maintainers_acted": len(acted),
        "owner_review": {"reviewed": len(reviewed), "published": len([r for r in reviewed if r.review == "published"]),
                         "rejected": len([r for r in reviewed if r.review == "rejected"]),
                         "repos": sorted({r.repo for r in reviewed})},
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


def _verdict_section(rows: list[Scored], v: dict) -> list[str]:
    """The maintainers' verdict on notes delivered through the triage app."""
    if not v["notes"]:
        return []
    bound = f", lower bound {v['helpful_lower_bound']:.0%} (Wilson, 95%)" if v["rated"] else ""
    users = v["users"]
    lines = [
        "## What maintainers said about the notes",
        "",
        f"The triage app delivered **{v['notes']} notes** to {len(v['repos'])} "
        f"repositor{'y' if len(v['repos']) == 1 else 'ies'}. Maintainers rated {v['rated']}: **{v['helpful']} "
        f"helpful**, {v['not_helpful']} not helpful{bound}. They replied {v['replies']} times on the notes issues.",
        "",
        f"**Users: {len(users)}.** A user is someone other than Counsel's author who rated, answered or acted on a "
        "note delivered to them. Installs, stars and page views do not count."
        + ("" if not users else " " + "; ".join(f"`{u}` on {', '.join(repos)}" for u, repos in users.items()) + "."),
        "",
    ]
    if v["author_reactions"]:
        lines += [f"The author reacted {v['author_reactions']} times on their own notes, to test the loop; "
                  "those are not counted.", ""]
    lines += ["| Note | Verdict | Replies |", "|---|---|---|"]
    for r in (r for r in rows if r.delivered_to):
        votes = " ".join(["👍"] * len(r.thumbs_up) + ["👎"] * len(r.thumbs_down)) or "not rated"
        said = "<br>".join(f"`{x['by']}`: {_cell(x['body'], 160)}" for x in r.replies) or "none"
        lines.append(f"| [{r.repo}#{r.number}]({r.delivered_to}) {_cell(r.title, 60)} | {votes} | {said} |")
    down = [r for r in rows if r.verdict == "not helpful"]
    if down:
        lines += ["", "Each note rated not helpful becomes a test case. Write down what it should have said, then:",
                  "", "```"]
        lines += [f"counsel challenge {r.run_id} --id field_{re.sub(r'[^a-z0-9]+', '_', r.repo.lower())}_{r.number} "
                  "--must-contain \"<what the note should have said>\"" for r in down if r.run_id]
        lines += ["```"]
    return lines + [""]


def render(rows: list[Scored], summary: dict, since: datetime, until: datetime) -> str:
    d, m, o = summary["duplicates"], summary["missing_info"], summary["owner_review"]
    repos = sorted({r.repo for r in rows})
    lines = [
        "# Field run: issue triage on real repositories",
        "",
        f"From {since:%d %b %Y} to {until:%d %b %Y %H:%M} UTC, Counsel's `issue_triage` agent read every new issue "
        f"on {', '.join(f'`{r}`' for r in repos)} and wrote a triage draft. On the author's own repositories the "
        "owner reviews each draft and publishes or rejects it. Everywhere else it is **shadow mode**: nothing is "
        "posted to GitHub, labelled or assigned, and the drafts stay in Counsel's database. Then each draft is "
        "compared with what the maintainers actually did. Generated by `counsel field`.",
        "",
        "## What it found",
        "",
        f"- **{summary['triaged']} issues triaged**, {summary['maintainers_acted']} of them acted on by a maintainer "
        "so far (a reply, a label or closing it).",
        f"- **Reviewed by the repository's owner:** {o['reviewed']} drafts on "
        f"{', '.join(f'`{r}`' for r in o['repos']) or 'no repository yet'}: {o['published']} published as written, "
        f"{o['rejected']} rejected. Only owners who review drafts count here: the author on their own repositories, "
        "and maintainers who installed the triage app.",
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
        *_verdict_section(rows, summary["delivered"]),
        "## Every issue",
        "",
        "| Issue | The draft said | Owner's review | What maintainers did |",
        "|---|---|---|---|",
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
        lines.append(f"| [{r.repo}#{r.number}]({r.url}) {_cell(r.title, 80)} | {_cell(r.note)} | {r.review} | "
                     f"{'; '.join(did) or 'nothing yet'} |")
    lines += ["", "## Limits", "",
              "- One week and a handful of repositories: a field check, not a benchmark.",
              "- In shadow mode maintainers never saw the drafts, so there it measures agreement with what they did "
              "anyway, not whether the drafts would have helped. Only the notes delivered through the app were seen.",
              "- Issues still untouched when the report ran count as \"nothing yet\", not as a miss.",
              "- Anyone can react on a public issue, so a 👍 is not proof the reactor maintains the repository: "
              "every rater is named above so you can check. A tie counts as not helpful.", ""]
    return "\n".join(lines)


def run(conn: psycopg.Connection, since: datetime, out: Path, doc: Path, get: Get = _get,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    found = drafts(conn, since)
    rows = [score(d, get) for d in found]
    verdicts(rows, {d["repo"]: d["notes_issue"] for d in found if d.get("delivered_to") and d.get("notes_issue")}, get)
    summary = summarise(rows)
    until = now()
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(
        {"since": since.isoformat(), "until": until.isoformat(), "summary": summary,
         "issues": [{**asdict(r), "truly_duplicate": r.truly_duplicate, "verdict": r.verdict} for r in rows]},
        indent=2, default=str) + "\n", encoding="utf-8")
    doc.write_text(render(rows, summary, since, until), encoding="utf-8", newline="\n")
    return summary
