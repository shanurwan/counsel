-- How far each team has come in using the platform, computed from what actually
-- happened in the window. A team that disputes its grade gets this query and the
-- rows behind it (`counsel maturity --team <id>`), not an opinion.
--
-- What counts, per team:
--   digests   its team_digest drafts a person published
--   notes     notes its people approved and the carrier put on a task
--   questions handbook answers given to its people
--   rejected  its digests a person rejected
--
-- Grades, each including the one before:
--   0  none      nothing accepted
--   1  trying    at least one accepted output
--   2  using     accepted outputs in 2 or more weeks
--   3  relying   3 or more weeks, and a published digest in each of 2 of them: a
--                workflow that runs on a schedule and that people keep accepting
--   4  pulling   grade 3, 2 or more kinds of output, under a quarter of digests rejected
with activity as (
  select split_part(d.kind, ':', 2) as team, 'digest' as kind, d.reviewed_at as at
  from counsel.drafts d
  where d.kind like 'team_digest:%%' and d.status = 'published'
  union all
  select p.team_id, 'note', u.created_at
  from work.task_updates u
  join work.people p on p.handle = u.on_behalf_of
  where u.retracted_at is null
  union all
  select p.team_id, 'question', a.created_at
  from counsel.questions q
  join work.people p on p.id = q.asker_id
  join counsel.jobs j on j.idempotency_key = 'question:' || q.id
  join counsel.answers a on a.run_id = j.run_id
  where a.retracted_at is null and a.body not like 'The handbook does not cover this.%%'
), windowed as (
  select team, kind, date_trunc('week', at) as week
  from activity
  where at >= %(since)s and at < %(until)s
), reviewed as (
  select split_part(kind, ':', 2) as team,
         count(*) filter (where status = 'rejected') as rejected,
         count(*) as digests_reviewed
  from counsel.drafts
  where kind like 'team_digest:%%' and status in ('published', 'rejected')
    and reviewed_at >= %(since)s and reviewed_at < %(until)s
  group by 1
), rolled as (
  select t.id as team,
         count(w.kind) filter (where w.kind = 'digest') as digests,
         count(w.kind) filter (where w.kind = 'note') as notes,
         count(w.kind) filter (where w.kind = 'question') as questions,
         count(w.kind) as accepted,
         count(distinct w.week) as active_weeks,
         count(distinct w.week) filter (where w.kind = 'digest') as digest_weeks,
         count(distinct w.kind) as kinds,
         coalesce(max(r.rejected), 0) as rejected,
         coalesce(max(r.digests_reviewed), 0) as digests_reviewed
  from work.teams t
  left join windowed w on w.team = t.id
  left join reviewed r on r.team = t.id
  group by t.id
)
select *,
       case
         when active_weeks >= 3 and digest_weeks >= 2 and kinds >= 2 and rejected * 4 < digests_reviewed then 4
         when active_weeks >= 3 and digest_weeks >= 2 then 3
         when active_weeks >= 2 then 2
         when accepted >= 1 then 1
         else 0
       end as grade
from rolled
order by team
