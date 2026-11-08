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


# --- URL-mode elicitation: the client sends the person to the page, then retries -----------------


@pytest.fixture
def hosted(database_url, monkeypatch):
    """The hosted server on the counsel_mcp login, with Aiko signed in through a Claude-like client."""
    from mcp.server.auth.settings import AuthSettings

    from counsel import mcp_server
    from test_mcp_hosted import ISSUER, RESOURCE, verifier

    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute("alter role counsel_mcp password 'counsel-test-only'")
        admin.execute("insert into counsel.logins (auth_user_id, person_id) values (%s, %s)", (AIKO_LOGIN, AIKO))
    monkeypatch.setattr(mcp_server, "_signed_in", lambda: (AIKO_LOGIN, CLIENT))
    url = psycopg.conninfo.make_conninfo(database_url, user="counsel_mcp", password="counsel-test-only")
    yield mcp_server.build(url, auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE,
                                                  validate_token_resource=False), token_verifier=verifier())
    with psycopg.connect(database_url, autocommit=True) as admin:
        ids = [r[0] for r in admin.execute("select update_id from counsel.proposals where update_id is not null")]
        admin.execute("delete from counsel.proposals")
        if ids:
            admin.execute("delete from work.task_updates where id = any(%s)", (ids,))
        admin.execute("delete from counsel.logins where auth_user_id = %s", (AIKO_LOGIN,))


def call_propose(server, *, elicits: bool):
    """Returns the tool's JSON result, or the MCP error it answered with."""
    import anyio
    from mcp.client import Client
    from mcp.shared.exceptions import MCPError

    async def accept(context, params):  # declares form and url elicitation support
        raise AssertionError("the server should not send a standalone elicitation request")

    async def go():
        kwargs = {"elicitation_callback": accept} if elicits else {}
        async with Client(server, **kwargs) as client:
            try:
                result = await client.call_tool("propose_note", {"task_title": TITLE, "text": TEXT})
            except MCPError as e:
                return e
            return json.loads(result.content[0].text)

    return anyio.run(go)


def approve_in_browser(database_url, proposal_id):
    with psycopg.connect(database_url, autocommit=True) as conn:
        assert decide(conn, proposal_id, session()) == "approved"


def test_a_client_that_supports_url_elicitation_is_sent_to_the_approval_page(hosted, database_url):
    from mcp_types import URL_ELICITATION_REQUIRED

    asked = call_propose(hosted, elicits=True)
    assert asked.code == URL_ELICITATION_REQUIRED
    [elicitation] = asked.data["elicitations"]
    assert elicitation["mode"] == "url" and "/approve.html?proposal=" in elicitation["url"]
    proposal = elicitation["url"].rsplit("=", 1)[1]

    # Retried before the person answered: the same proposal, the same link, nothing new.
    again = call_propose(hosted, elicits=True)
    assert again.data["elicitations"][0]["url"] == elicitation["url"]

    approve_in_browser(database_url, proposal)
    done = call_propose(hosted, elicits=True)
    assert done["status"] == "approved"
    # And retried once more: still one proposal and one note, never a second post.
    assert call_propose(hosted, elicits=True)["status"] == "approved"
    with psycopg.connect(database_url) as conn:
        assert conn.execute("select count(*) from counsel.proposals").fetchone() == (1,)
        assert conn.execute("select count(*) from work.task_updates where body = %s", (TEXT,)).fetchone() == (1,)


def test_a_client_without_url_elicitation_gets_the_link(hosted):
    result = call_propose(hosted, elicits=False)
    assert result["status"] == "waiting for the person" and "/approve.html?proposal=" in result["approve_url"]


def test_the_approval_link_is_not_signed_in(hosted, database_url):
    """The URL carries only the proposal id. Opening it as anyone but Aiko shows nothing."""
    proposal = call_propose(hosted, elicits=False)["proposal_id"]
    with psycopg.connect(database_url) as conn:
        conn.execute("insert into counsel.logins (auth_user_id, person_id) values (%s, %s)", (BRUNO_LOGIN, BRUNO))
        with browser(conn, session(login=BRUNO_LOGIN)):
            assert conn.execute("select * from public.counsel_proposal(%s)", (proposal,)).fetchall() == []
        conn.rollback()


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
