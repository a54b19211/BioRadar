"""One monitor at a time: the advisory-lock leader (research/leader.py) and the monitor round that uses it,
on a real PostgreSQL."""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from bioradar import db, guard, ops
from bioradar.adapters import llm, services
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
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


# ---- the lock connection drops while a round is running ---------------------------------------------------


def drop_lock_connection(leader):
    """The leader's session ends on the server side (killed); returns once the backend is gone."""
    pid = leader._connection.execute(text("SELECT pg_backend_pid()")).scalar()
    with db.engine().connect() as admin:
        admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        admin.commit()
        deadline = time.monotonic() + 10
        while admin.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p"), {"p": pid}
        ).scalar():
            assert time.monotonic() < deadline, "the backend did not end"
            time.sleep(0.02)
            admin.rollback()


def test_a_round_whose_lock_connection_drops_sends_and_writes_nothing_more(monkeypatch, leaders):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: sent.append("post"))
    monkeypatch.setattr(analyses, "queue", lambda: sent.append("queue"))
    outcomes = {}

    def after_the_drop(name, action):
        try:
            action()
        except guard.Revoked:
            outcomes[name] = "revoked"
        else:
            outcomes[name] = "went through"

    def write(question):
        with db.session() as s:
            s.add(db.Investigation(workspace_id="w", question=question, direction="science", is_demo=True))

    def write_dropped_before_commit():
        with db.session() as s:
            s.add(
                db.Investigation(
                    workspace_id="w", question="before commit", direction="science", is_demo=True
                )
            )
            s.flush()
            drop_lock_connection(leader)

    leader = leaders()

    def tick(is_demo=False):
        write("while leader")  # an ordinary write of the round
        after_the_drop("commit", write_dropped_before_commit)
        after_the_drop("write", lambda: write("after the drop"))
        after_the_drop("model", lambda: llm.post(settings(), {}, {}))
        after_the_drop("biocheck", lambda: services.biocheck(settings(), "verify", {}))
        after_the_drop("fabric", lambda: Fabric(settings()).sources())
        after_the_drop("dispatch", lambda: analyses.dispatch("an-analysis"))
        return {}

    monkeypatch.setattr(monitor, "tick", tick)
    with pytest.raises(guard.Revoked):  # the round ends instead of going on to recover
        ops.monitor_round(leader, SimpleNamespace(monitor_enabled=True))
    assert outcomes == dict.fromkeys(
        ("commit", "write", "model", "biocheck", "fabric", "dispatch"), "revoked"
    )
    assert sent == []  # not a request, not a queue entry, not a recover
    with db.session() as s:
        questions = sorted(s.scalars(select(db.Investigation.question)))
    assert questions == ["while leader"]  # the write the drop interrupted before its commit is not there
    other = leaders()
    assert other.acquire() is True  # and the next leader takes over at once
