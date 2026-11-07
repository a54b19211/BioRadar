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
- **The radar's corner of a shared Redis** (`bioradar/jobs.py`). The queue name must be `bioradar` or start with
  `bioradar-` (one per environment on a shared Redis, e.g. `bioradar-staging`); jobs travel as JSON, so a pickled
  payload of another application is refused at read time and never unpickled; and a job whose function is not on
  the radar's list (`JOB_FUNCTIONS`) fails with `ForeignJob` without running it or its callbacks. Checked on a
  throwaway Redis by hand (a pickled `os.getcwd` job on the radar's queue fails with DeserializationError; the
  radar's own job is picked up); the unit tests cover the job class and the settings without a Redis.
- **The deadline counts as well as the token.** `lease.renew` is one UPDATE that extends the lease only while
  the token is the row's and `lease_until` has not passed; `Run.check_lease`, `Run.writing` and the final write
  use it, so a lease that ran out is not revived by a look-then-write gap and a worker whose lease ran out
  sends and writes nothing even if nobody took the analysis over.
- **A late answer is audit, not a business write.** A request registered by `check_lease` before the lease was
  lost still has its answer, usage and raw text recorded (`llm_calls.lease_token`, `llm_outputs`), so the
  successor's budget includes it. A call that no send was registered for is refused. When the lease goes
  between the first request and a format retry, the guard turns it into `SendBlocked`, the part already sent is
  returned and settled, and the run stops before the retry.
- **An expired lease is a question for RQ.** `recover` re-queues a row whose job is not in Redis; leaves one
  whose job is merely waiting; re-queues one whose job is started but whose worker is gone (key expired or
  heartbeat older than the worker TTL) with `recovered_from_unknown` in the notes; leaves one whose worker still
  beats and notes `stale_lease_running_worker` once. When Redis cannot be asked the row is left as it is.
- **A round stops when the lock connection drops.** `bioradar/guard.py` carries a per-round check that
  `ops.monitor_round` sets to `Leader.assert_held`; it runs before every database session and before its
  commit, and before every model, BioCheck, MechEvidence and Fabric request and queue entry. A half-open
  connection that the server already dropped but the client has not noticed is caught only when the next check
  times out.
- **The monitor settles what it already sent.** Triage and continuation reading call `db.settle_call` after
  every model call: the call, its answer and usage (`llm_calls`, `llm_outputs`) and the final state of its
  outbound records (`material_reads`: sent / failed, or not_sent for a call stopped before its first request)
  are written inside `guard.audit()`, which suspends the leader check for that append and nothing else. The
  client returns a revoked round as a blocked send, so what was already sent comes back to be settled before a
  retry; `guard.check()` right after raises `Revoked` and the answer is not used - observations, topics,
  candidates and window progress stay behind the guard.
- Who uses what:
  - analyses: RQ delivers; `analyses.execute` takes the row lock and returns when a lease is live or the
    analysis is finished; `analyses.recover` (monitor loop) re-queues rows whose lease expired or whose job
    never reached Redis. It locks the rows it looks at (`FOR UPDATE SKIP LOCKED`) and sends the job before it
    commits, so two recovers at the same moment re-queue a row once.
  - measurement check tasks and read windows: no RQ; the monitor polls and claims them with a token.
- The guarantees are tested on a real PostgreSQL (`make test-pg`, `tests_pg/`); SQLite cannot show row locks.
