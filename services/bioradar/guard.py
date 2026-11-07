"""A check that runs before anything leaves the process or is written, set by the caller for the time of one
piece of work. The monitor sets it for a round (ops.monitor_round): while the process is no longer the leader
(research/leader.py - its lock connection dropped or the lock is gone), the check raises, so a round that is
still running stops sending requests and writing business data at the next one. With no check set (workers,
the API, tests) nothing happens."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[Callable[[], None] | None] = ContextVar("bioradar_guard", default=None)


class Revoked(Exception):
    """The work this process was doing is no longer its own."""


def check():
    """Raise Revoked when the current piece of work may no longer send or write."""
    fn = _current.get()
    if fn is not None:
        fn()


@contextmanager
def holding(fn: Callable[[], None]):
    token = _current.set(fn)
    try:
        yield
    finally:
        _current.reset(token)
