-- Contacting people, and undoing a run.
--
-- The interruption budget is enforced where nothing can talk its way past it:
-- a trigger on the outbox. A person's budget is how many messages agents may
-- send them in any 7 days; 0 opts them out. Over budget raises CN004, which the
-- runtime records as a denial and never retries.
--
-- Rollback is one row: inserting into counsel.run_rollbacks retracts everything
-- the run produced that a person has not already accepted, in that one
-- transaction. Nothing is deleted: retracted output stays visible, marked.
-- The runtime rolls a run back when it dies or is refused, and `counsel
-- rollback <run>` does it on request.

alter table work.people add column interruption_budget int not null default 2
  check (interruption_budget between 0 and 20);

alter table counsel.outbound_messages
  add column task_ids uuid[] not null default '{}',
  add column cancelled_at timestamptz;

create function counsel.guard_contact() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  allowance int;
  spent     int;
begin
  -- The row lock makes two concurrent sends to one person count each other.
  select interruption_budget into allowance from work.people where id = new.recipient_id for update;
  select count(*) into spent from counsel.outbound_messages
  where recipient_id = new.recipient_id and cancelled_at is null
    and created_at > now() - interval '7 days';
  if spent >= allowance then
    raise exception 'interruption budget spent for %', new.recipient_id
      using errcode = 'CN004', detail = format('%s of %s messages in the last 7 days', spent, allowance);
  end if;
  return new;
end $$;

create trigger guard_contact
  before insert on counsel.outbound_messages
  for each row execute function counsel.guard_contact();

-- Which run wrote a carried note, so it can be retracted with that run.
alter table work.task_updates
  add column run_id uuid,
  add column retracted_at timestamptz;

alter table counsel.answers add column retracted_at timestamptz;

alter table counsel.drafts drop constraint drafts_status_check;
alter table counsel.drafts add constraint drafts_status_check
  check (status in ('pending', 'published', 'rejected', 'retracted'));

create table counsel.run_rollbacks (
  run_id  uuid primary key references counsel.runs (id),
  reason  text not null,
  at      timestamptz not null default now()
);

create function counsel.apply_rollback() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  drafts_n   int;
  answers_n  int;
  notes_n    int;
  messages_n int;
  kept_n     int;
begin
  update counsel.drafts set status = 'retracted' where run_id = new.run_id and status = 'pending';
  get diagnostics drafts_n = row_count;
  update counsel.answers set retracted_at = new.at where run_id = new.run_id and retracted_at is null;
  get diagnostics answers_n = row_count;
  update work.task_updates set retracted_at = new.at where run_id = new.run_id and retracted_at is null;
  get diagnostics notes_n = row_count;
  update counsel.outbound_messages set cancelled_at = new.at where run_id = new.run_id and cancelled_at is null;
  get diagnostics messages_n = row_count;
  -- A person's decision stands: a published draft may already be with a customer.
  select count(*) into kept_n from counsel.drafts where run_id = new.run_id and status = 'published';
  insert into counsel.audit_log (run_id, action, target, decision, detail)
  values (new.run_id, 'rollback', 'run:' || new.run_id, 'reverted',
          jsonb_build_object('reason', new.reason, 'drafts', drafts_n, 'answers', answers_n,
                             'notes', notes_n, 'messages', messages_n, 'kept_published', kept_n));
  return new;
end $$;

create trigger apply_rollback
  after insert on counsel.run_rollbacks
  for each row execute function counsel.apply_rollback();

grant select, insert on counsel.run_rollbacks to counsel_orchestrator;

-- A retracted answer no longer counts as work absorbed. (Published drafts were
-- accepted by a person and always count.)
create or replace view counsel.accepted_outputs as
select r.agent, a.run_id, r.started_at as accepted_at, 'answer' as kind
from counsel.answers a
join counsel.runs r on r.id = a.run_id
where a.body not like 'The handbook does not cover this.%' and a.retracted_at is null
union all
select r.agent, d.run_id, d.reviewed_at, split_part(d.kind, ':', 1)
from counsel.drafts d
join counsel.runs r on r.id = d.run_id
where d.status = 'published';
