"""Explicit feature scopes and complete monitor cohorts; synthetic in-memory HTTP only."""

import copy
import hashlib
import json

import httpx
import pytest
from bioradar.adapters import weknora_kdd
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.mining.kdd import run_discovery
from bioradar.research.mining.models import DiscoveryPolicy, EngineRecord, MiningPolicy
from bioradar.research.monitor.service import MonitorService
from pydantic import ValidationError
from test_weknora_kdd import AS_OF, feature_response, fixed_snapshot

pytestmark = pytest.mark.ENGINE
DIRECTIONS = ("research", "pipeline", "business", "public_health")


def cohort(sources, *, is_demo=False):
    original = fixed_snapshot()
    rows = []
    for index, source in enumerate(sources):
        value = original.records[0].model_dump(mode="json")
        text = f"SYNTHETIC fixed material {index} for source-bound vector population."
        value.update(
            source_id=source,
            normalized_text=text,
            normalized_text_hash=None,
            document_hash=hashlib.sha256(text.encode()).hexdigest(),
            ingestion_mode="fixture" if is_demo else "sample_export",
        )
        value["record_ref"]["object_id"] = json.dumps([source, f"synthetic-{index}", is_demo])
        rows.append(EngineRecord.model_validate(value))
    return original.model_copy(update={"records": tuple(rows), "is_demo": is_demo})


def monitor(**values):
    value = MonitorService.__new__(MonitorService)
    value.settings = Settings(
        environment="test",
        weknora_kdd_url="http://synthetic.invalid",
        weknora_kdd_token="synthetic-no-network-token",
        weknora_kdd_knowledge_base_id="SYNTHETIC-real-kb",
        weknora_kdd_demo_knowledge_base_id="SYNTHETIC-demo-kb",
        weknora_kdd_demo_source_scope="SYNTHETIC-simulation-scope",
        **values,
    )
    return value


def mock_features(monkeypatch, data, requests, *, change=None, status=200):
    """Exercise the existing adapter guards rather than synthesizing adopted embeddings."""
    by_identity = {
        tuple(weknora_kdd._member(row, is_demo=data.is_demo).values()): row for row in data.records
    }
    original = weknora_kdd.features_for_snapshot

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if status != 200:
            return httpx.Response(status)
        reply = feature_response(fixed_snapshot())
        features = []
        for member in body["members"]:
            row = by_identity[tuple(member.values())]
            feature = copy.deepcopy(reply["features"][0])
            feature.update(
                identity=member,
                source_scope=body["source_scope"],
                is_demo=body.get("is_demo", False),
                text=row.normalized_text,
                content_sha256=row.normalized_text_hash,
            )
            features.append(feature)
        reply.update(requested_count=len(body["members"]), returned_count=len(features), features=features)
        if change:
            change(reply)
        return httpx.Response(200, json=reply)

    def features(settings, snapshot, *, direction):
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            return original(settings, snapshot, direction=direction, client=client)

    monkeypatch.setattr(weknora_kdd, "features_for_snapshot", features)


def test_demo_discovery_reads_each_fixed_document_once_in_authorized_scope_without_direction_mapping(
    monkeypatch,
):
    sources = ["SYNTHETIC-research", "SYNTHETIC-business", "SYNTHETIC-unclassified"]
    data = cohort([sources[index % 3] for index in range(101)] + ["SYNTHETIC-graph"], is_demo=True)
    before = data.model_dump(mode="json")
    service = monitor(monitor_kdd_directions={sources[0]: "research", sources[1]: "business"})
    requests = []
    mock_features(monkeypatch, data, requests)
    enriched, receipt = service._feature_snapshot(
        data,
        {"evidence": [{"record_kind": "graph", "ref_key": list(data.records[-1].record_ref.version_key)}]},
    )
    assert [len(item["members"]) for item in requests] == [100, 1]
    members = [member for item in requests for member in item["members"]]
    expected = [weknora_kdd._member(row, is_demo=True) for row in data.records[:-1]]
    assert members == expected and len({tuple(item.values()) for item in members}) == 101
    assert all(item["knowledge_base_id"] == "SYNTHETIC-demo-kb" for item in requests)
    assert all(item["source_scope"] == "SYNTHETIC-simulation-scope" for item in requests)
    assert all(item["is_demo"] is True for item in requests)
    assert all(row.embedding is not None for row in enriched.records[:-1])
    assert enriched.records[-1] == data.records[-1]
    assert [row.record_ref for row in enriched.records] == [row.record_ref for row in data.records]
    assert receipt["record_count"] == 102 and receipt["adopted_count"] == 101
    assert receipt["groups"][0]["source_scope"] == "SYNTHETIC-simulation-scope"
    assert "direction" not in receipt["groups"][0]
    assert receipt["excluded"][0]["reason"] == "graph_package_is_not_document_embedding_unit"
    result = run_discovery(enriched, DiscoveryPolicy(as_of=AS_OF, discovery_directions=DIRECTIONS))
    vectors = next(item for item in result.method_results if item.method_id == "research_embedding_clusters")
    assert vectors.readiness == "exploratory_only" and vectors.execution_status == "success"
    assert len(vectors.statistics["model_spaces"][0]["processed_unit_ids"]) == 101
    assert data.model_dump(mode="json") == before


def test_real_scope_map_is_independent_of_classification_and_never_falls_back_to_demo(monkeypatch):
    data = cohort(["SYNTHETIC-unclassified", "SYNTHETIC-classified-only"])
    service = monitor(
        monitor_kdd_directions={"SYNTHETIC-classified-only": "research"},
        weknora_kdd_feature_source_scopes={"SYNTHETIC-unclassified": "SYNTHETIC-authorized-scope"},
    )
    requests = []
    mock_features(monkeypatch, data, requests)
    enriched, receipt = service._feature_snapshot(data, {})
    assert len(requests) == 1 and len(requests[0]["members"]) == 1
    assert requests[0]["knowledge_base_id"] == "SYNTHETIC-real-kb"
    assert requests[0]["source_scope"] == "SYNTHETIC-authorized-scope"
    assert "is_demo" not in requests[0]
    assert enriched.records[0].embedding is not None
    assert enriched.records[1] == data.records[1]
    assert receipt["record_count"] == 2 and receipt["adopted_count"] == 1
    assert receipt["excluded"][0]["reason"] == "feature_source_scope_not_configured"
    service.settings.weknora_kdd_feature_source_scopes = {}
    requests.clear()
    original, blocked = service._feature_snapshot(data, {})
    assert not requests and original.records == data.records
    assert blocked["adopted_count"] == 0 and len(blocked["excluded"]) == 2


@pytest.mark.parametrize("is_demo", [False, True])
def test_missing_selected_kb_never_uses_other_population_kb(monkeypatch, is_demo):
    data = cohort(["SYNTHETIC-doc"], is_demo=is_demo)
    service = monitor(weknora_kdd_feature_source_scopes={"SYNTHETIC-doc": "SYNTHETIC-scope"})
    name = "weknora_kdd_demo_knowledge_base_id" if is_demo else "weknora_kdd_knowledge_base_id"
    setattr(service.settings, name, None)
    requests = []
    mock_features(monkeypatch, data, requests)
    enriched, receipt = service._feature_snapshot(data, {})
    assert not requests and enriched.records == data.records
    assert receipt["excluded"][0]["reason"] == "weknora_kdd_not_configured"


@pytest.mark.parametrize("invalid", ["member_version", "raw_hash", "scope", "population", "text", "model"])
def test_monitor_keeps_full_fixed_members_when_adapter_rejects_feature_binding(monkeypatch, invalid):
    data = cohort(["SYNTHETIC-doc", "SYNTHETIC-doc"])
    service = monitor(weknora_kdd_feature_source_scopes={"SYNTHETIC-doc": "SYNTHETIC-scope"})

    def change(reply):
        feature = reply["features"][0]
        if invalid == "member_version":
            feature["identity"]["version_id"] = "9" * 64
        elif invalid == "raw_hash":
            feature["identity"]["raw_sha256"] = "9" * 64
        elif invalid == "scope":
            feature["source_scope"] = "SYNTHETIC-another-scope"
        elif invalid == "population":
            reply["population"] = "search_top_k"
        elif invalid == "text":
            feature["text"] = "SYNTHETIC another original version"
        else:
            feature["embedding_provenance"]["model_version"] = None

    requests = []
    mock_features(monkeypatch, data, requests, change=change)
    enriched, receipt = service._feature_snapshot(data, {})
    assert len(requests) == 1 and len(requests[0]["members"]) == 2
    assert [row.record_ref for row in enriched.records] == [row.record_ref for row in data.records]
    assert enriched.records[0] == data.records[0]
    if invalid in {"text", "model"}:
        assert enriched.records[1].embedding is not None
        assert receipt["adopted_count"] == 1
    else:
        assert enriched.records == data.records and receipt["adopted_count"] == 0
    assert receipt["record_count"] == len(data.records) == 2 and receipt["excluded"]


def test_server_rejected_scope_is_recorded_without_retry_or_alternate_kb(monkeypatch):
    data = cohort(["SYNTHETIC-doc"])
    service = monitor(weknora_kdd_feature_source_scopes={"SYNTHETIC-doc": "SYNTHETIC-scope"})
    requests = []
    mock_features(monkeypatch, data, requests, status=403)
    enriched, receipt = service._feature_snapshot(data, {})
    assert len(requests) == 1 and enriched.records == data.records
    assert receipt["excluded"][0]["reason"] == "weknora_kdd_http_error"
    assert receipt["groups"][0]["knowledge_base_id"] == "SYNTHETIC-real-kb"


@pytest.mark.parametrize("mapping", [{"": "scope"}, {"source": " "}, {"source": " scope"}])
def test_feature_scope_map_rejects_ambiguous_empty_or_padded_identifiers(mapping):
    with pytest.raises(ValidationError):
        Settings(weknora_kdd_feature_source_scopes=mapping)


def test_legacy_mining_and_revoked_snapshot_never_start_feature_http():
    data = fixed_snapshot()
    service = monitor()
    service.actor = object()
    calls = []
    service._saved_response = lambda operation, key: None
    service._save_batch = lambda identifier, **values: None
    service._feature_snapshot = lambda *args: pytest.fail("unexpected feature request")

    def reject(actor, snapshot):
        raise DomainError("source_not_authorized", "SYNTHETIC revoked source", 403)

    from types import SimpleNamespace

    service.engine = SimpleNamespace(
        _authorize_snapshot=reject,
        adopt_snapshot=lambda *args: (201, {"id": "SYNTHETIC-snapshot"}),
        create_run=lambda *args: (202, {"id": "SYNTHETIC-run"}),
        dispatch=lambda identifier: calls.append(identifier),
    )
    payload = {
        "snapshot": data.model_dump(mode="json"),
        "policy": MiningPolicy(as_of=AS_OF).model_dump(mode="json"),
    }
    service._adopt_and_queue("SYNTHETIC-legacy-batch", payload)
    assert calls == ["SYNTHETIC-run"]
    payload["policy"] = DiscoveryPolicy(as_of=AS_OF, discovery_directions=DIRECTIONS).model_dump(mode="json")
    with pytest.raises(DomainError, match="SYNTHETIC revoked source"):
        service._adopt_and_queue("SYNTHETIC-revoked-batch", payload)
    assert calls == ["SYNTHETIC-run"]
