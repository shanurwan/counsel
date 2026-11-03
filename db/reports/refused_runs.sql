-- Runs Postgres refused, with what they tried. Not a cost problem: each one
-- is a model that asked for something its role does not have.
select rc.agent, rc.run_id::text, rc.requester, rc.started_at, rc.usd::float, r.error
from counsel.run_costs rc
join counsel.runs r on r.id = rc.run_id
where rc.status = 'denied'
  and rc.started_at >= %(since)s and rc.started_at < %(until)s
order by rc.started_at;
