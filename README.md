# Counsel

**Counsel can read everything, draft anything, and commit to nothing.**

Counsel is an internal AI operations platform. Its agents read the work system,
write digests and briefs, carry approved notes and send well-timed nudges. None
of them can set a commitment: who owns a task, when it is due, or its status.
That is not a rule in a prompt. Postgres refuses the write, and CI proves it on
every push.

> "It may never autonomously write a commitment (an owner, a due date, a
> status) into a shared work system. Your job is to make that structurally
> impossible rather than merely forbidden."

## The proof, first

CI builds this table from a live Postgres on every push using
`has_column_privilege` and `has_table_privilege`. If any commitment cell turns
true, the build fails.

| Agent role | Tier | read tasks | add update note | owner_id | due_date | status |
|---|---|---|---|---|---|---|
| `agent_ask_handbook` | T0 | no | no | no | no | no |
| `agent_drift_watcher` | T1 | yes | no | no | no | no |
| `agent_exec_weekly` | T1 | yes | no | no | no | no |
| `agent_issue_triage` | T1 | yes | no | no | no | no |
| `agent_nudge` | T3 | yes | no | no | no | no |
| `agent_standup_carrier` | T2 | yes | with consent | no | no | no |
| `agent_team_digest` | T1 | yes | no | no | no | no |

The audit checks more than grants. It fails on any agent-acting role that has
a dangerous attribute (`LOGIN`, `BYPASSRLS`, `CREATEROLE`, ...), an unexpected
membership, owned objects, an executable `SECURITY DEFINER` function, or that
is not in the registry.

## Four walls

| Wall | Where | Status |
|---|---|---|
| No tool takes an owner, due date or status | runtime tool registry | planned |
| The credential cannot do it | [`agents/*.yaml`](agents) → [`counsel compile`](src/counsel/compiler.py) → [`db/generated/agents.sql`](db/generated/agents.sql) | done |
| The database refuses it, even after a mistaken grant | [`db/migrations/0002_guards.sql`](db/migrations/0002_guards.sql) | done |
| CI proves it on every push | [`counsel audit`](src/counsel/audit.py), [`tests/`](tests), [`ci.yml`](.github/workflows/ci.yml) | done |

Design and trade-offs: [ADR-001](docs/adr/001-commitment-walls.md).

## Risk tiers

Agents pick named capabilities, never raw grants. The validator refuses an
agent whose tier and capabilities disagree in either direction.

| Tier | May do | Writes |
|---|---|---|
| T0 read | answer from the handbook | its own answers table |
| T1 draft | write reports for a person to publish | drafts only |
| T2 carry | carry a person's own words into a task | a note, only with consent to that exact text |
| T3 contact | message a person | messages, within their interruption budget |
| — commit | set owner, due date or status | no code path exists |

## Run it

Needs Python 3.11+ and Postgres 16+.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

counsel validate                       # registry rules
counsel compile --check                # generated grants match the registry
export COUNSEL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/counsel
counsel migrate                        # schema, guards, agent roles
counsel audit                          # the matrix above, exit 1 on any violation

export COUNSEL_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres
pytest -q                              # every forbidden write, attempted and refused
```

## What the tests attempt

Each test acts as a real agent role and expects Postgres to refuse:

- every agent role, and the runtime, setting owner, due date or status
- inserting, deleting or truncating tasks
- a mistaken `GRANT UPDATE ON work.tasks`: the trigger still refuses
  commitment columns, and the audit flags the grant
- a `SECURITY DEFINER` back door, a person granted an agent role, an
  unregistered agent role, `LOGIN` on an agent role: all flagged by the audit
- carrying a note with edited text, for another task, with an expired or
  reused consent, or with none
- forging a consent, an author or a draft's agent identity
- rewriting the audit log, as an agent or as its owner

And one that must succeed: people can still set their own commitments.

## Status

| Part | Status |
|---|---|
| Schema, agent roles, guards, privilege audit, CI | done |
| Runtime: pgmq queue, step journal, NVIDIA NIM adapter, replay | next |
| Evaluation gate: golden suites, safety cases, calibrated judge | planned |
| Contact budget, value ledger, Terraform for Supabase and GCP | planned |

All data is synthetic (a made-up company, Harbor Labs). Counsel has no real
users.

## License

MIT
