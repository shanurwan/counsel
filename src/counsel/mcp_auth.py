"""Bearer tokens for the hosted MCP server, issued by this project's Supabase Auth.

Supabase Auth's OAuth 2.1 server signs a person in, asks for their consent on
site/consent.html, and issues the MCP client an access token. Before any tool runs,
the token must be:

- signed with the project's asymmetric signing key, published as JWKS. HS256 is
  refused: with a shared secret, anything holding the secret could mint tokens;
- issued by this project (`iss`) and unexpired;
- issued to an OAuth client after consent (`client_id` present), and not a session
  token from some other app on the same project;
- for this server (`aud`), as far as Supabase can say.

The last point is the gap. The MCP authorization specification requires a server to
refuse tokens that were not issued for it (RFC 8707 audience binding). Supabase Auth
currently stamps every token `aud: "authenticated"` whatever resource the client asked
for (https://github.com/supabase/auth/issues/2610). A token issued to any OAuth client
of this project, for any MCP server, therefore looks the same as one issued for this
one. Until tokens carry this server's URL, COUNSEL_MCP_REQUIRE_AUDIENCE stays off and
every call is logged with its client_id. A token bound to a different resource is
refused either way.
"""

from __future__ import annotations

from typing import Any

import anyio
import jwt
from mcp.server.auth.provider import AccessToken

# Algorithms of Supabase's asymmetric signing keys. Never HS256.
ALGORITHMS = ["ES256", "RS256", "EdDSA"]
# The audience Supabase Auth gives every access token today.
UNBOUND_AUDIENCE = "authenticated"


class SupabaseTokenVerifier:
    def __init__(self, issuer: str, resource: str, *, require_audience: bool = False,
                 jwks_client: Any | None = None) -> None:
        self.issuer = issuer.rstrip("/")
        self.resource = resource
        self.require_audience = require_audience
        self.jwks = jwks_client or jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json",
                                                   cache_keys=True, lifespan=600, timeout=10)

    def check(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=ALGORITHMS, issuer=self.issuer,
                                options={"require": ["exp", "iss", "sub"], "verify_aud": False})
        except jwt.PyJWTError:
            return None

        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else [a for a in (aud or []) if isinstance(a, str)]
        if self.resource in audiences:
            resource: str | None = self.resource
        elif any(a != UNBOUND_AUDIENCE for a in audiences):
            return None  # issued for some other resource
        elif self.require_audience or UNBOUND_AUDIENCE not in audiences:
            return None  # strict mode, or no audience at all
        else:
            resource = None  # Supabase could not say who it is for

        client_id = claims.get("client_id")
        if not client_id or claims.get("role") != "authenticated":
            return None

        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=str(claims.get("scope") or "").split(),
            expires_at=int(claims["exp"]),
            resource=resource,
            subject=str(claims["sub"]),
            claims={k: claims[k] for k in ("iss", "aud", "session_id") if k in claims},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # Fetching the key set is blocking network I/O; keep it off the event loop.
        return await anyio.to_thread.run_sync(self.check, token)
