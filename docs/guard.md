# counsel guard: what can your AI agent write?

You gave an agent a database login: a support bot, an MCP server, a LangChain SQL agent,
a cron job with a model in it. `counsel guard` tells you what that login can actually do
on your Postgres or Supabase database, and gives you the SQL to cut it down to what the
agent needs, with a trigger that refuses the writes it must never make.

It is the rule Counsel's own agents live under ([README](../README.md)), packaged for
anyone's database.

```bash
pipx install "git+https://github.com/shanurwan/counsel"

# What can the role do right now? Read-only; exit 1 on a failure.
DATABASE_URL="postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres" \
  counsel guard check --role support_agent

# Describe what it should do, compile it, review it, apply it.
counsel guard compile guard.yaml > agent.sql

# Prove the live database matches. Put this in CI.
counsel guard check --config guard.yaml
```

`check` connects with your admin connection string inside a read-only transaction and
asks Postgres about the agent's role (`has_table_privilege`, `pg_auth_members`,
`pg_policy` and friends). It never logs in as the agent and never changes anything.

## What `check` finds

On Supabase's own Postgres image (`supabase/postgres` 17.11 and 15.19), with the shortcut
people take, an agent role granted `service_role`:

```
  extensions.pg_stat_statements       view     SELECT
  extensions.pg_stat_statements_info  view     SELECT
  public.ticket_drafts                rls on   SELECT, INSERT, UPDATE, DELETE, TRUNCATE
  public.tickets                      rls on   SELECT, INSERT, UPDATE, DELETE, TRUNCATE
  vault.decrypted_secrets             view     SELECT, DELETE
  vault.secrets                       rls OFF  SELECT, DELETE

  FAIL  can become a powerful role: inherits every privilege of service_role, which bypasses row-level security
  FAIL  can truncate: public.ticket_drafts: TRUNCATE skips row-level security and row triggers
  FAIL  can truncate: public.tickets: TRUNCATE skips row-level security and row triggers
  FAIL  reads a sensitive schema: vault.decrypted_secrets: vault holds users, sessions or secrets
  FAIL  writes a sensitive schema: vault.decrypted_secrets: DELETE
  FAIL  reads a sensitive schema: vault.secrets: vault holds users, sessions or secrets
  FAIL  writes a sensitive schema: vault.secrets: DELETE
  warn  row-level security is off: vault.secrets: the agent reaches every row it is granted

7 failing, 1 warning. Exit 1.
```

The agent could read every decrypted secret in Vault, and delete them. After
`counsel guard compile` with the config below, and apply:

```
  public.ticket_drafts  rls on   INSERT ticket_id, INSERT body
  public.tickets        rls on   SELECT id, SELECT team, SELECT subject, SELECT summary, SELECT status, UPDATE summary

0 failing, 0 warnings.
```

Then, as the agent's own login: it reads only its team's tickets, may draft and
summarise, and is refused changing a status, deleting, reading `auth.users` and
`set role service_role`. After someone runs `grant update on public.tickets` to it, the
trigger still refuses `update ... set assignee` and `check` fails again. The same steps
are [tests/test_guard.py](../tests/test_guard.py), on a database shaped like Supabase's,
and they passed on the real `supabase/postgres` images, run as Supabase's non-superuser
`postgres`.

| Finding | Level | Why it matters |
|---|---|---|
| superuser, `BYPASSRLS`, or inherits/can become one (`service_role`, `postgres`) | fail | every row-level policy is skipped |
| reads or writes `auth`, `vault`, `pgsodium`, `cron`, `net`, `supabase_functions` | fail | users, sessions and secrets |
| `TRUNCATE` on anything | fail | skips row-level security and row triggers |
| owns a table | fail | an owner can grant itself anything |
| anything `guard.yaml` does not allow; a `never_write` column writable; its trigger missing | fail | the config is the contract, so CI can gate on it |
| `CREATEROLE`, `CREATEDB`, `REPLICATION` | fail | more than an agent needs |
| inherits `authenticated` or `anon` | warn | every grant and policy for your app's users applies to the agent too |
| a column named like a password, token, secret or key | warn | named, wherever it is |
| a table it reaches with row-level security off | warn | it reaches every row it is granted |
| a view that runs as its owner | warn | the policies under it are checked for the owner, not the agent |
| a SECURITY DEFINER function it can call | warn | it runs with its owner's rights |
| `CREATE` on a schema | warn | it could add a function or view of its own |
| row-level security on and no policy for it | info | it sees no rows: probably not what you meant |

`--json` prints the same as JSON.

## guard.yaml

```yaml
role: support_agent          # the agent's own role; never service_role, authenticated or anon
login: true                  # it connects with its own password (set it yourself)

reads:
  public.tickets: [id, team, subject, body, summary, status]   # columns, or: all
  public.customers: [id, name, plan]

writes:
  public.ticket_drafts:
    insert: [ticket_id, body]
  public.tickets:
    update: [summary]        # it may summarise a ticket, nothing else

rows:                        # which rows, as one SQL condition per table (default: every row)
  public.tickets: "team = 'support'"

never_write:                 # refused by a trigger, even if someone grants too much later
  public.tickets: [assignee, status, refund_cents]
```

`compile` writes, in one transaction:

- the role, with no attributes and no memberships (it revokes any it has);
- grants down to the column, and nothing else in the schemas it touches;
- a policy per table and command for the role, with your `rows` condition;
- a trigger, `counsel_guard.refuse_columns`, that refuses an `UPDATE` changing a
  `never_write` column, and any `DELETE` unless you allowed one, when the statement runs
  as the agent. People and your app are untouched.

It never turns row-level security on or off for a table: that would change what your
app's own users see. `check` warns where it is off.

Inserting a `never_write` column is stopped by the grants: the agent may only insert the
columns you list, and the rest take their defaults.

## How the agent uses the role

- **Its own login** (`login: true`): give the agent a connection string with this user.
  Works for MCP Postgres servers, SQL agents and scripts.
- **Through your API with a JWT** (`login: false`): `grant support_agent to authenticator;`
  then sign the agent's JWT with `"role": "support_agent"`. PostgREST switches to it per
  request, so the grants, policies and trigger apply there too.
- **`SET ROLE`** from a service that holds the role: the same, per transaction.

## In CI

The safest gate needs no production secret: start Supabase locally, apply your
migrations, check. `.github/workflows/agent-guard.yml`:

```yaml
name: agent guard
on: [push, pull_request]
permissions:
  contents: read
jobs:
  guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: supabase/setup-cli@v1
      - run: supabase start && supabase db reset    # your migrations, including agent.sql
      - uses: shanurwan/counsel/guard@main
        with:
          config: guard.yaml
          database-url: postgresql://postgres:postgres@127.0.0.1:54322/postgres
```

A migration that gives the agent a grant `guard.yaml` does not allow now fails the build.
To check a live project instead, pass its connection string from a repository secret.

## Limits

- It reads privileges, policies, memberships, triggers and SECURITY DEFINER functions.
  It does not read your policies' conditions for mistakes, or what a function does.
- Supabase's sensitive schemas are listed by name; a secret in your own schema is caught
  only if its column is named like one.
- A trigger guards a table it was created on. A new table the agent can write needs a
  new `never_write` entry, and `check --config` fails on the grant until it has one.
