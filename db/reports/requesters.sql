-- Who the spend was for: the customer behind a ticket, the asker's team behind
-- a question. A requester with an outsized share points at a product or docs gap.
select agent, requester, count(*) as runs, sum(usd)::float as usd,
       (sum(usd) / nullif(sum(sum(usd)) over (partition by agent), 0))::float as share
from counsel.run_costs
where started_at >= %(since)s and started_at < %(until)s
group by agent, requester
order by agent, usd desc;
