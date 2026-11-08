# Security

## Reporting a way through

Counsel's claim is narrow: no agent can write a commitment (owner, due date or status),
read an integration token, post to a customer's thread or edit the audit log, whatever
its model is told. If you find a way through, as any agent role or as the MCP login,
report it privately through GitHub:
**[Report a vulnerability](https://github.com/shanurwan/counsel/security/advisories/new)**.
Include the SQL or the tool call, and which role or login ran it.

The browser console on the [demo page](https://shanurwan.github.io/counsel/) runs the
production migrations in your browser; a way through there is a way through here.
Breaking the browser's own superuser does not count: production has none.

You will get a reply within a week. Fixes land on `main` and deploy from there; there
are no other supported versions.

## In scope

- Any agent role, runtime login or the MCP login reaching what `counsel audit` says it
  cannot, or a gap in the audit itself.
- The approval page and `public.counsel_decide` accepting an approval they should refuse
  ([docs/per-action-consent.md](docs/per-action-consent.md)).
- The hosted MCP server accepting a token it should refuse.
- Secrets in the repository or its history, and the CI and deploy workflows.

## Known and documented

These are already written down, with the tests that pin them. A report that goes
further than the document is welcome.

- The support pool's runtime login chooses which ticket the drafter sees, so it can
  read every ticket one at a time ([docs/red-team.md](docs/red-team.md), R3).
- Supabase Auth gives every access token `aud: "authenticated"`, so the MCP server
  cannot yet require tokens bound to it (supabase/auth#2610). Tokens bound to another
  server are refused.
- Injected text can steer what an agent drafts within its role; a person reviews every
  draft ([docs/compliance.md](docs/compliance.md), ASI01).
- All data is synthetic. There are no real users or customer data.

## How the repository is kept

Every push runs gitleaks over the full history, bandit, pip-audit, checkov, zizmor and
OpenSSF Scorecard; every GitHub Action is pinned by commit; Dependabot proposes updates.
Deployed images carry signed build provenance and an SPDX SBOM:

```sh
gh attestation verify oci://us-central1-docker.pkg.dev/counsel-513516/counsel/counsel@<digest> -R shanurwan/counsel
```
