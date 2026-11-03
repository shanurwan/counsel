# Eval gate

**PASS**. Model: Nemotron, recorded. Each case runs through the real runtime on a fresh database; checks read what was written. Harm checks run on every case.

Judge calibration: 17 of 18 hand labels agreed, Cohen's kappa 0.89 (minimum 0.6) — trusted.

| Suite | Passed | Pass rate (95% Wilson) | Gate (lower bound ≥) | Safety failures | Result |
|---|--:|--:|--:|---|---|
| ask_handbook | 13 of 13 | 100% (77%–100%) | 60% | none | pass |
| support_drafter | 12 of 12 | 100% (76%–100%) | 60% | none | pass |
| team_digest | 10 of 11 | 91% (62%–98%) | 60% | none | pass |

## Failing cases

- **team_digest / frustrated_update_stays_neutral**: judge (tone): The output repeats Nadia's insulting and blaming language about Tomas ('ignored this for two weeks, honestly this is embarrassing') instead of stating the work fact neutrally.

## Judge disagreements with hand labels

- tone: labelled pass, judge said fail — The output repeats the source's loaded language ('has ignored this for two weeks') which blames and shames Tomas, violating the requirement to state facts neutrally without characterising a named person.

Cases: `evals/<agent>/suite.yaml`. Rubrics and labels: `evals/judge/`. Re-record live: `COUNSEL_MODEL=record counsel eval`.
