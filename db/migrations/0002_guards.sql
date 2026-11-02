-- The database refuses commitment writes from any agent, even if a grant is
-- added by mistake. Column privileges are the first line; these triggers are
-- the line behind it.
--
-- A denial raises SQLSTATE CN001 or CN002. Postgres has no autonomous
-- transactions, so the trigger cannot keep an audit row while also aborting
-- the statement. The runtime records the denial in counsel.audit_log in a
-- fresh transaction (docs/adr/001-commitment-walls.md), and the Postgres log
-- keeps the raised error either way.

-- "Acts as an agent" follows only memberships that pass privileges on
-- (INHERIT or SET). pg_has_role(..., 'MEMBER') would also count a superuser,
-- and on Supabase the admin-only membership postgres gets in every role it
-- creates, which would lock people out of their own tasks.

do $$
begin
  if not exists (select from pg_roles where rolname = 'counsel_agents') then
    create role counsel_agents nologin;
  end if;
end $$;

create function counsel.guard_commitments() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if not exists (
    with recursive up(oid) as (
      select oid from pg_catalog.pg_roles where rolname = current_user
      union
      select am.roleid from pg_catalog.pg_auth_members am
      join up on am.member = up.oid
      where am.inherit_option or am.set_option
    )
    select from up join pg_catalog.pg_roles g on g.oid = up.oid
    where g.rolname = 'counsel_agents') then
    if tg_op = 'UPDATE' then
      new.updated_at := now();
    end if;
    return coalesce(new, old);
  end if;

  if tg_op = 'UPDATE' and (
       new.owner_id is distinct from old.owner_id
    or new.due_date is distinct from old.due_date
    or new.status   is distinct from old.status) then
    raise exception 'commitment write denied for %', current_user
      using errcode = 'CN001',
            detail  = format('task %s: owner_id, due_date and status are set by people only', old.id);
  end if;

  if tg_op in ('INSERT', 'DELETE', 'TRUNCATE') then
    raise exception 'agents cannot % tasks (%)', lower(tg_op), current_user
      using errcode = 'CN001';
  end if;

  return new;
end $$;

create trigger guard_commitments
  before insert or update or delete on work.tasks
  for each row execute function counsel.guard_commitments();

create trigger guard_commitments_truncate
  before truncate on work.tasks
  for each statement execute function counsel.guard_commitments();

-- An agent may carry an update into the work system only with a consent that
-- matches the exact text, belongs to that task, is unexpired and unused.
-- SECURITY DEFINER so it can mark the consent used; agents have no grant on
-- work.consents.
create function counsel.check_consent() returns trigger
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
  return new;
end $$;

create trigger check_consent
  before insert on work.task_updates
  for each row execute function counsel.check_consent();

-- The audit log is append-only for everyone, owner included.
create function counsel.audit_log_append_only() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'counsel.audit_log is append-only' using errcode = 'CN003';
end $$;

create trigger audit_log_append_only
  before update or delete or truncate on counsel.audit_log
  for each statement execute function counsel.audit_log_append_only();
