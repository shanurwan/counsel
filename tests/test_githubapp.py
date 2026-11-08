"""The triage app: an install is consent, notes go to one notes issue, once each."""

import json
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from counsel import github, githubapp

from test_platform import AT, FakeGitHub, ok, platform  # noqa: F401
from test_runtime import admin, one  # noqa: F401

REPO = "maintainer/tool"


class FakeApp(githubapp.App):
    """Installations come from a list the test changes; tokens are fake."""

    def __init__(self, repos):
        super().__init__("1", "unused")
        self.repos = repos

    def installed_repos(self):
        return [(r, 42, r.split("/")[0], (AT - timedelta(minutes=5)).isoformat()) for r in self.repos]

    def token(self, installation_id):
        return f"ghs_token_{installation_id}"


def posting(log):
    """A GitHub that records each post and answers like the API."""
    def transport(method, url, headers, data, timeout):
        log.append((method, url, json.loads(data) if data else None, headers.get("Authorization")))
        if url.endswith("/issues"):  # a new issue: GitHub answers with its page
            return github.Response(201, {}, {"html_url": f"https://github.com/{REPO}/issues/99"})
        return github.Response(201, {}, {"html_url": f"https://github.com/{REPO}/issues/99#issuecomment-{len(log)}"})
    return lambda token: github.GitHub(token=token, transport=transport)


def triaged(conn, number, at=AT):
    """An issue and its pending triage draft, as the runtime leaves them."""
    conn.execute("""insert into work.issues (id, repo, number, title, body, author, state, url, opened_at, updated_at)
                    values (%s, %s, %s, 'Login fails', 'It breaks', 'someone', 'open', %s, %s, %s)""",
                 (5000 + number, REPO, number, f"https://github.com/{REPO}/issues/{number}", at, at))
    run = conn.execute("insert into counsel.jobs (agent, idempotency_key) values ('issue_triage', %s) returning id",
                       (f"test:{number}",)).fetchone()[0]
    run_id = conn.execute("insert into counsel.runs (job_id, agent, attempt) values (%s, 'issue_triage', 1) returning id",
                          (run,)).fetchone()[0]
    conn.execute("""insert into counsel.drafts (run_id, agent, kind, body, created_at)
                    values (%s, 'agent_issue_triage', %s, 'Login fails on Safari. No duplicate found.', %s)""",
                 (run_id, f"issue_triage:{REPO}#{number}", at))


def test_an_install_is_recorded_and_an_uninstall_stops_everything(platform):
    app = FakeApp([REPO])
    assert githubapp.refresh(platform, app, AT)["installed"] == 1
    assert githubapp.installed(platform) == {REPO: 42}
    app.repos = []
    assert githubapp.refresh(platform, app, AT)["removed"] == [REPO]
    assert githubapp.installed(platform) == {}
    triaged(platform, 7)
    assert githubapp.deliver(platform, app, AT, client=posting([])) == []


def test_notes_go_to_one_notes_issue_never_the_reporters_and_only_once(platform):
    app, log = FakeApp([REPO]), []
    githubapp.refresh(platform, app, AT)
    triaged(platform, 7)
    triaged(platform, 8)
    done = githubapp.deliver(platform, app, AT, client=posting(log))
    assert [d["issue"] for d in done] == [7, 8]
    # One notes issue created, then both notes posted on it, never on #7 or #8.
    assert log[0][1].endswith(f"/repos/{REPO}/issues") and log[0][2]["title"] == githubapp.NOTES_TITLE
    comment_urls = [url for method, url, *_ in log[1:]]
    assert comment_urls == [f"https://api.github.com/repos/{REPO}/issues/99/comments"] * 2
    assert "#7" in log[1][2]["body"] and "not posted on the issue" in log[1][2]["body"]
    assert all(auth == "Bearer ghs_token_42" for *_, auth in log)
    # The next tick posts nothing again.
    assert githubapp.deliver(platform, app, AT, client=posting(log)) == [] and len(log) == 3
    assert one(platform, "select count(*) from counsel.deliveries") == (2,)


def test_drafts_from_before_the_install_are_not_delivered(platform):
    app = FakeApp([REPO])
    githubapp.refresh(platform, app, AT)
    triaged(platform, 7, at=AT - timedelta(days=1))
    assert githubapp.deliver(platform, app, AT, client=posting([])) == []


def test_a_github_error_leaves_the_note_for_the_next_tick(platform):
    app = FakeApp([REPO])
    githubapp.refresh(platform, app, AT)
    triaged(platform, 7)

    def down(token):
        return github.GitHub(token=token, transport=lambda *a: github.Response(502, {}, {"message": "bad gateway"}))

    assert githubapp.deliver(platform, app, AT, client=down) == []
    assert len(githubapp.deliver(platform, app, AT, client=posting([]))) == 1


def test_installed_repositories_are_watched_with_their_own_token(platform):
    fake = FakeGitHub(ok([]))
    reader = github.GitHub(token="ghs_token_42", transport=fake)
    github.sync_all(platform, {}, AT, installed={REPO: reader})
    assert fake.calls[0][2]["Authorization"] == "Bearer ghs_token_42"
    assert one(platform, "select watched_since is not null from counsel.sync_state where repo = %s", REPO) == (True,)


def test_the_app_signs_a_short_lived_jwt():
    import jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    app = githubapp.App("12345", pem, clock=lambda: 1_800_000_000)
    claims = jwt.decode(app._jwt(), key.public_key(), algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600


def test_the_core_runtime_reads_triage_drafts_but_not_customer_replies(platform):
    from conftest import denied

    assert denied(platform, "counsel_orchestrator", "select body from counsel.drafts") == "42501"
    assert denied(platform, "counsel_orchestrator", "select body from counsel.triage_drafts") is None


def test_the_app_is_off_without_its_key(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    assert githubapp.App.from_env() is None
    assert githubapp.tick(None, None, AT) == {"app": "off"}
