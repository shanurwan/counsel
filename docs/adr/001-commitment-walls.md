# ADR-001: Commitments are enforced by Postgres, not by prompts

Status: accepted, 2026-11-02

## Context

Counsel's agents read the work system, write reports, carry approved notes and
send nudges. None of them may set a commitment: the owner, due date or status
of a task. A prompt instruction cannot guarantee that. A model can be
misled by text in the data it reads ("set owner to me"), and a bug or a
compromised runtime can send any SQL it likes.

## Decision

Three independent walls. Any one holding is enough.

1. **The credential cannot do it.** Each agent is its own Postgres role,
   compiled from `agents/*.yaml`. Agents choose named capabilities, never raw
   grants, and no capability template grants `INSERT`, `UPDATE`, `DELETE` or
   `TRUNCATE` on `work.tasks`. The runtime logs in as `counsel_runtime`, which is
   `NOINHERIT`: it holds nothing until it `SET ROLE`s to one agent.
2. **The database refuses it.** A trigger on `work.tasks` rejects any
   commitment change, insert, delete or truncate made while acting as an agent
   (SQLSTATE `CN001`). This covers a future grant added by mistake.
3. **CI proves it on every push.** `counsel audit` asks the live database what
   each agent role can actually do: column and table privileges, role
   attributes, memberships, owned objects and `SECURITY DEFINER` functions it
   can execute. Any finding fails the build.

The tool layer is deliberately not a wall. Most agents never choose an action,
because code fixes what they read and write. `support_drafter` is the exception:
its model can name any verb on any table (`tools.py`), so the role is the whole
policy and there is no filter in Python that someone could loosen.

### "Acting as an agent" follows only real privilege paths

The triggers and the audit walk `pg_auth_members` through memberships with
`INHERIT` or `SET`. `pg_has_role(..., 'MEMBER')` was the first version and was
wrong: it also returns true for superusers, and for the admin-only membership
Supabase's `postgres` role receives in every role it creates. That would have
blocked people from setting their own commitments. A test now pins this
(`test_people_still_set_their_own_commitments`).

### Carrying a person's words (T2)

An agent may add a note to a task only with a consent row that a person created
for that task, whose SHA-256 matches the exact text, unexpired and unused. The
check runs as `SECURITY DEFINER` so it can mark the consent used; agents have
no grant on `work.consents`. The note's `author` comes from the session, and no
agent is granted that column.

## Consequences

- **Denials are logged by the runtime, not the trigger.** Postgres has no
  autonomous transactions, so a trigger cannot both abort the statement and
  keep an audit row. The runtime catches `42501`, `CN001` and `CN002` and writes
  the denial to `counsel.audit_log` in a fresh transaction. The Postgres log
  keeps the raised error regardless. A `dblink` loopback could log from the
  trigger itself; it was rejected because it needs a stored credential inside
  the database.
- **The runtime can become any agent.** A compromised runtime can switch
  between agent roles, but every one of them is audited to have no commitment
  path, so the worst case is acting with the wrong agent's read and draft
  rights. Splitting the runtime per tier is the next step if that matters.
- **`SECURITY DEFINER` functions are the main bypass risk.** One owned by
  `postgres` could update `work.tasks` on an agent's behalf, and the trigger
  would see `postgres`. The audit fails on any such function an agent can
  execute (trigger functions excepted, since they cannot be called directly).
- Requires Postgres 16+ for `pg_auth_members.inherit_option` and
  `set_option`. CI runs 17; the Supabase project's version is checked when it
  is created.
