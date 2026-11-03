-- Calls the report had to estimate (no usage from the provider) or could not
-- price (model missing from counsel.model_prices).
select model, provider,
       count(*) filter (where estimated) as estimated_calls,
       count(*) filter (where unpriced and cached_from is null) as unpriced_calls
from counsel.call_costs
where created_at >= %(since)s and created_at < %(until)s
group by model, provider
having count(*) filter (where estimated) > 0
    or count(*) filter (where unpriced and cached_from is null) > 0
order by model;
