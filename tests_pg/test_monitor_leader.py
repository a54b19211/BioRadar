"""One monitor at a time: the advisory-lock leader (research/leader.py) and the monitor round that uses it,
on a real PostgreSQL."""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, text

from bioradar import db, guard, ops
from bioradar.adapters import llm, services
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.material import Material
from bioradar.research import analyses, monitor, triage
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
    transport = httpx.MockTransport(lambda request: sent.append("post"))
    monkeypatch.setattr(llm, "client", lambda: httpx.Client(transport=transport))
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

    def blocked_model_request():
        try:
            llm.post(settings(), {}, {})
        except llm.SendBlocked as blocked:  # the client turns a revoked round into a blocked send
            raise guard.Revoked(blocked.reason) from None

    leader = leaders()

    def tick(is_demo=False):
        write("while leader")  # an ordinary write of the round
        after_the_drop("commit", write_dropped_before_commit)
        after_the_drop("write", lambda: write("after the drop"))
        after_the_drop("model", blocked_model_request)
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


# ---- an answer that arrives after leadership was lost: audit yes, business result no -----------------------


def triage_document(tag):
    return dict(
        source="synthetic",
        record_id="r-late",
        version_id=f"r-late-{tag}",
        title="Synthetic",
        published_at=dict(value="2020-01-01T00:00:00Z", meaning="source_reported"),
        material_kind="official_notice",
        verified=True,
        events=[],
        material=Material.part(
            "Synthetic 3 cases.",
            dict(
                kind="clean",
                source="synthetic",
                record_id="r-late",
                version_id=f"r-late-{tag}",
                processor_version="test",
            ),
        ),
    )


@pytest.mark.parametrize(
    "answer, error",
    [('{"items": []}', None), ("not JSON", "send_blocked")],
    ids=["answer-arrives-after-lock-loss", "retry-blocked-after-lock-loss"],
)
def test_the_answer_of_a_request_sent_before_leadership_was_lost_is_settled_in_the_old_rounds_audit(
    monkeypatch, leaders, answer, error
):
    leader, sent = leaders(), []

    def reply(*args, **kwargs):
        sent.append(1)
        drop_lock_connection(leader)  # leadership is lost while the request is on the way
        body = dict(
            choices=[dict(finish_reason="stop", message=dict(content=answer))],
            usage=dict(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        )
        return httpx.Response(200, json=body)

    monkeypatch.setattr(llm, "client", lambda: httpx.Client(transport=httpx.MockTransport(reply)))
    document = triage_document(error or "ok")
    monkeypatch.setattr(monitor, "tick", lambda **kw: triage.triage(settings(), [document], []))
    with pytest.raises(guard.Revoked):  # the round ends; the answer is not used
        ops.monitor_round(leader, SimpleNamespace(monitor_enabled=True))
    assert len(sent) == 1  # a retry, if there was one, was not sent
    with db.session() as s:
        (call,) = s.scalars(select(db.LlmCall))
        (output,) = s.scalars(select(db.LlmOutput))
        (read,) = s.scalars(select(db.MaterialRead))
        observations = s.scalar(select(func.count()).select_from(db.Observation))
    assert (call.requests, call.total_tokens, call.error) == (1, 3, error)
    assert output.content == answer and (read.status, read.llm_call_id) == ("sent", call.id)
    assert observations == 0  # no business result was adopted


def test_a_call_stopped_before_its_first_request_leaves_no_call_and_its_records_are_not_sent():
    document = triage_document("not-sent")
    with db.session() as s:
        read = db.material_read(
            document["material"],
            stage="triage",
            service="radar",
            ref="R1",
            source="synthetic",
            record_id="r-late",
            version_id=document["version_id"],
        )
        s.add(read)
        s.flush()
        read_id = read.id
    stopped = dict(requests=0, usage={}, model="m", error="send_blocked", finish_reason=None, seconds=0)
    assert db.settle_call(stopped, [read_id], stage="triage") is None
    with db.session() as s:
        assert s.get(db.MaterialRead, read_id).status == "not_sent"
        assert s.scalar(select(func.count()).select_from(db.LlmCall)) == 0


def test_the_audit_exception_does_not_open_the_guard_for_anything_else(monkeypatch, leaders):
    leader = leaders()
    assert leader.acquire()
    drop_lock_connection(leader)
    with guard.holding(leader.assert_held):
        with guard.audit():
            guard.check()  # appending to the audit of a sent attempt is allowed ...
        with pytest.raises(guard.Revoked):  # ... a business write right after it is not
            with db.session():
                pass
