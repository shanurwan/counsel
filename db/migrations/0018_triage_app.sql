-- The Counsel Triage GitHub App (docs/triage-app.md).
--
-- A maintainer installs the app on their repository: that install is their consent,
-- recorded by GitHub. Counsel then watches the repository and posts each triage note
-- to one notes issue there, addressed to the maintainer, never on the reporter's
-- issue. Uninstalling stops it on the next tick.

create table counsel.installations (
  repo             text primary key,
  installation_id  bigint not null,
  account          text not null,
  installed_at     timestamptz not null,
  removed_at       timestamptz,
  -- The issue the notes go to, created on the first delivery.
  notes_issue      int
);

create table counsel.deliveries (
  draft_id      uuid primary key references counsel.drafts (id),
  repo          text not null,
  issue_number  int not null,
  comment_url   text not null,
  delivered_at  timestamptz not null default now()
);

alter table counsel.installations enable row level security;
alter table counsel.deliveries enable row level security;
create policy orchestrator_installations on counsel.installations
  for all to counsel_orchestrator using (true) with check (true);
create policy orchestrator_deliveries on counsel.deliveries
  for all to counsel_orchestrator using (true) with check (true);
revoke all on counsel.installations, counsel.deliveries from public;
grant select, insert, update on counsel.installations to counsel_orchestrator;
grant select, insert on counsel.deliveries to counsel_orchestrator;

-- Delivery runs in the core runtime, as the orchestrator. It needs the triage drafts
-- and nothing else: support-reply drafts hold customer text, which the core runtime
-- must not read (docs/red-team.md, R3). So it reads a view of the triage drafts only,
-- not counsel.drafts itself.
create view counsel.triage_drafts as
  select id, kind, body, status, created_at from counsel.drafts where kind like 'issue_triage:%';
revoke all on counsel.triage_drafts from public;
grant select on counsel.triage_drafts to counsel_orchestrator;
