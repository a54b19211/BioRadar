"""Real PG lock/idempotency regression, no model or external source request."""

import pytest
from bioradar.research.engine.models import EngineIdempotency
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_monitor import runtime as runtime  # noqa: F401

pytestmark = pytest.mark.ENGINE


def test_batch_cap_survives_repoll_and_restart_and_topic_has_its_own_pool(runtime):
    make, _, settings = runtime
    settings.monitor_investigations_per_batch = 1
    settings.monitor_hourly_investigations = 1
    settings.topic_hourly_investigations = 1
    monitor = make()
    assert monitor.claim()
    assert monitor.reserve_investigation("a" * 64, "batch-one", "routine")
    assert not monitor.reserve_investigation("b" * 64, "batch-one", "immediate")
    assert monitor.reserve_investigation("a" * 64, "batch-one", "routine")
    monitor.release()
    recovered = make()
    assert recovered.claim()
    assert not recovered.reserve_investigation("b" * 64, "batch-one", "immediate")
    assert recovered.reserve_investigation("c" * 64, "topic-evaluation-one", "routine", origin="topic")
    assert not recovered.reserve_investigation("d" * 64, "topic-evaluation-two", "immediate", origin="topic")
    recovered.release()
    with Session(recovered.engine.database) as session:
        rows = list(
            session.scalars(
                select(EngineIdempotency).where(EngineIdempotency.operation == "monitor.dig.start")
            )
        )
        assert len(rows) == 2
        assert {r.response_body["origin"] for r in rows} == {"monitor", "topic"}
        assert sum(r.response_body["batch_id"] == "batch-one" for r in rows) == 1


def test_normal_monitor_discovery_rq_queues_automatic_topics_and_admits_after_manual_close(
    runtime, monkeypatch
):
    import os
    from uuid import uuid4

    from bioradar.research.engine import worker
    from bioradar.research.engine.models import EngineRun
    from bioradar.research.monitor.models import MonitorLead
    from bioradar.topics.models import TopicControl, TopicScopeRecord
    from bioradar.topics.service import TopicService
    from pydantic import SecretStr
    from redis import Redis
    from rq import Queue, SimpleWorker
    from test_monitor import record

    factory, source, settings = runtime

    def make():
        return factory(legacy=False)

    redis_url = os.environ.get("BIORADAR_ENGINE_TEST_REDIS_URL")
    assert redis_url, "Dedicated real test Redis is required for this RQ regression"
    settings.redis_url = SecretStr(redis_url)
    settings.queue_prefix = "opus-native-" + uuid4().hex
    settings.topic_max_auto_active = 1
    settings.monitor_hourly_investigations = 0  # No semantic/model execution in this test.
    source.records = [
        record(1, "北江医院报告2名医护人员感染，传播途径尚未确定。", seq=1),
        record(2, "河西医院报告3名医护人员感染，病原尚未确认。", seq=2),
    ]
    monitor = make()
    diagnostics = []
    original_engine = worker.EngineService

    class CheckedEngine(original_engine):
        def __init__(self, *args):
            super().__init__(*args)
            self.history_source_reader = monitor.reader.sources

        def start_run(self, *args):
            try:
                return super().start_run(*args)
            except Exception as exc:
                diagnostics.append(("start", getattr(exc, "code", type(exc).__name__)))
                raise

        def finish_run(self, *args):
            try:
                return super().finish_run(*args)
            except Exception as exc:
                diagnostics.append(("finish", getattr(exc, "code", type(exc).__name__)))
                raise

    monkeypatch.setattr(worker, "EngineService", CheckedEngine)
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "create_database", lambda _: monitor.engine.database)
    connection = Redis.from_url(redis_url)
    queue = Queue(settings.queue_prefix + ":routine", connection=connection)
    try:
        make().tick()  # Ordinary source page -> snapshot adoption -> persisted run -> existing RQ.
        assert queue.count == 1
        SimpleWorker([queue], connection=connection).work(burst=True, logging_level="CRITICAL")
        with Session(monitor.engine.database) as session:
            run = session.scalar(select(EngineRun))
            assert run.state in {"completed", "partial"}, (run.error_code, diagnostics)
            assert run.policy["discovery_directions"] == ["research", "pipeline", "business", "public_health"]
            result_hash = run.result_hash
        make().tick()
        with Session(monitor.engine.database) as session:
            leads = list(session.scalars(select(MonitorLead)))
            created = [v for v in leads if v.payload.get("auto_topic", {}).get("status") == "created"]
            waiting = [v for v in leads if v.payload.get("auto_topic", {}).get("status") == "queued"]
            assert len(created) == 1 and waiting
            waiting_id, intent_key = waiting[0].id, waiting[0].payload["auto_topic"]["key"]
            scope = session.scalar(select(TopicScopeRecord))
            topic_id, revision = scope.topic_id, scope.control_revision
        topics = TopicService(monitor.engine, monitor.reader)
        topics.control(
            topics.actor,
            topic_id,
            TopicControl(
                expected_revision=revision, paused=True, lifecycle="closed", reason="SYNTHETIC human stop"
            ),
            "synthetic-close",
        )
        recovered = make()
        assert recovered.claim()
        recovered._drain_tracking()  # Persistent intent is retried, not reconstructed or dropped.
        recovered.release()
        with Session(monitor.engine.database) as session:
            intent = session.get(MonitorLead, waiting_id).payload["auto_topic"]
            assert intent["status"] == "created" and intent["key"] == intent_key
            assert session.scalar(select(EngineRun.result_hash)) == result_hash
            assert len(list(session.scalars(select(TopicScopeRecord)))) == 2
    finally:
        for job_id in queue.get_job_ids():
            queue.remove(job_id, delete_job=True)
        queue.delete(delete_jobs=True)
        connection.close()


def test_queued_old_v03_compute_is_refused_and_frozen_input_is_retained(runtime):
    import copy

    from bioradar.research.engine.models import EngineRun, EngineSnapshot
    from bioradar.research.mining.models import canonical_hash
    from test_monitor import record

    make, source, _ = runtime
    source.records = [record(1, "SYNTHETIC explicit old KDD input.", seq=1)]
    monitor = make(legacy=False)
    monitor.tick()
    with Session(monitor.engine.database) as session, session.begin():
        row = session.scalar(select(EngineRun))
        # This isolated schema simulates an old fixed run; actual old rows are untouched.
        versions = {**row.method_versions, "__engine_compute__": "bioradar-mining-compute-v0.3"}
        row.method_versions = versions
        fixed_policy = copy.deepcopy(row.policy)
        stored = session.get(EngineSnapshot, row.snapshot_id)
        old_hash = canonical_hash(
            {"snapshot_hash": stored.snapshot_hash, "policy": fixed_policy, "method_versions": versions}
        )
        row.input_hash = old_hash
        args = row.id, row.attempt, row.accept_token, row.input_hash
    assert monitor.engine.start_run(*args) is None
    with Session(monitor.engine.database) as session:
        row = session.get(EngineRun, args[0])
        assert row.error_code == "method_version_changed" and row.result is None
        assert row.policy == fixed_policy and row.method_versions == versions and row.input_hash == old_hash
