"""The radar's queue on a real Redis: the JSON wire format, the job allow-list, a pickled job left over from
before the switch, and recover's questions to RQ about a row whose lease ran out. The worker runs in this
process (rq.SimpleWorker with the radar's job class and serializer), so the pipeline can be a stand-in;
what is real is Redis, RQ and the radar's own queue code."""

import json
import uuid
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from redis import Redis
from rq import Queue, SimpleWorker
from rq.serializers import JSONSerializer
from sqlalchemy import select, update

from bioradar import db, jobs
from bioradar.config import settings
from bioradar.research import analyses

TRIPPED = []


def trip(*args):
    TRIPPED.append("unpickled")


class Booby:
    def __reduce__(self):  # a pickled payload that runs code when it is loaded
        return (trip, ())


class FakeRun:
    def __init__(self, row, config, fabric=None, lease_token=None):
        pass

    def run(self):
        return {"report": "done"}


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    monkeypatch.setattr(analyses, "Run", FakeRun)
    TRIPPED.clear()


def connection():
    return Redis.from_url(settings().redis_url)


def drain():
    jobs.worker(settings(), SimpleWorker).work(burst=True, with_scheduler=False)


def status(job_id):
    return jobs.RadarJob.fetch(job_id, connection=connection(), serializer=JSONSerializer).get_status(
        refresh=True
    )


def add_analysis(state="queued", lease_until=None, age=timedelta(0)):
    with db.session() as s:
        investigation = db.Investigation(workspace_id="w", question="q", direction="science", is_demo=True)
        s.add(investigation)
        s.flush()
        row = db.Analysis(
            investigation_id=investigation.id,
            trigger="manual",
            method="baseline",
            state=state,
            input={"question": "q", "budget": {"max_llm_calls": 9}},
            input_hash=uuid.uuid4().hex,
            lease_until=lease_until,
            created_at=datetime.now(UTC) - age,
        )
        s.add(row)
        s.flush()
        return row.id


def analysis_of(analysis_id):
    with db.session() as s:
        row = s.get(db.Analysis, analysis_id)
        s.expunge(row)
        return row


def test_the_radars_own_job_travels_as_json_and_is_run():
    analysis_id = add_analysis()
    analyses.dispatch(analysis_id)
    raw = connection().hget(f"rq:job:{analyses.job_id(analysis_id)}", "data")
    assert json.loads(zlib.decompress(raw))[0] == "bioradar.research.analyses.execute"  # JSON, not pickle
    drain()
    row = analysis_of(analysis_id)
    assert (row.state, row.result) == ("completed", {"report": "done"})


def test_a_job_of_another_application_on_the_radars_queue_fails_and_is_never_unpickled():
    other_app = Queue(settings().queue_name, connection=connection())  # RQ's default: pickle, any function
    other_app.enqueue("os.getcwd", job_id="other-cwd")
    other_app.enqueue(trip, Booby(), job_id="other-booby")
    drain()
    assert status("other-cwd") == "failed" and status("other-booby") == "failed"
    assert TRIPPED == []  # nothing from the strangers' payload ran, not even its pickle


def test_a_pickled_job_left_from_before_the_switch_fails_and_recover_sends_it_again_as_json():
    analysis_id = add_analysis(age=timedelta(minutes=30))
    old = Queue(settings().queue_name, connection=connection())  # how the previous code enqueued
    old.enqueue("bioradar.research.analyses.execute", analysis_id, job_id=analyses.job_id(analysis_id))
    drain()
    assert status(analyses.job_id(analysis_id)) == "failed"  # it cannot be read any more
    assert analysis_of(analysis_id).state == "queued"
    assert analyses.recover() == 1  # its job is gone as far as the radar is concerned: sent again
    drain()
    assert analysis_of(analysis_id).state == "completed"


def started_job(analysis_id, worker_name):
    """The job of the analysis as RQ lists it while a worker runs it."""
    analyses.dispatch(analysis_id)
    job = jobs.RadarJob.fetch(
        analyses.job_id(analysis_id), connection=connection(), serializer=JSONSerializer
    )
    job.set_status("started")
    job.worker_name = worker_name
    job.save()


def test_a_started_job_is_told_dead_or_alive_by_its_workers_heartbeat():
    worker = jobs.worker(settings())
    worker.register_birth()  # the worker's key and a fresh heartbeat
    try:
        analysis_id = add_analysis()
        started_job(analysis_id, worker.name)
        job_id = analyses.job_id(analysis_id)
        assert jobs.started_worker_dead(job_id, settings()) is False
        later = datetime.now(UTC) + timedelta(seconds=worker.worker_ttl + 60)
        assert jobs.started_worker_dead(job_id, settings(), now=later) is True  # the beat stopped long ago
    finally:
        worker.register_death()
    gone = add_analysis()
    started_job(gone, "no-such-worker")
    assert jobs.started_worker_dead(analyses.job_id(gone), settings()) is True
    waiting = add_analysis()
    analyses.dispatch(waiting)
    assert jobs.started_worker_dead(analyses.job_id(waiting), settings()) is None  # only waiting
    assert jobs.started_worker_dead("analysis-not-there", settings()) is None


def expired(analysis_id):
    with db.session() as s:
        s.execute(
            update(db.Analysis)
            .where(db.Analysis.id == analysis_id)
            .values(state="running", lease_until=datetime.now(UTC) - timedelta(minutes=5))
        )


def test_recover_leaves_a_row_whose_started_job_has_a_beating_worker_and_takes_one_whose_worker_is_gone():
    worker = jobs.worker(settings())
    worker.register_birth()
    try:
        alive, dead = add_analysis(), add_analysis()
        started_job(alive, worker.name)
        started_job(dead, "no-such-worker")
        expired(alive)
        expired(dead)
        assert analyses.recover() == 1  # only the row whose worker is gone
        assert analysis_of(alive).state == "running"
        assert analysis_of(dead).state == "queued"
        assert status(analyses.job_id(dead)) == "queued"  # sent again
    finally:
        worker.register_death()
    with db.session() as s:
        codes = {
            r.id: [n["code"] for n in (r.progress or {}).get("notes") or []]
            for r in s.scalars(select(db.Analysis))
        }
    assert codes[alive] == [analyses.WORKER_ALIVE] and codes[dead] == [analyses.FROM_UNKNOWN]


def test_recover_does_not_take_an_expired_row_for_dead_when_redis_cannot_be_reached(monkeypatch):
    analysis_id = add_analysis()
    expired(analysis_id)
    monkeypatch.setattr(jobs, "connection", lambda s: Redis.from_url("redis://127.0.0.1:1/0"))
    assert analyses.recover() == 0
    row = analysis_of(analysis_id)
    assert row.state == "running" and not (row.progress or {}).get("notes")
