# How Counsel contacts people

Every notification spends a limited amount of trust. These rules are enforced in
this order, and `nudge` is the only agent that can send anything.

| Rule | Enforced by | If it were broken |
|---|---|---|
| A person gets at most their budget of messages in any 7 days (default 2; 0 opts out) | A Postgres trigger on the outbox: `CN004`, audited, never retried | No code path can exceed it, including a hijacked agent |
| An agent cannot raise a budget or read message text | Column grants, checked by the audit | |
| Only owned, unfinished work that is blocked or due within a week | The agent's query | Most quiet work needs no ping |
| Silent for at least 3 days | The agent's query | A person who just wrote a note hears nothing |
| One task at most once a week | The agent's query, against what was sent | |
| One message per person, bundling up to 5 tasks | The agent's loop | No stream of pings |
| 10:00 to 16:00 on a weekday, in the person's own time zone | The agent's query | Not first thing, and not as they try to finish |

Only the budget is a wall. The rest are the agent's judgement, tested by the suite
in `evals/nudge`, because getting them wrong annoys someone but cannot exceed what
the person allowed.

## The message

```
Where these stand, in case it saves you looking:
- Fix flaky checkout test: blocked, due 2026-11-04; last note 5 days ago, from dev.
- Ship rate limiter: in progress, due 2026-11-08; no notes yet.
If anything has moved, post a standup and you can approve the note before it goes on the task. No reply needed otherwise.
```

It gives something useful before asking for anything, and the ask is optional.

**No model writes it.** The text needs no judgement, and a wrong message spends
trust that is hard to earn back. Code cannot invent progress, repeat a complaint
from a note, or read as an accusation, and it costs nothing. That is why a note's
text never appears, only who wrote it and when.

## Delivery

Messages go to `counsel.outbound_messages`, and `counsel inbox <handle>` reads them.
Counsel has no chat integration yet. A Slack or email sender would read the outbox
and needs no new privileges for agents.
