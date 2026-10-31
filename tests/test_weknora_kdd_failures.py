"""Actual synthetic HTTP failure receipts distinguish a failed response from unknown completion."""

import copy
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.weknora_kdd import features_for_snapshot, search_candidates
from bioradar.research.investigations.analysis_service import AnalysisService
from bioradar.research.monitor.service import MonitorService
from test_weknora_kdd import AS_OF, fixed_snapshot, settings

pytestmark = pytest.mark.ENGINE


def search(config, client, **kwargs):
    return search_candidates(
        config,
        "SYNTHETIC fixed query",
        direction="research",
        reader=SimpleNamespace(requests_made=0, source_instance="synthetic-test-instance"),
        source_ids=["synthetic_source"],
        cutoff=AS_OF,
        client=client,
        **kwargs,
    )


def test_known_http_failure_counts_the_sent_request_and_keeps_token_cost_unknown():
    requests = []

    def failed(request):
        requests.append(request)
        return httpx.Response(503, json={"secret": "SYNTHETIC upstream details must not leak"})

    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        receipt = search(settings(), client)
    assert len(requests) == 1 and requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/kdd/search"
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "weknora_kdd_http_error"
    assert receipt["tool_requests"] == 1
    assert receipt["usage"]["request_status"] == "failed"
    assert receipt["usage"]["embedding_rerank_tokens"] is None
    assert receipt["records"] == receipt["candidates"] == []
    assert "upstream details" not in repr(receipt)


def test_transport_unknown_is_not_empty_or_resent_and_feature_failure_retains_originals(monkeypatch):
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("SYNTHETIC transport details must not leak", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        receipt = search(settings(), client)
        assert receipt["status"] == "unconfirmed"
        assert receipt["error_code"] == "weknora_kdd_transport_unconfirmed"
        assert receipt["tool_requests"] == 1
        assert receipt["usage"]["request_status"] == "unconfirmed"
        assert receipt["usage"]["embedding_rerank_tokens"] is None
        # The executor persists this status and its resume path returns before another provider call.
        service = AnalysisService.__new__(AnalysisService)
        checkpoints = []
        service._checkpoint = lambda *args, **kwargs: checkpoints.append(copy.deepcopy(args[2]))
        action = {
            "id": "SYNTHETIC-unknown-action",
            "kind": "document_search",
            "arguments": {"provider": "weknora"},
        }
        progress = {
            "queries": [{"action_id": action["id"], "status": receipt["status"]}],
            "query_policy": {"phase": "followup"},
        }
        for _ in range(2):
            persisted = service._execute_policy_action("SYNTHETIC", "token", {}, progress, action, 0)
            assert persisted["status"] == "unconfirmed"
            assert persisted["error_code"] == "analysis_query_outcome_unconfirmed"
        assert len(requests) == 1 and len(checkpoints) == 2

        snapshot = fixed_snapshot()
        original = copy.deepcopy(snapshot.model_dump(mode="json"))
        monitor = MonitorService.__new__(MonitorService)
        monitor.settings = settings()
        monitor.settings.monitor_kdd_directions = {"synthetic_source": "research"}
        monitor.settings.weknora_kdd_feature_source_scopes = {"synthetic_source": "research"}
        monitor.settings.weknora_kdd_source_scope = None
        import bioradar.adapters.weknora_kdd as adapter

        monkeypatch.setattr(
            adapter,
            "features_for_snapshot",
            lambda config, fixed, **kwargs: features_for_snapshot(config, fixed, client=client, **kwargs),
        )
        enriched, feature_receipt = monitor._feature_snapshot(snapshot, {"evidence": []})
    assert len(requests) == 2
    assert enriched.records == snapshot.records
    assert snapshot.model_dump(mode="json") == original
    assert feature_receipt["record_count"] == 1 and feature_receipt["adopted_count"] == 0
    assert feature_receipt["status"] == "partial"
    assert feature_receipt["groups"][0]["receipt"]["error_code"] == "weknora_kdd_transport_unconfirmed"
    assert feature_receipt["excluded"][0]["reason"] == "weknora_kdd_transport_unconfirmed"


@pytest.mark.parametrize("preflight", ["missing_endpoint", "expired_time"])
def test_known_preflight_stop_is_not_sent_and_costs_no_request(preflight):
    requests = []
    config = settings()
    kwargs = {}
    if preflight == "missing_endpoint":
        config.weknora_kdd_url = None
    else:
        kwargs["timeout_seconds"] = 0
    with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request))) as client:
        receipt = search(config, client, **kwargs)
    assert requests == []
    assert receipt["status"] == "failed"
    assert receipt["usage"]["request_status"] == "not_sent"
    assert receipt["tool_requests"] == 0
    assert receipt["usage"]["embedding_rerank_tokens"] is None
