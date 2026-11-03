# Cost benchmark

One synthetic month (30 days, 80 employees, 24 customers, 1,846 runs) played twice through the real runtime: as shipped, then after acting on the report's top cost finding (`cache_hours: 168` on ask_handbook). Same events, same seed, same simulated model.

| | Before | After | Change |
|---|--:|--:|--:|
| Total spend | $0.4426 | $0.3280 | -26% |
| ask_handbook spend | $0.1561 | $0.0414 | -73% |
| Paid model calls | 1,245 | 711 | -43% |
| Answers served from cache | 0 | 534 | |
| Answers that changed | | 0 of 798 | |
| Hours of work absorbed (owner estimates) | 146.4 | 146.4 | |
| Spend per hour absorbed | $0.0030 | $0.0022 | |

What drives the saving is how often people ask the exact same thing. In this workload 65% of questions are copied from a list of common ones and the rest reworded from a few templates, so 88% of handbook calls repeated an earlier request. A real org's rate will differ: run `counsel report` on real traffic first. The cache key is the whole prompt, so a reworded question or an edited handbook page is a new request and is never served stale.

Reports: [before](before/REPORT.md) · [after](after/REPORT.md). Reproduce: `counsel simulate --admin-url <postgres superuser url>`.
