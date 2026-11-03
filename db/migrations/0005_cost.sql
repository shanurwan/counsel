-- Cost: what every model call spent, on which run, on whose behalf.
--
-- counsel.model_calls already holds each call's tokens, latency and model.
-- This adds prices and two views that turn those rows into dollars per call
-- and per run. db/reports/*.sql query the views; `counsel report` renders
-- them into findings an owner can act on. Design: docs/cost.md.
--
-- Agents cannot read any of this: nothing here is granted to counsel_agents
-- or the orchestrator. The report runs with an operator connection.

-- A response served from an earlier identical request (see RunContext.complete).
alter table counsel.model_calls add column cached_from bigint references counsel.model_calls (id);

-- Prices per model, in USD per million tokens. Counsel runs on NVIDIA's free
-- tier, so what it actually pays today is $0; these are assumed list prices
-- so spend can be compared and projected. Replace them with contract rates.
create table counsel.model_prices (
  model             text primary key,
  usd_per_mtok_in   numeric(10, 4) not null check (usd_per_mtok_in >= 0),
  usd_per_mtok_out  numeric(10, 4) not null check (usd_per_mtok_out >= 0),
  note              text not null
);

insert into counsel.model_prices values
  ('nvidia/nemotron-3-ultra-550b-a55b',     0.60, 1.80, 'assumed list price; free tier today'),
  ('nvidia/nemotron-3.5-lightning-30b-a3b', 0.10, 0.30, 'assumed list price; free tier today');

-- One row per model call, in dollars. Providers that return no usage get an
-- estimate (4 characters per token), flagged so the report can say so.
-- Calls that errored without usage were not billed and count as 0.
create view counsel.call_costs as
with priced as (
  select c.id, c.run_id, c.step_seq, c.provider, c.model, c.created_at, c.latency_ms,
         c.error, c.cached_from, c.request_hash,
         t.input_tokens, t.output_tokens,
         c.cached_from is null and c.prompt_tokens is null and c.error is null as estimated,
         p.model is null as unpriced,
         coalesce((t.input_tokens * p.usd_per_mtok_in + t.output_tokens * p.usd_per_mtok_out)
                  / 1000000.0, 0) as usd
  from counsel.model_calls c
  cross join lateral (
    select
      case when c.prompt_tokens is not null then c.prompt_tokens
           when c.error is not null or c.cached_from is not null then 0
           else ceil(octet_length(c.request ->> 'messages') / 4.0)::int end as input_tokens,
      case when c.output_tokens is not null then c.output_tokens
           when c.error is not null or c.cached_from is not null then 0
           else ceil(coalesce(octet_length(c.response ->> 'text'), 0) / 4.0)::int end as output_tokens
  ) t
  left join counsel.model_prices p on p.model = c.model
)
select p.*, coalesce(src.usd, 0) as saved_usd
from priced p
left join priced src on src.id = p.cached_from;

-- One row per run: what it cost, who it was for and what it produced.
-- requester is the customer for a ticket, the asker's team for a question,
-- and 'schedule' for scheduled work.
create view counsel.run_costs as
select r.id as run_id, r.agent, r.status, r.attempt, r.started_at, r.finished_at,
       coalesce('customer:' || tk.customer_id, 'team:' || pe.team_id, 'schedule') as requester,
       count(c.id) filter (where c.cached_from is null and c.error is null) as model_calls,
       count(c.id) filter (where c.cached_from is not null) as cache_hits,
       count(c.id) filter (where c.error is not null) as model_errors,
       coalesce(sum(c.input_tokens), 0) as input_tokens,
       coalesce(sum(c.output_tokens), 0) as output_tokens,
       coalesce(sum(c.usd), 0) as usd,
       coalesce(sum(c.saved_usd), 0) as saved_usd,
       coalesce(sum(c.latency_ms), 0) as model_ms,
       bool_or(c.estimated) as estimated
from counsel.runs r
join counsel.jobs j on j.id = r.job_id
left join counsel.questions q on q.id = (j.payload ->> 'question_id')::uuid
left join work.people pe on pe.id = q.asker_id
left join support.tickets tk on tk.id = (j.payload ->> 'ticket_id')::uuid
left join counsel.call_costs c on c.run_id = r.id
group by r.id, tk.customer_id, pe.team_id;
