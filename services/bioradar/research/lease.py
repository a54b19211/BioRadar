"""Leased work rows: ownership, fencing and recovery of the radar's analyses. Delivery is not done here: an
analysis reaches a worker as an RQ job (docs/decisions/0007).

A worker claims a row by writing a fresh token and an expiry (lease_token, lease_until) under a row lock. Only
the holder of a lease that has not run out may settle the row afterwards, so a worker that took longer than
its lease never overwrites what the worker that took the row over did."""

import uuid
from datetime import timedelta

from sqlalchemy import update

from bioradar.research.clock import utc

ANALYSIS_LEASE = timedelta(minutes=90)  # an analysis's lease; also the RQ job timeout (analyses.dispatch)


def new_token() -> str:
    return uuid.uuid4().hex


def take(rows, token, now, lease: timedelta):
    for row in rows:
        row.lease_token, row.lease_until = token, now + lease


def held(row, token, now) -> bool:
    return (
        row is not None and row.lease_token == token and bool(row.lease_until) and utc(row.lease_until) >= now
    )


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
