"""standup_carrier: a person's own words, carried only with their consent to the exact text."""

import json

import pytest

from counsel import review
from counsel.behaviors import _activity, carry_proposals, standup_carrier

from conftest import AIKO, BRUNO, TASK_A, TASK_B, acting_as, denied
from test_runtime import FakeModel, admin, make_worker, one, runtime_url  # noqa: F401

CARRIER = "agent_standup_carrier"
ONLY = {"standup_carrier": standup_carrier}
STANDUP = "Rotated the staging keys today. Lunch was great."
NOTE = "Rotated the staging keys today."


def notes(*pairs):
    return json.dumps({"notes": [{"task": t, "text": x} for t, x in pairs]})


@pytest.fixture
def standup(admin):
    admin.execute("truncate work.standups, work.consents cascade")
    admin.execute("delete from work.task_updates")
    sid = admin.execute("insert into work.standups (person_id, body) values (%s, %s) returning id::text",
                        (AIKO, STANDUP)).fetchone()[0]
    yield sid
    admin.execute("delete from work.task_updates")
    admin.execute("truncate work.standups, work.consents cascade")


def proposals(admin):
    return admin.execute("select id::text, body from counsel.drafts where kind like 'carry:%' order by body").fetchall()


# --- what may become a proposal ---------------------------------------------------------


STANDUP_DATA = {"body": STANDUP, "tasks": {TASK_A: "Rotate staging keys"}}


@pytest.mark.parametrize("model_notes, kept, reason", [
    ([("Rotate staging keys", NOTE)], 1, None),
    ([("Rotate staging keys", "Rotated keys and fixed the CI too.")], 0, "not the person's own words"),
    ([("Pricing page copy", NOTE)], 0, "not a task of the person's team"),
    ([("Rotate staging keys", NOTE), ("Rotate staging keys", NOTE)], 1, "duplicate"),
    ([("Rotate staging keys", "")], 0, "empty"),
])
def test_only_verbatim_notes_for_the_persons_tasks_become_proposals(model_notes, kept, reason):
    keep, dropped = carry_proposals(notes(*model_notes), STANDUP_DATA)
    assert len(keep) == kept
    if reason:
        assert dropped[-1]["reason"] == reason


def test_an_unreadable_reply_proposes_nothing():
    assert carry_proposals("Sure! Here are the notes.", STANDUP_DATA) == ([], [])


# --- the flow, end to end ------------------------------------------------------------------


def test_a_standup_becomes_a_proposal_and_nothing_is_carried_yet(admin, make_worker, standup):
    make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY).tick()
    assert [b for _, b in proposals(admin)] == [NOTE]
    assert one(admin, "select count(*) from work.task_updates") == (0,)


def test_approval_carries_the_exact_words_credited_to_the_person(admin, make_worker, standup):
    worker = make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY)
    worker.tick()
    review.publish(admin, proposals(admin)[0][0])
    worker.tick()
    body, author, who, consent_used = one(admin, """
        select u.body, u.author, u.on_behalf_of, c.used_at is not null
        from work.task_updates u join work.consents c on c.id = u.consent_id""")
    assert (body, author, who, consent_used) == (NOTE, CARRIER, "aiko", True)


def test_the_digest_credits_the_person_not_the_agent(admin, make_worker, standup):
    from datetime import datetime, timedelta, timezone

    worker = make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY)
    worker.tick()
    review.publish(admin, proposals(admin)[0][0])
    worker.tick()
    activity = _activity(admin, "core", datetime.now(timezone.utc) + timedelta(minutes=5))
    assert [(u["by"], u["note"]) for u in activity["updates"]] == [("aiko", NOTE)]


def test_an_edit_at_approval_is_what_gets_carried(admin, make_worker, standup):
    worker = make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY)
    worker.tick()
    review.publish(admin, proposals(admin)[0][0], body="Rotated the staging keys; prod next week.")
    worker.tick()
    assert one(admin, "select body from work.task_updates") == ("Rotated the staging keys; prod next week.",)


def test_a_rejected_proposal_is_never_carried(admin, make_worker, standup):
    worker = make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY)
    worker.tick()
    review.reject(admin, proposals(admin)[0][0])
    worker.tick()
    assert one(admin, "select count(*) from work.task_updates") == (0,)


def test_a_carry_whose_text_was_changed_after_consent_is_refused_and_audited(admin, make_worker, standup):
    worker = make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY)
    worker.tick()
    review.publish(admin, proposals(admin)[0][0])
    # Tamper with the queued carry: same consent, different words.
    admin.execute("""update counsel.jobs set payload = jsonb_set(payload, '{text}', '"Keys rotated, task done."')
                     where idempotency_key like 'carry:%'""")
    summary = worker.tick()
    assert [r["result"] for r in summary["runs"]] == ["denied"]
    assert one(admin, "select count(*) from work.task_updates") == (0,)
    assert one(admin, "select detail->>'sqlstate' from counsel.audit_log where action <> 'rollback' "
                      "order by id desc limit 1") == ("CN002",)


def test_a_consent_carries_once(admin, make_worker, standup):
    worker = make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY)
    worker.tick()
    review.publish(admin, proposals(admin)[0][0])
    worker.tick()
    admin.execute("""insert into counsel.jobs (agent, idempotency_key, payload)
                     select agent, 'replay:' || id, payload from counsel.jobs where idempotency_key like 'carry:%'""")
    assert [r["result"] for r in worker.tick()["runs"]] == ["denied"]
    assert one(admin, "select count(*) from work.task_updates") == (1,)


def test_carrying_never_changes_a_commitment(admin, make_worker, standup):
    before = admin.execute("select owner_id, due_date, status from work.tasks order by id").fetchall()
    worker = make_worker(FakeModel(text=notes(("Rotate staging keys", NOTE))), ONLY)
    worker.tick()
    review.publish(admin, proposals(admin)[0][0])
    worker.tick()
    assert admin.execute("select owner_id, due_date, status from work.tasks order by id").fetchall() == before


# --- the walls -----------------------------------------------------------------------------


def test_the_carrier_sees_only_the_standup_it_was_started_for(db):
    own = db.execute("insert into work.standups (person_id, body) values (%s, 'mine') returning id::text",
                     (AIKO,)).fetchone()[0]
    db.execute("insert into work.standups (person_id, body) values (%s, 'someone else')", (BRUNO,))
    with acting_as(db, CARRIER):
        assert db.execute("select count(*) from work.standups").fetchone() == (0,)
        db.execute("select set_config('counsel.standup', %s, true)", (own,))
        assert db.execute("select body from work.standups").fetchall() == [("mine",)]


def test_no_agent_can_grant_itself_consent_or_choose_whose_words(db, agents):
    for role in [a.role for a in agents] + ["counsel_orchestrator", "counsel_runtime"]:
        assert denied(db, role, "insert into work.consents (person_id, task_id, text_sha256) "
                                "values (%s, %s, repeat('a', 64))", (AIKO, TASK_B)) == "42501", role
    assert denied(db, CARRIER, "insert into work.task_updates (task_id, body, on_behalf_of) "
                               "values (%s, 'x', 'bruno')", (TASK_A,)) == "42501"
