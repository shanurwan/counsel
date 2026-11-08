# What the runtime guarantees

Precisely, and where each guarantee stops. Every line names the test that holds it.

## Steps

- **A step's database write and its journal entry commit together or not at all.**
  They share one transaction: the agent's role is set, the write runs, the journal row
  is inserted, then it commits. A worker killed inside a step leaves neither behind.
  `test_guarantees.py::test_a_worker_killed_mid_step_loses_that_step_and_the_next_worker_finishes_once`
  kills a real worker process with SIGKILL inside a step to show it.
- **A journaled step is never run again.** A resumed run replays finished steps from
  the journal and runs only what is left (same test, and
  `test_runtime.py::test_killed_run_resumes_without_repeating_the_model_call_or_the_write`).
- **So database effects happen exactly once, and code runs at least once.** A step's
  code can run again after a crash, because the crash undid its transaction. Anything a
  step does outside Postgres would be repeated, which is why no step does: messages,
  GitHub notes and customer replies are written as rows a person publishes.
- **Only the lease holder writes.** Each step renews the job's lease in its own
  transaction and is refused if the lease expired or another worker took the job.
  Closing a run and scheduling a retry are refused the same way. A worker that loses its
  lease stops and writes nothing more
  (`test_red_team.py::test_after_a_takeover_only_the_new_holder_writes_and_closes_the_run`).

## Model calls

- **A model call is recorded before it is made.** An intent row goes in first. If the
  worker dies while waiting, the next attempt records that call as lost, so its cost
  is visible, then calls again
  (`test_red_team.py::test_a_call_lost_mid_flight_is_recorded_as_lost_before_it_is_retried`).
- **An answer recorded but not journaled is reused**, not bought again
  (`test_red_team.py::test_an_answer_recorded_but_not_journaled_is_reused_not_bought_again`).
- **Not covered:** a worker that dies in the milliseconds between the provider
  answering and the answer being recorded pays twice. The intent records it as lost.
  Closing that window needs idempotency keys from the provider.
- **One call ends inside the lease.** A call, with every retry and fallback model, has a
  60 s budget; two providers fit inside the 150 s lease
  (`test_red_team.py::test_one_call_cannot_outlast_its_budget_whatever_the_retries`).

## Jobs

- **At least once.** A job is leased, not taken: if its worker dies, the lease expires
  after 150 s and the next tick resumes the same run. After three attempts it becomes a
  dead letter, and its run's writes are rolled back
  (`test_runtime.py::test_failures_back_off_then_dead_letter_with_an_audit_record`).
- **Once per event and per schedule slot.** Each job has an idempotency key (the
  question, ticket, standup or schedule fire time), so a trigger or tick that repeats
  enqueues nothing new
  (`test_runtime.py::test_schedules_enqueue_once_per_fire_time_and_only_implemented_agents`).
- **A refused write is never retried.** Postgres refusing an agent ends the run, records
  the refusal and rolls the run back
  (`test_runtime.py::test_a_denied_write_is_recorded_and_never_retried`).

## Waiting for a person

A run never waits. Work that needs a person ends as a draft or a proposal, and the
person's decision starts a new job (a carry, a publish). No worker or instance is held
while someone decides, and an approval can come hours later
(`test_standup.py::test_approval_carries_the_exact_words_credited_to_the_person`).

## Deploys

- **Migrations only add**, because CI migrates before it deploys and a run on the old
  code meets the new schema
  (`test_red_team.py::test_migrations_only_add_so_a_run_on_the_old_code_survives_a_deploy`).
- **An instance being replaced takes no new jobs** after SIGTERM. A run cut off resumes
  from its journal (`test_red_team.py::test_an_instance_shutting_down_takes_no_new_jobs`).
- **Code changes and runs in flight.** There is no workflow versioning. A run resumed on
  new code replays its journaled steps by position, so a change that adds, removes or
  reorders steps in an agent should ship when that agent has no run in flight. DBOS and
  Temporal handle this properly ([ADR-003](adr/003-build-the-runtime.md)).
