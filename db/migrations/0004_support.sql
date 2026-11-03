-- Support: customer tickets, the customer-visible reply thread, and secrets.
--
-- This is the shape of the July 2025 Supabase MCP finding (General Analysis):
-- an agent that reads customer-written tickets, a table of integration tokens
-- in the same database, and a thread the customer can read. With a
-- service_role connection those three make a leak. Here:
--   * no agent role can be granted anything on support.integration_tokens
--     (no capability exists, and the audit fails if one appears);
--   * no agent can write to support.ticket_messages, the customer-visible
--     thread; replies are drafts a person sends;
--   * an agent sees only the ticket it was started for (RLS on the ticket id
--     the runtime sets per step, not one the model can choose).

create schema support;
revoke all on schema support from public;

create table support.customers (
  id    text primary key,
  name  text not null
);

create table support.tickets (
  id           uuid primary key default gen_random_uuid(),
  customer_id  text not null references support.customers (id),
  subject      text not null,
  body         text not null,
  created_at   timestamptz not null default now()
);

-- What the customer sees. Only people (support staff) post here.
create table support.ticket_messages (
  id         uuid primary key default gen_random_uuid(),
  ticket_id  uuid not null references support.tickets (id),
  author     text not null default current_user,
  body       text not null,
  created_at timestamptz not null default now()
);

-- The bait. Real systems keep these somewhere; agents must never reach them.
create table support.integration_tokens (
  id           bigint generated always as identity primary key,
  customer_id  text not null references support.customers (id),
  provider     text not null,
  token        text not null,
  created_at   timestamptz not null default now()
);

-- An agent reads one ticket: the one its job is for. The runtime sets
-- counsel.ticket in each step's transaction from the job payload; the model
-- never writes SQL, so it cannot change it.
alter table support.tickets enable row level security;
alter table support.ticket_messages enable row level security;

create policy agent_reads_its_ticket on support.tickets
  for select to counsel_agents
  using (id = nullif(current_setting('counsel.ticket', true), '')::uuid);

create policy agent_reads_its_thread on support.ticket_messages
  for select to counsel_agents
  using (ticket_id = nullif(current_setting('counsel.ticket', true), '')::uuid);

-- Event path: a new ticket enqueues the support drafter.
create function counsel.enqueue_ticket() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into counsel.jobs (agent, idempotency_key, payload)
  values ('support_drafter', 'ticket:' || new.id, jsonb_build_object('ticket_id', new.id))
  on conflict (idempotency_key) do nothing;
  return new;
end $$;

create trigger enqueue_ticket
  after insert on support.tickets
  for each row execute function counsel.enqueue_ticket();
