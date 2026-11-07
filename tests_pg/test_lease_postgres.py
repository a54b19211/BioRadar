"""The lease on a real PostgreSQL: the row locks and the token check that SQLite cannot show.

The code under test is the production one: analyses.execute / recover, on the tables the migrations created.
Workers are threads, each with its own connection from the engine's pool."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select, text, update

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, lease
from bioradar.research.pipeline import Run
from bioradar.research.run import submit


def utc_now():
    return datetime.now(UTC)


def race(fn, workers=2):
    """Start `workers` calls of fn at the same moment and return their results."""
    barrier = threading.Barrier(workers)

    def go():
        barrier.wait(10)
        return fn()

    with ThreadPoolExecutor(workers) as pool:
        return [f.result(60) for f in [pool.submit(go) for _ in range(workers)]]


def returns_within(seconds, fn, *args):
    """fn(*args) in another thread; TimeoutError if still blocked after the time (e.g. on a row lock)."""
    pool = ThreadPoolExecutor(1)
    try:
        return pool.submit(fn, *args).result(seconds)
    finally:
        pool.shutdown(wait=False)


# ---- analyses: a lease without a token ---------------------------------------------------------------------


def add_analysis(state, lease_until=None, age=timedelta(0)):
    with db.session() as s:
        investigation = db.Investigation(workspace_id="w", question="q", direction="science", is_demo=True)
        s.add(investigation)
        s.flush()
        row = db.Analysis(
            investigation_id=investigation.id,
            trigger="manual",
            method="baseline",
            state=state,
            input={"question": "q"},
            input_hash=uuid.uuid4().hex,
            lease_until=lease_until,
            created_at=utc_now() - age,
        )
        s.add(row)
        s.flush()
        return row.id


def analysis_of(analysis_id):
    with db.session() as s:
        row = s.get(db.Analysis, analysis_id)
        s.expunge(row)
        return row


class FakeRun:
    """Stands for the pipeline: counts its runs; `gate`, when set, holds a run until the test lets it go."""

    runs = 0
    started = threading.Event()
    gate = None

    def __init__(self, row, config, fabric=None, lease_token=None):
        pass

    def run(self):
        type(self).runs += 1
        type(self).started.set()
        if type(self).gate is not None:
            assert type(self).gate.wait(30)
        time.sleep(0.2)
        return {"report": "done"}


@pytest.fixture
def fake_run(monkeypatch):
    monkeypatch.setattr(FakeRun, "runs", 0)
    monkeypatch.setattr(FakeRun, "started", threading.Event())
    monkeypatch.setattr(FakeRun, "gate", None)
    monkeypatch.setattr(analyses, "Run", FakeRun)
    return FakeRun


def test_two_workers_racing_for_one_analysis_run_it_once(fake_run):
    analysis_id = add_analysis("queued")
    race(lambda: analyses.execute(analysis_id))
    assert fake_run.runs == 1
    row = analysis_of(analysis_id)
    assert (row.state, row.lease_until) == ("completed", None) and row.result == {"report": "done"}


def test_a_second_worker_does_not_run_an_analysis_whose_lease_holds(fake_run):
    analysis_id = add_analysis("queued")
    fake_run.gate = threading.Event()
    first = threading.Thread(target=analyses.execute, args=(analysis_id,))
    first.start()
    try:
        assert fake_run.started.wait(30)
        assert analysis_of(analysis_id).state == "running"
        returns_within(10, analyses.execute, analysis_id)  # the row lock is free again; the lease says taken
        assert fake_run.runs == 1
    finally:
        fake_run.gate.set()
        first.join(60)
    assert analysis_of(analysis_id).state == "completed"


@pytest.fixture
def queue(monkeypatch):
    """Redis stand-in: what recover() dispatched is what is waiting in the queue."""
    sent = []
    monkeypatch.setattr(analyses, "dispatch", sent.append)
    monkeypatch.setattr(analyses, "waiting_in_queue", lambda analysis_id: analysis_id in sent)
    monkeypatch.setattr(analyses, "worker_gone", lambda analysis_id: None)
    return sent


def test_an_analysis_whose_worker_crashed_is_requeued_once(queue):
    crashed = add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))
    working = add_analysis("running", lease_until=utc_now() + timedelta(minutes=5))
    assert analyses.recover() == 1 and queue == [crashed]
    assert analyses.recover() == 0 and queue == [crashed]  # re-queued rows are not re-queued again
    row = analysis_of(crashed)
    assert (row.state, row.lease_until) == ("queued", None)
    assert analysis_of(working).state == "running"  # a live lease is left alone


def test_a_job_that_never_reached_the_queue_is_dispatched_once(queue):
    lost = add_analysis("queued", age=timedelta(minutes=30))
    fresh = add_analysis("queued")  # young: its job may still be on its way
    assert analyses.recover() == 1 and queue == [lost]
    assert analyses.recover() == 0 and queue == [lost]
    assert analysis_of(fresh).state == "queued"


@pytest.fixture
def slow_queue(monkeypatch):
    """Redis stand-in whose dispatch takes a moment: the window in which a second recover could act."""
    sent = []

    def slow_dispatch(analysis_id):
        time.sleep(0.2)
        sent.append(analysis_id)

    monkeypatch.setattr(analyses, "dispatch", slow_dispatch)
    monkeypatch.setattr(analyses, "waiting_in_queue", lambda analysis_id: analysis_id in sent)
    return sent


def test_two_recovers_at_the_same_moment_requeue_one_row_once(slow_queue):
    crashed = add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))
    results = race(analyses.recover)
    assert slow_queue == [crashed] and sorted(results) == [
        0,
        1,
    ]  # one recover got the row, the other skipped it
    assert analysis_of(crashed).state == "queued"


def test_two_recovers_at_the_same_moment_requeue_every_row_exactly_once(slow_queue):
    """With several rows the two recovers may split them (SKIP LOCKED lets each take the rows the other has
    not locked yet), so which recover handles which row is not fixed; that every row is handled once is."""
    crashed = add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))
    lost = add_analysis("queued", age=timedelta(minutes=30))
    results = race(analyses.recover)
    assert sorted(slow_queue) == sorted([crashed, lost])  # each dispatched once, never once per recover
    assert sum(results) == 2
    assert analysis_of(crashed).state == "queued" and analysis_of(lost).state == "queued"


@pytest.fixture
def racy_run(monkeypatch):
    """A pipeline whose first run is held until the test lets it go; later runs finish at once."""

    class RacyRun:
        runs = []
        started = threading.Event()
        gate = threading.Event()

        def __init__(self, row, config, fabric=None, lease_token=None):
            pass

        def run(self):
            n = len(type(self).runs)
            type(self).runs.append(n)
            if n == 0:
                type(self).started.set()
                assert type(self).gate.wait(30)
            return {"report": f"run{n}"}

    monkeypatch.setattr(analyses, "Run", RacyRun)
    return RacyRun


def test_a_worker_that_outlived_its_lease_cannot_write_over_the_one_that_took_over(racy_run, queue, caplog):
    analysis_id = add_analysis("queued")
    late = threading.Thread(target=analyses.execute, args=(analysis_id,))
    late.start()
    try:
        assert racy_run.started.wait(30)
        old_token = analysis_of(analysis_id).lease_token
        assert old_token
        with db.session() as s:  # the first worker is slow: its lease runs out
            s.execute(update(db.Analysis).values(lease_until=utc_now() - timedelta(minutes=1)))
        assert analyses.recover() == 1 and queue == [analysis_id]
        assert analysis_of(analysis_id).lease_token is None  # recover released the old holder
        analyses.execute(analysis_id)  # the second worker runs it to the end
        assert analysis_of(analysis_id).result == {"report": "run1"}
    finally:
        racy_run.gate.set()
        late.join(60)
    row = analysis_of(analysis_id)
    assert (row.state, row.result, row.lease_token) == ("completed", {"report": "run1"}, None)
    assert "final write rejected" in caplog.text


# ---- a superseded worker: no business write and no send of an analysis goes through ----------------------


def superseded_run():
    """A Run holding the token of a worker that another worker has replaced."""
    analysis_id = add_analysis("running", lease_until=utc_now() + timedelta(minutes=5))
    with db.session() as s:
        s.execute(
            update(db.Analysis)
            .where(db.Analysis.id == analysis_id)
            .values(lease_token="old", input={"question": "q", "budget": {"max_llm_calls": 9}})
        )
    run = Run(analysis_of(analysis_id), settings(), lease_token="old")
    with db.session() as s:  # recover released it and a second worker took it
        s.execute(update(db.Analysis).where(db.Analysis.id == analysis_id).values(lease_token="new"))
    return run, analysis_id


def rows_of(model, analysis_id):
    with db.session() as s:
        return list(s.scalars(select(model).where(model.analysis_id == analysis_id)))


def test_a_superseded_run_cannot_save_progress_or_log_calls_or_record_outbound_material():
    run, analysis_id = superseded_run()
    run.p["notes"] = [{"code": "late", "detail": "", "stage": "plan"}]
    with pytest.raises(lease.Lost):
        run.save("investigate")
    with pytest.raises(lease.Lost):
        run.log([{"stage": "plan", "usage": {}, "requests": 1}])
    with pytest.raises(lease.Lost):
        submit.send_rows(run, "read", "radar", [])
    with pytest.raises(lease.Lost):
        submit.mark(run, [1], "sent")
    with pytest.raises(lease.Lost):
        submit.withheld_attempt(run, [1])
    row = analysis_of(analysis_id)
    assert (row.stage, row.progress) == ("plan", {}) and row.lease_token == "new"
    assert rows_of(db.LlmCall, analysis_id) == [] and rows_of(db.MaterialRead, analysis_id) == []


def test_a_superseded_run_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: sent.append("model") or ({}, {"error": None}))
    monkeypatch.setattr(services, "biocheck", lambda *a, **k: sent.append("biocheck") or {})
    run, _ = superseded_run()
    with pytest.raises(lease.Lost):
        run.ask("plan", "system", {}, dict)
    with pytest.raises(lease.Lost):
        submit.service(run, "verify", {})
    with pytest.raises(lease.Lost):
        with submit.outbound(run, []):
            sent.append("outbound")
    assert sent == []  # not a single request left the process


def test_a_run_that_holds_its_token_writes_and_a_successor_waits_for_the_write():
    analysis_id = add_analysis("running", lease_until=utc_now() + timedelta(minutes=5))
    with db.session() as s:
        s.execute(update(db.Analysis).where(db.Analysis.id == analysis_id).values(lease_token="mine"))
    run = Run(analysis_of(analysis_id), settings(), lease_token="mine")
    with run.writing() as s:  # the write is open: the row is locked against a taking-over claim
        s.add(db.LlmCall(analysis_id=analysis_id, stage="plan", service="radar", requests=1))
        s.flush()
        with db.engine().connect() as other:
            other.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(Exception, match="lock timeout|canceling statement"):
                other.execute(text("SELECT 1 FROM analyses WHERE id = :i FOR UPDATE"), {"i": analysis_id})
    assert len(rows_of(db.LlmCall, analysis_id)) == 1


# ---- recover when the outcome of a send is unknown --------------------------------------------------------


def prepared_send(analysis_id):
    with db.session() as s:
        s.add(
            db.MaterialRead(
                analysis_id=analysis_id,
                stage="read",
                service="radar",
                ref="E1",
                status="prepared",
                source="s",
                record_id="r",
                version_id="v",
                sha256="0" * 64,
                chars=1,
                binding={},
            )
        )


def notes_of(analysis_id):
    return [n["code"] for n in (analysis_of(analysis_id).progress or {}).get("notes") or []]


def test_a_crash_with_an_unsettled_send_is_requeued_with_the_reason_recorded(queue):
    clean = add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))
    unsure = add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))
    prepared_send(unsure)
    assert analyses.recover() == 2 and sorted(queue) == sorted([clean, unsure])
    assert notes_of(clean) == [] and notes_of(unsure) == [analyses.UNKNOWN_OUTCOME]
    with db.session() as s:  # the record of the possible send is kept, not erased or settled by a guess
        assert (
            s.scalar(select(db.MaterialRead.status).where(db.MaterialRead.analysis_id == unsure))
            == "prepared"
        )


def test_a_run_that_keeps_dying_with_an_unsettled_send_is_stopped_for_review_not_sent_again(queue):
    unsure = add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))
    prepared_send(unsure)
    for _ in range(analyses.MAX_UNKNOWN_RECOVERIES - 1):
        assert analyses.recover() == 1
        queue.clear()  # the resumed worker died again: its job is gone
        with db.session() as s:
            s.execute(
                update(db.Analysis).values(state="running", lease_until=utc_now() - timedelta(minutes=5))
            )
    queue.clear()
    assert analyses.recover() == 1 and queue == []  # counted as handled, but no job was sent
    row = analysis_of(unsure)
    assert (row.state, row.error_code, row.lease_until) == ("failed", "recovery_unknown_outcome", None)
    assert notes_of(unsure) == [analyses.UNKNOWN_OUTCOME] * analyses.MAX_UNKNOWN_RECOVERIES


# ---- the deadline counts as well as the token -------------------------------------------------------------


def expired_run():
    """A Run whose token is still the row's, but whose lease ran out five minutes ago."""
    analysis_id = add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))
    budget = {"question": "q", "budget": {"max_llm_calls": 9}}
    with db.session() as s:
        s.execute(
            update(db.Analysis).where(db.Analysis.id == analysis_id).values(lease_token="mine", input=budget)
        )
    return Run(analysis_of(analysis_id), settings(), lease_token="mine"), analysis_id


def test_a_worker_whose_lease_ran_out_sends_nothing_and_writes_nothing_even_with_its_token(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: sent.append("model") or ({}, {"error": None}))
    run, analysis_id = expired_run()
    with pytest.raises(lease.Lost):
        run.ask("plan", "system", {}, dict)
    run.p["notes"] = [{"code": "late", "detail": "", "stage": "plan"}]
    with pytest.raises(lease.Lost):
        run.save("investigate")
    assert sent == []
    row = analysis_of(analysis_id)
    assert (row.stage, row.progress, row.lease_token) == ("plan", {}, "mine")


def test_a_renewal_cannot_revive_an_expired_lease_and_a_live_one_is_extended():
    run, analysis_id = expired_run()
    before = analysis_of(analysis_id).lease_until
    with db.session() as s:
        assert lease.renew(s, db.Analysis, analysis_id, "mine", utc_now()) is False
    assert analysis_of(analysis_id).lease_until == before  # nothing was written
    with db.session() as s:
        s.execute(update(db.Analysis).values(lease_until=utc_now() + timedelta(minutes=1)))
    run.check_lease(send=False)  # a live lease is renewed by the check itself
    extended = analysis_of(analysis_id).lease_until
    assert extended > utc_now() + timedelta(minutes=60)


def test_the_final_write_of_a_worker_whose_lease_ran_out_is_rejected(racy_run, caplog):
    analysis_id = add_analysis("queued")
    late = threading.Thread(target=analyses.execute, args=(analysis_id,))
    late.start()
    try:
        assert racy_run.started.wait(30)
        with db.session() as s:  # no one took it over, but the deadline has passed
            s.execute(update(db.Analysis).values(lease_until=utc_now() - timedelta(minutes=1)))
    finally:
        racy_run.gate.set()
        late.join(60)
    row = analysis_of(analysis_id)
    assert (row.state, row.result) == ("running", None)  # recover will decide, the late result is not stored
    assert "final write rejected" in caplog.text


# ---- the late answer of a request that was already sent --------------------------------------------------


class Answer(BaseModel):
    ok: int = 0


class Reply:
    def __init__(self, content):
        self.status_code = 200
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }


def take_over(analysis_id):
    with db.session() as s:
        s.execute(
            update(db.Analysis)
            .where(db.Analysis.id == analysis_id)
            .values(lease_token="new", lease_until=utc_now() + timedelta(minutes=5))
        )


def valid_run():
    run, analysis_id = expired_run()
    with db.session() as s:
        s.execute(update(db.Analysis).values(lease_until=utc_now() + timedelta(minutes=5)))
    return run, analysis_id


def test_the_answer_of_a_request_sent_before_the_takeover_is_recorded_as_the_old_attempts_audit(monkeypatch):
    run, analysis_id = valid_run()

    def answer_after_takeover(*args, **kwargs):
        take_over(analysis_id)  # while the request is on the way
        return Reply('{"ok": 1}')

    monkeypatch.setattr(llm.httpx, "post", answer_after_takeover)
    with pytest.raises(lease.Lost):  # the run stops, its result is not used ...
        run.ask("plan", "system", {}, Answer)
    (call,) = rows_of(db.LlmCall, analysis_id)  # ... but the spent call and its raw answer are kept
    assert (call.requests, call.total_tokens, call.lease_token) == (1, 3, "mine")
    with db.session() as s:
        (output,) = s.scalars(select(db.LlmOutput).where(db.LlmOutput.llm_call_id == call.id))
        assert output.content == '{"ok": 1}'
    row = analysis_of(analysis_id)
    assert (row.lease_token, row.progress) == ("new", {})  # the successor's row is untouched


def test_a_lost_lease_is_noticed_before_a_format_retry_and_the_first_attempt_is_settled(monkeypatch):
    run, analysis_id = valid_run()
    posts = []

    def invalid_answer_then_takeover(*args, **kwargs):
        posts.append(1)
        take_over(analysis_id)
        return Reply("not json")  # asks for the format retry, which must not go out

    monkeypatch.setattr(llm.httpx, "post", invalid_answer_then_takeover)
    with pytest.raises(lease.Lost):
        run.ask("plan", "system", {}, Answer)
    assert len(posts) == 1  # the retry was not sent
    (call,) = rows_of(db.LlmCall, analysis_id)
    assert (call.requests, call.error, call.lease_token) == (
        1,
        "send_blocked",
        "mine",
    )  # the retry was refused


def test_a_call_nobody_sent_is_not_recorded_by_a_superseded_run():
    run, analysis_id = superseded_run()
    with pytest.raises(lease.Lost):
        run.log(
            [{"stage": "plan", "usage": {"total_tokens": 9}, "requests": 1}]
        )  # fabricated: no send registered
    assert rows_of(db.LlmCall, analysis_id) == []


# ---- recover asks RQ about a row whose lease ran out ------------------------------------------------------


@pytest.fixture
def rq(monkeypatch):
    """What RQ says about a job: present (waiting or started) or not, and for a started one whether its
    worker is gone. `asked` records every lookup."""
    state = SimpleNamespace(present=True, gone=None, asked=[], sent=[], unreachable=False)

    def present(analysis_id):
        state.asked.append(analysis_id)
        if state.unreachable:
            raise ConnectionError("redis is down")
        return state.present

    def gone(analysis_id):
        if state.unreachable:
            raise ConnectionError("redis is down")
        return state.gone

    monkeypatch.setattr(analyses, "waiting_in_queue", present)
    monkeypatch.setattr(analyses, "worker_gone", gone)
    monkeypatch.setattr(analyses, "dispatch", state.sent.append)
    return state


def expired_running():
    return add_analysis("running", lease_until=utc_now() - timedelta(minutes=5))


def test_an_expired_running_row_is_looked_up_in_rq_and_requeued_only_when_its_job_is_not_there(rq):
    rq.present = False
    row = expired_running()
    assert analyses.recover() == 1 and rq.asked == [row] and rq.sent == [row]
    assert notes_of(row) == []  # plainly lost


def test_a_row_whose_job_is_started_with_its_worker_gone_is_recovered_and_the_reason_recorded(rq):
    rq.present, rq.gone = True, True
    row = expired_running()
    assert analyses.recover() == 1 and rq.sent == [row]
    assert notes_of(row) == [analyses.FROM_UNKNOWN]
    assert analysis_of(row).state == "queued"


def test_a_row_whose_job_is_started_with_its_worker_beating_is_left_alone_and_noted_once(rq):
    rq.present, rq.gone = True, False
    row = expired_running()
    assert analyses.recover() == 0 and analyses.recover() == 0
    assert rq.sent == [] and analysis_of(row).state == "running"
    assert notes_of(row) == [analyses.WORKER_ALIVE]  # not every round


def test_a_row_whose_job_is_only_waiting_is_left_alone_without_a_note(rq):
    rq.present, rq.gone = True, None
    row = expired_running()
    assert analyses.recover() == 0 and rq.sent == [] and notes_of(row) == []


def test_when_redis_cannot_be_asked_an_expired_row_is_not_taken_for_dead(rq):
    rq.unreachable = True
    row = expired_running()
    assert analyses.recover() == 0 and rq.sent == []
    assert analysis_of(row).state == "running" and notes_of(row) == []
    rq.unreachable, rq.present, rq.gone = (
        False,
        True,
        True,
    )  # Redis is back, the job is started, its worker gone
    assert analyses.recover() == 1 and rq.sent == [row]
