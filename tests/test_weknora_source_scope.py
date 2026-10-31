"""Real authorization scope is shared by investigation and monitor; no live HTTP."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters import weknora_kdd
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from pydantic import ValidationError
from test_monitor_feature_scopes import cohort, mock_features, monitor
from test_weknora_kdd import AS_OF, feature_response, fixed_snapshot

pytestmark = pytest.mark.ENGINE
REAL_SCOPE = "SYNTHETIC-fixed-history-scope"


def settings(**values):
    return Settings(
        environment="test",
        weknora_kdd_url="http://synthetic.invalid",
        weknora_kdd_token="SYNTHETIC-no-network-token",
        weknora_kdd_knowledge_base_id="SYNTHETIC-real-kb",
        weknora_kdd_demo_knowledge_base_id="SYNTHETIC-demo-kb",
        weknora_kdd_demo_source_scope="SYNTHETIC-demo-scope",
        **values,
    )


def test_explicit_real_scope_matches_search_and_feature_requests_and_receipts():
    config = settings(weknora_kdd_source_scope=REAL_SCOPE)
    data = fixed_snapshot()
    requests = []

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path.endswith("/features"):
            value = feature_response(data)
            value["features"][0]["source_scope"] = body["source_scope"]
            return httpx.Response(200, json=value)
        return httpx.Response(200, json={"contract_version": "weknora-kdd-v1", "results": []})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result = weknora_kdd.search_candidates(
            config,
            "SYNTHETIC query",
            direction="public_health",
            reader=None,
            source_ids=[data.records[0].source_id],
            cutoff=AS_OF,
            client=client,
        )
        enriched, receipt = weknora_kdd.features_for_snapshot(
            config, data, direction="business", client=client
        )
    assert [item["source_scope"] for item in requests] == [REAL_SCOPE, REAL_SCOPE]
    assert all(
        item["knowledge_base_id"] == "SYNTHETIC-real-kb" and "is_demo" not in item for item in requests
    )
    assert result["usage"]["scope"] == receipt["source_scope"] == REAL_SCOPE
    assert result["usage"]["knowledge_base_id"] == receipt["knowledge_base_id"]
    assert enriched.records[0].embedding is not None


def test_monitor_uses_same_explicit_scope_even_without_or_with_legacy_source_map(monkeypatch):
    data = cohort(["SYNTHETIC-unmapped", "SYNTHETIC-previously-mapped"])
    before = copy.deepcopy(data.model_dump(mode="json"))
    service = monitor(
        weknora_kdd_source_scope=REAL_SCOPE,
        weknora_kdd_feature_source_scopes={"SYNTHETIC-previously-mapped": "SYNTHETIC-old-scope"},
    )
    requests = []
    mock_features(monkeypatch, data, requests)
    enriched, receipt = service._feature_snapshot(data, {})
    assert len(requests) == 1 and len(requests[0]["members"]) == 2
    assert requests[0]["source_scope"] == REAL_SCOPE
    assert receipt["groups"][0]["source_scope"] == REAL_SCOPE
    assert receipt["groups"][0]["receipt"]["source_scope"] == REAL_SCOPE
    assert all(row.embedding is not None for row in enriched.records)
    assert [row.record_ref for row in enriched.records] == [row.record_ref for row in data.records]
    assert data.model_dump(mode="json") == before


def test_unconfigured_real_scope_keeps_direction_and_legacy_feature_scope_map(monkeypatch):
    config = settings()
    assert weknora_kdd.retrieval_scope(config, direction="business")["source_scope"] == "business"
    data = cohort(["SYNTHETIC-mapped", "SYNTHETIC-unmapped"])
    service = monitor(weknora_kdd_feature_source_scopes={"SYNTHETIC-mapped": "SYNTHETIC-source-grant"})
    requests = []
    mock_features(monkeypatch, data, requests)
    enriched, receipt = service._feature_snapshot(data, {})
    assert len(requests) == 1 and requests[0]["source_scope"] == "SYNTHETIC-source-grant"
    assert enriched.records[1] == data.records[1]
    assert receipt["excluded"][0]["reason"] == "feature_source_scope_not_configured"


def test_real_scope_cannot_override_demo_scope_or_rescue_a_missing_real_kb():
    config = settings(weknora_kdd_source_scope=REAL_SCOPE)
    demo = weknora_kdd.retrieval_scope(config, direction="public_health", is_demo=True)
    assert demo == {
        "knowledge_base_id": "SYNTHETIC-demo-kb",
        "source_scope": "SYNTHETIC-demo-scope",
        "is_demo": True,
    }
    config.weknora_kdd_knowledge_base_id = None
    with pytest.raises(DomainError) as failure:
        weknora_kdd.retrieval_scope(config, direction="research")
    assert failure.value.code == "weknora_kdd_not_configured"


@pytest.mark.parametrize("bad", ["", " padded", "bad\nlabel"])
def test_explicit_real_scope_rejects_ambiguous_configuration(bad):
    with pytest.raises(ValidationError):
        settings(weknora_kdd_source_scope=bad)


@pytest.mark.parametrize("operation", ["features", "search"])
def test_direction_alias_response_cannot_replace_the_explicit_authorized_scope(operation):
    config, data = settings(weknora_kdd_source_scope=REAL_SCOPE), fixed_snapshot()
    response = feature_response(data)
    response["features"][0]["source_scope"] = "public_health"
    identity = response["features"][0]["identity"]
    reader = SimpleNamespace(source_instance=identity["source_instance"], requests_made=0, max_requests=None)
    if operation == "search":
        response = {
            "contract_version": "weknora-kdd-v1",
            "results": [
                {
                    "identity": identity,
                    "source_scope": "public_health",
                    "is_demo": False,
                }
            ],
        }
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        if operation == "features":
            with pytest.raises(DomainError) as failure:
                weknora_kdd.features_for_snapshot(config, data, direction="public_health", client=client)
            assert failure.value.code == "weknora_feature_identity"
        else:
            result = weknora_kdd.search_candidates(
                config,
                "SYNTHETIC query",
                direction="public_health",
                reader=reader,
                source_ids=[data.records[0].source_id],
                cutoff=AS_OF,
                client=client,
            )
            assert result["status"] == "unavailable" and not result["records"]
            assert result["candidates"][0]["binding_status"] == "unbound"
    assert reader.requests_made == 0
