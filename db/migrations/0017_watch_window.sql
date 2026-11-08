-- Watching a repository starts at a moment (docs/field-week.md).
--
-- The first sync of a new repository records when watching began. Issues opened
-- before then are history: stored so the triage agent can spot a duplicate among
-- them, but never triaged themselves, so adding a busy repository does not spend a
-- day of model calls on its backlog. Issues opened after it are triaged as before.
-- A repository synced before this migration has no start and behaves as it did.

alter table counsel.sync_state add column watched_since timestamptz;

create or replace function counsel.enqueue_issue() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.state = 'open' and new.opened_at >= coalesce(
       (select s.watched_since from counsel.sync_state s where s.repo = new.repo), '-infinity') then
    insert into counsel.jobs (agent, idempotency_key, payload)
    values ('issue_triage', 'issue:' || new.repo || '#' || new.number,
            jsonb_build_object('repo', new.repo, 'number', new.number))
    on conflict (idempotency_key) do nothing;
  end if;
  return new;
end $$;
