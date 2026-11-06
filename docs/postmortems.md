# What went wrong

Counsel has no real users, so none of these hurt anyone. They are real faults in
the running system and its pipeline, recorded because how a system fails is more
useful than how it was meant to work.

## Dashboard panels failed: too many connections (53300)

- **What happened.** Most panels on the live Grafana dashboard showed errors or no
  data. Postgres returned `too many connections for role counsel_reader`.
- **Found by.** A person trying to screenshot the dashboard. No alert fired, because
  the alerts read through the same login.
- **Cause.** Migration 0006 capped the reader at 3 connections so a dashboard could
  never starve the agents. Grafana opens a connection per panel, and there are 13.
- **Fix.** Migration 0007 raised the cap to 10, keeping the 10 s statement timeout.
- **Lesson.** A limit chosen for safety still needs a load test. Alerts that share a
  dependency with what they watch cannot report that dependency's failure.

## Code deployed ahead of its schema

- **What happened.** CI deployed new code on merge, while migrations were applied by
  hand afterwards. Twice (0005 and 0009), the deployed code expected columns that did
  not exist yet, until a person ran the migration.
- **Found by.** Noticing the gap while preparing the next change.
- **Cause.** The deploy pipeline and the schema had no ordering between them.
- **Fix.** Deploy now runs `counsel migrate`, then `counsel audit`, then ships the
  image. If either fails, nothing deploys. Migrations only add, so the running
  revision keeps working on the new schema.
- **Trade-off.** CI now holds a privileged database URL. It is a separate secret,
  readable only by the deployer's identity, reached through keyless federation.

## The digest credited the wrong person, and repeated blame

- **What happened.** The first eval gate run failed `team_digest`. Work was credited
  to a task's owner when someone else did it, and the digest repeated "Bruno missed
  the deadline again".
- **Found by.** The eval suite, before anyone relied on the digest.
- **Cause.** The credit failure was a data bug, not a prompt bug: the digest's input
  never included who wrote each note, so the model could only guess the owner.
- **Fix.** The query now passes each note's author, and the prompt forbids judgements
  about people.
- **Still open.** `frustrated_update_stays_neutral` still fails: the model softens a
  complaint but keeps "ignored for two weeks". It is reported in the gate, not hidden,
  and it is not a safety case for the digest.

## The leadership brief repeated a complaint about a named person

- **What happened.** On its first live run, `exec_weekly` wrote "Note states Tomas
  has ignored this for two weeks" in the brief for leadership.
- **Found by.** A safety case, before the agent shipped.
- **Fix.** A note that complains about someone is now not treated as a fact about the
  work: the prompt says to report the task's status and date as if the note were not
  there. The case passes on the recorded model.
- **Limit.** That is a prompt, so it reduces the chance rather than removing it. The
  brief goes through a person, and the safety case holds the behaviour in CI.

## A reader migration failed on the real database

- **What happened.** Migration 0006 passed in CI and failed on Supabase with "Only
  roles with SUPERUSER...".
- **Cause.** `NOSUPERUSER` and `NOREPLICATION` may not be named by an admin who is not
  a superuser, even to turn them off. CI's Postgres runs as a superuser.
- **Fix.** Removed the attributes, since a new role has neither, and the audit checks
  them against the live database anyway. The migration's transaction left nothing
  half applied.
- **Lesson.** A superuser test database hides what a managed platform refuses.

## The model was retired, and thinking used up the token budget

- **What happened.** Both models in the original plan returned 410 Gone. Later, a
  digest was cut off mid-line.
- **Cause.** The provider retires models without much notice. Nemotron also reasons
  before answering by default, and those tokens count against `max_tokens`.
- **Fix.** The adapter takes an ordered list and falls through on 404 and 410, thinking
  is disabled, and a `length` finish is an error, so a truncated answer is never
  written. It is still recorded with its usage, because it was billed.

## GitHub polling missed its own cache

- **What happened.** During development, every poll cost a rate-limit request.
- **Found by.** Running against the real API. The fake transport passed.
- **Cause.** The moving `since` parameter changed the URL, and an ETag belongs to one
  URL. Details: [github.md](github.md).
- **Fix.** The URL no longer moves, and a regression test states the rule.
