"""The value ledger counts only what a person accepted, and only a person can accept."""

import pytest

from counsel import registry, review
from counsel.behaviors import ACTION_FLAG, NOT_COVERED, SUPPORT_NOT_COVERED, ask_handbook, support_drafter

from conftest import denied
from test_runtime import FakeModel, admin, ask, make_worker, one, runtime_url  # noqa: F401
from test_support import OWN, add_ticket, support_admin  # noqa: F401

DRAFT_REPLY = '{"tool": "draft_reply", "args": {"body": "Use the pooler on port 6543 [product/connection-pooling.md]."}}'


def ledger(conn, agent):
    row = conn.execute("select coalesce(sum(accepted), 0), coalesce(sum(hours_absorbed), 0) "
                       "from counsel.value_ledger where agent = %s", (agent,)).fetchone()
    return int(row[0]), float(row[1])


def drafted(admin, make_worker, text=DRAFT_REPLY):
    add_ticket(admin, OWN, "tidewater", "Too many connections", "Is the pooler on?")
    make_worker(FakeModel(text=text), {"support_drafter": support_drafter}).tick()
    return one(admin, "select id::text from counsel.drafts")[0]


def test_registry_rejects_a_bad_minutes_estimate():
    entry = {"name": "x_agent", "description": "d", "owner": "ops", "tier": "T0",
             "capabilities": ["read_handbook"], "triggers": {"events": ["e"]}, "suite": "s",
             "minutes_per_output": 0}
    assert any("minutes_per_output" in p for p in registry.validate_entry(entry, "agents/x_agent.yaml"))


def test_the_compiled_registry_sets_each_agents_estimate(admin, agents):
    rows = dict(admin.execute("select agent, minutes_per_output from counsel.agent_settings").fetchall())
    assert rows == {a.name: a.minutes_per_output for a in agents}


def test_an_answered_question_counts_and_not_covered_does_not(admin, make_worker):
    ask(admin, "How many days of annual leave do we get?")
    ask(admin, "What is the dress code for the moon office?")
    make_worker(FakeModel(), {"ask_handbook": ask_handbook}).tick()
    assert one(admin, "select count(*) from counsel.answers where body = %s", NOT_COVERED) == (1,)
    assert ledger(admin, "ask_handbook") == (1, pytest.approx(6 / 60))


def test_a_draft_counts_only_once_a_person_publishes_it(support_admin, make_worker):
    draft = drafted(support_admin, make_worker)
    assert ledger(support_admin, "support_drafter")[0] == 0
    review.publish(support_admin, draft)
    assert ledger(support_admin, "support_drafter") == (1, pytest.approx(12 / 60))


def test_a_rejected_draft_never_counts(support_admin, make_worker):
    review.reject(support_admin, drafted(support_admin, make_worker))
    assert ledger(support_admin, "support_drafter")[0] == 0
    assert one(support_admin, "select count(*) from support.ticket_messages") == (0,)


def test_publishing_a_support_reply_posts_it_as_the_person(support_admin, make_worker):
    review.publish(support_admin, drafted(support_admin, make_worker))
    author, body = one(support_admin, "select author, body from support.ticket_messages")
    assert author == "postgres" and "6543" in body
    assert one(support_admin, "select reviewed_by from counsel.drafts") == ("postgres",)


def test_an_edit_keeps_what_the_agent_wrote(support_admin, make_worker):
    draft = drafted(support_admin, make_worker)
    review.publish(support_admin, draft, body="Edited by a person [product/connection-pooling.md].")
    body, published = one(support_admin, "select body, published_body from counsel.drafts")
    assert "6543" in body and published.startswith("Edited by a person")


@pytest.mark.parametrize("text, reason", [
    ('{"tool": "draft_reply", "args": {"body": "Shall I reset your pool size '
     '[product/connection-pooling.md]?"}}', "reviewer flag"),
])
def test_a_flagged_draft_cannot_be_published_unedited(support_admin, make_worker, text, reason):
    draft = drafted(support_admin, make_worker, text)
    assert ACTION_FLAG in one(support_admin, "select body from counsel.drafts")[0]
    with pytest.raises(review.ReviewError, match=reason):
        review.publish(support_admin, draft)
    review.publish(support_admin, draft, body="You can lower the pool size in Database settings.")


def test_nothing_to_publish_when_the_docs_did_not_cover_it(support_admin, make_worker):
    add_ticket(support_admin, OWN, "tidewater", "Invoice address", "Change our billing address?")
    make_worker(FakeModel(), {"support_drafter": support_drafter}).tick()
    draft = one(support_admin, "select id::text, body from counsel.drafts")
    assert draft[1] == SUPPORT_NOT_COVERED
    with pytest.raises(review.ReviewError, match="did not cover"):
        review.publish(support_admin, draft[0])


def test_a_draft_is_reviewed_once(support_admin, make_worker):
    draft = drafted(support_admin, make_worker)
    review.reject(support_admin, draft)
    with pytest.raises(review.ReviewError, match="already rejected"):
        review.publish(support_admin, draft)


def test_no_agent_can_review_a_draft(db, agents):
    for role in [a.role for a in agents] + ["counsel_orchestrator", "counsel_runtime"]:
        assert denied(db, role, "update counsel.drafts set status = 'published'") == "42501", role


def test_the_dashboard_reads_the_ledger_but_not_the_drafts(db):
    assert denied(db, "counsel_reader", "select * from counsel.value_ledger") is None
    assert denied(db, "counsel_reader", "select body from counsel.drafts") == "42501"
