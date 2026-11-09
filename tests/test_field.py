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


NOTES = "https://api.github.com/repos/acme/widgets/issues/50/comments?per_page=100"


def delivered(number, comment_id, run="11111111-1111-1111-1111-111111111111"):
    return field.Scored(repo="acme/widgets", number=number, title=f"Issue {number}",
                        url=f"https://github.com/acme/widgets/issues/{number}", note="n", drafted_at="",
                        claimed_duplicates=[], says_missing=False, run_id=run,
                        delivered_to=f"https://github.com/acme/widgets/issues/50#issuecomment-{comment_id}")


def person(login, kind="User"):
    return {"login": login, "type": kind}


def answers(reactions=None, notes=()):
    out = {NOTES: list(notes)}
    for cid, items in (reactions or {}).items():
        out[f"https://api.github.com/repos/acme/widgets/issues/comments/{cid}/reactions?per_page=100"] = items
    return lambda url: out.get(url, [])


def test_reactions_on_a_delivered_note_are_the_maintainers_verdict_and_the_author_never_counts():
    a, b = delivered(7, 100), delivered(8, 200)
    field.verdicts([a, b], {"acme/widgets": 50}, answers(reactions={
        100: [{"content": "+1", "user": person("kuba")}, {"content": "+1", "user": person("shanurwan")},
              {"content": "heart", "user": person("someone")}, {"content": "+1", "user": person("bot", "Bot")}],
        200: [{"content": "-1", "user": person("kuba")}]}))
    assert a.thumbs_up == ["kuba"] and a.author_reactions == ["👍"] and a.verdict == "helpful"
    assert b.thumbs_down == ["kuba"] and b.verdict == "not helpful"
    s = field.summarise([a, b])["delivered"]
    assert (s["notes"], s["rated"], s["helpful"], s["not_helpful"]) == (2, 2, 1, 1)
    assert 0 < s["helpful_lower_bound"] < 0.5 and s["users"] == {"kuba": ["acme/widgets"]}
    page = field.render([a, b], field.summarise([a, b]), *(2 * [__import__("datetime").datetime(2026, 11, 9)]))
    assert "**Users: 1.**" in page and "not counted" in page
    assert "counsel challenge 11111111-1111-1111-1111-111111111111 --id field_acme_widgets_8" in page


def test_only_the_author_reacting_means_no_users_and_no_rating():
    a = delivered(7, 100)
    field.verdicts([a], {}, answers(reactions={100: [{"content": "+1", "user": person("ShanUrwan")}]}))
    s = field.summarise([a])["delivered"]
    assert a.verdict is None and s["rated"] == 0 and s["users"] == {} and s["helpful_lower_bound"] is None


def test_a_reply_on_the_notes_issue_goes_to_the_note_it_mentions_else_the_latest_before_it():
    a, b = delivered(7, 100), delivered(8, 200)
    field.verdicts([a, b], {"acme/widgets": 50}, answers(notes=[
        {"id": 150, "user": person("kuba"), "author_association": "OWNER", "body": "Spot on."},
        {"id": 250, "user": person("kuba"), "author_association": "OWNER", "body": "#7 was not a duplicate"},
        {"id": 260, "user": person("shanurwan"), "body": "thanks"},
        {"id": 270, "user": person("counsel-triage[bot]", "Bot"), "body": "a note"}]))
    assert [x["body"] for x in a.replies] == ["Spot on.", "#7 was not a duplicate"] and b.replies == []
    assert field.summarise([a, b])["delivered"]["users"] == {"kuba": ["acme/widgets"]}


def test_a_maintainer_acting_on_a_delivered_issue_is_a_user_but_not_on_a_shadow_one():
    draft = {**ISSUE, "note": "No duplicate found.", "delivered_to": "https://github.com/acme/widgets/issues/50"
             "#issuecomment-100", "run_id": "r"}
    acted = field.score(draft, github(timeline=[{"event": "labeled", "actor": person("kuba")},
                                                {"event": "labeled", "actor": person("shanurwan")}]))
    shadow = field.score({**ISSUE, "note": "No duplicate found."},
                         github(timeline=[{"event": "closed", "actor": person("maint")}]))
    assert acted.acted_by == ["kuba"] and shadow.acted_by == ["maint"]
    assert field.summarise([acted, shadow])["delivered"]["users"] == {"kuba": ["acme/widgets"]}


def test_github_down_while_reading_verdicts_is_recorded_not_raised():
    def down(url):
        raise OSError("network unreachable")

    a = delivered(7, 100)
    field.verdicts([a], {"acme/widgets": 50}, down)
    assert a.error and a.verdict is None
