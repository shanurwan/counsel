# The Counsel Triage app

A GitHub App that brings `issue_triage` to a maintainer's own repository. The page
maintainers see is [site/triage.html](../site/triage.html); the code is
[githubapp.py](../src/counsel/githubapp.py).

## Consent is the install

Installing the app on a repository is the maintainer's consent, and GitHub keeps the
record. Nobody else can opt a repository in. Uninstalling removes the consent: the next
tick records the removal and nothing more is read or posted there.

Public repositories only. A private repository on an installation is skipped, because
issue text goes to a hosted model and into Counsel's database.

## What each tick does

1. Lists the app's installations and records installs and removals
   (`counsel.installations`).
2. Watches each installed repository like any other, with that installation's own
   token: issues opened before the install are history, kept for duplicate checks;
   new issues are triaged.
3. After the agents run, posts each new triage draft once, as a comment on one notes
   issue in that repository (`counsel.deliveries`), created on the first delivery.

The note is for the maintainer. It is never posted on the reporter's issue, and the
app never labels, assigns or closes. GitHub's `Issues: write` permission would allow
those; the code does not call them, and the red-team table names the key as what a
compromise would expose ([red-team.md](red-team.md)).

## Who reads what

Delivery runs in the core runtime, as the orchestrator. It reads triage drafts through
`counsel.triage_drafts`, a view of those drafts only: support-reply drafts hold
customer text, which the core runtime must not read.

## Counting users

A user is a maintainer, other than the author, who installed the app on a public
repository, received at least one note, and reacted to it, replied or acted on the
issue. Installs alone, stars and page views do not count. Each delivered note is in
`counsel.deliveries` with the link to its comment, so the reactions on it can be read
back.

## Setting it up

1. Create the GitHub App under the author's account: Issues read and write, Metadata
   read, no webhook, installable by any account. Generate a private key.
2. Store the key as the first version of the `github-app-key` secret.
3. Set `github_app_id` in `infra/terraform.tfvars` and apply. The core runtime starts
   listing installations on its next tick.
