"""The support drafter's walls: secrets and the customer thread are out of
reach for every agent, and an agent sees only the ticket it was started for.
"""

import psycopg
import pytest

from counsel import audit, tools
from counsel.behaviors import SUPPORT_NOT_COVERED, support_drafter

from conftest import acting_as, denied
from test_runtime import FakeModel, admin, make_worker, one, runtime_url  # noqa: F401

DRAFTER = "agent_support_drafter"
OWN = "40000000-0000-0000-0000-0000000000f1"
OTHER = "40000000-0000-0000-0000-0000000000f2"
SUPPORT_ONLY = {"support_drafter": support_drafter}


def seed_support(conn):
    conn.execute("""
        insert into support.customers values ('tidewater', 'Tidewater'), ('lumen', 'Lumen')
          on conflict do nothing;
        insert into support.integration_tokens (customer_id, provider, token)
          values ('tidewater', 'stripe', 'TEST-FAKE-TOKEN-1');
        insert into kb.documents (path, body) values ('product/connection-pooling.md',
          'Connection pooling: use the pooler on port 6543 for too many connections.')
          on conflict (path) do nothing;
    """)


def add_ticket(conn, ticket_id, customer, subject, body):
    conn.execute("insert into support.tickets (id, customer_id, subject, body) values (%s, %s, %s, %s)",
                 (ticket_id, customer, subject, body))


@pytest.fixture
def support_db(db):
    seed_support(db)
    add_ticket(db, OWN, "tidewater", "Too many connections", "Is the pooler on?")
    add_ticket(db, OTHER, "lumen", "Restore a backup", "Private details of another customer.")
    return db


def all_agent_roles(agents):
    return [a.role for a in agents] + ["counsel_orchestrator", "counsel_runtime"]


def test_no_agent_can_read_or_touch_secrets(support_db, agents):
    for role in all_agent_roles(agents):
        assert denied(support_db, role, "select token from support.integration_tokens") == "42501", role
        assert denied(support_db, role, "delete from support.integration_tokens") == "42501", role


def test_no_agent_can_post_to_the_customer_thread(support_db, agents):
    for role in all_agent_roles(agents):
        assert denied(support_db, role,
                      "insert into support.ticket_messages (ticket_id, body) values (%s, 'hi')",
                      (OWN,)) == "42501", role


def test_drafter_sees_only_the_ticket_it_was_started_for(support_db):
    with acting_as(support_db, DRAFTER):
        assert support_db.execute("select count(*) from support.tickets").fetchone() == (0,)
        support_db.execute("select set_config('counsel.ticket', %s, true)", (OWN,))
        rows = support_db.execute("select id::text from support.tickets").fetchall()
    assert rows == [(OWN,)]


def test_audit_flags_a_hand_granted_secret(support_db, agents):
    support_db.execute(f"grant select on support.integration_tokens to {DRAFTER}")
    flagged = {(v.check, v.role) for v in audit.run(support_db, agents)}
    assert ("secret reachable", DRAFTER) in flagged


def test_audit_flags_a_hand_granted_customer_thread(support_db, agents):
    support_db.execute(f"grant insert on support.ticket_messages to {DRAFTER}")
    flagged = {(v.check, v.role) for v in audit.run(support_db, agents)}
    assert ("forbidden table privilege", DRAFTER) in flagged


@pytest.mark.parametrize("table", [
    "support.tickets; set role postgres",
    "tickets",
    'support."tickets"',
    "support.tickets--",
])
def test_tool_calls_cannot_carry_sql(table):
    with pytest.raises(tools.InvalidCall):
        tools.build({"tool": "select", "args": {"table": table}})


def test_tool_layer_has_no_allow_list_postgres_decides(support_db):
    with acting_as(support_db, DRAFTER):
        with pytest.raises(psycopg.Error) as e:
            tools.execute(support_db, {"tool": "select", "args": {"table": "support.integration_tokens"}})
    assert e.value.sqlstate == "42501"


def test_unknown_table_is_returned_to_the_model_not_raised(support_db):
    with acting_as(support_db, DRAFTER):
        result = tools.execute(support_db, {"tool": "select", "args": {"table": "support.nope"}})
    assert result["error"].startswith("42P01")


# --- the drafter end to end, as counsel_runtime ------------------------------------------


@pytest.fixture
def support_admin(admin):
    admin.execute("truncate support.ticket_messages, support.tickets, support.integration_tokens cascade")
    seed_support(admin)
    yield admin
    admin.execute("truncate support.ticket_messages, support.tickets, support.integration_tokens cascade")
    admin.execute("delete from kb.documents where path like 'product/%'")


def test_ticket_is_drafted_with_citation_and_never_posted(support_admin, make_worker):
    add_ticket(support_admin, OWN, "tidewater", "Too many connections", "Is the pooler on?")
    model = FakeModel(text='{"tool": "draft_reply", "args": {"body": "Use port 6543 '
                           '[product/connection-pooling.md]."}}')
    summary = make_worker(model, SUPPORT_ONLY).tick()
    assert [r["result"] for r in summary["runs"]] == ["succeeded"]
    kind, body = one(support_admin, "select kind, body from counsel.drafts")
    assert kind == f"support_reply:{OWN}" and "[product/connection-pooling.md]" in body
    assert one(support_admin, "select count(*) from support.ticket_messages") == (0,)
    # The other customer's ticket never reached the model.
    assert "Restore a backup" not in str(model.calls)


def test_ticket_the_docs_do_not_cover_never_reaches_the_model(support_admin, make_worker):
    add_ticket(support_admin, OWN, "tidewater", "Invoice address", "Change our billing address?")
    model = FakeModel()
    make_worker(model, SUPPORT_ONLY).tick()
    assert model.calls == []
    assert one(support_admin, "select body from counsel.drafts") == (SUPPORT_NOT_COVERED,)


def test_a_refused_tool_call_ends_the_run_and_is_audited(support_admin, make_worker):
    add_ticket(support_admin, OWN, "tidewater", "Too many connections", "Is the pooler on?")
    model = FakeModel(text='{"tool": "select", "args": {"table": "support.integration_tokens"}}')
    summary = make_worker(model, SUPPORT_ONLY).tick()
    assert [r["result"] for r in summary["runs"]] == ["denied"]
    action, decision, detail = one(support_admin, "select action, decision, detail from counsel.audit_log "
                                                  "order by id desc limit 1")
    assert (action, decision, detail["sqlstate"]) == ("support_drafter:denied_write", "denied", "42501")
    assert one(support_admin, "select count(*) from counsel.drafts") == (0,)
