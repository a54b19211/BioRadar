"""The one lease mechanism behind check tasks, continuation windows and analyses."""

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


def test_a_failure_counts_one_attempt_backs_off_and_ends_final():
    steps = (60, 300)
    r = row(lease_token="t", lease_until=NOW)
    rule = {"due": "next_at", "max_attempts": 3, "retry_state": "failed", "final_state": "failed_final"}
    for attempt in (1, 2, 3):
        lease.fail_with_backoff(r, NOW, delay=lease.backoff(r.attempts, steps), **rule)
        assert (r.attempts, r.lease_token, r.lease_until) == (attempt, None, None)
    assert r.state == "failed_final" and r.next_at == NOW + timedelta(seconds=300)  # the last step repeats
    done = row(lease_token="t", lease_until=NOW)
    lease.finish(done, "done", NOW, due="next_at", delay=timedelta(days=1))
    assert (done.state, done.attempts, done.lease_token, done.next_at) == (
        "done",
        0,
        None,
        NOW + timedelta(days=1),
    )
