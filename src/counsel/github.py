"""GitHub: the first real system of record Counsel works against.

The runtime only reads. `sync` pulls new and changed issues from public
repositories into work.issues, and a new open issue enqueues issue_triage.

Writing is a person's act: `comment` and `create_issue` run from `counsel review`
with that person's own GH_TOKEN, after they publish a draft. A token that can
comment on an issue can also assign it, close it and relabel it, so the scope
cannot express "comments only". The runtime therefore holds no token, and this
module has no method that assigns, closes or labels. Failure modes and why each
choice was made: docs/github.md.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import psycopg

from .compiler import ORCHESTRATOR_ROLE
from .log import log

API = "https://api.github.com"
VERSION = "2022-11-28"
MAX_BODY = 8000  # an issue body can be 65k; a prompt and a free-tier database cannot
MAX_PAGES = 3
PER_PAGE = 30
# GitHub's clock is not ours: re-reading a little is harmless, because the write is an upsert.
SINCE_OVERLAP = timedelta(minutes=5)
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    """Stop now and try on a later tick. Hammering a limit gets an integration banned."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: object


Transport = Callable[[str, str, dict, bytes | None, float], Response]


def _urllib(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> Response:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # Only ever API + a path built here, or a Link header from that host (checked in issues).
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()},
                            json.loads(resp.read() or b"null"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = {"message": raw[:200].decode("utf-8", "replace")}
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, body)
    except (urllib.error.URLError, TimeoutError) as e:
        raise GitHubError(f"{method} {url}: {e}") from e


@dataclass
class GitHub:
    token: str | None = None
    transport: Transport = _urllib
    timeout_s: float = 10.0
    clock: Callable[[], float] = field(default=time.time)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": VERSION,
                   "User-Agent": "counsel"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return {**headers, **(extra or {})}

    def _check(self, resp: Response, what: str) -> None:
        """Both rate limits arrive as 403 or 429, and only the headers tell them apart."""
        message = resp.body.get("message", "") if isinstance(resp.body, dict) else ""
        if resp.status in (403, 429):
            if "retry-after" in resp.headers:  # the secondary limit
                raise RateLimited(f"{what}: {message}", float(resp.headers["retry-after"]))
            if resp.headers.get("x-ratelimit-remaining") == "0":  # the primary limit
                reset = float(resp.headers.get("x-ratelimit-reset", "0"))
                raise RateLimited(f"{what}: {message}", max(0.0, reset - self.clock()))
        if resp.status >= 400:
            raise GitHubError(f"{what}: {resp.status} {message}")

    def issues(self, repo: str, *, etag: str | None = None, since: datetime | None = None
               ) -> tuple[list[dict], str | None]:
        """Issues changed since `since`, newest change first; ([], etag) on 304.

        A conditional request answered 304 does not count against the rate limit,
        which is what makes polling every 30 minutes without a token sustainable.
        An ETag belongs to one exact URL, so the URL never carries the moving cursor:
        GitHub's `since` parameter would make every poll a miss. Instead the list is
        sorted by last change and reading stops at the first page older than `since`.
        The list endpoint also returns pull requests: they are dropped here.
        """
        query = {"state": "all", "sort": "updated", "direction": "desc", "per_page": PER_PAGE}
        url = f"{API}/repos/{repo}/issues?{urllib.parse.urlencode(query)}"
        cutoff = since - SINCE_OVERLAP if since else None
        extra = {"If-None-Match": etag} if etag else None
        found: list[dict] = []
        new_etag = etag
        for page in range(MAX_PAGES):
            resp = self.transport("GET", url, self._headers(extra if page == 0 else None), None, self.timeout_s)
            if resp.status == 304:
                return [], etag
            self._check(resp, f"GET {repo} issues")
            if page == 0:
                new_etag = resp.headers.get("etag")
            changed = [i for i in resp.body if not cutoff or _time(i["updated_at"]) >= cutoff]
            found += [i for i in changed if "pull_request" not in i]
            link = NEXT_LINK.search(resp.headers.get("link", ""))
            if not link or len(changed) < len(resp.body):
                break
            url = link.group(1)
            if not url.startswith(f"{API}/"):
                raise GitHubError(f"refusing a next link outside the API: {url[:80]}")
        return found, new_etag

    def _post(self, path: str, payload: dict, what: str) -> str:
        if not self.token:
            raise GitHubError(f"{what} needs GH_TOKEN: your token, because you are the one publishing")
        resp = self.transport("POST", f"{API}{path}", self._headers({"Content-Type": "application/json"}),
                              json.dumps(payload).encode(), self.timeout_s)
        self._check(resp, what)
        return resp.body["html_url"]

    def comment(self, repo: str, number: int, body: str) -> str:
        return self._post(f"/repos/{repo}/issues/{number}/comments", {"body": body}, f"comment on {repo}#{number}")

    def create_issue(self, repo: str, title: str, body: str) -> str:
        return self._post(f"/repos/{repo}/issues", {"title": title, "body": body}, f"new issue in {repo}")


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _clean(text: str) -> str:
    # GitHub stores what people paste; Postgres text refuses a NUL byte.
    return text.replace("\x00", "")


def _row(repo: str, issue: dict) -> tuple:
    body = issue.get("body") or ""
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n[truncated]"
    return (issue["id"], repo, issue["number"], _clean(issue["title"]), _clean(body),
            (issue.get("user") or {}).get("login", "ghost"), issue["state"],
            [label["name"] for label in issue.get("labels") or []],
            issue["html_url"], issue["created_at"], issue["updated_at"])


def sync(conn: psycopg.Connection, client: GitHub, repo: str, *, max_new: int = 5, now: datetime) -> dict:
    """Pull one repository. At most `max_new` new issues per sync, so a first sync of
    a busy repository cannot spend a day's free model calls at once."""
    with conn.transaction():
        conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
        state = conn.execute("select etag, since, watched_since from counsel.sync_state where repo = %s",
                             (repo,)).fetchone()
        if state is None:
            # Watching starts now. Recorded before any issue is stored, because the
            # trigger that enqueues triage reads it (0017_watch_window.sql).
            conn.execute("insert into counsel.sync_state (repo, watched_since) values (%s, %s)", (repo, now))
            state = (None, None, now)
        known = {r[0] for r in conn.execute("select number from work.issues where repo = %s", (repo,))}
    etag, since, watched_since = state
    issues, new_etag = client.issues(repo, etag=etag, since=since)

    def before_watching(i: dict) -> bool:
        return watched_since is not None and _time(i["created_at"]) < watched_since

    # Opened before watching began: stored as history for duplicate checks, never triaged.
    history = {i["number"] for i in issues if i["number"] not in known and before_watching(i)}
    fresh = sorted((i for i in issues if i["number"] not in known and i["number"] not in history),
                   key=lambda i: i["number"])
    # The newest issues are the ones a person is waiting on; older ones are history.
    allowed = {i["number"] for i in fresh[-max_new:]} if max_new else set()
    rows = [i for i in issues if i["number"] in known or i["number"] in allowed or i["number"] in history]
    with conn.transaction():
        conn.execute(f'set local role "{ORCHESTRATOR_ROLE}"')
        for issue in rows:
            conn.execute(
                """insert into work.issues (id, repo, number, title, body, author, state, labels, url,
                                            opened_at, updated_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (id) do update set title = excluded.title, body = excluded.body,
                     state = excluded.state, labels = excluded.labels, updated_at = excluded.updated_at""",
                _row(repo, issue))
        # A cursor only moves when the write it describes commits with it.
        newest = max((_time(i["updated_at"]) for i in rows), default=since)
        conn.execute(
            """insert into counsel.sync_state (repo, etag, since, synced_at) values (%s, %s, %s, %s)
               on conflict (repo) do update set etag = excluded.etag, since = excluded.since,
                 synced_at = excluded.synced_at""",
            (repo, new_etag, newest, now))
    return {"repo": repo, "seen": len(issues), "new": len(allowed), "skipped": len(fresh) - len(allowed),
            "history": len(history),
            "not_modified": not issues and etag is not None and new_etag == etag}


def sync_all(conn: psycopg.Connection, agents: dict, now: datetime, client: GitHub | None = None) -> list[dict]:
    """Every repository an agent watches. A GitHub outage never costs a tick its agents."""
    client = client or GitHub(token=os.environ.get("COUNSEL_GITHUB_READ_TOKEN"))
    results = []
    for repo in sorted({r for a in agents.values() for r in a.watches}):
        try:
            results.append(sync(conn, client, repo, now=now))
        except RateLimited as e:
            log("WARNING", "github rate limited; next tick retries", repo=repo, retry_after=e.retry_after)
            results.append({"repo": repo, "rate_limited": True})
        except GitHubError as e:
            log("WARNING", "github sync failed; next tick retries", repo=repo, error=str(e))
            results.append({"repo": repo, "error": str(e)[:200]})
    return results
