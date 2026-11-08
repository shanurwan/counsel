"""The hosted MCP server: who may call it, and what a signed-in person sees.

Token checks need no database: tokens are signed here with a throwaway EC key and
checked against it as if it were the project's JWKS. The person-scoped tools are
checked in Postgres, as the counsel_mcp login.
"""

import time
import uuid
from types import SimpleNamespace

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from counsel import audit, cli
from counsel.mcp_auth import SupabaseTokenVerifier

from conftest import AIKO, TASK_A, acting_as, denied

ISSUER = "https://project.supabase.co/auth/v1"
RESOURCE = "https://counsel-mcp.example.run.app/mcp"
KEY = ec.generate_private_key(ec.SECP256R1())


class FakeJWKS:
    """The project's key set: one ES256 key with kid k1."""

    def get_signing_key_from_jwt(self, token):
        if jwt.get_unverified_header(token).get("kid") != "k1":
            raise jwt.PyJWKClientError("unknown kid")
        return SimpleNamespace(key=KEY.public_key())


def token(**overrides):
    claims = {"iss": ISSUER, "sub": str(uuid.uuid4()), "aud": "authenticated", "role": "authenticated",
              "exp": int(time.time()) + 600, "client_id": str(uuid.uuid4()), "scope": "email"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="ES256", headers={"kid": "k1"})


def verifier(require_audience=False):
    return SupabaseTokenVerifier(ISSUER, RESOURCE, require_audience=require_audience, jwks_client=FakeJWKS())


# --- tokens -----------------------------------------------------------------------------------


def test_a_consented_token_is_accepted_and_marked_unbound():
    checked = verifier().check(token())
    assert checked is not None and checked.client_id and checked.subject
    # Supabase could not say who the token is for (supabase/auth#2610).
    assert checked.resource is None


def test_a_token_bound_to_this_server_is_accepted_in_strict_mode():
    checked = verifier(require_audience=True).check(token(aud=["authenticated", RESOURCE]))
    assert checked is not None and checked.resource == RESOURCE


def test_strict_mode_refuses_an_unbound_token():
    assert verifier(require_audience=True).check(token()) is None


def test_a_token_bound_to_another_server_is_refused_even_when_lenient():
    assert verifier().check(token(aud=["authenticated", "https://other.example.com/mcp"])) is None
    assert verifier().check(token(aud="https://other.example.com/mcp")) is None


def test_a_session_token_without_a_client_is_refused():
    # A token from some other app on the same project, never consented to for an MCP client.
    assert verifier().check(token(client_id=None)) is None


@pytest.mark.parametrize("overrides", [
    {"iss": "https://someone-else.supabase.co/auth/v1"},
    {"exp": int(time.time()) - 5},
    {"role": "anon"},
    {"role": "service_role"},
])
def test_wrong_issuer_expiry_or_role_is_refused(overrides):
    assert verifier().check(token(**overrides)) is None


def test_a_shared_secret_token_is_refused():
    forged = jwt.encode({"iss": ISSUER, "sub": "x", "aud": "authenticated", "role": "authenticated",
                         "exp": int(time.time()) + 600, "client_id": "c"},
                        "the-project-jwt-secret-of-32-bytes", algorithm="HS256", headers={"kid": "k1"})
    assert verifier().check(forged) is None


def test_an_unknown_key_is_refused():
    other = ec.generate_private_key(ec.SECP256R1())
    assert verifier().check(jwt.encode({"iss": ISSUER}, other, algorithm="ES256", headers={"kid": "k2"})) is None


def test_hosted_mode_adds_the_person_tools_and_stdio_does_not():
    import anyio
    from mcp.client import Client
    from mcp.server.auth.settings import AuthSettings

    from counsel.mcp_server import build

    async def names(server):
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    stdio = anyio.run(names, build("postgresql://unused"))
    hosted = anyio.run(names, build("postgresql://unused",
                                    auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE,
                                                      validate_token_resource=False),
                                    token_verifier=verifier()))
    assert {"whoami", "my_work"} <= hosted - stdio


# --- what a signed-in person sees ----------------------------------------------------------------


LOGIN = "00000000-0000-0000-0000-0000000000f1"


def test_a_linked_person_sees_only_their_own_work(db):
    db.execute("insert into counsel.logins (auth_user_id, person_id) values (%s, %s)", (LOGIN, AIKO))
    with acting_as(db, audit.MCP_ROLE):
        person = db.execute("select handle from counsel.person_for_login(%s)", (LOGIN,)).fetchall()
        work = db.execute("select title from counsel.work_for_login(%s)", (LOGIN,)).fetchall()
        stranger = db.execute("select * from counsel.work_for_login(%s)", (str(uuid.uuid4()),)).fetchall()
    assert person == [("aiko",)]
    assert work == [("Rotate staging keys",)]
    assert stranger == []


def test_the_mcp_login_still_cannot_read_tasks_or_links_directly(db):
    for statement in ("select * from work.tasks", "select * from counsel.logins",
                      "insert into counsel.logins (auth_user_id, person_id) values (gen_random_uuid(), "
                      f"'{AIKO}')", f"update work.tasks set status = 'done' where id = '{TASK_A}'"):
        assert denied(db, audit.MCP_ROLE, statement) == "42501", statement


def test_the_audit_catches_the_mcp_login_given_another_definer_function(db, agents):
    assert not [v for v in audit.run(db, agents) if v.role == audit.MCP_ROLE]
    db.execute("""create function counsel.all_tasks() returns setof work.tasks
                  language sql security definer as 'select * from work.tasks'""")
    db.execute("grant execute on function counsel.all_tasks() to counsel_mcp")
    found = [v for v in audit.run(db, agents) if v.role == audit.MCP_ROLE]
    assert found and "EXECUTE counsel.all_tasks()" in found[0].detail


def test_an_owner_links_a_sign_in_by_email(database_url):
    email = f"linked-{uuid.uuid4().hex[:8]}@example.com"
    user_id = str(uuid.uuid4())
    with psycopg.connect(database_url, autocommit=True) as conn:
        # Supabase provides auth.users; a plain test database needs a stand-in.
        conn.execute("create schema if not exists auth")
        conn.execute("create table if not exists auth.users (id uuid primary key, email text)")
        conn.execute("insert into auth.users (id, email) values (%s, %s)", (user_id, email))
        try:
            assert cli.main(["link-login", email.upper(), "aiko", "--dsn", database_url]) == 0
            assert cli.main(["link-login", "nobody@example.com", "aiko", "--dsn", database_url]) == 1
            assert cli.main(["link-login", email, "no-such-handle", "--dsn", database_url]) == 1
            assert conn.execute("select person_id::text from counsel.logins where auth_user_id = %s",
                                (user_id,)).fetchone() == (AIKO,)
        finally:
            conn.execute("delete from counsel.logins where auth_user_id = %s", (user_id,))
            conn.execute("delete from auth.users where id = %s", (user_id,))
