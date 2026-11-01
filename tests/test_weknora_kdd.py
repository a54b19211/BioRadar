"""No-network fixed-vector adapter regressions; every provider response is synthetic."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.weknora_kdd import features_for_snapshot, search_candidates
from bioradar.research.mining.models import EngineRecord, NormalizedSnapshot, ProviderRef
from pydantic import SecretStr

pytestmark = pytest.mark.ENGINE
AS_OF = datetime(2026, 10, 30, tzinfo=UTC)


def fixed_snapshot():
    text = "SYNTHETIC fixed original for no-network adapter regression."
    reference = ProviderRef(
        provider="data_fabric_history",
        object_type="document",
        object_id=json.dumps(["synthetic_source", "synthetic-native-id", False]),
        adopted_version="1" * 64,
        version_source="provider",
        content_hash="2" * 64,
        authorization_ref="synthetic-test-only-sources",
    )
    record = EngineRecord(
        record_ref=reference,
        document_hash=hashlib.sha256(text.encode()).hexdigest(),
        source_instance="synthetic-test-instance",
        source_id="synthetic_source",
        observed_at=AS_OF,
        available_at=AS_OF,
        ingestion_mode="sample_export",
        text_status="inline_authorized",
        normalized_text=text,
        normalization_version="synthetic-identity-v1",
    )
    return NormalizedSnapshot(
        snapshot_id="synthetic-no-network",
        source_instance=record.source_instance,
        observed_at=AS_OF,
        records=(record,),
    )


def settings():
    return SimpleNamespace(
        weknora_kdd_url="http://synthetic.invalid",
        weknora_kdd_token=SecretStr("synthetic-no-network-token"),
        weknora_kdd_knowledge_base_id="synthetic-kb",
    )


def feature_response(snapshot):
    record = snapshot.records[0]
    identity = {
        "source_instance": record.source_instance,
        "source_id": record.source_id,
        "source_record_id": "synthetic-native-id",
        "version_id": record.record_ref.adopted_version,
        "raw_sha256": record.record_ref.content_hash,
    }
    feature = {
        "identity": identity,
        "source_scope": "research",
        "knowledge_id": "synthetic-knowledge",
        "publication_id": "synthetic-publication",
        "index_version": "synthetic-index-v1",
        "status": "ready",
        "text": record.normalized_text,
        "content_sha256": record.normalized_text_hash,
        "indexed_text_sha256": "3" * 64,
        "embedding": [1.0, 0.0],
        "embedding_provenance": {
            "verified": True,
            "provider": "synthetic-vector-provider",
            "model_id": "synthetic-model",
            "model_version": "synthetic-configuration-v1",
            "artifact_version": None,
            "dimension": 2,
        },
        "aggregation": "mean_l2_normalized_enabled_text_chunks_v1",
        "chunking": {"chunk_size": 500, "chunk_overlap": 50},
        "chunk_count": 1,
        "vector_count": 1,
    }
    return {
        "contract_version": "weknora-kdd-v1",
        "requested_count": 1,
        "returned_count": 1,
        "features": [feature],
        "excluded": [],
        "population": "explicit_members_only",
        "scope_fingerprint": "4" * 64,
    }


def test_exact_fixed_text_version_model_and_member_adopt_vector_without_mutating_snapshot():
    snapshot = fixed_snapshot()
    before = copy.deepcopy(snapshot.model_dump(mode="json"))
    response = feature_response(snapshot)
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        enriched, receipt = features_for_snapshot(settings(), snapshot, direction="research", client=client)
    assert len(requests) == 1
    assert requests[0]["members"] == receipt["requested_members"]
    assert requests[0]["include_chunks"] is False
    assert len(enriched.records) == len(snapshot.records) == 1
    embedding = enriched.records[0].embedding
    assert embedding.vector == (1.0, 0.0)
    assert embedding.text_hash == snapshot.records[0].normalized_text_hash
    assert embedding.provenance.provider_ref.object_id == "synthetic-knowledge"
    assert embedding.provenance.provider_ref.adopted_version == "synthetic-index-v1"
    assert receipt["adopted"][0]["member"]["raw_sha256"] == snapshot.records[0].record_ref.content_hash
    assert receipt["model_token_usage"] is None
    assert not receipt["excluded"]
    assert snapshot.model_dump(mode="json") == before


def test_ready_feature_null_knowledge_id_is_excluded_before_ref_construction():
    snapshot = fixed_snapshot()
    response = feature_response(snapshot)
    response["features"][0]["knowledge_id"] = None
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        enriched, receipt = features_for_snapshot(settings(), snapshot, direction="research", client=client)
    assert not receipt["adopted"]
    assert enriched.records == snapshot.records
    assert enriched.records[0].embedding is None
    assert receipt["excluded"][0]["reason"] == "feature_model_or_aggregation_unverified"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"content_sha256": "0" * 64}, "feature_text_hash_mismatch"),
        ({"text": "SYNTHETIC another source version."}, "feature_text_differs_from_fixed_analysis_text"),
    ],
)
def test_wrong_fixed_text_or_hash_preserves_original_and_excludes_feature(change, reason):
    snapshot = fixed_snapshot()
    response = feature_response(snapshot)
    response["features"][0].update(change)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        enriched, receipt = features_for_snapshot(settings(), snapshot, direction="research", client=client)
    assert not receipt["adopted"]
    assert enriched.records == snapshot.records
    assert receipt["excluded"][0]["reason"] == reason


def test_search_composite_budget_never_broadens_existing_reader_hard_limit():
    reader = SimpleNamespace(requests_made=0, max_requests=2, source_instance="synthetic-test-instance")
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"contract_version": "weknora-kdd-v1", "results": []})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        receipt = search_candidates(
            settings(),
            "SYNTHETIC query",
            direction="research",
            reader=reader,
            source_ids=["synthetic_source"],
            cutoff=AS_OF,
            max_tool_calls=64,
            client=client,
        )
    assert len(requests) == 1
    assert reader.max_requests == 2
    assert reader.requests_made == 0
    assert receipt["status"] == "empty"
    assert receipt["tool_requests"] == 1
    assert receipt["records"] == []
