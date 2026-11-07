"""The one lease mechanism behind the analyses."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bioradar.research import lease

NOW = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


def row(**fields):
    return SimpleNamespace(
        **{"lease_token": None, "lease_until": None, "attempts": 0, "state": "queued"} | fields
    )


def test_only_the_holder_of_a_lease_that_has_not_run_out_holds_it():
    r = row()
    lease.take([r], "t1", NOW, timedelta(minutes=2))
    assert lease.held(r, "t1", NOW + timedelta(minutes=2))
    assert not lease.held(r, "t1", NOW + timedelta(minutes=2, seconds=1))  # ran out: another worker's now
    assert not lease.held(r, "t2", NOW)
    naive = row(lease_token="t1", lease_until=(NOW + timedelta(minutes=1)).replace(tzinfo=None))  # SQLite
    assert lease.held(naive, "t1", NOW) and lease.running(naive, NOW)
    assert not lease.held(None, "t1", NOW) and not lease.running(row(), NOW)
