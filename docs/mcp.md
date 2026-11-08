# The MCP server

`counsel-mcp` puts Counsel's journal and handbook in the tools people already use.
It runs two ways: as a local stdio process per client, or hosted on Cloud Run, where
people sign in with this project's Supabase Auth and also see their own work
([Hosted](#hosted)).

| Tool | What it does | Writes |
|---|---|---|
| `list_agents` | Owner, tier, capabilities, triggers and suite for each agent | no |
| `privilege_matrix` | What each agent's role can do right now, asked of the live database | no |
| `list_runs` | Recent runs with status and cost | no |
| `replay_run` | One run in full: steps, prompts, responses, audit entries | no |
| `refused_writes` | Forbidden writes Postgres refused, and rollbacks | no |
| `value_ledger` | Accepted outputs, hours absorbed and spend, per agent and month | no |
| `pending_reviews` | Drafts waiting for a person | no |
| `ask_handbook` | Queues a question; at most 20 an hour | a question |
| `handbook_answer` | The latest answer to that exact question | no |
| `whoami` | Hosted only: your sign-in and the person it is linked to | no |
| `my_work` | Hosted only: your own unfinished tasks | no |

## The tool list is not the policy

The server logs in as `counsel_mcp`, which can read the journal and insert a
question, and nothing else. A bug here, or a hijacked client, still cannot publish
a draft, carry a note, send a message or touch a commitment, because Postgres
refuses. `counsel audit` fails if that login is ever granted more, and a test grants
it `UPDATE` to prove the audit notices.

Publishing is deliberately missing. `counsel review` runs on a person's own
connection, so `reviewed_by` is that person. A publish tool would make the reviewer
"whoever holds the MCP login", and the human gate would stop identifying a human.

## Choices

- **`ask_handbook` is asynchronous.** The agent runs on the next tick, up to 30
  minutes away. The tool says so, and `handbook_answer` fetches the result. Blocking
  a client for that long would break most of them, and running the agent inside the
  server would give this login the agent's privileges.
- **Results are data.** A replay holds ticket and issue text, including injection
  attempts. Tool descriptions say so and results are JSON, but a client's model
  still reads them, so point this server only at clients you would show the journal
  to.
- **The rate limit is in the server, not the database.** A client looping on a
  question costs model calls. This is a cost guard, not a security boundary: anyone
  with the login's password could insert directly, and the worst outcome is spend.
- **Tools declare `readOnlyHint`,** so a client can skip the confirmation prompt on
  reads and keep it for `ask_handbook`.

## Run it

```sh
pip install "counsel[mcp]"
psql "$ADMIN_URL" -c "alter role counsel_mcp password '...'"
export COUNSEL_MCP_URL=postgresql://counsel_mcp:...@host:5432/postgres
counsel-mcp
```

A client configuration, for example in Claude Desktop:

```json
{
  "mcpServers": {
    "counsel": {
      "command": "counsel-mcp",
      "env": { "COUNSEL_MCP_URL": "postgresql://counsel_mcp:...@host:5432/postgres" }
    }
  }
}
```

The login holds at most 3 connections and has a 10 s statement timeout, so a busy
client cannot starve the agents.

## Hosted

`counsel-mcp --http` serves the same tools over streamable HTTP
([infra/mcp.tf](../infra/mcp.tf)). Supabase Auth's OAuth 2.1 server is the
authorization server, so a person connects Counsel to Claude or ChatGPT by its URL
and signs in in the browser:

1. The client calls the server, gets `401` and the protected resource metadata,
   which names `https://<ref>.supabase.co/auth/v1` as the authorization server.
2. It registers with Supabase Auth (dynamic registration) and opens
   `/oauth/authorize` with PKCE and `resource=<this server>/mcp`.
3. Supabase Auth sends the person to [site/consent.html](../site/consent.html), which
   signs them in, shows the client and the host they will be sent back to, and
   approves or denies.
4. The client exchanges the code for an access token and calls the server with it.

Every request's token is checked before any tool runs
([mcp_auth.py](../src/counsel/mcp_auth.py)): signed with the project's asymmetric
key, issued by this project, unexpired, and issued to an OAuth client after consent
(`client_id`), not a session token from another app on the project.

`whoami` and `my_work` answer for the signed-in person. An owner links a sign-in to
a person with `counsel link-login <email> <handle>`; nobody links themselves. The
server passes the token's user id to `counsel.work_for_login`, and `counsel_mcp`
still holds no grant on `work.tasks` or `counsel.logins`. `counsel audit` fails if
that login can run any other SECURITY DEFINER function.

### The gap: tokens do not say which server they are for

The MCP specification requires a server to refuse tokens that were not issued for
it. Clients send `resource` (RFC 8707) so the authorization server can bind the
token's audience. Supabase Auth currently ignores it: every access token carries
`aud: "authenticated"` ([supabase/auth#2610](https://github.com/supabase/auth/issues/2610)).

So a token issued to any OAuth client of this project, for any MCP server, is
indistinguishable from one issued for Counsel. Tested against Supabase Auth with
the MCP SDK's own OAuth client: a second client asked for a token for
`https://other-mcp.example.com/mcp`, received `aud: "authenticated"`, and this server
had to accept it.

What Counsel does meanwhile:

- A token bound to a different resource is refused.
- Every call is logged with its `client_id` and whether the audience was bound.
- `COUNSEL_MCP_REQUIRE_AUDIENCE=1` refuses any token not bound to this server. It
  stays off until Supabase Auth binds audiences, because today it would refuse every
  token. The MCP Python SDK plans to make this check its default in 3.0.

### Set it up

1. In the Supabase dashboard: **Authentication > OAuth Server**, enable it, allow
   dynamic registration, and set the authorization path to `/consent.html`. Set the
   Site URL to `https://shanurwan.github.io/counsel`. Use an asymmetric signing key
   (**Project Settings > JWT Keys**); the server refuses HS256 tokens.
2. Create each person's account under **Authentication > Users** (sign-up stays off),
   then `counsel link-login <email> <handle>`.
3. Repository variables for the consent page: `COUNSEL_SUPABASE_URL` and
   `COUNSEL_SUPABASE_PUBLISHABLE_KEY` (public by design).
4. `alter role counsel_mcp password '...'`, and store its connection string in the
   `mcp-database-url` secret.
5. Set `supabase_auth_issuer` in `infra/terraform.tfvars` and apply. The next deploy
   from main puts the image on the `counsel-mcp` service.
