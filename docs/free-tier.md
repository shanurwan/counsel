# Staying on the free tier

Counsel runs on Supabase Free, the GCP Free Tier and the NVIDIA API free tier.
"Free" is a design constraint here, not a hope: every paid dimension is either
inside a published allowance by construction, or stopped by the billing guard.

Allowances below are from Google's [Free Tier page](https://docs.cloud.google.com/free/docs/free-cloud-features)
and [Cloud Scheduler pricing](https://cloud.google.com/scheduler/pricing),
read on 2026-11-03. They are per billing account per month.

| Service | Allowance | Counsel's use | How it stays inside |
|---|---|---|---|
| Cloud Run | 2M requests, 180k vCPU-s, 360k GiB-s, 1 GB egress from North America | one tick every 30 min plus events | worst case below; `us-central1` so egress to Supabase and NVIDIA is covered |
| Cloud Scheduler | 3 jobs | 1 | one clock tick; agent schedules live in `agents/*.yaml` |
| Secret Manager | 6 active versions | 3 | `database-url`, `nvidia-api-key`, `migrate-database-url` |
| BigQuery | 10 GiB storage, 1 TiB queries | MBs | batch load jobs only, which are free; streaming inserts are billed and never used; partitions expire after 90 days |
| Artifact Registry | 0.5 GB | ~0.2 GB | cleanup keeps the latest 2 images per repository |
| Cloud Storage | 5 GB in US regions | KBs | Terraform state, guard source (deleted after 7 days) |
| Cloud Build | 2,500 build-minutes | a few per guard deploy | runtime images are built in GitHub Actions |
| Pub/Sub | 10 GiB | KBs | budget notifications only |
| Cloud Run functions | 2M invocations | a few per day | the billing guard |

## Cloud Run worst case

The runtime is capped so that even if every tick ran to its timeout, it would
stay inside the allowance:

```
ticks/month        = 48/day × 30            = 1,440
vCPU-seconds       = 1,440 × 120 s × 1 vCPU = 172,800   (free: 180,000)
GiB-seconds        = 1,440 × 120 s × 0.5    =  86,400   (free: 360,000)
```

Settings that make this hold: `max_instances = 1`, `timeout = 120s`,
`cpu = 1`, `memory = 512Mi`, request-based billing, invocation by the
scheduler's service account only.

The worst case holds as the platform grows, because it is set by the request timeout,
not by what a tick does. A tick now also polls GitHub and loads finished runs, and the
export is skipped once a tick has used 85 s.

Everything else added runs outside GCP or costs nothing there: the MCP server is a
local process and is left out of the Cloud Run image, GitHub reads are free, and
`nudge` and `drift_watcher` make no model calls. A second model provider is off
unless a key is set.

## If something still costs money

- The budget is RM 4 (about USD 1). Billing admins are emailed at 1% (4 sen),
  50% and 100%.
- At 100%, the billing guard (`infra/billing_guard`) unlinks billing from the
  project, which stops every paid service. It fails closed: if it cannot read
  the billing state, it unlinks anyway.
- Budget data lags usage by a few hours, so the true ceiling is RM 4 plus a few
  hours of spend.
- The stop path was tested on 2026-11-03 in dry-run mode with a synthetic
  RM 5 notification ("dry run: would disable billing"), then switched live.
