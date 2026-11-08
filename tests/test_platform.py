"""Rollback, the interruption budget, the new agents, the warehouse, and disputes.

These run against a real Postgres, logged in as counsel_runtime as Cloud Run is.
GitHub and BigQuery are fake transports; the failure modes they reproduce are the
ones in docs/github.md and docs/warehouse.md.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
import yaml

from counsel import audit, challenge, github, review, warehouse
from counsel.behaviors import drift_watcher, issue_triage, nudge, nudge_text, team_digest
from counsel.model import ChatModel, Completion, FallbackModel, ModelError, gemini_model

from conftest import AIKO, BRUNO, TASK_A, acting_as, denied
from test_runtime import Clock, FakeModel, admin, make_worker, one, runtime_url  # noqa: F401

NUDGE = "agent_nudge"


def _next_weekday() -> datetime:
    """The next Monday to Friday, always after jobs the database dates with now()."""
    day = datetime.now(timezone.utc) + timedelta(days=1)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day.replace(hour=5, minute=0, second=0, microsecond=0)


# 05:00 UTC is 14:00 in Tokyo, inside working hours for aiko; 02:00 for bruno in Sao Paulo.
AT = _next_weekday()
REPO = "acme/widgets"


@pytest.fixture
def platform(admin):
    def clean():
        admin.execute("""truncate counsel.outbound_messages, work.issues, counsel.sync_state, work.standups,
                                  work.consents cascade""")
        admin.execute("delete from work.task_updates")
        admin.execute("update counsel.warehouse_cursor set finished_at = '1970-01-01Z', "
                      "run_id = '00000000-0000-0000-0000-000000000000', attempt = 0")
        admin.execute("update work.people set interruption_budget = 2")
        admin.execute("update work.tasks set status = 'in_progress', due_date = %s where id = %s",
                      (AT.date() + timedelta(days=2), TASK_A))

    clean()
    yield admin
    clean()


def job(admin, agent, key, **payload):
    admin.execute("insert into counsel.jobs (agent, idempotency_key, payload) values (%s, %s, %s)",
                  (agent, key, json.dumps(payload)))


# --- rollback ------------------------------------------------------------------------------


def _digest_then_die(ctx, payload):
    ctx.step("draft one", lambda c: c.execute(
        "insert into counsel.drafts (run_id, kind, body) values (%s, 'team_digest:core', 'half done')",
        (ctx.run_id,)) and {"ok": True})
    raise RuntimeError("the model went away")


def test_a_dead_run_takes_back_what_it_wrote_atomically(platform, make_worker):
    job(platform, "team_digest", "test:dies")
    worker = make_worker(implementations={"team_digest": _digest_then_die}, clock=Clock(AT))
    for minutes in (0, 1, 2):
        worker.clock = Clock(AT + timedelta(minutes=minutes))
        worker.tick()
    assert one(platform, "select status from counsel.jobs where idempotency_key = 'test:dies'") == ("dead",)
    assert one(platform, "select status from counsel.drafts where body = 'half done'") == ("retracted",)
    action, decision, detail = one(platform, "select action, decision, detail from counsel.audit_log "
                                             "where action = 'rollback' order by id desc limit 1")
    assert (decision, detail["drafts"], detail["reason"]) == ("reverted", 1, "dead letter")


def test_a_refused_run_takes_back_what_it_wrote_too(platform, make_worker):
    def half_then_forbidden(ctx, payload):
        ctx.step("draft", lambda c: c.execute(
            "insert into counsel.drafts (run_id, kind, body) values (%s, 'team_digest:core', 'before the refusal')",
            (ctx.run_id,)) and {"ok": True})
        ctx.step("mark done", lambda c: c.execute("update work.tasks set status = 'done'"))

    job(platform, "team_digest", "test:refused")
    make_worker(implementations={"team_digest": half_then_forbidden}, clock=Clock(AT)).tick()
    assert one(platform, "select status from counsel.drafts where body = 'before the refusal'") == ("retracted",)


def test_a_rollback_leaves_alone_what_a_person_already_accepted(platform, make_worker):
    job(platform, "team_digest", "test:ok")

    def two_drafts(ctx, payload):
        for body in ("accepted", "not yet"):
            ctx.step(body, lambda c, b=body: c.execute(
                "insert into counsel.drafts (run_id, kind, body) values (%s, 'team_digest:core', %s)",
                (ctx.run_id, b)) and {"ok": True})

    make_worker(implementations={"team_digest": two_drafts}, clock=Clock(AT)).tick()
    draft = one(platform, "select id::text from counsel.drafts where body = 'accepted'")[0]
    run_id = one(platform, "select run_id from counsel.drafts where body = 'accepted'")[0]
    review.publish(platform, draft)
    platform.execute("insert into counsel.run_rollbacks (run_id, reason) values (%s, 'asked')", (run_id,))
    assert dict(platform.execute("select body, status from counsel.drafts where run_id = %s", (run_id,)).fetchall()) == {
        "accepted": "published", "not yet": "retracted"}
    detail = one(platform, "select detail from counsel.audit_log where run_id = %s and action = 'rollback'", run_id)[0]
    assert detail["kept_published"] == 1


def test_a_retracted_draft_cannot_be_published(platform, make_worker):
    job(platform, "team_digest", "test:retracted")
    make_worker(implementations={"team_digest": lambda ctx, p: ctx.step("d", lambda c: c.execute(
        "insert into counsel.drafts (run_id, kind, body) values (%s, 'team_digest:core', 'x')", (ctx.run_id,))
        and {"ok": True})}, clock=Clock(AT)).tick()
    draft, run_id = one(platform, "select id::text, run_id from counsel.drafts where body = 'x'")
    platform.execute("insert into counsel.run_rollbacks (run_id, reason) values (%s, 'asked')", (run_id,))
    with pytest.raises(review.ReviewError, match="retracted"):
        review.publish(platform, draft)


def test_a_retracted_note_leaves_the_digest(platform, make_worker):
    run = None
    job(platform, "team_digest", "test:note")
    make_worker(implementations={"team_digest": lambda ctx, p: None}, clock=Clock(AT)).tick()
    run = one(platform, "select id from counsel.runs order by started_at desc limit 1")[0]
    platform.execute("insert into work.task_updates (task_id, body, author, run_id, created_at) "
                     "values (%s, 'carried, then retracted', 'aiko', %s, %s)", (TASK_A, run, AT - timedelta(hours=1)))
    platform.execute("insert into counsel.run_rollbacks (run_id, reason) values (%s, 'asked')", (run,))
    from counsel.behaviors import _activity
    assert _activity(platform, "core", AT)["updates"] == []


def test_no_agent_can_undo_a_run_or_read_the_rollback_log(db, agents):
    for a in agents:
        assert denied(db, a.role, "select * from counsel.run_rollbacks") == "42501", a.role
        assert denied(db, a.role, "update counsel.drafts set status = 'retracted'") == "42501", a.role


# --- the interruption budget -------------------------------------------------------------------


def send(conn, recipient=AIKO, body="hello"):
    conn.execute("insert into counsel.outbound_messages (recipient_id, body) values (%s, %s)", (recipient, body))


def test_postgres_refuses_a_message_over_the_budget(db):
    db.execute("update work.people set interruption_budget = 2 where id = %s", (AIKO,))
    with acting_as(db, NUDGE):
        send(db)
        send(db)
    assert denied(db, NUDGE, "insert into counsel.outbound_messages (recipient_id, body) values (%s, 'third')",
                  (AIKO,)) == "CN004"


def test_a_budget_of_zero_opts_a_person_out(db):
    db.execute("update work.people set interruption_budget = 0 where id = %s", (AIKO,))
    assert denied(db, NUDGE, "insert into counsel.outbound_messages (recipient_id, body) values (%s, 'hi')",
                  (AIKO,)) == "CN004"


def test_one_persons_budget_does_not_spend_anothers(db):
    db.execute("update work.people set interruption_budget = 1")
    with acting_as(db, NUDGE):
        send(db, AIKO)
        send(db, BRUNO)


def test_the_nudge_agent_cannot_raise_a_budget_or_read_message_text(db):
    assert denied(db, NUDGE, "update work.people set interruption_budget = 20") == "42501"
    assert denied(db, NUDGE, "select body from counsel.outbound_messages") == "42501"
    assert denied(db, NUDGE, "update counsel.outbound_messages set cancelled_at = now()") == "42501"


def test_a_cancelled_message_gives_the_budget_back(db):
    db.execute("update work.people set interruption_budget = 1 where id = %s", (AIKO,))
    send(db)
    db.execute("update counsel.outbound_messages set cancelled_at = now() where recipient_id = %s", (AIKO,))
    with acting_as(db, NUDGE):
        send(db)


# --- nudge ---------------------------------------------------------------------------------


NUDGE_ONLY = {"nudge": nudge}


def run_nudge(admin, make_worker, at=AT):
    job(admin, "nudge", f"test:nudge:{at.isoformat()}", until=at.isoformat())
    return make_worker(implementations=NUDGE_ONLY, clock=Clock(at)).tick()


def messages(admin):
    return admin.execute("""select p.handle, m.body from counsel.outbound_messages m
                            join work.people p on p.id = m.recipient_id order by p.handle""").fetchall()


def test_quiet_work_due_soon_gets_one_message_with_no_model(platform, make_worker):
    model = FakeModel()
    job(platform, "nudge", "test:nudge:a", until=AT.isoformat())
    make_worker(model, NUDGE_ONLY, clock=Clock(AT)).tick()
    assert [h for h, _ in messages(platform)] == ["aiko"]
    assert model.calls == []


def test_nobody_is_messaged_outside_their_working_hours(platform, make_worker):
    night_in_tokyo = AT.replace(hour=15)
    run_nudge(platform, make_worker, night_in_tokyo)
    assert messages(platform) == []


def test_nobody_is_messaged_on_their_weekend(platform, make_worker):
    saturday = AT + timedelta(days=(5 - AT.weekday()))
    platform.execute("update work.tasks set due_date = %s where id = %s", (saturday.date(), TASK_A))
    run_nudge(platform, make_worker, saturday)
    assert messages(platform) == []


def test_a_recent_note_means_no_message(platform, make_worker):
    platform.execute("insert into work.task_updates (task_id, body, author, created_at) values (%s, 'moving', 'aiko', %s)",
                     (TASK_A, AT - timedelta(days=1)))
    run_nudge(platform, make_worker)
    assert messages(platform) == []


def test_a_task_is_not_nudged_twice_in_a_week(platform, make_worker):
    run_nudge(platform, make_worker)
    assert len(messages(platform)) == 1
    run_nudge(platform, make_worker, AT + timedelta(minutes=30))
    assert len(messages(platform)) == 1


def test_a_spent_budget_skips_the_person_without_failing_the_run(platform, make_worker):
    platform.execute("update work.people set interruption_budget = 0 where id = %s", (AIKO,))
    summary = run_nudge(platform, make_worker)
    assert {r["result"] for r in summary["runs"]} == {"succeeded"}
    assert messages(platform) == []


def test_tasks_are_bundled_and_useful_before_any_ask():
    text = nudge_text([
        {"title": "Fix flaky test", "status": "blocked", "due": "2026-11-06", "quiet_days": 5, "last_by": "dev"},
        {"title": "Ship limiter", "status": "in_progress", "due": None, "quiet_days": None, "last_by": None}])
    assert text.index("Fix flaky test") < text.index("If anything has moved")
    assert "last note 5 days ago, from dev" in text and "in progress" in text and "no notes yet" in text
    assert "No reply needed" in text
    for word in ("late", "overdue", "please", "you should", "forgot"):
        assert word not in text.lower()


def test_a_note_is_never_quoted_into_a_message(platform, make_worker):
    platform.execute("insert into work.task_updates (task_id, body, author, created_at) values "
                     "(%s, 'Aiko has ignored this, embarrassing', 'bruno', %s)", (TASK_A, AT - timedelta(days=5)))
    run_nudge(platform, make_worker)
    assert "embarrassing" not in messages(platform)[0][1]


# --- GitHub --------------------------------------------------------------------------------


def issue(number, title="Login fails", state="open", pr=False, updated="2026-11-04T01:00:00Z", body="It breaks",
          created=None):
    # Opened just after watching starts (AT), unless a test says otherwise.
    created = created or (AT + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"id": 1000 + number, "number": number, "title": title, "body": body, "state": state,
            "user": {"login": "someone"}, "labels": [{"name": "bug"}], "created_at": created,
            "updated_at": updated, "html_url": f"https://github.com/{REPO}/issues/{number}"}
    return {**data, "pull_request": {"url": "x"}} if pr else data


class FakeGitHub:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        return self.responses.pop(0)


def ok(body, etag='"v1"', link=""):
    return github.Response(200, {"etag": etag, "link": link}, body)


def test_a_pull_request_is_not_an_issue():
    found, _ = github.GitHub(transport=FakeGitHub(ok([issue(1), issue(2, pr=True)]))).issues(REPO)
    assert [i["number"] for i in found] == [1]


def test_a_304_returns_nothing_and_sends_the_etag():
    fake = FakeGitHub(github.Response(304, {}, None))
    found, etag = github.GitHub(transport=fake).issues(REPO, etag='"v1"')
    assert (found, etag) == ([], '"v1"')
    assert fake.calls[0][2]["If-None-Match"] == '"v1"'


def test_the_url_never_moves_so_the_etag_can_match():
    """Found against the real API: with a `since` parameter, every poll missed and was billed."""
    fake = FakeGitHub(ok([issue(1)]), github.Response(304, {}, None))
    client = github.GitHub(transport=fake)
    client.issues(REPO)
    client.issues(REPO, etag='"v1"', since=AT)
    assert fake.calls[0][1] == fake.calls[1][1] and "since" not in fake.calls[1][1]


def test_reading_stops_at_the_first_page_older_than_the_cursor():
    next_page = f'<{github.API}/repositories/1/issues?page=2>; rel="next"'
    since = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
    fake = FakeGitHub(ok([issue(2, updated="2026-11-04T01:00:00Z"), issue(1, updated="2026-11-01T00:00:00Z")],
                         link=next_page))
    found, _ = github.GitHub(transport=fake).issues(REPO, since=since)
    assert [i["number"] for i in found] == [2]
    assert len(fake.calls) == 1


def test_pages_follow_the_link_header_and_stop_at_the_limit():
    next_page = f'<{github.API}/repositories/1/issues?page=2>; rel="next"'
    fake = FakeGitHub(*[ok([issue(n)], link=next_page) for n in range(1, 10)])
    found, _ = github.GitHub(transport=fake).issues(REPO)
    assert len(found) == github.MAX_PAGES


def test_a_next_link_off_the_api_host_is_refused():
    fake = FakeGitHub(ok([issue(1)], link='<https://evil.example/x>; rel="next"'))
    with pytest.raises(github.GitHubError, match="outside the API"):
        github.GitHub(transport=fake).issues(REPO)


@pytest.mark.parametrize("headers, wait", [
    ({"retry-after": "60"}, 60.0),
    ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1100"}, 100.0),
])
def test_both_rate_limits_are_told_apart_by_their_headers(headers, wait):
    fake = FakeGitHub(github.Response(403, headers, {"message": "limit"}))
    with pytest.raises(github.RateLimited) as e:
        github.GitHub(transport=fake, clock=lambda: 1000.0).issues(REPO)
    assert e.value.retry_after == wait


def test_a_403_that_is_not_a_rate_limit_is_an_error_not_a_wait():
    fake = FakeGitHub(github.Response(403, {"x-ratelimit-remaining": "40"}, {"message": "Resource not accessible"}))
    with pytest.raises(github.GitHubError) as e:
        github.GitHub(transport=fake).issues(REPO)
    assert not isinstance(e.value, github.RateLimited)


def test_sync_stores_new_issues_and_enqueues_triage_once(platform):
    client = github.GitHub(transport=FakeGitHub(ok([issue(1), issue(2, pr=True)]), github.Response(304, {}, None)))
    assert github.sync(platform, client, REPO, now=AT)["new"] == 1
    assert one(platform, "select title, author, labels from work.issues where number = 1") == ("Login fails", "someone", ["bug"])
    assert one(platform, "select count(*) from counsel.jobs where idempotency_key = %s", f"issue:{REPO}#1") == (1,)
    assert github.sync(platform, client, REPO, now=AT)["not_modified"] is True


def test_an_edited_issue_updates_but_is_not_triaged_again(platform):
    client = github.GitHub(transport=FakeGitHub(ok([issue(1)]), ok([issue(1, title="Login fails on Safari")], etag='"v2"')))
    github.sync(platform, client, REPO, now=AT)
    github.sync(platform, client, REPO, now=AT)
    assert one(platform, "select title from work.issues where number = 1") == ("Login fails on Safari",)
    assert one(platform, "select count(*) from counsel.jobs where idempotency_key like 'issue:%%'") == (1,)


def test_a_first_sync_of_a_busy_repository_is_capped_at_the_newest(platform):
    client = github.GitHub(transport=FakeGitHub(ok([issue(n) for n in range(1, 21)])))
    result = github.sync(platform, client, REPO, max_new=3, now=AT)
    assert (result["new"], result["skipped"]) == (3, 17)
    numbers = [r[0] for r in platform.execute("select number from work.issues order by number")]
    assert numbers == [18, 19, 20]


def test_a_newly_watched_repositorys_backlog_is_history_not_triaged(platform):
    before = (AT - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    backlog = [issue(n, title=f"Old problem {n}", created=before) for n in range(1, 41)]
    client = github.GitHub(transport=FakeGitHub(ok(backlog + [issue(41, title="New problem")])))
    result = github.sync(platform, client, REPO, max_new=3, now=AT)
    assert (result["history"], result["new"]) == (40, 1)
    # All 41 are there to compare against; only the one opened after watching began is triaged.
    assert one(platform, "select count(*) from work.issues") == (41,)
    assert [r[0] for r in platform.execute("select payload->>'number' from counsel.jobs "
                                           "where idempotency_key like 'issue:%%'")] == ["41"]
    assert one(platform, "select watched_since = %s from counsel.sync_state where repo = %s", AT, REPO) == (True,)


def test_a_repository_watched_before_the_window_existed_triages_as_it_did(platform):
    platform.execute("insert into counsel.sync_state (repo) values (%s)", (REPO,))
    old = issue(1, created=(AT - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    github.sync(platform, github.GitHub(transport=FakeGitHub(ok([old]))), REPO, now=AT)
    assert one(platform, "select count(*) from counsel.jobs where idempotency_key like 'issue:%%'") == (1,)


def test_a_closed_issue_is_stored_but_never_triaged(platform):
    github.sync(platform, github.GitHub(transport=FakeGitHub(ok([issue(1, state="closed")]))), REPO, now=AT)
    assert one(platform, "select count(*) from counsel.jobs where idempotency_key like 'issue:%%'") == (0,)


def test_a_github_outage_costs_the_tick_nothing(platform, agents):
    fake = FakeGitHub(github.Response(503, {}, {"message": "down"}))
    watching = {"issue_triage": type("A", (), {"watches": (REPO,)})()}
    assert "error" in github.sync_all(platform, watching, AT, github.GitHub(transport=fake))[0]


def test_the_connector_has_no_way_to_assign_close_or_label():
    public = {n for n in dir(github.GitHub) if not n.startswith("_")}
    assert public == {"issues", "comment", "create_issue", "clock", "timeout_s", "token", "transport"}


def test_writing_needs_a_persons_token():
    with pytest.raises(github.GitHubError, match="your token"):
        github.GitHub(transport=FakeGitHub()).comment(REPO, 1, "hi")


# --- issue_triage -----------------------------------------------------------------------------


TRIAGE = {"issue_triage": issue_triage}


def store(admin, *issues):
    github.sync(admin, github.GitHub(transport=FakeGitHub(ok(list(issues)))), REPO, now=AT)


def test_triage_writes_a_draft_and_touches_nothing_on_github(platform, make_worker):
    store(platform, issue(1))
    make_worker(FakeModel(text="Login failure report. Area: auth. Missing: browser. No duplicate found."),
                TRIAGE, clock=Clock(AT)).tick()
    kind, body, status = one(platform, "select kind, body, status from counsel.drafts where kind like 'issue_triage:%%'")
    assert kind == f"issue_triage:{REPO}#1" and status == "pending" and "auth" in body


def test_an_invented_duplicate_is_flagged_for_the_reviewer(platform, make_worker):
    store(platform, issue(1))
    make_worker(FakeModel(text="Looks like a duplicate of #999."), TRIAGE, clock=Clock(AT)).tick()
    assert "not in this repository's open list: #999" in one(
        platform, "select body from counsel.drafts where kind like 'issue_triage:%%'")[0]


def test_a_real_candidate_duplicate_is_not_flagged(platform, make_worker):
    store(platform, issue(1, "Login fails on Safari", body="login session cookie dropped"),
          issue(2, "Login fails on Firefox", body="login session cookie dropped"))
    platform.execute("delete from counsel.jobs where idempotency_key like %s", (f"issue:{REPO}#1",))
    make_worker(FakeModel(text="Possible duplicate of #1."), TRIAGE, clock=Clock(AT)).tick()
    assert "[Reviewer" not in one(platform, "select body from counsel.drafts where kind like 'issue_triage:%%'")[0]


def test_triage_agent_sees_only_the_repository_it_was_started_for(db):
    for repo in (REPO, "other/secret"):
        db.execute("""insert into work.issues (id, repo, number, title, author, state, url, opened_at, updated_at)
                      values (%s, %s, 1, 'x', 'a', 'open', 'u', now(), now())""", (hash(repo) % 10**9, repo))
    with acting_as(db, "agent_issue_triage"):
        assert db.execute("select count(*) from work.issues").fetchone() == (0,)
        db.execute("select set_config('counsel.issue_repo', %s, true)", (REPO,))
        assert db.execute("select repo from work.issues").fetchall() == [(REPO,)]


def test_triage_agent_cannot_change_an_issue_or_a_task(db):
    role = "agent_issue_triage"
    assert denied(db, role, "update work.issues set state = 'closed'") == "42501"
    assert denied(db, role, "select * from work.tasks") == "42501"


def test_publishing_delivers_as_the_person_and_a_failure_leaves_it_pending(platform, make_worker):
    store(platform, issue(1))
    make_worker(FakeModel(text="Report. No duplicate found."), TRIAGE, clock=Clock(AT)).tick()
    draft = one(platform, "select id::text from counsel.drafts where kind like 'issue_triage:%%'")[0]

    def down(kind, text):
        raise github.GitHubError("502")

    with pytest.raises(github.GitHubError):
        review.publish(platform, draft, deliver=down)
    assert one(platform, "select status from counsel.drafts where id = %s", draft) == ("pending",)

    sent = []
    review.publish(platform, draft, deliver=lambda kind, text: sent.append((kind, text)))
    assert sent == [(f"issue_triage:{REPO}#1", "Report. No duplicate found.")]
    assert one(platform, "select status from counsel.drafts where id = %s", draft) == ("published",)


# --- drift_watcher ------------------------------------------------------------------------------


DRIFT = {"drift_watcher": drift_watcher}


def plant_dead(admin, agent="team_digest", key="dead:1"):
    admin.execute("insert into counsel.jobs (agent, idempotency_key, status, updated_at) values (%s, %s, 'dead', now())",
                  (agent, key))


def fixes(admin):
    return admin.execute("select kind, body from counsel.drafts where kind like 'fix:%%' order by kind").fetchall()


def test_a_dead_letter_becomes_one_draft_fix_with_no_model(platform, make_worker):
    plant_dead(platform)
    job(platform, "drift_watcher", "test:drift:1")
    model = FakeModel()
    make_worker(model, DRIFT, clock=Clock(AT)).tick()
    assert fixes(platform)[0][0] == "fix:dead_letters:team_digest"
    assert "1 job(s) dead-lettered" in fixes(platform)[0][1]
    assert model.calls == []


def test_one_problem_is_raised_once(platform, make_worker):
    plant_dead(platform)
    worker = make_worker(implementations=DRIFT, clock=Clock(AT))
    job(platform, "drift_watcher", "test:drift:1")
    worker.tick()
    job(platform, "drift_watcher", "test:drift:2")
    worker.tick()
    assert len(fixes(platform)) == 1


def test_a_healthy_platform_raises_nothing(platform, make_worker):
    job(platform, "drift_watcher", "test:drift:1")
    make_worker(implementations=DRIFT, clock=Clock(AT)).tick()
    assert fixes(platform) == []


def test_the_watcher_reads_counts_not_prompts_or_tickets(db):
    role = "agent_drift_watcher"
    for table in ("counsel.model_calls", "counsel.runs", "counsel.jobs", "support.tickets", "work.tasks"):
        assert denied(db, role, f"select * from {table}") == "42501", table
    with acting_as(db, role):
        db.execute("select * from counsel.platform_findings")


# --- the MCP login ----------------------------------------------------------------------------


def test_the_mcp_login_can_read_the_journal_and_ask_and_nothing_else(db):
    role = audit.MCP_ROLE
    with acting_as(db, role):
        db.execute("select * from counsel.runs")
        db.execute("insert into counsel.questions (body) values ('How many leave days?')")
    for statement in ("update counsel.drafts set status = 'published'", "update work.tasks set status = 'done'",
                      "select * from work.tasks", "select * from support.integration_tokens",
                      "select * from support.tickets", "insert into counsel.jobs (agent, idempotency_key) values ('x', 'y')",
                      "insert into counsel.outbound_messages (recipient_id, body) values (gen_random_uuid(), 'x')",
                      "delete from counsel.audit_log"):
        assert denied(db, role, statement) == "42501", statement


def test_the_audit_catches_the_mcp_login_being_granted_more(db, agents):
    assert not [v for v in audit.run(db, agents) if v.role == audit.MCP_ROLE]
    db.execute("grant update on counsel.drafts to counsel_mcp")
    found = [v for v in audit.run(db, agents) if v.role == audit.MCP_ROLE]
    assert found and "UPDATE on counsel.drafts" in found[0].detail


@pytest.fixture
def mcp_server(database_url, tmp_path):
    from pathlib import Path

    from counsel.mcp_server import build

    with psycopg.connect(database_url, autocommit=True) as admin_conn:
        admin_conn.execute("alter role counsel_mcp password 'counsel-test-only'")
    url = psycopg.conninfo.make_conninfo(database_url, user="counsel_mcp", password="counsel-test-only")
    return build(url, Path(__file__).resolve().parents[1] / "agents")


def call(server, tool, **args):
    import anyio
    from mcp.client import Client

    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return anyio.run(go)


def tool_names(server):
    import anyio
    from mcp.client import Client

    async def go():
        async with Client(server) as client:
            return await client.list_tools()

    return anyio.run(go).tools


def test_every_tool_but_asking_declares_itself_read_only(mcp_server):
    tools = {t.name: t for t in tool_names(mcp_server)}
    assert {"list_agents", "privilege_matrix", "replay_run", "ask_handbook"} <= set(tools)
    writers = {n for n, t in tools.items() if not t.annotations.read_only_hint}
    assert writers == {"ask_handbook"}


def test_no_tool_publishes_carries_or_commits(mcp_server):
    names = " ".join(t.name for t in tool_names(mcp_server))
    for word in ("publish", "approve", "carry", "assign", "status", "owner", "due"):
        assert word not in names


def test_a_tool_answers_from_the_database_over_the_protocol(mcp_server, platform, make_worker):
    job(platform, "drift_watcher", "test:mcp")
    make_worker(implementations=DRIFT, clock=Clock(AT)).tick()
    result = call(mcp_server, "list_runs", agent="drift_watcher")
    assert not result.is_error and "drift_watcher" in result.content[0].text
    matrix = call(mcp_server, "privilege_matrix")
    assert "agent_nudge" in matrix.content[0].text and "**YES**" not in matrix.content[0].text


def test_asking_through_mcp_queues_a_real_question(mcp_server, platform):
    assert not call(mcp_server, "ask_handbook", question="How many leave days?").is_error
    assert one(platform, "select count(*) from counsel.jobs where idempotency_key like 'question:%%'") == (1,)


# --- the warehouse --------------------------------------------------------------------------


class FakeBigQuery:
    def __init__(self, state="DONE", error=None, post=200):
        self.state, self.error, self.post = state, error, post
        self.posts, self.gets = [], []

    def __call__(self, method, url, headers, body, timeout):
        if method == "POST":
            self.posts.append((url, headers, body))
            return self.post, {}
        self.gets.append(url)
        return 200, {"status": {"state": self.state, **({"errorResult": self.error} if self.error else {})}}


def client(fake):
    return warehouse.BigQuery("proj", lambda: "tok", transport=fake, sleep=lambda _: None, wait_s=0)


@pytest.fixture
def finished_run(platform, make_worker):
    job(platform, "drift_watcher", "test:wh")
    make_worker(implementations=DRIFT, clock=Clock(AT)).tick()


def test_a_finished_run_loads_whole_and_only_once(platform, finished_run, make_worker):
    fake = FakeBigQuery()
    worker_conn = make_worker(implementations=DRIFT, clock=Clock(AT)).conn
    assert warehouse.export(worker_conn, client(fake)) == {"exported": 1}
    url, headers, body = fake.posts[0]
    text = body.decode()
    assert "uploadType=multipart" in url  # a load job: free. Streaming inserts are billed.
    assert "insertAll" not in url
    assert '"writeDisposition": "WRITE_APPEND"' in text and "read findings" in text
    assert warehouse.export(worker_conn, client(fake)) == {"exported": 0}
    assert len(fake.posts) == 1


def test_a_job_accepted_but_unanswered_is_read_not_loaded_twice(platform, finished_run, make_worker):
    conn = make_worker(implementations=DRIFT, clock=Clock(AT)).conn
    with pytest.raises(warehouse.WarehouseError, match="still running"):
        warehouse.export(conn, client(FakeBigQuery(state="RUNNING")))
    again = FakeBigQuery(post=409)
    assert warehouse.export(conn, client(again)) == {"exported": 1}


def test_the_retry_after_a_lost_answer_uses_the_same_job_id(platform, finished_run, make_worker):
    conn = make_worker(implementations=DRIFT, clock=Clock(AT)).conn
    first, second = FakeBigQuery(state="RUNNING"), FakeBigQuery()
    with pytest.raises(warehouse.WarehouseError):
        warehouse.export(conn, client(first))
    warehouse.export(conn, client(second))
    assert first.gets[0] == second.gets[0]


def test_a_failed_job_keeps_the_cursor_and_retries_with_a_new_id(platform, finished_run, make_worker):
    conn = make_worker(implementations=DRIFT, clock=Clock(AT)).conn
    bad, good = FakeBigQuery(error={"message": "bad row"}), FakeBigQuery()
    with pytest.raises(warehouse.JobFailed):
        warehouse.export(conn, client(bad))
    assert warehouse.export(conn, client(good)) == {"exported": 1}
    assert bad.gets[0] != good.gets[0]


def test_a_rejected_upload_moves_nothing(platform, finished_run, make_worker):
    conn = make_worker(implementations=DRIFT, clock=Clock(AT)).conn
    with pytest.raises(warehouse.WarehouseError, match="rejected"):
        warehouse.export(conn, client(FakeBigQuery(post=403)))
    assert warehouse.export(conn, client(FakeBigQuery())) == {"exported": 1}


def test_nothing_to_export_makes_no_call(platform, make_worker):
    fake = FakeBigQuery()
    platform.execute("update counsel.warehouse_cursor set finished_at = '2999-01-01Z'")
    assert warehouse.export(make_worker(implementations=DRIFT, clock=Clock(AT)).conn, client(fake)) == {"exported": 0}
    assert fake.posts == []


# --- a second provider --------------------------------------------------------------------------


class Down:
    def complete(self, messages, *, max_tokens=800):
        raise ModelError("all models failed: 503")


class Truncated:
    def complete(self, messages, *, max_tokens=800):
        raise ModelError("cut off", model="m", prompt_tokens=10, output_tokens=max_tokens)


class Up:
    def complete(self, messages, *, max_tokens=800):
        return Completion(text="ok", provider="second", model="s", latency_ms=1)


def test_an_outage_hands_over_to_the_next_provider():
    assert FallbackModel((Down(), Up())).complete([]).provider == "second"


def test_a_truncated_answer_does_not_spend_a_second_provider():
    with pytest.raises(ModelError, match="cut off"):
        FallbackModel((Truncated(), Up())).complete([])


def test_all_providers_down_is_an_error_with_every_reason():
    with pytest.raises(ModelError, match="503.*503"):
        FallbackModel((Down(), Down())).complete([])


def test_the_second_provider_speaks_to_its_own_url_and_is_recorded_as_itself():
    seen = []

    def transport(url, headers, body, timeout):
        seen.append((url, body))
        return 200, {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {}}

    model = gemini_model("key")
    model.transport = transport
    result = model.complete([{"role": "user", "content": "x"}])
    assert result.provider == "gemini" and "generativelanguage" in seen[0][0]
    assert "chat_template_kwargs" not in seen[0][1]  # Nemotron's setting is Nemotron's


# --- disputes -------------------------------------------------------------------------------


SUITE = "agent: ask_handbook\n# a comment that must survive\nmin_lower_bound: 0.6\n\ncases:\n  - id: first\n    question: Q\n    expect: {}\n"


def question_run(admin, make_worker):
    admin.execute("insert into counsel.questions (body) values ('How many leave days do we get?')")
    make_worker(FakeModel(), {"ask_handbook": __import__("counsel.behaviors", fromlist=["x"]).ask_handbook},
                clock=Clock(AT)).tick()
    return str(one(admin, "select id from counsel.runs order by started_at desc limit 1")[0])


def test_a_dispute_becomes_a_case_holding_what_the_agent_saw(platform, make_worker, tmp_path):
    run_id = question_run(platform, make_worker)
    agent, case = challenge.case_from_run(platform, run_id, case_id="leave_is_eighteen", must_contain=["18 days"],
                                          must_not_contain=["20 days"], rubric=None, by="aiko")
    assert agent == "ask_handbook" and case["question"] == "How many leave days do we get?"
    assert case["expect"] == {"contains_all": ["18 days"], "not_contains": ["20 days"]}
    assert case["challenged"] == {"run": run_id, "by": "aiko"}

    suite = tmp_path / "suite.yaml"
    suite.write_text(SUITE, encoding="utf-8")
    challenge.add_case(suite, case)
    text = suite.read_text(encoding="utf-8")
    assert "# a comment that must survive" in text
    assert [c["id"] for c in yaml.safe_load(text)["cases"]] == ["first", "leave_is_eighteen"]


def test_a_dispute_must_say_what_was_wrong(platform, make_worker):
    run_id = question_run(platform, make_worker)
    with pytest.raises(challenge.ChallengeError, match="what was wrong"):
        challenge.case_from_run(platform, run_id, case_id="vague", must_contain=[], must_not_contain=[],
                                rubric=None, by="aiko")


def test_a_case_id_cannot_be_used_twice(tmp_path):
    suite = tmp_path / "suite.yaml"
    suite.write_text(SUITE, encoding="utf-8")
    with pytest.raises(challenge.ChallengeError, match="already has"):
        challenge.add_case(suite, {"id": "first"})


def test_a_new_agent_starts_validated_and_with_a_suite_that_cannot_pass(tmp_path):
    (tmp_path / "agents").mkdir()
    made = challenge.scaffold(tmp_path, "standup_lint", "platform", "T1", ["read_tasks", "write_drafts"])
    assert len(made) == 2
    assert yaml.safe_load((tmp_path / "evals/standup_lint/suite.yaml").read_text(encoding="utf-8"))["cases"] == []


def test_scaffolding_refuses_a_tier_that_does_not_fit(tmp_path):
    (tmp_path / "agents").mkdir()
    with pytest.raises(challenge.ChallengeError, match="does not allow"):
        challenge.scaffold(tmp_path, "sneaky_agent", "platform", "T0", ["send_messages"])
    assert not (tmp_path / "agents/sneaky_agent.yaml").exists()


# --- maturity ---------------------------------------------------------------------------------


def maturity(admin, days=60):
    from pathlib import Path

    query = (Path(__file__).resolve().parents[1] / "db/reports/maturity.sql").read_text(encoding="utf-8")
    now = datetime.now(timezone.utc)
    names = None
    cur = admin.execute(query, {"since": now - timedelta(days=days), "until": now + timedelta(days=1)})
    names = [d.name for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


def digest(admin, team, weeks_ago, status="published"):
    at = datetime.now(timezone.utc) - timedelta(weeks=weeks_ago)
    admin.execute("""insert into counsel.drafts (kind, body, status, reviewed_at, reviewed_by)
                     values (%s, 'd', %s, %s, 'a person')""", (f"team_digest:{team}", status, at))


@pytest.fixture
def no_digests(platform):
    platform.execute("delete from counsel.drafts where kind like 'team_digest:%%'")
    yield platform
    platform.execute("delete from counsel.drafts where kind like 'team_digest:%%'")


def test_a_team_with_nothing_accepted_grades_zero(no_digests):
    assert maturity(no_digests)["core"]["grade"] == 0


def test_a_drafted_but_unreviewed_digest_counts_for_nothing(no_digests):
    no_digests.execute("insert into counsel.drafts (kind, body) values ('team_digest:core', 'd')")
    assert maturity(no_digests)["core"]["grade"] == 0


def test_grades_climb_with_sustained_use_only(no_digests):
    digest(no_digests, "core", 0)
    assert maturity(no_digests)["core"]["grade"] == 1
    digest(no_digests, "core", 1)
    assert maturity(no_digests)["core"]["grade"] == 2
    digest(no_digests, "core", 2)
    assert maturity(no_digests)["core"]["grade"] == 3
    # Another kind of output, carried in a person's own words.
    no_digests.execute("insert into work.task_updates (task_id, body, author, on_behalf_of) values (%s, 'n', 'x', 'aiko')",
                       (TASK_A,))
    assert maturity(no_digests)["core"]["grade"] == 4


def test_a_team_that_rejects_its_digests_does_not_reach_the_top(no_digests):
    for weeks in (0, 1, 2):
        digest(no_digests, "core", weeks)
    for weeks in (0, 1):
        digest(no_digests, "core", weeks, status="rejected")
    no_digests.execute("insert into work.task_updates (task_id, body, author, on_behalf_of) values (%s, 'n', 'x', 'aiko')",
                       (TASK_A,))
    row = maturity(no_digests)["core"]
    assert (row["grade"], row["rejected"], row["digests_reviewed"]) == (3, 2, 5)


def test_one_teams_use_never_lifts_another(no_digests):
    digest(no_digests, "core", 0)
    assert maturity(no_digests)["growth"]["grade"] == 0
