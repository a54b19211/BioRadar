"""One monitor at a time: the advisory-lock leader (research/leader.py) and the monitor round that uses it,
on a real PostgreSQL."""

import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from bioradar import db, ops
from bioradar.research import analyses, monitor
from bioradar.research.leader import Leader

KEY = 0x7E57_0001  # a key of the tests, not the monitor's own


@pytest.fixture
def leaders():
    made = []

    def make(key=KEY):
        made.append(Leader(key))
        return made[-1]

    yield make
    for leader in made:
        leader.release()


def test_only_one_process_is_the_leader_until_it_lets_go(leaders):
    first, second = leaders(), leaders()
    assert first.acquire() is True
    assert second.acquire() is False
    assert first.acquire() is True  # the leader stays the leader round after round
    first.release()
    assert second.acquire() is True
    assert first.acquire() is False


def test_a_leader_whose_connection_dies_gives_the_lock_up_without_a_clean_shutdown(leaders):
    first, second = leaders(), leaders()
    assert first.acquire()
    pid = first._connection.execute(text("SELECT pg_backend_pid()")).scalar()
    with db.engine().connect() as admin:  # the process is killed: its session ends
        admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        admin.commit()
    deadline = time.monotonic() + 10  # the backend ends a moment after the signal
    while not second.acquire() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert second._connection is not None  # nothing was left behind to wait for
    assert first.acquire() is False  # and the old leader finds out, and does not act


def test_the_monitors_own_key_is_not_the_tests_key():
    from bioradar.research.leader import MONITOR_LOCK

    assert MONITOR_LOCK != KEY


def test_two_monitor_loops_do_not_both_tick_and_recover(monkeypatch, leaders):
    calls = {"tick": 0, "recover": 0}
    lock = threading.Lock()

    def count(name):
        def call(*args, **kwargs):
            with lock:
                calls[name] += 1
            return 0

        return call

    monkeypatch.setattr(monitor, "tick", count("tick"))
    monkeypatch.setattr(analyses, "recover", count("recover"))
    settings = SimpleNamespace(monitor_enabled=True)
    first, second = leaders(), leaders()
    barrier = threading.Barrier(2)

    def loop(leader, rounds=5):
        barrier.wait(10)
        return [ops.monitor_round(leader, settings) for _ in range(rounds)]

    results = {}
    threads = [
        threading.Thread(target=lambda name=n, leader=lead: results.update({name: loop(leader)}))
        for n, lead in (("a", first), ("b", second))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    acted = sorted(sum(r) for r in results.values())
    assert acted == [0, 5]  # one process did every round, the other none
    assert calls == {"tick": 5, "recover": 5}
