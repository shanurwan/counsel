-- Spend per agent: runs, tokens, dollars, and what a typical and a bad run cost.
select agent,
       count(*) as runs,
       count(*) filter (where status = 'succeeded') as succeeded,
       sum(model_calls)::int as model_calls,
       sum(cache_hits)::int as cache_hits,
       sum(input_tokens)::bigint as input_tokens,
       sum(output_tokens)::bigint as output_tokens,
       sum(usd)::float as usd,
       sum(saved_usd)::float as saved_usd,
       percentile_cont(0.5) within group (order by usd)::float as p50_run_usd,
       percentile_cont(0.95) within group (order by usd)::float as p95_run_usd,
       percentile_cont(0.95) within group (order by model_ms)::int as p95_model_ms,
       coalesce(bool_or(estimated), false) as estimated
from counsel.run_costs
where started_at >= %(since)s and started_at < %(until)s
group by agent
order by usd desc, agent;
