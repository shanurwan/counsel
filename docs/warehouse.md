# Run logging in BigQuery

After each tick, Cloud Run loads every newly finished run into
`counsel_runs.runs`, one row per run. The row holds the metadata, the cost, and the
whole journal as JSON, so a run can be reconstructed from that row alone. Code:
[`src/counsel/warehouse.py`](../src/counsel/warehouse.py).

Postgres stays the system of record and keeps the agents running. If BigQuery is
down the agents carry on and the cursor waits.

## Staying free

| Choice | Why |
|---|---|
| Batch load jobs, never streaming inserts | Load jobs are free; streaming is billed per byte |
| Partitioned by day, with 90-day expiry | Storage stays far below the 10 GiB allowance |
| Counsel itself runs no queries | The 1 TiB of queries is left for people. Filter on `started_at` to scan only the partitions you need |
| The runtime's service account, from the metadata server | No key, so no extra Secret Manager version |

## Exactly once

A tick can die at any moment, so:

1. The job id comes from the batch's run ids and an attempt counter.
2. If the answer to the load was lost, the retry gets a 409 for that id, and the
   existing job is read instead of loading twice.
3. The cursor moves only after BigQuery reports the job done.
4. A job BigQuery ran and refused raises the attempt counter, because a finished job
   id cannot run again.

## Reconstructing a run

```sql
select agent, status, started_at, usd,
       json_query(journal, '$.steps') as steps,
       json_query(journal, '$.model_calls') as model_calls,
       json_query(journal, '$.audit') as audit
from counsel_runs.runs
where started_at >= timestamp('2026-11-01') and run_id = '...';
```

Spend per agent by day, reading no journal, so it scans very little:

```sql
select date(started_at) as day, agent, count(*) as runs, round(sum(usd), 4) as usd
from counsel_runs.runs
where started_at >= timestamp_sub(current_timestamp(), interval 28 day)
group by 1, 2 order by 1 desc, usd desc;
```

From a person's machine, `counsel warehouse --project <id>` loads everything still
waiting, as that person.
