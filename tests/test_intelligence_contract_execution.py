"""SYNTHETIC new frozen contracts, existing host ledger and actual PG/RQ persistence."""

import copy

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.datasets.build import build_dataset, dataset_version
from bioradar.research.datasets.narrative import narrative_extraction_schema
from bioradar.research.datasets.narrative_proposal import PROPOSAL_VERSION, proposal_payload, proposal_schema
from bioradar.research.datasets.narrative_v2 import selection_payload
from bioradar.research.investigations.kdd import policy_for
from bioradar.research.investigations.narrative_execution import prepare_narrative_snapshot
from bioradar.research.mining.models import KddPolicy, canonical_hash
from test_engine_persistence import database as database  # noqa: F401
from test_mining import snapshot
from test_monitor import runtime as runtime  # noqa: F401
from test_narrative_observations_v2 import VERSION, Host, fixed, separate_header, source, span

pytestmark = pytest.mark.ENGINE
SOURCE_VERSION = "public-health-source-intelligence-v1"


def test_new_proposal_uses_host_call_and_restores_exact_qualified_rows_without_call():
    text, row = separate_header()
    original = snapshot([source(text)])
    data = fixed()
    data["query_policy"]["narrative_proposal_version"] = PROPOSAL_VERSION
    host, progress = Host({"observations": [row]}), {}
    prepared = prepare_narrative_snapshot(host, "id", "lease", data, progress, original, allow_model=True)
    assert host.posts == 1 and prepared.records[0].metric_observations
    schema, payload, options = host.inputs[0]
    assert schema == proposal_schema() and payload == proposal_payload(original.records[0])
    expected_key = canonical_hash(
        [
            original.records[0].record_ref.model_dump(mode="json"),
            original.records[0].normalized_text_hash,
            VERSION,
            PROPOSAL_VERSION,
        ]
    )
    assert options["call_key"] == "narrative:" + expected_key
    assert progress["narrative_extractions"][expected_key]["proposal_version"] == PROPOSAL_VERSION
    restored = prepare_narrative_snapshot(
        host, "id", "lease", data, copy.deepcopy(progress), original, allow_model=True
    )
    assert host.posts == 1 and restored == prepared
    assert (
        build_dataset(prepared, policy_for(data)).manifest()
        == build_dataset(restored, policy_for(data)).manifest()
    )
    assert not original.records[0].metric_observations

    # Omitting the new flag retains the exact old v2 request and call identity.
    old_host, old_progress = Host({"observations": [row]}), {}
    prepare_narrative_snapshot(old_host, "old", "lease", fixed(), old_progress, original, allow_model=True)
    assert old_host.inputs[0][0] == narrative_extraction_schema(version=VERSION)
    assert old_host.inputs[0][1] == selection_payload(original.records[0])
    old_key = canonical_hash(
        [
            original.records[0].record_ref.model_dump(mode="json"),
            original.records[0].normalized_text_hash,
            VERSION,
        ]
    )
    assert set(old_progress["narrative_extractions"]) == {old_key}
    assert "proposal_version" not in old_progress["narrative_extractions"][old_key]


@pytest.mark.parametrize("version", ["unknown-proposal-version", ""])
def test_unknown_proposal_is_rejected_before_any_send(version):
    text, _ = separate_header()
    data, host, progress = fixed(), Host({"observations": []}), {}
    data["query_policy"]["narrative_proposal_version"] = version
    with pytest.raises(DomainError, match="说明版本"):
        prepare_narrative_snapshot(
            host, "id", "lease", data, progress, snapshot([source(text)]), allow_model=True
        )
    assert host.posts == 0 and not progress


def test_new_proposal_keeps_invalid_rows_and_known_budget_stops_without_fabricating_metrics():
    text, row = separate_header()
    row["context"] = span(text, "净订单")
    data = fixed()
    data["query_policy"]["narrative_proposal_version"] = PROPOSAL_VERSION
    host, progress = Host({"observations": [row]}), {}
    output = prepare_narrative_snapshot(
        host, "id", "lease", data, progress, snapshot([source(text)]), allow_model=True
    )
    receipt = next(iter(progress["narrative_extractions"].values()))
    assert not output.records[0].metric_observations
    assert receipt["status"] == "partial" and receipt["gaps"]
    for code in ("analysis_model_budget", "analysis_token_budget", "analysis_time_budget"):
        host, progress = Host(DomainError(code, "SYNTHETIC exhausted", 409)), {}
        output = prepare_narrative_snapshot(
            host, "id", "lease", data, progress, snapshot([source(text)]), allow_model=True
        )
        receipt = next(iter(progress["narrative_extractions"].values()))
        assert receipt["status"] == "not_run" and receipt["reason"] == code
        assert receipt["proposal_version"] == PROPOSAL_VERSION
        prepare_narrative_snapshot(
            host, "id", "lease", data, progress, snapshot([source(text)]), allow_model=True
        )
        assert host.posts == 1 and not output.records[0].metric_observations


def test_source_contract_is_frozen_and_cannot_reinterpret_a_saved_legacy_policy():
    data = fixed()
    data["query_policy"]["direction"] = "public_health"
    old = policy_for(data)
    assert "source_intelligence_version" not in old.model_dump(mode="json")
    assert dataset_version(old) == "bioradar-analysis-dataset-v1"
    data["query_policy"]["source_intelligence_version"] = SOURCE_VERSION
    current = policy_for(data)
    assert current.source_intelligence_version == SOURCE_VERSION
    assert dataset_version(current) == "bioradar-analysis-dataset-v2"
    data["kdd_mining_policy"] = old.model_dump(mode="json")
    with pytest.raises(DomainError) as raised:
        policy_for(data)
    assert raised.value.code == "analysis_dataset_scope_changed"
    data["kdd_mining_policy"] = current.model_dump(mode="json")
    assert policy_for(data) == current
    assert KddPolicy.model_validate(current.model_dump(mode="json")) == current


def test_public_health_source_material_survives_native_queue_database_and_duplicate_dispatch(
    runtime, monkeypatch
):
    import os
    from uuid import uuid4

    from bioradar.research.engine import worker
    from bioradar.research.engine.models import EngineRun
    from bioradar.research.monitor.models import MonitorLead
    from pydantic import SecretStr
    from redis import Redis
    from rq import Queue, SimpleWorker
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from test_monitor import record

    factory, upstream, settings = runtime
    redis_url = os.environ.get("BIORADAR_ENGINE_TEST_REDIS_URL")
    assert redis_url, "Dedicated actual Redis is required"
    settings.redis_url = SecretStr(redis_url)
    settings.queue_prefix = "source-intelligence-synthetic-" + uuid4().hex
    settings.monitor_kdd_directions = {"fixture-source": "public_health"}
    settings.monitor_hourly_investigations = 0
    upstream.records = [
        record(1, "SYNTHETIC 医疗提醒：就医时请遵循发热分诊和院内感染防控安排。", seq=1),
    ]
    monitor = factory(legacy=False)
    original_engine = worker.EngineService

    class SourceBoundEngine(original_engine):
        def __init__(self, *args):
            super().__init__(*args)
            self.history_source_reader = monitor.reader.sources

    monkeypatch.setattr(worker, "EngineService", SourceBoundEngine)
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "create_database", lambda _: monitor.engine.database)
    connection = Redis.from_url(redis_url)
    queue = Queue(settings.queue_prefix + ":routine", connection=connection)
    try:
        monitor.tick()
        assert queue.count == 1
        SimpleWorker([queue], connection=connection).work(burst=True, logging_level="CRITICAL")
        with Session(monitor.engine.database) as session:
            row = session.scalar(select(EngineRun))
            assert row.state in {"completed", "partial"}, (row.error_code, row.error_message)
            run_id, before = row.id, copy.deepcopy(row.result)
            assert row.policy["source_intelligence_version"] == SOURCE_VERSION
            prep = before["data_preparation"]
            assert prep["source_statement_count"] == 1 and prep["quantitative_qualified_count"] == 0
            assert not prep["excluded_records"] and prep["quantitative_exclusions"]
            result = next(
                m for m in before["method_results"] if m["method_id"] == "public_health_source_claims"
            )
            retained = result["statistics"]["retained_source_units"]
            assert retained and retained[0]["members"][0]["record_ref"]["adopted_version"]
            assert not before["candidates"]
        # Existing dispatcher and worker see the completed durable run; no duplicate work.
        monitor.engine.dispatch(run_id)
        SimpleWorker([queue], connection=connection).work(burst=True, logging_level="CRITICAL")
        factory(legacy=False).tick()
        with Session(monitor.engine.database) as session:
            assert session.get(EngineRun, run_id).result == before
            assert len(list(session.scalars(select(EngineRun)))) == 1
            assert not list(session.scalars(select(MonitorLead)))
    finally:
        queue.delete(delete_jobs=True)
        connection.close()


def test_new_saved_source_dataset_passes_existing_content_hash_verifier_and_tampering_fails():
    import importlib.util
    from pathlib import Path

    from bioradar.research.mining.runner import run_mining
    from test_mining import AS_OF
    from test_opus_narrative import prose

    path = Path(__file__).resolve().parents[1] / "scripts/query_policy_assertions.py"
    spec = importlib.util.spec_from_file_location("intelligence_contract_verifier", path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    result = run_mining(
        snapshot([prose("SYNTHETIC-source-version", "SYNTHETIC医疗提醒：就医请先发热分诊。")]),
        KddPolicy(as_of=AS_OF, kdd_direction="public_health", source_intelligence_version=SOURCE_VERSION),
    ).model_dump(mode="json")
    verifier._pattern(result)
    result["data_preparation"]["units"][0]["members"][0]["text_hash"] = "changed"
    with pytest.raises(AssertionError):
        verifier._pattern(result)


def test_complete_dataset_payload_binds_extractions_and_replays_through_analysis_action(monkeypatch):
    import importlib.util
    from pathlib import Path

    from bioradar.research.investigations import kdd
    from bioradar.research.mining.kdd import kdd_versions

    path = Path(__file__).resolve().parents[1] / "scripts/query_policy_assertions.py"
    spec = importlib.util.spec_from_file_location("complete_dataset_verifier", path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    text, row = separate_header()
    data = fixed()
    data["query_policy"]["narrative_proposal_version"] = PROPOSAL_VERSION
    data["budget"] = {"max_queries": 4}
    original = snapshot([source(text)])
    host = Host({"observations": [row]})
    host._guard = lambda *args, **kwargs: None
    host._policy_budget = lambda *args, **kwargs: {"remaining": {"tool_calls": 4}}
    monkeypatch.setattr(kdd, "source_snapshot", lambda *args: original)
    origin = {
        "id": "sources:SYNTHETIC",
        "version": "source-fixed-version",
        "kind": "source_documents",
        "scope": {"direction": "business"},
        "snapshot_ids": ["SYNTHETIC-snapshot"],
    }
    state = {"queries": [], "kdd_artifacts": [origin]}
    policy = kdd.policy_for(data)
    action = {
        "id": "dataset:SYNTHETIC-action",
        "kind": "dataset_prepare",
        "arguments": {
            "scope": origin["scope"],
            "capability_version": dataset_version(policy),
            "input_refs": [{"id": origin["id"], "version": origin["version"]}],
        },
    }
    prepared_receipt = kdd.execute_action(host, "id", "lease", data, state, action, 0)
    prepared_ref = prepared_receipt["artifact_refs"][0]
    payload = state["kdd_results"][prepared_ref["id"]]
    assert payload["narrative_extractions"] and host.posts == 1
    assert prepared_ref["version"] == payload["dataset_hash"]
    verifier._dataset(payload)
    computed = {
        "id": "analysis:SYNTHETIC-action",
        "kind": "analysis_run",
        "arguments": {
            "scope": origin["scope"],
            "capability_version": canonical_hash(kdd_versions(policy)),
            "input_refs": [prepared_ref],
        },
    }
    result_receipt = kdd.execute_action(host, "id", "lease", data, state, computed, 1)
    result_ref = result_receipt["artifact_refs"][0]
    result = state["kdd_results"][result_ref["id"]]
    assert result["data_preparation"]["dataset_hash"] == prepared_ref["version"]
    assert result_ref["version"] == result["result_hash"]
    assert result["data_preparation"]["narrative_extractions"] == payload["narrative_extractions"]
    verifier._pattern(result)
    assert host.posts == 1
    saved = copy.deepcopy(state)
    assert kdd.execute_action(host, "id", "lease", data, state, action, 0) == prepared_receipt
    assert kdd.execute_action(host, "id", "lease", data, state, computed, 1) == result_receipt
    assert state == saved and host.posts == 1
    altered = copy.deepcopy(payload)
    altered["narrative_extractions"][0]["status"] = "forged"
    with pytest.raises(AssertionError):
        verifier._dataset(altered)

    # Existing tasks still retain the old manifest identity; never rewrite stored R4 receipts.
    old = fixed()
    manifest = build_dataset(original, kdd.policy_for(old)).manifest()
    legacy = kdd._dataset_payload(copy.deepcopy(manifest), original, old, state)
    assert legacy["dataset_hash"] == manifest["dataset_hash"]
    assert legacy["narrative_extractions"]
