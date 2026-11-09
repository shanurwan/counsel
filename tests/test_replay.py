"""The replay sees an issue as it was when it opened, never what came after. No network."""

import json
from datetime import datetime, timezone

from counsel import replay

UTC = timezone.utc
REPO = "acme/widgets"
BASE = f"https://api.github.com/repos/{REPO}"


def issue(n, created, title, *, closed=None, assoc="NONE", kind="User", labels=(), body="", pr=False):
    i = {"number": n, "title": title, "body": body, "created_at": created, "closed_at": closed,
         "author_association": assoc, "user": {"login": f"u{n}", "type": kind},
         "labels": [{"name": label} for label in labels], "html_url": f"https://github.com/{REPO}/issues/{n}",
         "state": "closed" if closed else "open"}
    if pr:
        i["pull_request"] = {}
    return i


POOL = [
    issue(9, "2026-10-20T00:00:00Z", "Login token refresh fails after upgrade", labels=["bug", "auth"]),
    issue(8, "2026-10-12T00:00:00Z", "Login token refresh fails on Safari"),
    issue(7, "2026-10-11T00:00:00Z", "Login token refresh fails, closed early", closed="2026-10-15T00:00:00Z"),
    issue(6, "2026-10-10T00:00:00Z", "Team roadmap for login token refresh", assoc="MEMBER"),
    issue(5, "2026-10-09T00:00:00Z", "Login token refresh fails (bot)", kind="Bot"),
    issue(4, "2026-10-08T00:00:00Z", "Login token refresh fails in a PR", pr=True),
    issue(3, "2026-06-01T00:00:00Z", "Login token refresh fails long ago"),
]


def get_factory(pages, extra=None):
    answers = {f"{BASE}/issues?state=all&sort=created&direction=desc&per_page=100&page={k + 1}": p
               for k, p in enumerate(pages)}
    answers.update(extra or {})

    def get(url):
        if url in answers:
            return answers[url]
        if "comments" in url or "timeline" in url:
            return []
        return {"created_at": "2026-10-20T00:00:00Z", "state": "open", "labels": []}
    return get


def test_the_agent_sees_only_issues_open_when_this_one_opened_and_no_labels():
    since, until = datetime(2026, 10, 15, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    pool = replay.fetch(REPO, since, until, 90, get_factory([POOL]))
    assert [i["number"] for i in pool] == [9, 8, 7, 6, 5]  # the PR and the issue before the pool are gone
    todo = replay.candidates(pool, since)
    assert [i["number"] for i in todo] == [9]  # in the window, from outside the team, not a bot
    seen = replay.as_seen(todo[0], pool)
    assert seen["labels"] == []
    # 8, 6 and 5 were open when 9 opened; 7 had already closed.
    assert {s["number"] for s in seen["similar"]} == {8, 6, 5}


def test_an_issue_opened_later_is_never_a_candidate():
    later = issue(10, "2026-10-25T00:00:00Z", "Login token refresh fails after upgrade again")
    assert not replay.open_at(later, datetime(2026, 10, 20, tzinfo=UTC))


def test_a_run_writes_the_report_with_bounds_and_every_issue(tmp_path):
    calls = []

    class Model:
        def complete(self, messages, max_tokens=800):
            calls.append(json.loads(messages[1]["content"]))
            return type("C", (), {"text": "Token refresh fails. Area: auth. Missing: the SDK version. "
                                          "Possible duplicate: #8."})()

    since, until = datetime(2026, 10, 15, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    get = get_factory([POOL], {f"{BASE}/issues/9": {"created_at": "2026-10-20T00:00:00Z", "state": "closed",
                                                     "state_reason": "duplicate", "labels": []}})
    summary = replay.run([REPO], since, until, Model(), tmp_path / "field", tmp_path / "doc.md", get=get,
                         log=lambda m: None, now=lambda: datetime(2026, 11, 12, tzinfo=UTC))
    assert len(calls) == 2 and calls[0]["labels"] == []  # the note, then the duplicate check
    assert summary["duplicate_precision"]["k"] == 1 and summary["duplicate_precision"]["n"] == 1
    page = (tmp_path / "doc.md").read_text(encoding="utf-8")
    assert "acme/widgets#9" in page and "lower bound" in page and "avoids hindsight" in page


def test_an_invented_issue_number_is_flagged_and_its_duplicate_claim_not_counted():
    from counsel import field
    from counsel.behaviors import check_references

    seen = replay.as_seen(POOL[0], POOL)
    note = check_references("Possible duplicate: #8. Also see #4242.", seen)
    assert "4242" in note and field.read_note(note, 9)[0] == []


def test_a_duplicate_the_check_calls_related_becomes_related_and_is_not_counted():
    from counsel import field
    from counsel.behaviors import triage_note

    seen = replay.as_seen(POOL[0], POOL)
    asked = []

    def complete(name, messages, max_tokens):
        asked.append((name, json.loads(messages[1]["content"])))
        if name == "triage":
            return "Token refresh fails after upgrade. Area: auth. Possible duplicate: #8."
        return "RELATED"

    note = triage_note(complete, seen)
    assert "Related: #8" in note and field.read_note(note, 9)[0] == []
    # The note's model sees titles only; the check sees both bodies.
    assert "candidates" not in asked[0][1] and asked[1][0] == "verify duplicate"
    assert asked[1][1]["older"]["number"] == 8


def test_a_duplicate_the_check_confirms_stays():
    from counsel import field
    from counsel.behaviors import triage_note

    seen = replay.as_seen(POOL[0], POOL)
    note = triage_note(lambda name, m, t: "Fails. Duplicate: #8." if name == "triage" else "DUPLICATE", seen)
    assert field.read_note(note, 9)[0] == [8]


def test_related_and_no_duplicate_are_not_claims():
    from counsel.behaviors import duplicate_references

    assert duplicate_references("Area: auth. Related: #8. No duplicate found.", 9) == []
    assert duplicate_references("Possible duplicate: #8; related: #6.", 9) == [8]
    assert duplicate_references("This is a duplicate of #8.", 9) == [8]


def test_a_rate_limited_run_waits_resumes_and_never_pays_twice(tmp_path):
    from counsel.model import ModelError

    pool = [issue(n, f"2026-10-{n + 10:02d}T00:00:00Z", f"Distinct problem number {n} widget") for n in range(1, 4)]
    get = get_factory([pool])
    since, until = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    calls, waits = [], []

    class Flaky:
        def __init__(self, fail_after):
            self.fail_after = fail_after

        def complete(self, messages, max_tokens=800):
            if len(calls) >= self.fail_after:
                raise ModelError("nvidia: 429 Too Many Requests")
            calls.append(1)
            return type("C", (), {"text": "A problem. No duplicate found."})()

    # Two notes, then the provider refuses for longer than the backoff lasts.
    try:
        replay.run([REPO], since, until, Flaky(2), tmp_path / "f", tmp_path / "d.md", get=get, name="x",
                   log=lambda m: None, sleep=waits.append)
    except ModelError:
        pass
    assert len(calls) == 2 and waits[:2] == [20, 40]
    assert len((tmp_path / "f" / "x.partial.jsonl").read_text().splitlines()) == 2
    # The rerun pays only for the third note, then removes the checkpoint.
    summary = replay.run([REPO], since, until, Flaky(99), tmp_path / "f", tmp_path / "d.md", get=get, name="x",
                         log=lambda m: None, sleep=lambda s: None)
    assert len(calls) == 3 and summary["triaged"] == 3
    assert not (tmp_path / "f" / "x.partial.jsonl").exists()
