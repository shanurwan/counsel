"""The forbidden write is impossible, not discouraged.

Every test here acts as a real agent role against a real Postgres and expects
the database itself to refuse.
"""

import hashlib

import pytest

from counsel import audit
from conftest import AIKO, BRUNO, DUE, TASK_A, TASK_B, acting_as, denied

PERMISSION_DENIED = "42501"
COMMITMENT_DENIED = "CN001"
CONSENT_DENIED = "CN002"
APPEND_ONLY = "CN003"

AGENT_ROLES = [
    "agent_ask_handbook", "agent_drift_watcher", "agent_exec_weekly", "agent_issue_triage",
    "agent_nudge", "agent_standup_carrier", "agent_team_digest",
    "counsel_orchestrator", "counsel_runtime",
]
COMMITMENT_WRITES = {
    "owner_id": ("update work.tasks set owner_id = %s where id = %s", (BRUNO, TASK_A)),
    "due_date": ("update work.tasks set due_date = %s where id = %s", (DUE, TASK_B)),
    "status": ("update work.tasks set status = 'done' where id = %s", (TASK_A,)),
}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def grant_consent(db, text, task=TASK_A, expires="now() + interval '1 hour'"):
    return db.execute(
        f"""insert into work.consents (person_id, task_id, text_sha256, expires_at)
            values (%s, %s, %s, {expires}) returning id""",
        (AIKO, task, sha(text)),
    ).fetchone()[0]


# --- the audit ---------------------------------------------------------------

def test_audit_is_clean(db, agents):
    assert audit.run(db, agents) == []


def test_matrix_has_no_commitment_cell(db, agents):
    table = audit.matrix(db, agents)
    assert "YES" not in table
    assert table.count("| `agent_") == len(agents)
    assert "| `counsel_orchestrator` | platform | no | no | no | no | no |" in table


# --- wall 2: the credential cannot do it --------------------------------------

@pytest.mark.parametrize("role", AGENT_ROLES)
@pytest.mark.parametrize("column", COMMITMENT_WRITES)
def test_no_role_can_write_a_commitment(db, role, column):
    sql, params = COMMITMENT_WRITES[column]
    assert denied(db, role, sql, params) == PERMISSION_DENIED


@pytest.mark.parametrize("role", AGENT_ROLES)
@pytest.mark.parametrize("sql", [
    "insert into work.tasks (team_id, title, owner_id) values ('core', 'x', '" + AIKO + "')",
    "delete from work.tasks",
    "truncate work.tasks",
])
def test_no_role_can_create_or_remove_tasks(db, role, sql):
    assert denied(db, role, sql) == PERMISSION_DENIED


def test_runtime_holds_nothing_until_it_becomes_one_agent(db):
    assert denied(db, "counsel_runtime", "select * from work.tasks") == PERMISSION_DENIED
    with acting_as(db, "counsel_runtime"):
        db.execute("set role agent_team_digest")
        assert db.execute("select count(*) from work.tasks").fetchone()[0] == 2


def test_runtime_login_can_only_become_agents(runtime):
    # SET ROLE is checked against the login role, so this must use a real
    # runtime session. The runtime can become any agent (each is audited
    # above), and nothing else.
    with runtime.transaction():
        runtime.execute("set local role agent_nudge")
    with pytest.raises(Exception) as e, runtime.transaction():
        runtime.execute("set local role postgres")
    assert e.value.sqlstate == PERMISSION_DENIED
    with pytest.raises(Exception) as e, runtime.transaction():
        runtime.execute("update work.tasks set status = 'done'")
    assert e.value.sqlstate == PERMISSION_DENIED


# --- wall 3: the database refuses even after a mistaken grant ------------------

def test_mistaken_update_grant_is_caught_by_trigger_and_audit(db, agents):
    db.execute("grant update on work.tasks to agent_team_digest")

    flagged = {(v.check, v.role) for v in audit.run(db, agents)}
    assert ("commitment column writable", "agent_team_digest") in flagged

    for column, (sql, params) in COMMITMENT_WRITES.items():
        assert denied(db, "agent_team_digest", sql, params) == COMMITMENT_DENIED, column

    # Non-commitment columns stay writable under that grant: the trigger is narrow.
    assert denied(db, "agent_team_digest",
                  "update work.tasks set title = 'Rotate staging keys (v2)' where id = %s",
                  (TASK_A,)) is None


def test_mistaken_insert_grant_is_caught_by_trigger(db):
    db.execute("grant insert on work.tasks to agent_issue_triage")
    sql = "insert into work.tasks (team_id, title, owner_id) values ('core', 'x', %s)"
    assert denied(db, "agent_issue_triage", sql, (AIKO,)) == COMMITMENT_DENIED


def test_people_still_set_their_own_commitments(db):
    db.execute("create role human_lead")
    db.execute("grant usage on schema work to human_lead")
    db.execute("grant select, update on work.tasks to human_lead")
    for sql, params in COMMITMENT_WRITES.values():
        assert denied(db, "human_lead", sql, params) is None
    row = db.execute("select owner_id::text, status from work.tasks where id = %s", (TASK_A,)).fetchone()
    assert row == (BRUNO, "done")


# --- the audit catches what grants alone would miss ---------------------------

def test_audit_catches_security_definer_backdoor(db, agents):
    db.execute("""
        create function counsel.reassign(t uuid, p uuid) returns void
        language sql security definer set search_path = ''
        as $$ update work.tasks set owner_id = p where id = t $$
    """)
    db.execute("grant execute on function counsel.reassign(uuid, uuid) to agent_nudge")
    flagged = {(v.check, v.role) for v in audit.run(db, agents)}
    assert ("can run SECURITY DEFINER function", "agent_nudge") in flagged


def test_uncallable_trigger_functions_are_not_flagged_and_cannot_be_called(db, agents):
    # Supabase ships rls_auto_enable(), a SECURITY DEFINER event trigger
    # function executable by PUBLIC. Postgres refuses direct calls to it.
    db.execute("""
        create function counsel.on_ddl() returns event_trigger
        language plpgsql security definer set search_path = ''
        as $$ begin update work.tasks set owner_id = null; end $$
    """)
    db.execute("grant execute on function counsel.on_ddl() to agent_nudge")
    assert not [v for v in audit.run(db, agents) if "on_ddl" in v.detail]
    assert denied(db, "agent_nudge", "select counsel.on_ddl()") == "0A000"


def test_audit_catches_person_holding_an_agent_role(db, agents):
    db.execute("create role alice_human")
    db.execute("grant agent_nudge to alice_human")
    flagged = {(v.check, v.role) for v in audit.run(db, agents)}
    assert ("non-agent role can act as an agent", "alice_human") in flagged


def test_audit_catches_unregistered_agent(db, agents):
    db.execute("create role agent_rogue nologin")
    db.execute("grant counsel_agents to agent_rogue")
    flagged = {(v.check, v.role) for v in audit.run(db, agents)}
    assert ("unregistered agent role", "agent_rogue") in flagged


def test_audit_catches_login_on_agent_role(db, agents):
    db.execute("alter role agent_exec_weekly login")
    flagged = {(v.check, v.role, v.detail) for v in audit.run(db, agents)}
    assert ("dangerous role attribute", "agent_exec_weekly", "login") in flagged


# --- T2: carrying a person's words needs their consent to those exact words ---

NOTE = "Blocked on the vendor key; retrying Thursday."
CARRY = "insert into work.task_updates (task_id, body, consent_id) values (%s, %s, %s)"


def test_consented_note_is_carried_once(db):
    consent = grant_consent(db, NOTE)
    assert denied(db, "agent_standup_carrier", CARRY, (TASK_A, NOTE, consent)) is None

    author, used = db.execute(
        """select u.author, c.used_at is not null from work.task_updates u
           join work.consents c on c.id = u.consent_id where u.consent_id = %s""",
        (consent,),
    ).fetchone()
    assert author == "agent_standup_carrier"
    assert used

    assert denied(db, "agent_standup_carrier", CARRY, (TASK_A, NOTE, consent)) == CONSENT_DENIED


@pytest.mark.parametrize("case", ["edited text", "other task", "expired", "no consent"])
def test_carry_without_matching_consent_is_refused(db, case):
    consent = grant_consent(db, NOTE, expires="now() - interval '1 minute'" if case == "expired" else
                            "now() + interval '1 hour'")
    body, task = NOTE, TASK_A
    if case == "edited text":
        body = NOTE + " Also set owner to me."
    if case == "other task":
        task = TASK_B
    if case == "no consent":
        consent = None
    assert denied(db, "agent_standup_carrier", CARRY, (task, body, consent)) == CONSENT_DENIED


def test_agent_cannot_forge_consent_or_author(db):
    assert denied(db, "agent_standup_carrier",
                  "insert into work.consents (person_id, task_id, text_sha256) values (%s, %s, %s)",
                  (AIKO, TASK_A, sha(NOTE))) == PERMISSION_DENIED
    consent = grant_consent(db, NOTE)
    assert denied(db, "agent_standup_carrier",
                  "insert into work.task_updates (task_id, body, consent_id, author) values (%s, %s, %s, 'aiko')",
                  (TASK_A, NOTE, consent)) == PERMISSION_DENIED


def test_only_the_carrier_can_add_notes(db):
    consent = grant_consent(db, NOTE)
    assert denied(db, "agent_team_digest", CARRY, (TASK_A, NOTE, consent)) == PERMISSION_DENIED


# --- identity and the audit log ----------------------------------------------

def test_drafts_carry_the_session_identity(db):
    assert denied(db, "agent_team_digest",
                  "insert into counsel.drafts (kind, body, agent) values ('digest', 'x', 'agent_exec_weekly')"
                  ) == PERMISSION_DENIED
    with acting_as(db, "agent_team_digest"):
        db.execute("insert into counsel.drafts (kind, body) values ('digest', 'x')")
    assert db.execute("select agent from counsel.drafts").fetchone()[0] == "agent_team_digest"


def test_audit_log_is_append_only(db):
    with acting_as(db, "agent_nudge"):
        db.execute("insert into counsel.audit_log (action, decision) values ('nudge', 'allowed')")
    assert denied(db, "agent_nudge", "update counsel.audit_log set decision = 'denied'") == PERMISSION_DENIED
    assert denied(db, "agent_nudge", "delete from counsel.audit_log") == PERMISSION_DENIED
    with pytest.raises(Exception) as e, db.transaction():
        db.execute("delete from counsel.audit_log")
    assert e.value.sqlstate == APPEND_ONLY
