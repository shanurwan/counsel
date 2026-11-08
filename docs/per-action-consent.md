# Consent for the call, not just the connection

When a person connects an assistant to an MCP server, they consent once, to the
connection. Every call after that carries a token that proves two things: who the
person is, and which OAuth client is acting for them. It cannot say whether the person
chose *this* call. For reads that is fine. For a write in someone's name it is the
gap, and Counsel closes it for the one write its hosted MCP server offers.

## Where the gap is

Supabase's guide to giving an app its own MCP server makes the user the boundary:

> Every tool call runs as that user, so your RLS policies decide what each agent can reach.

and leaves room for exactly this:

> Keep business rules in the code the tool calls. For example, an expense might need an approval check.

([Give your Supabase app its own MCP server](https://supabase.com/blog/give-your-supabase-app-its-own-mcp-server), 22 Oct 2026)

RLS answers *may this person do this?* An approval check has to answer *did this
person choose to do this, now?*, and nothing in the token can. Enterprise-managed
auth makes the connection step lighter still, which is its point:

> Members in the selected roles now see Supabase already connected in Claude with no sign-in or consent screen.

> It doesn't confirm a sign-in at your identity provider, check your multi-factor authentication, or apply Supabase's own sign-in rules.

([Enterprise-managed auth for MCP: Claude can now issue the identity assertion](https://supabase.com/blog/claude-issued-enterprise-managed-auth-for-mcp), 3 Nov 2026)

So the decision to act has to be taken somewhere other than the connection.

## Why the client's own prompt is not enough

MCP clients ask before calling a tool, and MCP elicitation lets a server ask the user
a question mid-call. Both are answered inside the client. The server receives the
same token whether a person clicked Allow, chose "always allow" last week, or a model
following injected instructions carried on. `readOnlyHint` and `destructiveHint` are
advice to the client. None of it is something the server can verify.

What the server *can* verify is a fresh sign-in by the person, to the project's own
Supabase Auth, in their own browser, through a session that was never handed to the
assistant.

## The pattern

`propose_note` ([mcp_server.py](../src/counsel/mcp_server.py)) is the hosted server's
only write, and it writes nothing:

1. **The assistant proposes.** `counsel.propose_note` stores the exact text for one
   of the signed-in person's own open tasks and returns a link. At most five wait at
   once, so a model in a loop cannot flood anyone.
2. **The person opens the link** ([site/approve.html](../site/approve.html)), signs in,
   and sees the task and the exact text.
3. **Postgres decides** ([0014_proposals.sql](../db/migrations/0014_proposals.sql)).
   `public.counsel_decide` runs only as PostgREST's `authenticated` role, and refuses:
   - **a token issued to an OAuth client.** Supabase Auth puts `client_id` in the
     tokens it issues to OAuth clients, and not in a person's own session. That one
     claim is what stops the assistant approving its own proposal with the token it
     already holds;
   - **a sign-in older than ten minutes**, read from the `amr` timestamps. The page
     asks for the password again, which also covers connections where no identity
     provider checked MFA;
   - **anyone but the person** it was proposed for;
   - **text other than what the page showed.** The page sends the SHA-256 of what it
     rendered, and the database compares it with what it holds;
   - **a proposal already decided, or older than 15 minutes.**
4. **Approving is the write.** In one transaction it records a `work.consents` row
   for that hash, inserts the note credited to the person (`on_behalf_of`) and
   attributed to the client that proposed it, and closes the proposal. There is no
   second step for the assistant to replay.

The server's own database login can call `propose_note` and `proposal_status`, and
nothing that approves. `counsel audit` fails if it ever could, and a test grants it
`counsel_decide` to prove the audit notices. The role check matters as much as the
claims: any role can set `request.jwt.claims` for itself, but only PostgREST, after
verifying the JWT, switches to `authenticated`.

## Evidence

The test suite covers each refusal (`tests/test_proposals.py`). End to end, with a
real Supabase Auth, PostgREST, and the MCP Python SDK's OAuth client playing Claude:

```
1. Claude proposes a note through the hosted MCP server
   -> waiting for the person; approve_url=.../approve.html?proposal=e3887425-...
2. Claude tries to approve it itself, with the OAuth token it holds
   counsel_decide: (400, ('CN011', 'an assistant cannot approve; open the link and sign in yourself'))
3. No sign-in at all (the publishable key alone)
   counsel_decide: (401, ('42501', 'permission denied for function counsel_decide'))
4. The person, signed in at the browser, reads it
   counsel_proposal: (200, [{'task_title': 'Rotate staging keys', 'body': 'Staging keys rotated; ...', 'status': 'waiting'}])
5. ...and approves different text than they read
   counsel_decide: (400, ('CN011', 'the text changed since you read it'))
6. ...and approves what they read
   counsel_decide: (200, 'approved')
7. ...and the same approval again
   counsel_decide: (400, ('CN011', 'this proposal was already decided or has expired'))
8. Claude checks back: proposal_status = approved
```

The note landed once, credited to `aiko`, authored by `assistant:<client_id>`. The
page itself was then driven in Chromium: sign in, read, *Post it*, and reopening the
link shows *Already posted*.

## What it costs

A browser round trip per write. That is the right price for a write in someone's
name and the wrong one for reads, which is why every other tool here stays read-only
and unconfirmed. Where people approve many small writes, the same table can hold a
batch and the page can show all of it; the hash then covers the batch.

## Fitting it into an MCP server on Supabase

Nothing here is Counsel-specific. The checks use only what Supabase Auth already puts
in its tokens (`role`, `client_id`, `amr`, `sub`), and PostgREST's role switch. For
an Edge Function MCP server, the same shape is a proposals table, one RPC to decide,
and a `requireApproval` wrapper that turns a tool's write into a proposal and
returns the link. The consent page the OAuth server already needs is the natural
place for the approval screen.

## Limits

- It proves a fresh sign-in, not that the person read carefully. The page shows the
  exact text and nothing else, to make reading easy.
- Freshness depends on `amr` timestamps. A project that signs people in some other
  way should check its tokens carry them.
- The person has to be at a browser. That is the point, and also the friction.
