# GitHub: what its API does, and what that forced

`counsel sync` polls public repositories every tick and enqueues `issue_triage`
for each new issue. Code: [`src/counsel/github.py`](../src/counsel/github.py).

## The design consequence

**A token cannot express "comments only".** A token with write access to issues
can comment, and it can also assign, close and relabel. Assignee and state are
commitments, so a runtime holding that token could make the forbidden write,
whatever its code says. So:

- The runtime holds no write token. It reads public repositories, optionally with
  a read-only `COUNSEL_GITHUB_READ_TOKEN`.
- A triage note reaches GitHub only when a person runs `counsel review --publish`,
  using that person's own `GH_TOKEN`. The comment is theirs.
- The connector has no method that assigns, closes or labels, and a test fails if
  one appears.

The app-level guarantee is weaker than the Postgres one, because GitHub is not ours
to constrain. Removing the token from the runtime is what makes it hold.

## Failure modes

| What GitHub does | What goes wrong | What Counsel does |
|---|---|---|
| An ETag belongs to one exact URL | Filtering with a moving `since` changes the URL, so every poll misses and counts against the limit | The URL never changes. The list is sorted by last update and reading stops at the first page older than the cursor |
| A 304 does not count against the rate limit | Without conditional requests, 48 polls a day per repository eat an unauthenticated 60 an hour on a shared IP | Sends `If-None-Match` on page 1 |
| The issues endpoint also returns pull requests | PRs get triaged as issues | Dropped by the `pull_request` key |
| On a busy repository, whole pages can be PRs | 90 items read can hold only a couple of issues, and a quiet issue can be missed behind them | Reads 3 pages at most. Known limit: acceptable for triage, wrong for an audit. A complete view needs the search API or webhooks |
| Two rate limits share 403 and 429, told apart only by headers | Treating a permissions error as a limit waits forever; treating a limit as an error hammers it | `retry-after` is the secondary limit, `x-ratelimit-remaining: 0` the primary, anything else is a real error |
| Pagination is in the `Link` header | A hand-built page URL breaks, and a followed link could point anywhere | Follows `Link`, and refuses a next link off `api.github.com` |
| Issue bodies reach 65k characters, and can hold a NUL byte | Prompt size and free-tier storage, and Postgres refuses NUL in text | Truncated at 8,000 characters and marked; NUL stripped |
| First sync of a busy repository returns dozens of open issues | A day's free model calls gone in one tick | At most 5 new issues per sync, newest first |
| Clocks differ | A change made just before the last sync is skipped | The cursor overlaps by 5 minutes; the write is an upsert |
| An author can edit an issue after triage | Re-triaging on every edit spends money and lets anyone trigger a model call | The row updates; the job's idempotency key means one triage per issue |
| An issue is written by a stranger | Prompt injection | The agent's role can only write a draft. Suite cases in `evals/issue_triage` try it |
| A POST can fail after a person approved | The draft is marked published but never arrived | Delivery runs last in the publish transaction: on failure the draft stays pending |

One limit remains in that last row: if GitHub accepted the comment and the commit
then failed, a retry posts twice. It is the rarer failure, and a duplicate comment
is cheap, where a lost approval is not.

## Found against the real API

The fake transport passed with the moving `since`. The first real sync showed the
second poll returning 200 instead of 304. Only running against the real service
found it, so the regression test now states the rule: the URL does not move.

## Setup

Nothing to configure for public repositories: the repositories to watch are in
`watches:` in [`agents/issue_triage.yaml`](../agents/issue_triage.yaml), reviewed in
a PR like every other agent setting.
