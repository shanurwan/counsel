-- Tool loops: every turn after the first re-sends the whole conversation
-- (system prompt, documents, ticket) plus the tool result. A turn is a call
-- whose request carries more than the system and user messages.
select r.agent,
       count(distinct c.run_id) filter (where t.turns > 2) as looped_runs,
       count(distinct c.run_id) as runs_with_calls,
       count(*) filter (where t.turns > 2) as later_turns,
       coalesce(sum(c.usd) filter (where t.turns > 2), 0)::float as later_turn_usd,
       coalesce(sum(c.usd), 0)::float as usd,
       coalesce(avg(c.input_tokens) filter (where t.turns <= 2), 0)::int as first_turn_input_tokens,
       coalesce(avg(c.input_tokens) filter (where t.turns > 2), 0)::int as later_turn_input_tokens
from counsel.call_costs c
join counsel.model_calls m on m.id = c.id
join counsel.runs r on r.id = c.run_id
cross join lateral (select jsonb_array_length(m.request -> 'messages') as turns) t
where c.cached_from is null
  and c.created_at >= %(since)s and c.created_at < %(until)s
group by r.agent
having count(*) filter (where t.turns > 2) > 0
order by later_turn_usd desc, r.agent;
