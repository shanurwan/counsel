-- Counsel core schema.
--
-- work.*     the shared work system people rely on (tasks, updates, consent)
-- kb.*       the handbook agents may read
-- counsel.*  what agents produce (answers, drafts, messages) and the audit log
--
-- Commitments are owner_id, due_date and status on work.tasks. Only people
-- set them. Nothing in this file grants anything to an agent: agent grants are
-- generated from agents/*.yaml by `counsel compile`.

create schema if not exists work;
create schema if not exists kb;
create schema if not exists counsel;

-- Functions are executable by PUBLIC by default. Agents get nothing by default.
alter default privileges revoke execute on functions from public;
revoke all on schema work, kb, counsel from public;

create table work.teams (
  id    text primary key,
  name  text not null
);

create table work.people (
  id       uuid primary key default gen_random_uuid(),
  handle   text not null unique,
  team_id  text not null references work.teams (id),
  tz       text not null
);

create table work.tasks (
  id          uuid primary key default gen_random_uuid(),
  team_id     text not null references work.teams (id),
  title       text not null,
  -- commitment columns: people only
  owner_id    uuid references work.people (id),
  due_date    date,
  status      text not null default 'open'
              check (status in ('open', 'in_progress', 'blocked', 'done')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- A person approves the exact text an agent may carry into the work system.
-- The agent receives the consent id, never the right to write anything else.
create table work.consents (
  id           uuid primary key default gen_random_uuid(),
  person_id    uuid not null references work.people (id),
  task_id      uuid not null references work.tasks (id),
  text_sha256  text not null check (text_sha256 ~ '^[0-9a-f]{64}$'),
  granted_at   timestamptz not null default now(),
  expires_at   timestamptz not null default now() + interval '24 hours',
  used_at      timestamptz
);

create table work.task_updates (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid not null references work.tasks (id),
  body        text not null,
  consent_id  uuid references work.consents (id),
  -- identity comes from the session, not from the caller
  author      text not null default current_user,
  created_at  timestamptz not null default now()
);

create table kb.documents (
  id     uuid primary key default gen_random_uuid(),
  path   text not null unique,
  body   text not null
);

create table counsel.answers (
  id          uuid primary key default gen_random_uuid(),
  agent       text not null default current_user,
  run_id      uuid,
  question    text not null,
  body        text not null,
  created_at  timestamptz not null default now()
);

create table counsel.drafts (
  id            uuid primary key default gen_random_uuid(),
  agent         text not null default current_user,
  run_id        uuid,
  kind          text not null,
  body          text not null,
  status        text not null default 'pending'
                check (status in ('pending', 'published', 'rejected')),
  created_at    timestamptz not null default now()
);

create table counsel.outbound_messages (
  id            uuid primary key default gen_random_uuid(),
  agent         text not null default current_user,
  run_id        uuid,
  recipient_id  uuid not null references work.people (id),
  body          text not null,
  created_at    timestamptz not null default now()
);

-- Append-only record of every consequential decision, including denials.
create table counsel.audit_log (
  id        bigint generated always as identity primary key,
  at        timestamptz not null default now(),
  actor     text not null default current_user,
  run_id    uuid,
  action    text not null,
  target    text,
  decision  text not null check (decision in ('allowed', 'denied', 'approved', 'reverted')),
  detail    jsonb not null default '{}'
);
