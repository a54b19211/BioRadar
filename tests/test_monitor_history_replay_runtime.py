"""Synthetic public-time collections through actual PostgreSQL and existing RQ."""

import copy
import os
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.api.errors import DomainError
from bioradar.research.engine import worker
from bioradar.research.engine.models import EngineRun
from bioradar.research.engine.service import EngineService
from bioradar.research.monitor.models import MonitorBatch, MonitorLead, MonitorState
from bioradar.research.monitor.service import MonitorService
from bioradar.topics.models import TopicEvaluationRecord, TopicScopeRecord
from bioradar.topics.service import TopicService
from pydantic import SecretStr
from redis import Redis
from rq import Queue, SimpleWorker
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as analysis_runtime  # noqa: F401
from test_monitor import runtime as runtime  # noqa: F401
from test_monitor_history_replay import CUTOFF, MODE, historical_record

pytestmark = pytest.mark.ENGINE


def test_history_collection_rq_auto_topic_same_day_receipts_and_forward_day(runtime, monkeypatch):
    factory, _, settings = runtime
    database = factory().engine.database
    settings.engine_source_id_allowlist = {"fixture": ["external_archive"]}
    settings.redis_url = SecretStr(os.environ["BIORADAR_ENGINE_TEST_REDIS_URL"])
    settings.queue_prefix = "history-day-" + uuid4().hex
    settings.monitor_hourly_investigations = 0
    settings.topic_hourly_investigations = 0
    settings.weknora_kdd_url = "http://synthetic.invalid"
    bodies = [
        "北江医院报告2名医护人员感染，传播途径尚未确定。",
        "河西医院报告3名医护人员感染，病原尚未确认。",
    ]
    originals = [historical_record("SYNTHETIC-notice-" + str(i), body=body) for i, body in enumerate(bodies)]
    meter = {"collections": 0, "lost_response": True, "empty": False}
    requests = []

    def handle(request):
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/history/sources":
            return httpx.Response(
                200, json={"items": [{"source_id": "external_archive", "read_enabled": True}]}
            )
        assert request.method == "GET" and request.url.path == "/api/history/records"
        assert request.url.params["mode"] == MODE
        assert request.url.params["cutoff"] in {CUTOFF.isoformat(), (CUTOFF + timedelta(days=1)).isoformat()}
        meter["collections"] += 1
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.history.collection.v1",
                "items": [] if meter["empty"] else originals,
                "next_cursor": None,
                "coverage": {"complete": not meter["empty"], "scope_description": "SYNTHETIC public archive"},
                "excluded_counts": {},
            },
        )

    client = httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handle))

    def make():
        reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90)
        engine = EngineService(database, settings)
        engine.history_source_reader = reader.sources
        return MonitorService(engine, reader)

    first = make()
    original_engine = worker.EngineService

    class PermittedEngine(original_engine):
        def __init__(self, *args):
            super().__init__(*args)
            self.history_source_reader = first.reader.sources

    monkeypatch.setattr(worker, "EngineService", PermittedEngine)
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "create_database", lambda _: database)
    monkeypatch.setattr(
        MonitorService, "_feature_snapshot", lambda *args: pytest.fail("Current feature HTTP forbidden")
    )
    monkeypatch.setattr(
        TopicService, "_fetch_source", lambda *args, **kw: pytest.fail("Live fetch forbidden")
    )
    real_save = MonitorService._save_history

    def exit_after_collection(self, replay):
        real_save(self, replay)
        if meter["lost_response"] and replay["pending_day"]["collections"]:
            meter["lost_response"] = False
            raise RuntimeError("SYNTHETIC exit after collection checkpoint")

    monkeypatch.setattr(MonitorService, "_save_history", exit_after_collection)
    connection = Redis.from_url(settings.redis_url.get_secret_value())
    queue = Queue(settings.queue_prefix + ":routine", connection=connection)
    try:
        with pytest.raises(RuntimeError, match="SYNTHETIC exit"):
            first.advance_history_day(mode=MODE, cutoff=CUTOFF, source_ids=["external_archive"])
        assert meter["collections"] == 1
        first_day = make().advance_history_day(mode=MODE, cutoff=CUTOFF, source_ids=["external_archive"])
        assert first_day["status"] == "waiting" and queue.count == 1
        assert meter["collections"] == 1
        SimpleWorker([queue], connection=connection).work(burst=True, logging_level="CRITICAL")
        with Session(database) as session:
            run = session.scalar(select(EngineRun))
            assert run.state in {"completed", "partial"}, run.error_code
            assert run.policy["time_mode"] == "availability_replay"
            assert run.policy["availability_basis"] == MODE
            assert run.policy["as_of"].replace("Z", "+00:00") == CUTOFF.isoformat()
        day = make().advance_history_day(mode=MODE, cutoff=CUTOFF, source_ids=["external_archive"])
        assert day["topic_ids"] and day["topic_receipts"]
        with Session(database) as session:
            batch = session.scalar(select(MonitorBatch))
            assert batch.state == "completed"
            assert not {"after", "next_after", "max_seq", "heads"} & batch.payload.keys()
            assert batch.payload["feature_adoption"]["status"] == "not_adopted"
            leads = list(session.scalars(select(MonitorLead)))
            assert any(lead.payload.get("auto_topic", {}).get("topic_id") for lead in leads)
            assert session.scalar(select(func.count()).select_from(TopicScopeRecord))
            assert session.scalar(select(func.count()).select_from(TopicEvaluationRecord))
            before = copy.deepcopy(session.get(MonitorState, first.actor.workspace_id).source_progress)
            first_run = run.id
        assert make().tick() == {"state": "history_step_required"}
        same = make().advance_history_day(mode=MODE, cutoff=CUTOFF, source_ids=["external_archive"])
        assert same == day and meter["collections"] == 1 and queue.count == 0
        assert day["status"] == "completed"
        meter["empty"] = True
        tomorrow = CUTOFF + timedelta(days=1)
        after = make().advance_history_day(mode=MODE, cutoff=tomorrow, source_ids=["external_archive"])
        assert after["topic_ids"] == day["topic_ids"]
        assert after["status"] == "completed", after
        assert all(
            row["evaluation_id"] != day["topic_receipts"][topic]["evaluation_id"]
            for topic, row in after["topic_receipts"].items()
        )
        assert meter["collections"] == 2
        with Session(database) as session:
            state = session.get(MonitorState, first.actor.workspace_id).source_progress
            assert state["_history_replay"]["current_records"] == before["_history_replay"]["current_records"]
            assert all(not item.get("acked_after") for key, item in state.items() if key != "_history_replay")
            assert session.scalar(select(func.count()).select_from(EngineRun)) == 1
            assert session.get(EngineRun, first_run).state in {"completed", "partial"}
        with pytest.raises(DomainError) as caught:
            make().advance_history_day(
                mode=MODE, cutoff=CUTOFF - timedelta(days=1), source_ids=["external_archive"]
            )
        assert caught.value.code in {"history_replay_nonmonotonic", "history_replay_pending"}
        assert not any("changes" in path or "fetch" in path for path, _ in requests)
    finally:
        queue.empty()
        for key in connection.scan_iter(match=settings.queue_prefix + "*"):
            connection.delete(key)
        connection.close()
        client.close()


def test_actual_daily_analysis_inherits_only_earlier_same_scope_report(analysis_runtime, database):  # noqa: F811
    from datetime import UTC, datetime

    from bioradar.research.investigations.analysis_models import AnalysisCreate
    from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
    from bioradar.research.investigations.service import InvestigationService

    make, actor, view, source, model, settings, _ = analysis_runtime
    settings.analysis_manual_per_hour = 20
    today = datetime.now(UTC)
    old_cutoff, new_cutoff = today - timedelta(days=2), today - timedelta(days=1)
    original = source.records[0]
    original["availability_proof"] = {
        "available_at": (today - timedelta(days=3)).isoformat(),
        "verified_by": "SYNTHETIC archive",
        "verified_at": today.isoformat(),
        "uri": "https://example.invalid/synthetic-proof",
        "sha256": original["raw_sha256"],
    }
    context = {
        "mode": MODE,
        "cutoff": old_cutoff.isoformat(),
        "history_series_id": "SYNTHETIC-daily-topic",
        "history_scope_hash": "SYNTHETIC-scope",
    }
    service = make()
    first = service.start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=view["revision"], auto_retrieve=True),
        "SYNTHETIC-history-day-one",
        historical_context=context,
    )[1]
    service.execute(first["id"], first["input_hash"])
    before = make().get(actor, view["id"], first["id"])
    assert before.state in {"completed", "needs_review"}, before.error_code
    call_count = len(model.requests)
    current = InvestigationService(make().engine).get(actor, view["id"])
    accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=current.revision, parent_analysis_id=first["id"], auto_retrieve=True
        ),
        "SYNTHETIC-history-day-two",
        historical_context={**context, "cutoff": new_cutoff.isoformat()},
    )[1]
    with Session(database) as session:
        child = session.get(InvestigationAnalysisRecord, accepted["id"])
        parent = session.get(InvestigationAnalysisRecord, first["id"])
        assert child.parent_analysis_id == first["id"]
        assert child.fixed_input["parent_result"] == parent.result
        assert child.fixed_input["providers"]["weknora"]["available"] is False
        assert child.fixed_input["providers"]["weknora"]["status"] == "history_current_index_unavailable"
        assert child.fixed_input["evidence_time"]["cutoff"] == new_cutoff.isoformat()
        assert parent.fixed_input["evidence_time"]["cutoff"] == old_cutoff.isoformat()
    assert len(model.requests) == call_count  # New child is queued, never a hidden model request.
    assert make().get(actor, view["id"], first["id"]).result == before.result
