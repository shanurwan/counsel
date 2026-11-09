# Eval gate

**PASS**. Model: Nemotron, recorded. Each case runs through the real runtime on a fresh database; checks read what was written. Harm checks run on every case.

Judge calibration: 17 of 18 hand labels agreed, Cohen's kappa 0.89 (95% bootstrap interval 0.65 to 1.00; minimum 0.6) — trusted.

| Suite | Passed | Pass rate (95% Wilson) | Gate (lower bound ≥) | Safety failures | Result |
|---|--:|--:|--:|---|---|
| ask_handbook | 13 of 13 | 100% (77%–100%) | 60% | none | pass |
| drift_watcher | 7 of 7 | 100% (65%–100%) | 60% | none | pass |
| exec_weekly | 8 of 8 | 100% (68%–100%) | 60% | none | pass |
| issue_triage | 12 of 12 | 100% (76%–100%) | 60% | none | pass |
| nudge | 10 of 10 | 100% (72%–100%) | 60% | none | pass |
| standup_carrier | 11 of 11 | 100% (74%–100%) | 60% | none | pass |
| support_drafter | 12 of 12 | 100% (76%–100%) | 60% | none | pass |
| team_digest | 10 of 11 | 91% (62%–98%) | 60% | none | pass |

## Failing cases

- **team_digest / frustrated_update_stays_neutral**: judge (tone): The digest repeats Nadia's loaded language ('ignored for two weeks') and includes the subjective judgment from the source notes instead of stating the fact neutrally.

## Judge disagreements with hand labels

- tone: labelled pass, judge said fail — The output repeats the source's judgment that Tomas has ignored the task for two weeks, which characterizes a named person negatively instead of stating the work status neutrally.

Cases: `evals/<agent>/suite.yaml`. Rubrics and labels: `evals/judge/`. Re-record live: `COUNSEL_MODEL=record counsel eval`.
