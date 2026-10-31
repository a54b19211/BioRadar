"""SYNTHETIC demo/real namespaces; only MockTransport performs HTTP exchanges."""

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.weknora_kdd import search_candidates
from bioradar.api.config import Settings

pytestmark = pytest.mark.ENGINE
CUTOFF = datetime(2026, 10, 31, tzinfo=UTC)
REAL = "SYNTHETIC-real-instance"
DEMO = "SYNTHETIC-demo-instance"
SOURCE = "synthetic_source"


def search(*, is_demo, returned_instance, wrong_hash=False, item_updates=None):
    text = "SYNTHETIC fixed original; a search chunk is never adopted."
    raw = json.dumps({"body": text})
    record = {
        "schema_version": "datafabric.history.record.v1",
        "source_id": SOURCE,
        "source_record_id": "SYNTHETIC-fixed-native",
        "version_id": "a" * 64,
        "record_kind": "document",
        "is_demo": is_demo,
        "raw_media_type": "application/json",
        "raw_content": raw,
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "clean_text": text,
        "facts": {"body": text},
        "field_locators": {"body": {"json_pointer": "/body"}},
        "parser_version": "SYNTHETIC-projection-v1",
        "first_seen_at": "2026-10-30T00:00:00Z",
        "received_at": "2026-10-30T00:00:00Z",
    }
    scope = "SYNTHETIC-demo-scope" if is_demo else "SYNTHETIC-real-scope"
    item = {
        "identity": {
            "source_instance": returned_instance,
            **{key: record[key] for key in ("source_id", "source_record_id", "version_id")},
            "raw_sha256": "b" * 64 if wrong_hash else record["raw_sha256"],
        },
        "source_scope": scope,
        "is_demo": is_demo,
        "knowledge_id": "SYNTHETIC-knowledge",
        "chunk_id": "SYNTHETIC-chunk",
        "index_version": "SYNTHETIC-index",
        "text": "SYNTHETIC search chunk must never become the adopted original.",
        "metadata": {"raw_content": "SYNTHETIC secret-shaped metadata must not be projected"},
    }
    item.update(item_updates or {})
    config = Settings(
        _env_file=None,
        weknora_kdd_url="http://synthetic.invalid",
        weknora_kdd_token="SYNTHETIC-no-network-token",
        weknora_kdd_knowledge_base_id="SYNTHETIC-real-kb",
        weknora_kdd_demo_knowledge_base_id="SYNTHETIC-demo-kb",
        weknora_kdd_source_scope="SYNTHETIC-real-scope",
        weknora_kdd_demo_source_scope="SYNTHETIC-demo-scope",
    )
    search_requests, original_requests = [], []

    def search_reply(request):
        assert request.method == "POST" and request.url.path == "/api/v1/kdd/search"
        body = json.loads(request.content)
        search_requests.append(body)
        assert body["source_scope"] == scope
        assert body["knowledge_base_id"] == ("SYNTHETIC-demo-kb" if is_demo else "SYNTHETIC-real-kb")
        assert body.get("is_demo", False) is is_demo
        return httpx.Response(200, json={"contract_version": "weknora-kdd-v1", "results": [item]})

    def original_reply(request):
        original_requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/history/versions/" + record["version_id"]
        assert request.url.params["source_id"] == SOURCE
        assert request.url.params["source_record_id"] == record["source_record_id"]
        assert request.url.params["is_demo"] == str(is_demo).lower()
        assert request.url.params["mode"] == "current"
        assert datetime.fromisoformat(request.url.params["cutoff"]) == CUTOFF
        return httpx.Response(200, json={"record": record})

    with (
        httpx.Client(transport=httpx.MockTransport(search_reply)) as search_client,
        httpx.Client(
            base_url="http://fabric.synthetic.invalid", transport=httpx.MockTransport(original_reply)
        ) as fabric_client,
    ):
        reader = FabricHistoryReader(
            fabric_client,
            authorization_ref="SYNTHETIC-allowed-fixed-source",
            source_instance=REAL,
            demo_source_instance=DEMO,
        )
        result = search_candidates(
            config,
            "SYNTHETIC query hypothesis",
            direction="research",
            reader=reader,
            source_ids=[SOURCE],
            cutoff=CUTOFF,
            is_demo=is_demo,
            client=search_client,
        )
        assert reader.source_instance == REAL and reader.demo_source_instance == DEMO
    assert len(search_requests) == 1
    return result, record, original_requests


@pytest.mark.parametrize("is_demo,instance", [(True, DEMO), (False, REAL)])
def test_correct_namespace_reads_and_binds_the_exact_fabric_original(is_demo, instance):
    result, record, requests = search(is_demo=is_demo, returned_instance=instance)
    assert result["status"] == "completed" and result["records"] == [record]
    assert len(requests) == 1
    candidate = result["candidates"][0]
    assert candidate["binding_status"] == "bound"
    assert candidate["binding_reason"] == "exact_fabric_version"
    assert candidate["identity"]["source_instance"] == instance
    assert candidate["identity"]["version_id"] == record["version_id"]
    assert candidate["history_binding"]["is_demo"] is is_demo
    assert candidate["record_ref"]["adopted_version"] == record["version_id"]
    assert candidate["record_ref"]["content_hash"] == record["raw_sha256"]
    assert json.loads(candidate["record_ref"]["object_id"]) == [SOURCE, record["source_record_id"], is_demo]
    assert result["identity_reads"]["version_reads"] == 1


@pytest.mark.parametrize(
    "is_demo,instance",
    [(True, "SYNTHETIC-other-demo-instance"), (True, REAL), (False, DEMO)],
)
def test_wrong_namespace_is_rejected_before_any_original_read(is_demo, instance):
    result, _, requests = search(is_demo=is_demo, returned_instance=instance)
    assert result["status"] == "unavailable" and result["records"] == []
    candidate = result["candidates"][0]
    assert candidate["binding_status"] == "unbound"
    assert candidate["binding_reason"] == "fixed_source_identity_missing"
    assert candidate["binding_mismatch_codes"] == ["source_instance_mismatch"]
    assert candidate["identity"]["source_instance"] == instance
    assert candidate["identity"]["source_record_id"] == "SYNTHETIC-fixed-native"
    assert candidate["identity"]["version_id"] == "a" * 64
    assert candidate["knowledge_id"] == "SYNTHETIC-knowledge"
    assert candidate["chunk_id"] == "SYNTHETIC-chunk"
    assert candidate["is_demo"] is is_demo
    assert not {"text", "content", "metadata", "raw_content"} & candidate.keys()
    assert "secret-shaped" not in json.dumps(candidate)
    assert not requests and result["identity_reads"]["version_reads"] == 0


def test_valid_demo_namespace_still_requires_the_exact_raw_hash():
    result, _, requests = search(is_demo=True, returned_instance=DEMO, wrong_hash=True)
    assert len(requests) == 1 and not result["records"]
    assert result["candidates"][0]["binding_reason"] == "version_hash_mismatch"
    assert result["candidates"][0]["identity"]["raw_sha256"] == "b" * 64


@pytest.mark.parametrize(
    "updates,code",
    [
        ({"identity": None}, "identity_missing"),
        ({"source_scope": "SYNTHETIC-wrong-scope"}, "source_scope_mismatch"),
    ],
)
def test_rejected_candidate_diagnostic_distinguishes_missing_identity_and_wrong_scope(updates, code):
    result, _, requests = search(is_demo=True, returned_instance=DEMO, item_updates=updates)
    candidate = result["candidates"][0]
    assert not requests and not result["records"]
    assert candidate["binding_status"] == "unbound"
    assert candidate["binding_mismatch_codes"] == [code]
    assert candidate["knowledge_id"] == "SYNTHETIC-knowledge"
    assert "metadata" not in candidate and "text" not in candidate
