"""Scoring a field run: each triage draft against what maintainers did. No network."""

from counsel import field

ISSUE = {"repo": "acme/widgets", "number": 7, "title": "Login fails", "url": "https://github.com/acme/widgets/issues/7",
         "opened_at": "2026-11-09T00:00:00Z", "drafted_at": "2026-11-09T00:30:00Z"}


def github(issue=None, comments=(), timeline=()):
    base = "https://api.github.com/repos/acme/widgets/issues/7"
    answers = {base: {"created_at": "2026-11-09T00:00:00Z", "state": "open", "labels": [], **(issue or {})},
               f"{base}/comments?per_page=100": list(comments), f"{base}/timeline?per_page=100": list(timeline)}
    return answers.__getitem__


def test_a_named_duplicate_is_read_from_the_note_but_not_an_unchecked_one():
    assert field.read_note("Login fails on Safari. Possible duplicate: #3.", 7)[0] == [3]
    flagged = field.UNCHECKED_REFERENCE.format("#99") + "\n\nPossible duplicate: #99."
    assert field.read_note(flagged, 7)[0] == []
    assert field.read_note("No duplicate found.", 7)[0] == []


def test_missing_information_is_read_both_ways():
    assert field.read_note("Missing: the client version and steps to reproduce.", 7)[1] is True
    assert field.read_note("Nothing is missing. No duplicate found.", 7)[1] is False
    assert field.read_note("No information is missing.", 7)[1] is False


def test_a_confirmed_duplicate_and_a_maintainer_asking_for_more():
    draft = {**ISSUE, "note": "Login fails. Missing: SDK version. Possible duplicate: #3."}
    scored = field.score(draft, github(
        issue={"state": "closed", "state_reason": "duplicate", "labels": [{"name": "bug"}]},
        comments=[{"author_association": "NONE", "created_at": "2026-11-09T01:00:00Z", "body": "same here"},
                  {"author_association": "MEMBER", "created_at": "2026-11-09T05:00:00Z",
                   "body": "Could you share which version of the SDK you use?"}],
        timeline=[{"event": "marked_as_duplicate", "source": {"issue": {"number": 3}}}]))
    assert scored.claimed_duplicates == [3] and scored.truly_duplicate and scored.marked_duplicate_of == [3]
    assert scored.maintainer_replied and scored.maintainer_asked_for_more and scored.first_reply_hours == 5.0
    summary = field.summarise([scored])
    assert summary["duplicates"] == {"claimed": 1, "confirmed": 1, "maintainers_closed_as_duplicate": 1, "missed": 0}
    assert summary["missing_info"]["both"] == 1


def test_a_reporter_comment_is_not_a_maintainer_and_untouched_is_not_a_miss():
    draft = {**ISSUE, "note": "No duplicate found. Nothing is missing."}
    scored = field.score(draft, github(comments=[{"author_association": "CONTRIBUTOR",
                                                   "created_at": "2026-11-09T01:00:00Z", "body": "please share logs"}]))
    assert not scored.maintainer_replied and not scored.maintainer_asked_for_more
    assert field.summarise([scored])["maintainers_acted"] == 0
    assert "nothing yet" in field.render([scored], field.summarise([scored]), *(2 * [__import__("datetime").datetime(
        2026, 11, 9)]))


def test_github_being_unreachable_is_recorded_not_raised():
    def down(url):
        raise OSError("network unreachable")

    scored = field.score({**ISSUE, "note": "No duplicate found."}, down)
    assert scored.error and field.summarise([scored])["scored"] == 0


def test_a_shadow_mode_draft_can_never_be_posted(monkeypatch):
    import pytest

    from counsel import cli, review

    monkeypatch.delenv("COUNSEL_PUBLISH_OWNERS", raising=False)
    assert cli.may_post("shanurwan/counsel") and not cli.may_post("supabase/auth")
    with pytest.raises(review.ReviewError, match="shadow mode"):
        cli._deliver("issue_triage:supabase/auth#123", "a note")
    # An owner who said yes is added by name, nobody else.
    monkeypatch.setenv("COUNSEL_PUBLISH_OWNERS", "shanurwan, some-maintainer")
    assert cli.may_post("Some-Maintainer/tool") and not cli.may_post("supabase/auth")


def test_the_published_note_itself_is_not_a_maintainer_reply():
    note = "The issue asks for hashed pip installs. Area: CI. Missing: the workflow paths. No duplicate found."
    scored = field.score({**ISSUE, "note": note, "review": "published"}, github(comments=[
        {"author_association": "OWNER", "created_at": "2026-11-09T01:00:00Z", "body": note}]))
    assert not scored.maintainer_replied and scored.review == "published"
    assert field.summarise([scored])["owner_review"] == {"reviewed": 1, "published": 1, "rejected": 0,
                                                         "repos": ["acme/widgets"]}
