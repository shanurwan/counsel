-- Standups, and carrying a person's own words into their tasks.
--
-- 1. A person posts a standup (work.standups). A trigger enqueues
--    standup_carrier, which sees that one standup only (RLS on the id the
--    runtime sets, like a support ticket).
-- 2. The agent proposes notes: each one a verbatim piece of the standup,
--    for one of the person's team's tasks. Proposals are drafts.
-- 3. The person approves a proposal (`counsel review <draft> --publish`).
--    That creates a work.consents row hashing the exact text, and enqueues
--    the carry.
-- 4. The agent inserts the note with the consent id. check_consent verifies
--    text, task, expiry and single use, and now also records whose words the
--    note carries (on_behalf_of), so the digest credits the person, not the
--    agent. The agent cannot set on_behalf_of: it has no grant on the column
--    and the trigger overwrites it.

create table work.standups (
  id          uuid primary key default gen_random_uuid(),
  person_id   uuid not null references work.people (id),
  body        text not null,
  created_at  timestamptz not null default now()
);

alter table work.standups enable row level security;

create policy agent_reads_its_standup on work.standups
  for select to counsel_agents
  using (id = nullif(current_setting('counsel.standup', true), '')::uuid);

alter table work.task_updates add column on_behalf_of text;

create or replace function counsel.check_consent() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  c       work.consents%rowtype;
  caller  text;
begin
  -- Inside SECURITY DEFINER, current_user is the owner. The caller is the
  -- role set with SET ROLE, or the login role when none is set.
  caller := nullif(current_setting('role'), 'none');
  caller := coalesce(caller, session_user);

  if not exists (
    with recursive up(oid) as (
      select oid from pg_catalog.pg_roles where rolname = caller
      union
      select am.roleid from pg_catalog.pg_auth_members am
      join up on am.member = up.oid
      where am.inherit_option or am.set_option
    )
    select from up join pg_catalog.pg_roles g on g.oid = up.oid
    where g.rolname = 'counsel_agents') then
    return new;
  end if;

  select * into c from work.consents where id = new.consent_id for update;

  if not found
     or c.task_id <> new.task_id
     or c.text_sha256 <> encode(sha256(convert_to(new.body, 'UTF8')), 'hex')
     or c.expires_at <= now()
     or c.used_at is not null then
    raise exception 'consent check failed for %', caller
      using errcode = 'CN002';
  end if;

  update work.consents set used_at = now() where id = c.id;
  -- Whose words these are, from the consent, never from the caller.
  new.on_behalf_of := (select handle from work.people where id = c.person_id);
  return new;
end $$;

-- Event path: a standup enqueues the carrier's proposal step.
create function counsel.enqueue_standup() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into counsel.jobs (agent, idempotency_key, payload)
  values ('standup_carrier', 'standup:' || new.id, jsonb_build_object('standup_id', new.id))
  on conflict (idempotency_key) do nothing;
  return new;
end $$;

create trigger enqueue_standup
  after insert on work.standups
  for each row execute function counsel.enqueue_standup();

revoke all on work.standups from public;
