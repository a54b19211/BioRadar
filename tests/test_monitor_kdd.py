"""KDD uses the existing monitor/run/investigation path; synthetic HTTP only."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.fabric_graph import project_graph_hints
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.api.errors import DomainError
from bioradar.research.engine.models import EngineRun
from bioradar.research.investigations.graph import GraphBundle
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.kdd import run_kdd
from bioradar.research.mining.models import (
    Embedding,
    KddPolicy,
    ResourceLimits,
    SectionProvenance,
)
from bioradar.research.monitor.models import MonitorBatch, MonitorLead
from bioradar.research.monitor.service import MonitorService
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_engine_persistence import GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_graph import bundle, original_record
from test_kdd_mining import AS_OF, metric_record, policy
from test_mining import snapshot
from test_monitor import finish_pending
from test_monitor import runtime as runtime  # noqa: F401

pytestmark = pytest.mark.ENGINE


def monitor_stub(mapping=None):
    value = MonitorService.__new__(MonitorService)
    value.settings = SimpleNamespace(engine_max_records=2000, monitor_kdd_directions=mapping or {})
    return value


def test_direction_policy_is_explicit_and_unconfigured_branch_discovers_all_domains():
    monitor = monitor_stub({"who_don": "public_health"})
    current = monitor._mining_policy("who_don", AS_OF)
    old = monitor._mining_policy("unconfigured", AS_OF)
    assert isinstance(current, KddPolicy) and current.kdd_direction == "public_health"
    from bioradar.research.mining.models import DiscoveryPolicy

    assert type(old) is DiscoveryPolicy
    expected = DiscoveryPolicy(
        discovery_directions=("research", "pipeline", "business", "public_health"),
        as_of=AS_OF,
        discovery_mode="stock_and_incremental",
        limits=ResourceLimits(max_records=2000, review_capacity=12, max_candidates_per_method=30),
    )
    assert old.model_dump(mode="json") == expected.model_dump(mode="json")


def test_live_monitor_uses_explicit_source_instance_for_source_permissions():
    monitor = monitor_stub({"who_don": "public_health"})
    monitor.settings.monitor_is_demo = False
    monitor.settings.data_fabric_source_instance = "datafabric.isolated-kdd"
    monitor.settings.engine_source_id_allowlist = {"datafabric.isolated-kdd": ["who_don"]}
    monitor.actor = SimpleNamespace(source_allowlist=("datafabric.isolated-kdd",))
    monitor._catalog = lambda: {"who_don", "clinicaltrials"}
    assert monitor.permitted() == {"who_don"}
    monitor.actor = SimpleNamespace(source_allowlist=("datafabric-test",))
    assert monitor.permitted() == set()
    monitor.settings.monitor_is_demo = True
    monitor.settings.engine_source_id_allowlist = {"fixture": ["who_don"]}
    monitor.actor = SimpleNamespace(source_allowlist=("fixture",))
    assert monitor.permitted() == {"who_don"}


def graph_hint_fixture():
    fixed, document = bundle()
    raw = fixed.model_dump(mode="json")
    for edge in raw["edges"]:
        edge["review"] = {"status": "pending", "reviewed_by": None, "reviewed_at": None}
    fixed = GraphBundle.model_validate(raw)
    original = original_record("synthetic-graph", raw, source="reviewed_graph", kind="graph")
    requests = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(500))
    )
    reader = FabricHistoryReader(client, authorization_ref=GRANT)
    hinted = project_graph_hints(reader, original, fixed, documents=(document,))
    return reader, client, hinted, original, document, requests


def test_fixed_pending_graph_hints_generate_hypotheses_and_actual_original_search_plans():
    reader, client, hinted, original, document, requests = graph_hint_fixture()
    try:
        assert not requests
        assert all(hint.review_status == "pending" for hint in hinted.graph_hints)
        assert all(hint.originals_validated for hint in hinted.graph_hints)
        data = reader.snapshot(
            {"items": [original], "source_id": "reviewed_graph", "is_demo": True, "coverage": {}}
        ).model_copy(update={"records": (hinted,)})
        result = run_kdd(data, KddPolicy(as_of=AS_OF, kdd_direction="pipeline"))
        assert result.candidates
        assert result.data_preparation["denominator"]["count"] == 0
        assert all(candidate.method_id == "pipeline_graph_hints" for candidate in result.candidates)
        assert all(candidate.kind == "hypothesis" for candidate in result.candidates)
        assert all(candidate.metrics["review_status"] == "pending" for candidate in result.candidates)
        assert all(candidate.metrics["project_count"] is None for candidate in result.candidates)
        monitor = monitor_stub({"reviewed_graph": "pipeline"})
        monitor.permitted = lambda: {"reviewed_graph", "fixture-source"}
        plan = monitor._candidate_followup_plan({"evidence": []}, result.candidates[0])
        assert plan and all(value["source_id"] == "fixture-source" for value in plan)
        assert all(value["term"] in result.candidates[0].metrics["query_terms"] for value in plan)
        assert all(value["review_status"] == "pending" for value in plan)
        question = monitor._candidate_question(result.candidates[0], "pipeline")
        assert "管线与竞争" in question and "图关系仅是待补证线索" in question
    finally:
        client.close()


def test_kdd_selection_summary_retains_value_and_capacity_reasons_for_unselected_patterns():
    rows = [
        metric_record(f"{company}-{period}", 100 if period == 0 else 200, period, subject=company)
        for company in ("a", "b", "c")
        for period in (0, 1)
    ]
    data = snapshot(rows)
    fixed_policy = policy("business", limits=ResourceLimits(review_capacity=1, max_candidates_per_method=1))
    result = run_kdd(data, fixed_policy)
    by_native = {row.record_ref.object_id: row.record_ref for row in rows}
    monitor = monitor_stub()
    monitor.reader = SimpleNamespace(ref=lambda row: by_native[row["source_record_id"]])
    payload = {
        "manifest": [
            {
                "source_id": row.source_id,
                "source_record_id": row.record_ref.object_id,
                "version_id": row.record_ref.adopted_version,
            }
            for row in rows
        ],
        "snapshot": data.model_dump(mode="json"),
        "policy": fixed_policy.model_dump(mode="json"),
        "limitations": [],
    }
    summary = monitor._selection_summary(payload, result, result.candidates)
    assert len(summary["methods"][0]["generated_candidate_keys"]) == 3
    assert len(summary["methods"][0]["pattern_evaluations"]) == 3
    assert (
        sum(
            value["decision"] == "capacity_deferred" for value in summary["methods"][0]["selection_decisions"]
        )
        == 2
    )
    assert any(
        row["reason"] == "value_eligible_pattern_deferred_by_review_capacity"
        for row in summary["record_outcomes"]
    )


def test_who_kdd_monitor_accepts_runs_and_starts_frozen_direction_analysis(runtime, monkeypatch):
    make, source, settings = runtime
    settings.monitor_kdd_directions = {"who_don": "public_health"}
    settings.engine_source_id_allowlist = {"fixture": ["fixture-source", "who_don"]}
    source.enabled = {"who_don"}
    source.records = [
        original_record(
            "SYNTHETIC-DON-1",
            {
                "body": "SYNTHETIC official notice body, not real public health evidence.",
                "native_notice_id": "SYNTHETIC-native-notice",
            },
            source="who_don",
        )
    ]
    make().tick()
    finish_pending(make)
    captured = []
    from bioradar.research.investigations import analysis_service

    monkeypatch.setattr(analysis_service, "semantic_enabled", lambda settings: True)

    def capture_start(self, actor, identifier, request, key, **kwargs):
        captured.append(request.model_dump(mode="json"))
        return 202, {"id": "synthetic-analysis-boundary-no-model"}

    monkeypatch.setattr(analysis_service.AnalysisService, "start", capture_start)
    make().tick()
    with Session(make().engine.database) as session:
        run = session.scalar(select(EngineRun))
        batch = session.scalar(select(MonitorBatch))
        lead = session.scalar(select(MonitorLead))
        assert run.policy["kdd_direction"] == "public_health"
        assert run.result["compute_version"] == "bioradar-mining-compute-v0.4"
        assert batch.state == "completed"
        assert lead.payload["kdd_direction"] == "public_health"
        assert lead.payload["candidate"]["method_id"] == "public_health_notifications"
        assert lead.payload["candidate"]["metrics"]["new_epidemic_claim_permitted"] is False
        assert lead.payload["analysis_intent"]["body"]["kdd_direction"] == "public_health"
        investigation_id = lead.investigation_id
    assert len(captured) == 1
    assert captured[0]["kdd_direction"] == "public_health"
    assert captured[0]["auto_retrieve"] is True
    view = InvestigationService(make().engine).get(make().actor, investigation_id)
    assert captured[0]["question"] == view.question
    assert "公共卫生" in view.question and "不是病例数或发病率" in view.question
    make().tick()
    assert len(captured) == 1


def test_pending_graph_kdd_monitor_enters_real_lead_and_original_followup(runtime, monkeypatch):
    make, source, settings = runtime
    reader, client, hinted, original, document, requests = graph_hint_fixture()
    client.close()
    settings.monitor_kdd_directions = {"reviewed_graph": "pipeline"}
    settings.engine_source_id_allowlist = {"fixture": ["fixture-source", "reviewed_graph"]}
    source.enabled = {"reviewed_graph", "fixture-source"}
    source.records = [original, document]

    def project(self, value):
        fixed = GraphBundle.model_validate_json(value["raw_content"])
        result = project_graph_hints(self.reader, value, fixed, documents=(document,))
        docs = self.reader.snapshot(
            {"items": [document], "source_id": document["source_id"], "is_demo": True, "coverage": {}}
        )
        return [result, *docs.records], ["synthetic_fixed_pending_graph_for_original_verification"]

    monkeypatch.setattr(MonitorService, "_graph_projection", project)
    monitor = make()
    assert monitor.claim()
    try:
        batch_id = monitor.accept_page("reviewed_graph")
        monitor.advance(batch_id)
        finish_pending(make)
        monitor.advance(batch_id)
    finally:
        monitor.release()
    with Session(make().engine.database) as session:
        batch = session.get(MonitorBatch, batch_id)
        leads = list(session.scalars(select(MonitorLead).where(MonitorLead.batch_id == batch_id)))
        assert batch.state == "completed" and leads
        assert all(lead.payload["candidate"]["method_id"] == "pipeline_graph_hints" for lead in leads)
        assert all(lead.payload["candidate"]["metrics"]["review_status"] == "pending" for lead in leads)
        assert any(lead.payload["queries"] for lead in leads)
    assert any(
        path == "/api/history/records"
        and any(key == "terms" and value.startswith("SYNTHETIC") for key, value in params)
        for path, params in source.calls
    )


@pytest.mark.parametrize("fail_pipeline", [False, True])
def test_feature_adoption_groups_full_fixed_scope_and_preserves_graph_and_failures(
    monkeypatch, fail_pipeline
):
    from test_weknora_kdd import fixed_snapshot

    original = fixed_snapshot()
    rows = []
    for index, source in enumerate(("research-source", "pipeline-source", "graph-source", "unconfigured")):
        base = original.records[0]
        ref = base.record_ref.model_copy(update={"object_id": json.dumps([source, f"native-{index}", False])})
        rows.append(base.model_copy(update={"record_ref": ref, "source_id": source}))
    data = original.model_copy(update={"records": tuple(rows)})
    before = copy.deepcopy(data.model_dump(mode="json"))
    monitor = monitor_stub(
        {"research-source": "research", "pipeline-source": "pipeline", "graph-source": "pipeline"}
    )
    observed = []

    def features(settings, scope, *, direction):
        observed.append((direction, [row.source_id for row in scope.records]))
        if fail_pipeline and direction == "pipeline":
            raise DomainError("synthetic_feature_unavailable", "synthetic unavailable", 502)
        records = tuple(
            row.model_copy(
                update={
                    "embedding": Embedding(
                        provider="fixture",
                        model_version="synthetic-v1",
                        dimension=2,
                        vector=(1, 0),
                        text_hash=row.normalized_text_hash,
                        provenance=SectionProvenance(
                            schema_version="synthetic-test-only", provider_ref=row.record_ref
                        ),
                    )
                }
            )
            for row in scope.records
        )
        return scope.model_copy(update={"records": records}), {
            "adopted": [row.record_ref.object_id for row in records],
            "excluded": [],
        }

    monkeypatch.setattr("bioradar.adapters.weknora_kdd.features_for_snapshot", features)
    enriched, receipt = monitor._feature_snapshot(
        data, {"evidence": [{"record_kind": "graph", "ref_key": list(rows[2].record_ref.version_key)}]}
    )
    assert observed == [("pipeline", ["pipeline-source"]), ("research", ["research-source"])]
    assert [row.record_ref for row in enriched.records] == [row.record_ref for row in data.records]
    assert enriched.records[0].embedding is not None
    assert (enriched.records[1].embedding is None) is fail_pipeline
    assert enriched.records[2:] == data.records[2:]
    assert receipt["record_count"] == len(data.records) == 4
    assert receipt["adopted_count"] == (1 if fail_pipeline else 2)
    assert receipt["model_token_usage"] is None
    assert any(
        item["reason"] == "graph_package_is_not_document_embedding_unit" for item in receipt["excluded"]
    )
    if fail_pipeline:
        assert "weknora_feature:synthetic_feature_unavailable" in enriched.metadata_gaps
    assert data.model_dump(mode="json") == before


def test_adopted_feature_snapshot_recovery_does_not_repeat_provider_read():
    from test_weknora_kdd import fixed_snapshot

    snapshot = fixed_snapshot()
    monitor = monitor_stub({"synthetic_source": "research"})
    monitor.settings.weknora_kdd_url = "http://synthetic.invalid"
    monitor.actor = SimpleNamespace()
    saved = {}
    payload = {
        "snapshot": snapshot.model_dump(mode="json"),
        "policy": policy("research").model_dump(mode="json"),
    }
    calls = []

    def features(current, body):
        calls.append("features")
        return current, {"adopted_count": 0, "model_token_usage": None}

    def adopt(actor, current, key):
        calls.append("adopt")
        saved[("snapshot.create", key)] = {"id": "synthetic-snapshot"}
        return 201, saved[("snapshot.create", key)]

    def create(actor, request, key):
        calls.append("create")
        saved[("run.create", key)] = {"id": "synthetic-run"}
        return 202, saved[("run.create", key)]

    monitor._feature_snapshot = features
    monitor._saved_response = lambda operation, key: saved.get((operation, key))
    monitor._save_batch = lambda identifier, **values: saved.update(values)
    monitor.engine = SimpleNamespace(
        adopt_snapshot=adopt, create_run=create, dispatch=lambda identifier: None
    )
    monitor._adopt_and_queue("synthetic-batch", payload)
    assert "feature_adoption" in saved["payload"]
    monitor._adopt_and_queue("synthetic-batch", saved["payload"])
    assert calls == ["features", "adopt", "create"]


def test_kdd_projection_uses_fabric_fixed_text_and_legacy_keeps_abstract():
    import hashlib

    from bioradar.adapters.fabric_monitor import project_record
    from bioradar.research.mining.models import canonical_hash

    value = original_record(
        "SYNTHETIC-source-text",
        {"body": "SYNTHETIC fixed full Fabric text.", "abstract": "SYNTHETIC legacy short abstract."},
        source="europe_pmc",
        demo=False,
    )
    value["clean_text"] = "SYNTHETIC fixed full Fabric text."
    before = copy.deepcopy(value)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        reader = FabricHistoryReader(client, authorization_ref=GRANT)
        legacy = project_record(reader, value, as_of=AS_OF)
        current = project_record(reader, value, as_of=AS_OF, kdd=True)
    assert legacy.normalized_text == value["facts"]["abstract"]
    assert current.normalized_text == value["clean_text"]
    assert current.normalized_text_hash == hashlib.sha256(value["clean_text"].encode()).hexdigest()
    assert current.record_ref == legacy.record_ref
    assert current.normalization_version == legacy.normalization_version == value["parser_version"]
    assert canonical_hash(current) != canonical_hash(legacy)
    assert value == before
