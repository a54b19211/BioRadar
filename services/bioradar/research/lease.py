"""Leased work rows: ownership, fencing and recovery for the radar's database work (measurement check tasks,
continuation windows, analyses). Delivery is not done here: an analysis reaches a worker as an RQ job, check
tasks and read windows are polled by the monitor (docs/decisions/0007).

A worker claims a due row inside SELECT ... FOR UPDATE SKIP LOCKED by writing a fresh token and an expiry
(lease_token, lease_until). Only the holder of a lease that has not run out may settle the row afterwards,
so a worker that took longer than its lease never overwrites what the worker that took the row over did. A
failure counts one attempt: the row is due again after a backoff, and after the last attempt it is left in a
final state for review instead of being retried for ever."""

import uuid
from datetime import timedelta

from sqlalchemy import or_, update

from bioradar.research.clock import utc

ANALYSIS_LEASE = timedelta(minutes=90)  # an analysis's lease; also the RQ job timeout (analyses.dispatch)


def new_token() -> str:
    return uuid.uuid4().hex


def claimable(model, now):
    """Where-clause: the row is not leased, or its lease has run out."""
    return or_(model.lease_until.is_(None), model.lease_until < now)


def take(rows, token, now, lease: timedelta):
    for row in rows:
        row.lease_token, row.lease_until = token, now + lease


def held(row, token, now) -> bool:
    return (
        row is not None and row.lease_token == token and bool(row.lease_until) and utc(row.lease_until) >= now
    )


def own(session, model, key, token, now):
    """The row, locked, while this token's lease on it holds; None once another worker may have it."""
    row = session.get(model, key, with_for_update=True)
    return row if held(row, token, now) else None


class Lost(Exception):
    """The worker no longer holds the row it was working on: another worker took it over."""


def renew(session, model, key, token, now, span: timedelta = ANALYSIS_LEASE) -> bool:
    """Extend the lease `token` holds on the row to now + span; False when the token is not the row's (taken
    over, released) or its lease has run out. One UPDATE carries both conditions, so there is no gap between
    looking and writing in which a lease that expired could be revived; the row stays locked until the
    commit, so a claimant waits for the write this renewal belongs to."""
    result = session.execute(
        update(model)
        .where(model.id == key, model.lease_token == token, model.lease_until >= now)
        .values(lease_until=now + span)
    )
    return result.rowcount == 1


def running(row, now) -> bool:
    """Someone holds the row until lease_until (the token says who)."""
    return bool(row.lease_until) and utc(row.lease_until) > now


def release(row):
    row.lease_token = row.lease_until = None


def backoff(attempts: int, steps) -> timedelta:
    """The wait before the next attempt: steps[attempts], the last step once they are used up (seconds)."""
    return timedelta(seconds=steps[min(attempts, len(steps) - 1)])


def finish(row, state, now, *, due: str, delay: timedelta):
    """Settle a leased row: released, in `state`, due again after `delay` (field `due`)."""
    release(row)
    row.state = state
    setattr(row, due, now + delay)


def fail(row, now, *, due: str, delay: timedelta, max_attempts: int, retry_state: str, final_state: str):
    """Count one failed attempt: due again after `delay` in retry_state, or final_state after the last one."""
    row.attempts += 1
    row.state = final_state if row.attempts >= max_attempts else retry_state
    setattr(row, due, now + delay)


def fail_with_backoff(row, now, **rule):
    """Release the lease and count the failed attempt (fail)."""
    release(row)
    fail(row, now, **rule)
