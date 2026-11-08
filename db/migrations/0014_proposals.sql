-- Consent for each action, not only for the connection (docs/per-action-consent.md).
--
-- Connecting an assistant (consent.html) proves who is calling. It says nothing
-- about whether that person chose a particular call. So a hosted MCP tool that
-- writes on someone's behalf only proposes; the write happens when the person
-- approves that exact proposal in a browser, signed in as themselves:
--
-- 1. propose_note (counsel_mcp, for the user id in a verified MCP token) stores
--    the exact text, for one of that person's own open tasks. Nothing is written.
-- 2. The person opens approve.html, signs in, and reads the proposal through
--    public.counsel_proposal.
-- 3. public.counsel_decide approves it. It runs only for PostgREST's
--    authenticated role, and refuses:
--      - a token issued to an OAuth client (an MCP token has client_id; the
--        person's own browser session does not), so the assistant cannot approve
--        its own proposal with the token it already holds;
--      - a sign-in older than ten minutes;
--      - anyone but the person it was proposed for;
--      - text other than what the page showed (the page sends its sha256);
--      - a proposal that expired or was already decided.
--    Approving records a work.consents row for the exact text and writes the note
--    in the same transaction, credited to the person. There is no second step for
--    the assistant to replay.

do $$
begin
  -- Supabase has these; a plain Postgres (CI, the page's PGlite) needs stand-ins.
  if not exists (select from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
end $$;

create table counsel.proposals (
  id            uuid primary key default gen_random_uuid(),
  person_id     uuid not null references work.people (id) on delete cascade,
  task_id       uuid not null references work.tasks (id) on delete cascade,
  body          text not null check (length(body) between 1 and 2000),
  -- who asked: the signed-in user and the OAuth client acting for them
  auth_user_id  uuid not null,
  client_id     text not null,
  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null default now() + interval '15 minutes',
  decision      text check (decision in ('approved', 'denied')),
  decided_at    timestamptz,
  consent_id    uuid references work.consents (id),
  update_id     uuid references work.task_updates (id)
);

create index proposals_person_pending on counsel.proposals (person_id) where decision is null;

alter table counsel.proposals enable row level security;
revoke all on counsel.proposals from public;

-- 1. The assistant proposes. Only for the person behind the token, on their own task.
create or replace function counsel.propose_note(p_auth_user uuid, p_client_id text, p_task_title text,
                                                p_body text)
returns table (proposal_id uuid, task_title text, expires_at timestamptz)
language plpgsql security definer set search_path = '' as $$
declare
  v_person uuid;
  v_task   work.tasks%rowtype;
  v_id     uuid;
  v_exp    timestamptz;
begin
  select l.person_id into v_person from counsel.logins l where l.auth_user_id = p_auth_user;
  if v_person is null then
    raise exception 'this sign-in is not linked to a person' using errcode = 'CN010';
  end if;

  select t.* into v_task from work.tasks t
  where t.owner_id = v_person and t.status <> 'done' and t.title = p_task_title
  limit 1;
  if not found then
    raise exception 'no open task of yours is titled %', p_task_title using errcode = 'CN010';
  end if;

  -- A model in a loop could flood the person with requests.
  if (select count(*) from counsel.proposals p
      where p.person_id = v_person and p.decision is null and p.expires_at > now()) >= 5 then
    raise exception 'five proposals are already waiting for you' using errcode = 'CN010';
  end if;

  insert into counsel.proposals (person_id, task_id, body, auth_user_id, client_id)
  values (v_person, v_task.id, p_body, p_auth_user, coalesce(p_client_id, ''))
  returning id, proposals.expires_at into v_id, v_exp;

  insert into counsel.audit_log (action, target, decision, detail)
  values ('propose_note', v_task.id::text, 'allowed',
          jsonb_build_object('proposal', v_id, 'client_id', p_client_id));

  return query select v_id, v_task.title, v_exp;
end $$;

-- What the assistant may learn back: the state of a proposal it made for this person.
create or replace function counsel.proposal_for_login(p_auth_user uuid, p_id uuid)
returns table (status text, task_title text, expires_at timestamptz, decided_at timestamptz)
language sql stable security definer set search_path = '' as $$
  select case when p.decision is not null then p.decision
              when p.expires_at <= now() then 'expired'
              else 'waiting' end,
         t.title, p.expires_at, p.decided_at
  from counsel.proposals p
  join counsel.logins l on l.person_id = p.person_id and l.auth_user_id = p_auth_user
  join work.tasks t on t.id = p.task_id
  where p.id = p_id
$$;

revoke all on function counsel.propose_note(uuid, text, text, text),
                       counsel.proposal_for_login(uuid, uuid) from public;
grant execute on function counsel.propose_note(uuid, text, text, text),
                          counsel.proposal_for_login(uuid, uuid) to counsel_mcp;

-- The person behind this browser request, or an error. PostgREST verifies the JWT,
-- switches to the authenticated role and puts the claims in request.jwt.claims.
-- Any role can set that setting itself, so the role switch is checked too.
create or replace function counsel.browser_person(p_fresh boolean)
returns uuid
language plpgsql stable security definer set search_path = '' as $$
declare
  claims    jsonb := nullif(current_setting('request.jwt.claims', true), '')::jsonb;
  caller    text  := nullif(current_setting('role', true), 'none');
  signed_at bigint;
  v_person  uuid;
begin
  if caller is distinct from 'authenticated' or claims is null
     or claims ->> 'role' is distinct from 'authenticated' then
    raise exception 'sign in first' using errcode = 'CN011';
  end if;
  -- An OAuth client's token (the assistant's) is not the person at a browser.
  if claims ? 'client_id' then
    raise exception 'an assistant cannot approve; open the link and sign in yourself'
      using errcode = 'CN011';
  end if;
  if p_fresh then
    select max((m ->> 'timestamp')::bigint) into signed_at
    from jsonb_array_elements(coalesce(claims -> 'amr', '[]'::jsonb)) m;
    if signed_at is null or signed_at < extract(epoch from now()) - 600 then
      raise exception 'sign in again to approve' using errcode = 'CN012';
    end if;
  end if;
  select l.person_id into v_person from counsel.logins l
  where l.auth_user_id = (claims ->> 'sub')::uuid;
  if v_person is null then
    raise exception 'this sign-in is not linked to a person' using errcode = 'CN011';
  end if;
  return v_person;
end $$;

revoke all on function counsel.browser_person(boolean) from public;

-- 2. The approval page reads a proposal: only the person's own.
create or replace function public.counsel_proposal(p_id uuid)
returns table (task_title text, body text, client_id text, created_at timestamptz, expires_at timestamptz,
               status text)
language plpgsql stable security definer set search_path = '' as $$
declare
  v_person uuid := counsel.browser_person(false);
begin
  return query
  select t.title, p.body, p.client_id, p.created_at, p.expires_at,
         case when p.decision is not null then p.decision
              when p.expires_at <= now() then 'expired'
              else 'waiting' end
  from counsel.proposals p join work.tasks t on t.id = p.task_id
  where p.id = p_id and p.person_id = v_person;
end $$;

-- 3. The person decides. Approving needs a fresh sign-in and the hash of what they read.
create or replace function public.counsel_decide(p_id uuid, p_approve boolean, p_body_sha256 text default null)
returns text
language plpgsql security definer set search_path = '' as $$
declare
  v_person  uuid := counsel.browser_person(p_approve);
  p         counsel.proposals%rowtype;
  v_consent uuid;
  v_update  uuid;
  v_handle  text;
begin
  select * into p from counsel.proposals where id = p_id and person_id = v_person for update;
  if not found then
    raise exception 'no such proposal for you' using errcode = 'CN011';
  end if;
  if p.decision is not null or p.expires_at <= now() then
    raise exception 'this proposal was already decided or has expired' using errcode = 'CN011';
  end if;

  if not p_approve then
    update counsel.proposals set decision = 'denied', decided_at = now() where id = p.id;
    insert into counsel.audit_log (action, target, decision, detail)
    values ('decide_proposal', p.task_id::text, 'denied', jsonb_build_object('proposal', p.id));
    return 'denied';
  end if;

  if p_body_sha256 is distinct from encode(sha256(convert_to(p.body, 'UTF8')), 'hex') then
    raise exception 'the text changed since you read it' using errcode = 'CN011';
  end if;

  insert into work.consents (person_id, task_id, text_sha256, expires_at, used_at)
  values (p.person_id, p.task_id, p_body_sha256, now(), now())
  returning id into v_consent;

  select handle into v_handle from work.people where id = p.person_id;
  insert into work.task_updates (task_id, body, consent_id, author, on_behalf_of)
  values (p.task_id, p.body, v_consent, 'assistant:' || p.client_id, v_handle)
  returning id into v_update;

  update counsel.proposals
  set decision = 'approved', decided_at = now(), consent_id = v_consent, update_id = v_update
  where id = p.id;

  insert into counsel.audit_log (action, target, decision, detail)
  values ('decide_proposal', p.task_id::text, 'approved',
          jsonb_build_object('proposal', p.id, 'consent', v_consent, 'client_id', p.client_id));
  return 'approved';
end $$;

-- Supabase grants new functions in public to anon and authenticated by default.
revoke all on function public.counsel_proposal(uuid), public.counsel_decide(uuid, boolean, text)
  from public, anon;
grant execute on function public.counsel_proposal(uuid), public.counsel_decide(uuid, boolean, text)
  to authenticated;
-- browser_person runs inside them, as their owner; nobody calls it directly.
