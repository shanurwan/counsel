# Contributing

Issues and pull requests are welcome. The most useful contribution is a way through:
SQL or a tool call that lets an agent do what the audit says it cannot. Report those
privately ([SECURITY.md](SECURITY.md)); everything else can be an issue.

## Run it locally

```sh
pip install -e ".[dev]"
export COUNSEL_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres
pytest -q                 # needs a Postgres 16+ superuser URL; database tests skip without one
counsel validate          # the agent registry
counsel compile --check   # compiled grants match the registry
counsel compliance --check
```

## What a change needs

- **A new agent** starts with `counsel new <name>`: a registry entry, a capability set,
  and an eval suite that fails until it is filled in.
- **A grant** comes only from a capability in `src/counsel/compiler.py`. Run
  `counsel compile` and commit `db/generated/agents.sql`.
- **A migration** only adds. A test refuses drops, renames, truncates and type
  changes, because CI migrates production before the new code is deployed
  ([docs/red-team.md](docs/red-team.md), R4).
- **A claim** in the docs about a risk goes in `compliance/owasp.yaml` with the test that
  shows it; `counsel compliance` renders the page.
- **A model change** re-records the eval suites (`COUNSEL_MODEL=record counsel eval`);
  CI replays the recordings.

CI must stay green: the commitment audit, the tests, the incident bench, the eval gate,
the page's browser database, and the security checks.
