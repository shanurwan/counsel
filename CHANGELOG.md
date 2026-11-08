# Changelog

## 0.1.0 — 2026-11-08

The first tagged release. Everything below runs on free tiers, on synthetic data.

**Walls**
- Agents are Postgres roles compiled from `agents/*.yaml`; no capability can grant a
  commitment write, and a trigger refuses one even after a mistaken grant.
- `counsel audit` reads the live privilege matrix on every push.
- Each runtime pool has its own database login and Cloud Run service; the agent that
  reads customer tickets runs apart from the rest, and the audit fails if a login
  reaches outside its pool.

**Runtime**
- Leased Postgres job queue with a step journal committed in the same transaction as
  each write; resume, replay and whole-run rollback.
- Each step renews and checks its lease; model calls are recorded as intents before
  they are made, have a time budget, and record their real duration and failed attempts.
- On SIGTERM an instance takes no new jobs.

**People in the loop**
- Drafts are published by a person on their own connection; carried notes need a
  consent hashing the exact text; Postgres caps how often each person is messaged.
- Hosted MCP server behind Supabase Auth (OAuth 2.1). Its one write only proposes; the
  person approves the exact text in their own browser, sent there by URL-mode
  elicitation where the client supports it.

**Evidence**
- Golden eval suites gated on a Wilson lower bound, with a judge trusted only while it
  agrees with hand labels (kappa with a bootstrap interval).
- Incident bench replaying public AI incidents against a control arm.
- OWASP Agentic, LLM and MCP coverage generated from tests (`docs/compliance.md`).
- Red-team findings, two of them from production logs (`docs/red-team.md`).
- Signed build provenance and an SPDX SBOM for every deployed image; OpenSSF Scorecard.
