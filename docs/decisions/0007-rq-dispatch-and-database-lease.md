# 0007 RQ dispatches, the database lease owns

- Status: accepted (2026-11-07)

## Context

The radar has two mechanisms that look like queues. `research/lease.py` called itself "the one queue mechanism
of the radar", while analyses are handed to workers as RQ jobs (`analyses.dispatch`). Both are needed and they
answer different questions, so the wording was wrong, not the design.

## Decision

- **RQ (Redis) is delivery and wake-up.** It carries an analysis id to a worker. Losing a job, restarting Redis
  or enqueueing twice must never lose or double a result, because nothing authoritative lives in Redis.
- **The database lease is ownership, idempotency, fencing and recovery.** A worker claims a due row with
  `SELECT … FOR UPDATE SKIP LOCKED` and writes a fresh token and an expiry. Only the holder of an unexpired
  lease may settle the row (`lease.own` checks the token under the row lock), so a worker that ran past its
  lease writes nothing over the one that took the row over. A worker that vanished leaves an expired lease,
  and the next claim takes the row over once. A failure counts one attempt, backs off and ends in a final
  state for review.
- **An analysis lease has a token too** (`analyses.lease_token`). `execute` writes a fresh token under the row
  lock; `recover` releases it. Every write that belongs to the analysis - progress, evidence, material_reads,
  llm_calls, topic watch events and the final result - goes through `Run.writing` / `lease.holds`, which
  locks the row (FOR KEY SHARE, or FOR UPDATE for the row itself) and finds it only while the token is still
  the worker's; every outbound request (model, BioCheck, MechEvidence) is preceded by the same check. A
  superseded worker gets `lease.Lost`, writes and sends nothing and logs the rejection. A worker that ran past
  its lease but was not replaced is not superseded: it may still finish.
- **Recover does not guess about a send.** An outbound record left `prepared` (db.MaterialRead) means the worker
  died around a send and the material may have gone out. Recover keeps the record as it is, writes
  `recovered_unknown_outcome` to the analysis's notes (count and stage) and sends the job again - the resumed
  run records its send as a new attempt. After `MAX_UNKNOWN_RECOVERIES` such crashes the analysis is not sent
  again: it ends `failed` / `recovery_unknown_outcome` for review.
- **One monitor at a time.** The monitor loop (`monitor.tick` and `analyses.recover`) runs only in the process
  that holds a PostgreSQL advisory lock on a dedicated connection (`research/leader.py`). The database drops the
  lock when that process exits or its connection breaks, so a crashed leader leaves nothing to clean up; the
  other monitor processes skip their rounds and try again at the next one.
- Who uses what:
  - analyses: RQ delivers; `analyses.execute` takes the row lock and returns when a lease is live or the
    analysis is finished; `analyses.recover` (monitor loop) re-queues rows whose lease expired or whose job
    never reached Redis. It locks the rows it looks at (`FOR UPDATE SKIP LOCKED`) and sends the job before it
    commits, so two recovers at the same moment re-queue a row once.
  - measurement check tasks and read windows: no RQ; the monitor polls and claims them with a token.
- The guarantees are tested on a real PostgreSQL (`make test-pg`, `tests_pg/`); SQLite cannot show row locks.
