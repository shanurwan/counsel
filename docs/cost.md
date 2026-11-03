# Cost and tracing

Every model call an agent makes is a row with its tokens, latency, model and
run. `counsel report` prices those rows, attributes them to the agent, the run
and the person or customer it was for, and writes a short list of findings.
Each finding has evidence, an owner, an action and what it is worth per month.
The aim is a report someone acts on, not a dashboard someone looks at.

## How it works

1. **Recorded at the source.** `RunContext.complete` writes every call to
   `counsel.model_calls`, in the same transaction as the journal step. That
   includes calls that failed after the provider billed them: a truncated
   answer is recorded with its usage even though it is never used.
2. **Priced in the database.** `db/migrations/0005_cost.sql` adds
   `counsel.model_prices` and two views. `counsel.call_costs` is dollars per
   call. `counsel.run_costs` is dollars per run, plus `requester`: the customer
   behind a ticket, or the asker's team behind a question.
3. **Asked as plain SQL.** Each question an owner would ask is one file in
   `db/reports/`, such as repeats, tool loops, billed errors, refusals,
   outliers and requesters. Anyone can run one with `psql`.
4. **Turned into findings.** `src/counsel/cost.py` ranks the findings:
   security first, then budget overruns, then savings by dollars per month.
5. **Traced.** `counsel trace <run>` exports a run as OpenTelemetry spans
   (OTLP/JSON, GenAI semantic conventions), so it opens in any tracing
   backend. `counsel replay <run>` still prints the whole story in the
   terminal.

6. **Watched.** A Grafana dashboard and four alert rules read the same views
   (see [Dashboard and alerts](#dashboard-and-alerts)).

Agents cannot read any of it. No cost table or view is granted to an agent,
the orchestrator or the runtime login (`tests/test_cost.py`).

## Dashboard and alerts

[`infra/grafana/`](../infra/grafana) is a separate Terraform root for Grafana
Cloud's free tier. It holds:

- **A data source.** Grafana connects to Supabase as `counsel_reader`
  ([`db/migrations/0006_reader.sql`](../db/migrations/0006_reader.sql)). That
  login has SELECT on `call_costs`, `run_costs` and `model_prices` and nothing
  else. It is read-only, has a 10-second statement timeout and at most 3
  connections. It sees dollars, tokens and run metadata, never a prompt, a
  ticket, a task or a secret. `counsel audit` fails if it can reach anything
  more.
- **A dashboard** ([`dashboard.json`](../infra/grafana/dashboard.json)) with:
  - spend, projected month, cache savings and refused runs;
  - spend per day by agent;
  - paid calls against cache hits;
  - tokens per day;
  - the most expensive runs, each with its `counsel replay` command;
  - billed-but-unused calls;
  - spend by requester.
- **Alert rules**, emailed to the owner:

  | Alert | Fires when | Severity |
  |---|---|---|
  | Agent run refused by Postgres | any refusal in the last tick | security |
  | Projected monthly spend over budget | the last 24 h × 30 > `monthly_budget_usd` | cost |
  | Billed model calls produced nothing | any truncated, billed call in 24 h | cost |
  | Runs failed every attempt | any dead letter in 24 h | reliability |

**Why Grafana reads Postgres instead of a metrics store.** Attribution per run
and per customer is relational and high-cardinality. As a metrics label,
`run_id` or `customer` would grow without bound. So the views stay the single
source of truth, and Grafana is a window on them. A metrics store (Prometheus,
VictoriaMetrics) earns its place once there is enough traffic for low-cardinality
aggregates such as latency, queue depth and tokens per minute. That is the
next step, not this one.

**Why not a self-hosted stack.** It would be one more server to patch, for one
engineer. Grafana Cloud free runs nothing of ours.

Setup, once:

```sh
counsel migrate                                            # creates counsel_reader
psql "$COUNSEL_DATABASE_URL" -c "alter role counsel_reader password '<generated>'"
counsel audit                                              # the reader reaches only the cost views
cp infra/grafana/terraform.tfvars.example infra/grafana/terraform.tfvars   # fill in; never committed
terraform -chdir=infra/grafana init && terraform -chdir=infra/grafana apply
```

## Configuration

An owner sets these in the agent's YAML, in a PR they approve:

```yaml
budget_usd: 20      # monthly; the report flags a projected overrun to the owner
cache_hours: 168    # reuse the answer to an identical request; T0 agents only
```

**Why the cache is T0 only.** A T0 prompt holds the question and handbook
text, never another person's tickets or tasks. The registry rejects
`cache_hours` on any higher tier.

**Why an answer is never stale.** The cache key is the hash of the whole
prompt: system text, documents and question. An edited handbook page is a new
request.

Prices live in `counsel.model_prices`. Counsel runs on NVIDIA's free tier, so
its actual spend is $0. The defaults are assumed list prices so spend can be
compared and projected; replace them with contract rates.

## Commands

```bash
counsel report [--since 2026-10-01] [--until 2026-11-01] [--out cost/]
counsel trace <run-id> > run.otlp.json
counsel simulate --admin-url postgresql://postgres@localhost:5432/postgres
```

## Synthetic users

There are no real users yet, so `counsel simulate` makes a month of them at
Harbor Labs:

- 80 employees asking the handbook agent questions
- 24 customers opening support tickets (about 1 in 45 tickets carries
  instructions aimed at an AI)
- task updates feeding the hourly digest schedule

Every run goes through the real worker, the real Postgres roles, the journal
and the cost log.

Only the model is simulated. `SimulatedModel` answers deterministically from
the request hash and reports token usage from the text it was sent and wrote.
Its behaviour is fixed:

- about 1 in 18 digest prompts is cut off at `max_tokens`
- about 4 in 10 support runs read the ticket thread before drafting
- a ticket that asks for the integration tokens is always obeyed

Because the same month replayed twice gives the same answers, the effect of a
change can be measured exactly. The workload assumptions are constants at the
top of `src/counsel/simulate.py`.

## The report

From the synthetic October, `cost/before/REPORT.md` ranks these six findings
(abridged):

| # | Finding | Owner | Action |
|---|---|---|---|
| 1 | 6 runs asked for `support.integration_tokens` and Postgres refused | security | Nothing exposed, nothing to rotate. Replay one and tell the account owner. |
| 2 | ask_handbook pays for the same answer again (88% of paid calls repeat) | people-ops | Add `cache_hours: 168` |
| 3 | support_drafter makes a second model call in 40% of runs | support | Put the ticket thread in the first prompt |
| 4 | team_digest: 12 billed calls cut off at `max_tokens=400` and retried unchanged | platform | Raise `max_tokens`; don't retry a truncation as-is |
| 5 | team_digest resends identical activity on quiet days | platform | Skip the run when nothing changed |
| 6 | One customer drives 65% of support_drafter spend | support | Look for the docs gap behind their tickets |

## Evals

`tests/test_cost.py` has 31 tests against a real Postgres. They cover:

- **Pricing and attribution:** each call is priced and attributed to the
  asker's team.
- **Estimates:** a provider that reports no usage gets an estimate and a
  data-quality finding.
- **Truncation:** a truncated answer is billed but never written.
- **Access:** agents cannot read cost data.
- **The cache:**
  - an identical question is served from cache, with the same answer and the
    saving recorded;
  - an edited document misses the cache;
  - the cache expires;
  - with no `cache_hours`, every call is paid.
- **The report:**
  - it finds repeats and refusals, and names the action;
  - a projected budget overrun is flagged to the owner.
- **Tracing:** a run exports as OTel spans with GenAI attributes.
- **The dashboard login:** `counsel_reader` sees the three cost views and is
  refused on prompts, tickets, tasks, secrets and every write. The audit flags
  a widened grant.
- **Registry:** bad cost settings are rejected.
- **The simulator:** it is deterministic.

## Benchmarks

One synthetic month (30 days, 1,846 runs) is played twice through the real
runtime:

- **Before:** as shipped.
- **After:** after acting on finding 2, `cache_hours: 168` on ask_handbook.

Both arms use the same events, the same seed and the same simulated model.

| | Before | After | Change |
|---|--:|--:|--:|
| Total spend | $0.3832 | $0.2686 | −30% |
| ask_handbook spend | $0.1561 | $0.0414 | −73% |
| Paid model calls | 1,266 | 732 | −42% |
| Answers served from cache | 0 | 534 | |
| Answers that changed | | 0 of 798 | |

Full output: `cost/RESULTS.md`, `cost/results.json`, and the two reports.

## Known limitations

- **The dollars are small.** The handbook has five short pages, and the
  prices are assumed. Read the percentages and tokens, not the dollars.
  Per-1k-runs cost is in the report for scaling.
- **The saving depends on the workload.** It comes from how often people ask
  the exact same thing (88% of handbook calls here). Run `counsel report` on
  real traffic before expecting the same.
- **Simulated token counts.** They are estimated from text length (4
  characters a token), not a real tokenizer. Live Nemotron calls record the
  provider's real usage.
- **Exact-match cache.** It is keyed on the exact request, so a reworded
  question misses it. A semantic cache would catch more, but it could serve an
  answer to a different question, so it is not used.
- **Some findings are upper bounds.** The tool-loop saving assumes every
  second turn could be avoided.
