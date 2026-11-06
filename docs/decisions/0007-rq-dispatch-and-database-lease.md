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
- Who uses what:
  - analyses: RQ delivers; `analyses.execute` takes the row lock and returns when a lease is live or the
    analysis is finished; `analyses.recover` (monitor loop) re-queues rows whose lease expired or whose job
    never reached Redis.
  - measurement check tasks and read windows: no RQ; the monitor polls and claims them with a token.
- The guarantees are tested on a real PostgreSQL (`make test-pg`, `tests_pg/`); SQLite cannot show row locks.

## Known gaps (not changed here)

- The lease of an analysis has no token. If its worker outlives the lease and `recover` hands the analysis to
  another worker, the late worker's final write is not rejected. Check tasks and read windows are fenced.
- `analyses.recover` reads without a row lock. Two `recover` calls at the same moment dispatch the same
  crashed analysis twice (seen in a test on PostgreSQL); `execute` then keeps the second job from running it
  twice. The monitor runs one `recover` loop, so this needs a second monitor process to happen.
