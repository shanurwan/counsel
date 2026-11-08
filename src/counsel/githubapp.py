"""The Counsel Triage GitHub App: deliver triage notes to maintainers who installed it.

Installing the app on a repository is the maintainer's consent, recorded by GitHub.
Each tick the core runtime:

1. asks GitHub which repositories the app is installed on, and records installs and
   removals (counsel.installations);
2. watches those repositories like any other (github.sync, read with the
   installation's own token);
3. posts each new triage draft for them, once, as a comment on a single notes issue
   in that repository (counsel.deliveries). Never on the reporter's issue: the note
   is for the maintainer, who decides what to do with it.

The app is off unless GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY are set. An
installation token reaches only the repositories that installed the app, and this
module only reads issues, creates the notes issue and comments on it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import psycopg

from .compiler import ORCHESTRATOR_ROLE
from .github import API, GitHub, GitHubError, Response, Transport, _urllib
from .log import log

NOTES_TITLE = "Counsel triage notes"
NOTES_BODY = (
    "This issue collects triage notes from the **Counsel Triage** app, installed on this repository.\n\n"
    "For each new issue, Counsel reads it and writes a short note for the maintainers: what is reported, the "
    "area, what information is missing, and a possible duplicate it checked exists. Notes are posted here only, "
    "never on the reporter's issue, and Counsel never labels, assigns or closes anything.\n\n"
    "React 👍 or 👎 on a note to say whether it helped. Uninstall the app to stop: "
    "Settings → GitHub Apps → Counsel Triage → Uninstall.\n\n"
    "How it works and what is stored: https://shanurwan.github.io/counsel/triage.html"
)
MAX_DELIVERIES_PER_TICK = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class App:
    app_id: str
    private_key: str
    transport: Transport = _urllib
    clock: Callable[[], float] = field(default=time.time)

    @classmethod
    def from_env(cls) -> "App | None":
        app_id, key = os.environ.get("GITHUB_APP_ID"), os.environ.get("GITHUB_APP_PRIVATE_KEY")
        return cls(app_id, key) if app_id and key else None

    def _jwt(self) -> str:
        import jwt

        now = int(self.clock())
        # Backdated a minute for clock drift; GitHub allows at most ten minutes.
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key,
                          algorithm="RS256")

    def _call(self, method: str, path: str, token: str, data: bytes | None = None) -> Response:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "counsel-triage", "Authorization": f"Bearer {token}"}
        resp = self.transport(method, f"{API}{path}", headers, data, 15.0)
        if resp.status >= 400:
            raise GitHubError(f"{method} {path}: {resp.status}")
        return resp

    def installed_repos(self) -> list[tuple[str, int, str, str]]:
        """(repo, installation id, account, installed at) for every repository the app may read."""
        out = []
        for inst in self._call("GET", "/app/installations?per_page=100", self._jwt()).body or []:
            token = self.token(inst["id"])
            repos = self._call("GET", "/installation/repositories?per_page=100", token).body or {}
            for repo in repos.get("repositories", []):
                # Public repositories only: issue text goes to a hosted model and into
                # Counsel's database, which a private repository's owner may not want.
                if repo.get("private"):
                    continue
                out.append((repo["full_name"], inst["id"], inst["account"]["login"], inst["created_at"]))
        return out

    def token(self, installation_id: int) -> str:
        resp = self._call("POST", f"/app/installations/{installation_id}/access_tokens", self._jwt(), b"{}")
        return resp.body["token"]


def refresh(conn: psycopg.Connection, app: App, now: datetime) -> dict:
    """Record installs and removals. A removed repository is no longer watched or written to."""
    seen = app.installed_repos()
    with conn.transaction():
        conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
        for repo, inst, account, installed_at in seen:
            conn.execute(
                """insert into counsel.installations (repo, installation_id, account, installed_at)
                   values (%s, %s, %s, %s)
                   on conflict (repo) do update set installation_id = excluded.installation_id,
                     account = excluded.account, removed_at = null""",
                (repo, inst, account, installed_at))
        removed = conn.execute(
            """update counsel.installations set removed_at = %s
               where removed_at is null and not (repo = any(%s)) returning repo""",
            (now, [r for r, *_ in seen])).fetchall()
    return {"installed": len(seen), "removed": [r for (r,) in removed]}


def readers(conn: psycopg.Connection, app: App) -> dict[str, GitHub]:
    """A client per installed repository, reading with that installation's token."""
    tokens: dict[int, str] = {}
    out = {}
    for repo, inst in installed(conn).items():
        try:
            tokens.setdefault(inst, app.token(inst))
            out[repo] = GitHub(token=tokens[inst])
        except GitHubError as e:
            log("WARNING", "no token for an installation; skipped this tick", repo=repo, error=str(e)[:200])
    return out


def tick(conn: psycopg.Connection, app: App | None, now: datetime) -> dict:
    """Record installs before the sync, so a new install is watched on the same tick."""
    if app is None:
        return {"app": "off"}
    try:
        return refresh(conn, app, now)
    except GitHubError as e:
        log("WARNING", "could not list installations; the next tick retries", error=str(e)[:200])
        return {"error": str(e)[:200]}


def installed(conn: psycopg.Connection) -> dict[str, int]:
    with conn.transaction():
        conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
        return dict(conn.execute("select repo, installation_id from counsel.installations where removed_at is null"))


def _note(issue: tuple, body: str) -> str:
    number, title, url = issue
    return (f"**[#{number}]({url}) {title}**\n\n{body}\n\n"
            "<sub>Counsel Triage · a draft for the maintainers, not posted on the issue · 👍 / 👎 if it helped</sub>")


def deliver(conn: psycopg.Connection, app: App, now: datetime,
            client: Callable[[str], GitHub] | None = None) -> list[dict]:
    """Post each undelivered triage draft for an installed repository, once."""
    repos = installed(conn)
    if not repos:
        return []
    with conn.transaction():
        conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
        pending = conn.execute(
            """select d.id, i.repo, i.number, i.title, i.url, d.body, n.notes_issue
               from counsel.triage_drafts d
               join work.issues i on d.kind = 'issue_triage:' || i.repo || '#' || i.number
               join counsel.installations n on n.repo = i.repo and n.removed_at is null
               where d.status = 'pending'
                 and not exists (select from counsel.deliveries x where x.draft_id = d.id)
                 and d.created_at >= n.installed_at
               order by d.created_at limit %s""", (MAX_DELIVERIES_PER_TICK,)).fetchall()
    tokens: dict[int, str] = {}
    done = []
    # One notes issue per repository, even when several notes go out on one tick.
    notes: dict[str, int] = {}
    for draft_id, repo, number, title, url, body, notes_issue in pending:
        try:
            inst = repos[repo]
            tokens.setdefault(inst, app.token(inst))
            gh = client(tokens[inst]) if client else GitHub(token=tokens[inst])
            notes_issue = notes.get(repo) or notes_issue
            if not notes_issue:
                notes_issue = int(gh.create_issue(repo, NOTES_TITLE, NOTES_BODY).rsplit("/", 1)[1])
                with conn.transaction():
                    conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
                    conn.execute("update counsel.installations set notes_issue = %s where repo = %s",
                                 (notes_issue, repo))
            notes[repo] = notes_issue
            comment = gh.comment(repo, notes_issue, _note((number, title, url), body))
            with conn.transaction():
                conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
                conn.execute("""insert into counsel.deliveries (draft_id, repo, issue_number, comment_url, delivered_at)
                                values (%s, %s, %s, %s, %s)""", (draft_id, repo, number, comment, now))
            done.append({"repo": repo, "issue": number, "comment": comment})
        except (GitHubError, KeyError, ValueError, IndexError) as e:
            log("WARNING", "triage note not delivered; the next tick retries", repo=repo, issue=number,
                error=str(e)[:200])
    return done
