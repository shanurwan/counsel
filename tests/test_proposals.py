"""Consent for each action: an assistant proposes a note, only the person approves it.

The browser side is called the way PostgREST calls it: as the authenticated role,
with the verified JWT's claims in request.jwt.claims (db/migrations/0014_proposals.sql).
"""

import hashlib
import json
import time
import uuid
from contextlib import contextmanager

import psycopg
import pytest

from counsel import audit

from conftest import AIKO, BRUNO, acting_as, denied

AIKO_LOGIN = "00000000-0000-0000-0000-0000000000e1"
BRUNO_LOGIN = "00000000-0000-0000-0000-0000000000e2"
CLIENT = "3f0c1a52-claude-connector"
TITLE = "Rotate staging keys"
TEXT = "Staging keys rotated; prod rotation booked for Thursday."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def linked(db):
    db.execute("insert into counsel.logins (auth_user_id, person_id) values (%s, %s), (%s, %s)",
               (AIKO_LOGIN, AIKO, BRUNO_LOGIN, BRUNO))
    return db


def propose(db, text=TEXT, title=TITLE, login=AIKO_LOGIN):
    with acting_as(db, audit.MCP_ROLE):
        return db.execute("select proposal_id from counsel.propose_note(%s, %s, %s, %s)",
                          (login, CLIENT, title, text)).fetchone()[0]


def session(login=AIKO_LOGIN, signed_in_ago=60, **extra):
    """The person's own browser session, as Supabase Auth issues it."""
    claims = {"sub": login, "role": "authenticated", "aud": "authenticated", "session_id": str(uuid.uuid4()),
              "amr": [{"method": "password", "timestamp": int(time.time()) - signed_in_ago}]}
    claims.update(extra)
    return claims


@contextmanager
def browser(db, claims, role="authenticated"):
    with acting_as(db, role):
        db.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        yield db


def decide(db, proposal, claims, approve=True, digest=None, role="authenticated"):
    """Returns the decision, or the SQLSTATE it was refused with."""
    try:
        with browser(db, claims, role):
            return db.execute("select public.counsel_decide(%s, %s, %s)",
                              (proposal, approve, digest if digest is not None else sha(TEXT))).fetchone()[0]
    except psycopg.Error as e:
        return e.sqlstate


def notes(db):
    return db.execute("select body, author, on_behalf_of, consent_id is not null from work.task_updates "
                      "where body = %s", (TEXT,)).fetchall()


# --- proposing writes nothing ----------------------------------------------------------------------


def test_a_proposal_writes_nothing_until_the_person_approves(linked):
    proposal = propose(linked)
    assert notes(linked) == []
    assert decide(linked, proposal, session()) == "approved"
    assert notes(linked) == [(TEXT, f"assistant:{CLIENT}", "aiko", True)]
    used = linked.execute("select c.used_at is not null, c.text_sha256 from counsel.proposals p "
                          "join work.consents c on c.id = p.consent_id where p.id = %s", (proposal,)).fetchone()
    assert used == (True, sha(TEXT))


@pytest.mark.parametrize("login, title", [
    (AIKO_LOGIN, "A task that does not exist"),
    (BRUNO_LOGIN, TITLE),                       # Aiko's task, proposed for Bruno
    (str(uuid.uuid4()), TITLE),                 # a sign-in nobody linked
])
def test_proposals_are_only_for_the_signed_in_persons_own_open_tasks(linked, login, title):
    with pytest.raises(psycopg.Error) as e:
        propose(linked, login=login, title=title)
    assert e.value.sqlstate == "CN010"


def test_a_model_in_a_loop_cannot_flood_the_person(linked):
    for i in range(5):
        propose(linked, text=f"note {i}")
    with pytest.raises(psycopg.Error) as e:
        propose(linked, text="one more")
    assert e.value.sqlstate == "CN010"


# --- only the person, at a browser, approves --------------------------------------------------------


def test_the_assistants_own_token_cannot_approve(linked):
    # Same person, same project, but issued to the OAuth client: it has client_id.
    proposal = propose(linked)
    assert decide(linked, proposal, session(client_id=CLIENT)) == "CN011"
    assert notes(linked) == []


def test_the_mcp_login_cannot_reach_the_approval_at_all(linked):
    proposal = propose(linked)
    # It could set the claims setting itself, so it must not be able to call the function.
    assert decide(linked, proposal, session(), role=audit.MCP_ROLE) == "42501"
    assert denied(linked, audit.MCP_ROLE, "select * from counsel.proposals") == "42501"
    assert denied(linked, audit.MCP_ROLE, "update counsel.proposals set decision = 'approved'") == "42501"
    assert denied(linked, audit.MCP_ROLE, "select counsel.browser_person(true)") == "42501"


def test_anonymous_callers_and_forged_settings_are_refused(linked):
    proposal = propose(linked)
    assert decide(linked, proposal, session(), role="anon") == "42501"
    # The claims setting without PostgREST's switch to the authenticated role.
    linked.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(session()),))
    with pytest.raises(psycopg.Error) as e:
        linked.execute("select public.counsel_decide(%s, true, %s)", (proposal, sha(TEXT)))
    assert e.value.sqlstate == "CN011"


def test_approving_needs_a_recent_sign_in(linked):
    proposal = propose(linked)
    assert decide(linked, proposal, session(signed_in_ago=3600)) == "CN012"
    assert decide(linked, proposal, session(amr=[])) == "CN012"
    assert decide(linked, proposal, session(signed_in_ago=30)) == "approved"


def test_someone_else_cannot_approve_or_read_it(linked):
    proposal = propose(linked)
    assert decide(linked, proposal, session(login=BRUNO_LOGIN)) == "CN011"
    with browser(linked, session(login=BRUNO_LOGIN)):
        assert linked.execute("select * from public.counsel_proposal(%s)", (proposal,)).fetchall() == []
    with browser(linked, session()):
        assert linked.execute("select body, status from public.counsel_proposal(%s)",
                              (proposal,)).fetchall() == [(TEXT, "waiting")]


def test_the_approval_covers_only_the_text_the_person_read(linked):
    proposal = propose(linked)
    assert decide(linked, proposal, session(), digest=sha(TEXT + " Also: delete the backups.")) == "CN011"
    assert notes(linked) == []


def test_an_approval_is_used_once_and_expires(linked):
    proposal = propose(linked)
    assert decide(linked, proposal, session()) == "approved"
    assert decide(linked, proposal, session()) == "CN011"
    assert len(notes(linked)) == 1

    late = propose(linked, text="late note")
    linked.execute("update counsel.proposals set expires_at = now() - interval '1 second' where id = %s", (late,))
    assert decide(linked, late, session(), digest=sha("late note")) == "CN011"


def test_denying_needs_no_fresh_sign_in_and_writes_nothing(linked):
    proposal = propose(linked)
    assert decide(linked, proposal, session(signed_in_ago=86400), approve=False) == "denied"
    assert decide(linked, proposal, session()) == "CN011"
    assert notes(linked) == []


def test_the_assistant_learns_the_outcome_and_only_for_this_person(linked):
    proposal = propose(linked)
    with acting_as(linked, audit.MCP_ROLE):
        status = lambda login: linked.execute("select status from counsel.proposal_for_login(%s, %s)",  # noqa: E731
                                              (login, proposal)).fetchall()
        assert status(AIKO_LOGIN) == [("waiting",)]
        assert status(BRUNO_LOGIN) == []
    decide(linked, proposal, session())
    with acting_as(linked, audit.MCP_ROLE):
        assert status(AIKO_LOGIN) == [("approved",)]
    trail = linked.execute("select action, decision from counsel.audit_log where detail ->> 'proposal' = %s "
                           "order by id", (str(proposal),)).fetchall()
    assert trail == [("propose_note", "allowed"), ("decide_proposal", "approved")]


def test_the_audit_fails_if_the_mcp_login_could_approve(db, agents):
    assert not [v for v in audit.run(db, agents) if v.role == audit.MCP_ROLE]
    db.execute("grant execute on function public.counsel_decide(uuid, boolean, text) to counsel_mcp")
    found = [v for v in audit.run(db, agents) if v.role == audit.MCP_ROLE]
    assert found and "EXECUTE counsel_decide(uuid,boolean,text)" in found[0].detail


def test_hosted_mode_offers_the_proposal_tools():
    import anyio
    from mcp.client import Client
    from mcp.server.auth.settings import AuthSettings

    from counsel.mcp_server import build
    from test_mcp_hosted import ISSUER, RESOURCE, verifier

    async def tools(server):
        async with Client(server) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    hosted = anyio.run(tools, build("postgresql://unused",
                                    auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE,
                                                      validate_token_resource=False),
                                    token_verifier=verifier()))
    assert {"propose_note", "proposal_status"} <= set(hosted)
    assert hosted["proposal_status"].annotations.read_only_hint
    assert not hosted["propose_note"].annotations.read_only_hint
    stdio = anyio.run(tools, build("postgresql://unused"))
    assert "propose_note" not in stdio
