-- People sign in to the hosted MCP server with this project's Supabase Auth
-- (docs/mcp.md). A sign-in means nothing until an owner links it to a person
-- with `counsel link-login`; nobody links themselves.

create table if not exists counsel.logins (
  auth_user_id uuid primary key,
  person_id    uuid not null references work.people (id) on delete cascade,
  linked_by    text not null default current_user,
  linked_at    timestamptz not null default now()
);

alter table counsel.logins enable row level security;
revoke all on counsel.logins from public;

-- What a signed-in person sees: their own unfinished work, and who they are.
-- counsel_mcp holds no grant on work.tasks or counsel.logins. It can run these
-- two functions, with a user id the server took from a verified token, and
-- `counsel audit` fails if it can run anything else that is SECURITY DEFINER.

create or replace function counsel.person_for_login(p_auth_user uuid)
returns table (handle text, team_id text)
language sql stable security definer set search_path = '' as $$
  select p.handle, p.team_id
  from counsel.logins l join work.people p on p.id = l.person_id
  where l.auth_user_id = p_auth_user
$$;

create or replace function counsel.work_for_login(p_auth_user uuid)
returns table (title text, team_id text, status text, due_date date, updated_at timestamptz)
language sql stable security definer set search_path = '' as $$
  select t.title, t.team_id, t.status, t.due_date, t.updated_at
  from counsel.logins l join work.tasks t on t.owner_id = l.person_id
  where l.auth_user_id = p_auth_user and t.status <> 'done'
  order by t.due_date nulls last, t.updated_at desc
  limit 50
$$;

revoke all on function counsel.person_for_login(uuid), counsel.work_for_login(uuid) from public;
grant execute on function counsel.person_for_login(uuid), counsel.work_for_login(uuid) to counsel_mcp;
