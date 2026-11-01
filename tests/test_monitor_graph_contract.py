"""Published node metadata and honest monitor watermarks; no model calls."""

import copy
import json
import os
from uuid import uuid4

import httpx
import pytest
from bioradar.adapters.fabric_graph import (
    adopted_graph_snapshot,
    project_graph_hints,
    read_graph_version,
    read_neighborhood,
)
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.research.engine.models import EngineRun
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.graph import GraphBundle
from bioradar.research.monitor.models import MonitorBatch, MonitorState
from bioradar.research.monitor.service import MonitorService
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_graph import bundle, original_record, request
from test_graph import source as graph_source
from test_monitor import runtime as runtime  # noqa: F401

pytestmark = pytest.mark.ENGINE
NAMESPACE = "synthetic-explicit-graph-namespace"


def test_node_properties_are_preserved_metadata_and_never_confirm_pending_graph():
    value, doc = bundle()
    payload = value.model_dump(mode="json")
    assert all("properties" not in node for node in payload["nodes"])
    for node in payload["nodes"]:
        node.update(
            properties={"source_note": "SYNTHETIC", "identity_status": "confirmed"},
            identity_status="unresolved", identity_review_basis=None,
            review={"status": "pending"},
        )
    for edge in payload["edges"]:
        edge["review"] = {"status": "pending"}
    parsed = GraphBundle.model_validate(payload)
    assert parsed.model_dump(mode="json")["nodes"][0]["properties"] == payload["nodes"][0]["properties"]
    reader, record, _ = graph_source(parsed, doc)
    try:
        query = request(record)
        original, adopted = read_graph_version(reader, query, query.cutoff)
        neighborhood = read_neighborhood(reader, original, adopted, query, query.cutoff)
        snapshot = adopted_graph_snapshot(
            reader, original, adopted, neighborhood=neighborhood, documents=[doc],
        )
        current = next(r for r in snapshot.records if r.source_id == "reviewed_graph")
        assert current.relations == ()
        assert all(entity.mapping_status == "unresolved" for entity in current.canonical_entity_refs)
        hinted = project_graph_hints(
            reader, original, adopted, engine_record=current, documents=[doc], neighborhood=neighborhood,
        )
        assert hinted.graph_hints
        assert all(hint.review_status == "pending" for hint in hinted.graph_hints)
        assert all(
            set(hint.identity_statuses) == {"unresolved"} for hint in hinted.graph_hints
        )
    finally:
        reader.client.close()
    invalid = copy.deepcopy(payload)
    invalid["nodes"][0]["properties"] = ["not-a-metadata-object"]
    with pytest.raises(ValidationError):
        GraphBundle.model_validate(invalid)
    invalid = copy.deepcopy(payload)
    invalid["nodes"][0]["identity_status"] = "confirmed"
    with pytest.raises(ValidationError):
        GraphBundle.model_validate(invalid)


def configure(settings):
    settings.data_fabric_demo_source_instance = NAMESPACE
    settings.engine_source_allowlist = [NAMESPACE]
    settings.engine_source_id_allowlist = {NAMESPACE: ["fixture-source", "reviewed_graph"]}
    settings.monitor_kdd_directions = {"reviewed_graph": "pipeline"}
    settings.monitor_hourly_investigations = 0


def test_property_bearing_graph_event_completes_existing_pg_rq_and_advances_ack(runtime, monkeypatch):
    from bioradar.research.engine import worker
    from redis import Redis
    from rq import Queue, SimpleWorker

    factory, source, settings = runtime
    configure(settings)
    redis_url = os.environ.get("BIORADAR_ENGINE_TEST_REDIS_URL")
    assert redis_url, "Dedicated real test Redis is required for this RQ regression"
    settings.redis_url = SecretStr(redis_url)
    settings.queue_prefix = "graph-contract-" + uuid4().hex
    database = factory().engine.database
    value, doc = bundle()
    payload = value.model_dump(mode="json")
    for node in payload["nodes"]:
        node["properties"] = {"source_note": "SYNTHETIC graph metadata"}
    parsed = GraphBundle.model_validate(payload)
    graph_reader, graph_record, _ = graph_source(parsed, doc)
    source.enabled.add("reviewed_graph")
    source.records = [graph_record]
    source_handler = source.handler

    def handle(req):
        if "/api/history/graph/" in req.url.path:
            assert req.url.params["review_scope"] == "with_candidates"
            return graph_reader.client.send(req)
        if req.url.path.startswith("/api/history/versions/"):
            return graph_reader.client.send(req)
        return source_handler(req)

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handle)) as client:
        reader = FabricHistoryReader(
            client, authorization_ref=GRANT, demo_source_instance=NAMESPACE, max_seconds=90,
        )

        def make():
            engine = EngineService(database, settings)
            engine.history_source_reader = reader.sources
            return MonitorService(engine, reader)

        original_engine = worker.EngineService

        class PermittedEngine(original_engine):
            def __init__(self, *args):
                super().__init__(*args)
                self.history_source_reader = reader.sources

        monkeypatch.setattr(worker, "EngineService", PermittedEngine)
        monkeypatch.setattr(worker, "Settings", lambda: settings)
        monkeypatch.setattr(worker, "create_database", lambda _: database)
        connection = Redis.from_url(redis_url)
        queue = Queue(settings.queue_prefix + ":routine", connection=connection)
        try:
            assert make().tick()["source_id"] == "fixture-source"  # Empty document stream.
            assert make().tick()["source_id"] == "reviewed_graph"
            assert queue.count == 1
            with Session(database) as session:
                batch = session.scalar(select(MonitorBatch))
                assert batch.source_id == "reviewed_graph" and batch.state == "queued"
                assert batch.payload["manifest"][0]["version_id"] == graph_record["version_id"]
                fixed_snapshot = copy.deepcopy(batch.payload["snapshot"])
            SimpleWorker([queue], connection=connection).work(burst=True, logging_level="CRITICAL")
            with Session(database) as session:
                run = session.scalar(select(EngineRun))
                assert run.state in {"completed", "partial"}, run.error_code
                result_hash = run.result_hash
                assert run.policy["kdd_direction"] == "pipeline"
                adopted = make().engine.get_snapshot(make().actor, run.snapshot_id).snapshot
                assert adopted.source_instance == NAMESPACE
                assert all(record.source_instance == NAMESPACE for record in adopted.records)
            make().tick()
            make().tick()
            with Session(database) as session:
                batch = session.scalar(select(MonitorBatch))
                state = session.get(MonitorState, make().actor.workspace_id)
                progress = state.source_progress[make().stream_key("reviewed_graph")]
                assert batch.state == "completed"
                assert progress["acked_after"] == progress["scan_after"] == 1
                assert progress["error_code"] is None
                assert batch.payload["snapshot"] == fixed_snapshot
            make().tick()
            make().tick()
            with Session(database) as session:
                assert session.scalar(select(func.count()).select_from(MonitorBatch)) == 1
                assert session.scalar(select(EngineRun.result_hash)) == result_hash
            assert json.loads(graph_record["raw_content"])["nodes"][0]["properties"] == {
                "source_note": "SYNTHETIC graph metadata",
            }
        finally:
            for job_id in queue.get_job_ids():
                queue.remove(job_id, delete_job=True)
            queue.delete(delete_jobs=True)
            connection.close()
            graph_reader.client.close()


def test_invalid_graph_contract_is_visible_and_never_acknowledges_or_queues(runtime):
    factory, source, settings = runtime
    configure(settings)
    value, _ = bundle()
    payload = value.model_dump(mode="json")
    payload["nodes"][0]["properties"] = ["not-a-metadata-object"]
    graph = original_record("invalid-graph", payload, source="reviewed_graph", kind="graph")
    source.records = [graph]
    source.enabled.add("reviewed_graph")
    database = factory().engine.database
    with httpx.Client(
        base_url="http://synthetic.invalid", transport=httpx.MockTransport(source.handler)
    ) as client:
        reader = FabricHistoryReader(
            client, authorization_ref=GRANT, demo_source_instance=NAMESPACE, max_seconds=90,
        )

        def make():
            engine = EngineService(database, settings)
            engine.history_source_reader = reader.sources
            return MonitorService(engine, reader)

        make().tick()
        result = make().tick()
        assert result == {"state": "source_unavailable", "error_code": "monitor_graph_bundle_invalid"}
        for _ in range(2):
            make().tick()
        with Session(database) as session:
            state = session.get(MonitorState, make().actor.workspace_id)
            progress = state.source_progress[make().stream_key("reviewed_graph")]
            assert progress["state"] == "unavailable"
            assert progress["error_code"] == "monitor_graph_bundle_invalid"
            assert progress.get("acked_after", 0) == progress.get("scan_after", 0) == 0
            assert not progress.get("active_batch_id")
            assert session.scalar(select(func.count()).select_from(MonitorBatch)) == 0
            assert session.scalar(select(func.count()).select_from(EngineRun)) == 0
        graph_polls = [
            dict(params) for path, params in source.calls
            if path == "/api/history/changes" and dict(params)["source_id"] == "reviewed_graph"
        ]
        assert len(graph_polls) == 2 and all(params["after"] == "0" for params in graph_polls)
        assert source.records == [graph]
