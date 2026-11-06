-- Real issues from GitHub: the first system of record that is not synthetic.
--
-- The sync only ever reads GitHub. issue_triage sees the one repository it was
-- started for (row-level security, like a ticket) and writes a draft. Posting a
-- triage note happens from a person's own session with a person's own token,
-- because a GitHub token that can comment can also assign and close: the scope
-- cannot separate them, so the runtime holds no write token at all
-- (docs/github.md).

create table work.issues (
  id          bigint primary key,
  repo        text not null,
  number      int not null,
  title       text not null,
  body        text not null default '',
  author      text not null,
  state       text not null check (state in ('open', 'closed')),
  labels      text[] not null default '{}',
  url         text not null,
  opened_at   timestamptz not null,
  updated_at  timestamptz not null,
  unique (repo, number)
);

alter table work.issues enable row level security;

-- The agent sees its repository's issues, to spot a duplicate, and nothing else.
create policy agent_reads_its_repo on work.issues
  for select to counsel_agents
  using (repo = nullif(current_setting('counsel.issue_repo', true), ''));

-- The orchestrator is also in counsel_agents; this is the sync, not an agent.
create policy orchestrator_syncs on work.issues
  for all to counsel_orchestrator using (true) with check (true);

revoke all on work.issues from public;

create table counsel.sync_state (
  repo       text primary key,
  etag       text,
  since      timestamptz,
  synced_at  timestamptz
);

grant usage on schema work to counsel_orchestrator;
grant select, insert, update on work.issues to counsel_orchestrator;
grant select, insert, update on counsel.sync_state to counsel_orchestrator;

-- Event path: an issue seen open for the first time enqueues the triage agent.
create function counsel.enqueue_issue() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.state = 'open' then
    insert into counsel.jobs (agent, idempotency_key, payload)
    values ('issue_triage', 'issue:' || new.repo || '#' || new.number,
            jsonb_build_object('repo', new.repo, 'number', new.number))
    on conflict (idempotency_key) do nothing;
  end if;
  return new;
end $$;

create trigger enqueue_issue
  after insert on work.issues
  for each row execute function counsel.enqueue_issue();
