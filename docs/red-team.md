# Red-teaming the runtime

Counsel's walls are built against the model: grants, row-level scope, a trigger
that refuses commitment writes. This is the same exercise pointed at the parts I
trust instead: the runtime, its time limits and its deploys. I read the code as
someone trying to break it, then read six days of production logs and the journal
against what I found. Two findings were already in the logs.

Each finding has a test that reproduces it in
[tests/test_red_team.py](../tests/test_red_team.py).

| | Finding | How it was found | Severity | Status |
|---|---|---|---|---|
| R1 | A worker kept writing after its lease expired | Production logs and journal, 4 and 5 Nov | High: two workers could write one run | Fixed |
| R2 | A model call could be paid for twice, and its real duration was hidden | Code review, confirmed by R1's timings | Medium: cost and blind spots | Fixed; a narrow window remains |
| R3 | The runtime login chooses which ticket an agent sees | Code review | Medium: a compromised runtime reads every ticket | Planned |
| R4 | Deploys and migrations while a run is in flight | Code review, two aborted ticks | Low | Fixed; the aborts were already handled |

## R1: a worker past its lease kept writing

A worker leases a job for 150 s, longer than Cloud Run's 120 s request timeout, so
a run killed with its request can be resumed by the next tick. Nothing checked that
a worker *still* held the lease when it committed.

It happened twice. On 4 and 5 Nov the 01:00 tick landed on a cold instance and the
digest's model call took about 150 s. Cloud Run ended the request at 120 s, but the
process carried on:

| | 4 Nov | 5 Nov |
|---|---|---|
| Leased | 01:00:10 | 01:00:28 |
| Lease expired | 01:02:40 | 01:02:58 |
| Request cut off (504) | 01:02:03 | 01:02:25 |
| Last two steps committed | 01:02:43, 01:02:44 | 01:03:05, 01:03:06 |

No other tick came before 01:30, so nothing was written twice. Had one come, two
workers would have run the same job, and only the journal's primary key stood
between them and a duplicate write.

**Fix** ([runtime.py](../src/counsel/runtime.py)). Every step's transaction renews the
lease as its first statement, and fails if this worker no longer holds an unexpired
lease. A run that keeps making progress keeps its lease; one that stalls past it
cannot commit, cannot close the run and cannot schedule a retry. It stops, writes an
audit record and ends its tick, leaving the job for whoever leases it next.

**Tests:** a step after the lease ran out is refused; a run of four 100 s steps keeps
its lease; after a takeover only the new holder writes and closes the run.

## R2: a model call could be paid twice, and the journal hid how long it took

`complete()` called the model, then recorded the answer in a separate transaction.
A worker that died in between left no trace, and the retry paid again.

R1's timings showed a second problem. Both runs spent about 150 s between their
last step and the model call, yet the journal recorded calls of 1.5 s and 4.8 s. Both
answers came from the fallback model, so the first model had failed, most likely by
timing out three times at 45 s each. Nothing recorded those attempts: only the answer
was kept, with the last attempt's latency. A single call had no overall limit: two
models, three attempts each, 45 s per attempt.

**Fix:**

- **An intent is written before the request goes out**, under the lease fence. An
  intent with no recorded call is a call that was lost; the next attempt records it
  as a failure, so it appears in the cost report instead of vanishing.
- **An answer that was recorded but not yet journaled is reused** on resume, not
  bought again. The call row is written outside the fence, so a call that was paid
  for is always in the cost log, even when the lease was lost.
- **Each call has a 60 s budget** across every model and retry, so even with a second
  provider it ends inside the lease.
- **The real wall time and every failed attempt are recorded** with the call.

**What remains:** a worker that dies in the milliseconds between the provider
answering and the call row committing still pays twice. The intent records it as
lost. Closing it needs idempotency keys from the provider, which these APIs don't
offer.

## R3: the runtime login chooses which ticket an agent sees

The support drafter reads one ticket: row-level security compares each row with
`counsel.ticket`, a setting the runtime takes from the job. The model never writes
SQL, so it cannot widen that. The runtime's own login can: it may switch to any
agent's role and set the scope to any ticket. A compromised runtime, or a bug that
passes the wrong id, reads every ticket, one at a time.

So the per-agent walls protect against the model, not against the runtime. What each
component can do if compromised:

| Component | If compromised, it can | Still bounded by |
|---|---|---|
| The model | Ask for the tool calls its agent offers | The agent's grants, row scope, the commitment trigger |
| The runtime login | Act as any agent, with any scope: every agent's rights together, and any ticket | No agent can write a commitment, read integration tokens, or edit the audit log; `counsel audit` checks all three on every push |
| The MCP login | Read the journal, ask a question, propose a note | Cannot approve a note ([per-action-consent.md](per-action-consent.md)) |
| The migration login | Everything | It is the owner. It lives in Secret Manager, read by CI's deploy job and by the owner |

**Plan:**

1. **Bind the scope to a running job.** RLS will show a ticket only while a leased
   job names it. A compromised runtime then has to forge job history to read a
   ticket, which leaves a trace. This changes the browser console, the incident
   bench and the eval harness, which set the scope directly, so it ships with them.
2. **Split the runtime by risk tier.** A tier-0 runtime that cannot become the
   support drafter needs a second Cloud Run service and login. It fits the free
   tier, but it is the larger change.

`test_the_runtime_login_can_still_choose_any_ticket_known_boundary` pins the
boundary as it stands, so the fix has to change it on purpose.

## R4: deploys and migrations while a run is in flight

CI migrates, then deploys. A run on the old revision meets the new schema. That is
safe only while migrations add, and nothing enforced it. Cloud Run also sends
SIGTERM before it replaces an instance, and nothing listened.

**Fix:**

- **Migrations must only add.** A test fails on a migration that drops, renames,
  truncates or changes a column's type, unless the line above says
  `-- destructive:` with the two-step plan.
- **On SIGTERM the instance takes no new jobs.** A run already going finishes or
  resumes from its journal once its lease expires; R1's fence makes that resume
  safe.

**Already handled:** on 3 and 6 Nov a tick was aborted while waiting for an instance
(one instance, scaled to zero). Scheduling looks back 35 minutes, so both digests ran
on the next tick, 30 minutes late. Nothing was lost.
