"""Bounded catalog discovery uses synthetic fixed packages, never hidden scenario IDs."""

import copy
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.fixed_graph import remember_graph_package
from bioradar.adapters.query_graph import build_graph_candidates, execute_graph_action
from test_graph import AS_OF, bundle, source

pytestmark = pytest.mark.BOOT


def setup_graph():
    graph_bundle, _ = bundle()
    reader, original, calls = source(graph_bundle, mutate=lambda v: v["coverage"].update(max_depth=1))
    original_transport = reader.client._transport
    catalog_calls = []

    def handler(request):
        if request.url.path == "/api/history/records":
            catalog_calls.append(request)
            assert request.url.params["record_kind"] == "graph"
            assert request.url.params["limit"] == "4"
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.history.collection.v1",
                    "items": [original],
                    "next_cursor": "page-remains-unread",
                },
            )
        return original_transport.handle_request(request)

    reader.client = httpx.Client(base_url="http://fabric.test", transport=httpx.MockTransport(handler))
    settings = SimpleNamespace(engine_source_id_allowlist={"fixture": ["reviewed_graph"]})
    fixed = {
        "question": "How does SYNTHETIC B relate?",
        "is_demo": True,
        "graph_scope": ["reviewed_graph"],
        "evidence_time": {"mode": "current", "cutoff": AS_OF.isoformat()},
        "query_policy": {"research_contract_version": "bioradar-research-input-v1"},
    }
    progress = {"query_policy": {"named_objects": [{"id": "b", "name": "SYNTHETIC B"}]}}
    return settings, fixed, progress, reader, calls, catalog_calls


def candidates(settings, fixed, progress):
    return build_graph_candidates(
        settings, fixed, progress, None, checks=[{"id": "technology", "status": "unknown"}]
    )


def execute(settings, reader, action):
    return execute_graph_action(
        settings,
        reader,
        action["arguments"],
        mode="current",
        cutoff=AS_OF,
        idempotency_key="synthetic",
        timeout_seconds=5,
    )


def test_discovery_fixed_adoption_resolution_and_neighbors_are_separate_counted_actions():
    settings, fixed, progress, reader, calls, catalogs = setup_graph()
    before = copy.deepcopy(progress)
    catalog = candidates(settings, fixed, progress)
    assert [c["arguments"]["operation"] for c in catalog] == ["catalog"]
    assert not calls and not catalogs and progress == before
    receipt = execute(settings, reader, catalog[0])
    assert receipt["actual_cost"]["tool_calls"] == 1
    assert receipt["graph_records"] == receipt["records"] == []
    assert receipt["graph_context"]["next_cursor"] == "page-remains-unread"
    progress["graph_context"] = [receipt["graph_context"]]
    adopt = candidates(settings, fixed, progress)
    assert [c["arguments"]["operation"] for c in adopt] == ["adopt"]
    adopted = execute(settings, reader, adopt[0])
    assert adopted["actual_cost"]["tool_calls"] == 1
    assert adopted["records"] == []
    remember_graph_package(fixed, progress, adopted["graph_records"][0])
    progress["graph_context"].append(adopted["graph_context"])
    resolve = next(
        c for c in candidates(settings, fixed, progress) if c["arguments"]["operation"] == "resolve"
    )
    resolved = execute(settings, reader, resolve)
    assert resolved["actual_cost"]["tool_calls"] == 2
    progress["graph_context"].append(resolved["graph_context"])
    neighbors = next(
        c for c in candidates(settings, fixed, progress) if c["arguments"]["operation"] == "neighbors"
    )
    output = execute(settings, reader, neighbors)
    assert output["status"] == "read" and output["actual_cost"]["tool_calls"] == 2
    assert output["records"] == [] and "不自动确认" in output["usage"]["qualification"]
    assert len(catalogs) == 1 and len(calls) == 5
    reader.client.close()


def test_catalog_requires_explicit_scope_objects_and_relationship_gap():
    settings, fixed, progress, reader, _, _ = setup_graph()
    assert not candidates(settings, {**fixed, "graph_scope": []}, progress)
    assert not candidates(settings, fixed, {})
    assert not build_graph_candidates(
        settings, fixed, progress, None, checks=[{"id": "technology", "status": "passed"}]
    )
    settings.engine_source_id_allowlist = {"fixture": []}
    assert not candidates(settings, fixed, progress)
    reader.client.close()


def test_catalog_replays_known_context_without_a_new_discovery_and_keeps_time_mode():
    settings, fixed, progress, reader, _, catalog_calls = setup_graph()
    action = candidates(settings, fixed, progress)[0]
    output = execute_graph_action(
        settings,
        reader,
        action["arguments"],
        mode="operational_replay",
        cutoff=AS_OF,
        idempotency_key="synthetic",
        timeout_seconds=5,
    )
    assert catalog_calls[0].url.params["mode"] == "operational_replay"
    progress["graph_context"] = [output["graph_context"]]
    assert all(c["arguments"]["operation"] != "catalog" for c in candidates(settings, fixed, progress))
    assert len(catalog_calls) == 1
    stopped = execute_graph_action(
        settings,
        reader,
        action["arguments"],
        mode="current",
        cutoff=AS_OF,
        idempotency_key="synthetic",
        timeout_seconds=0,
    )
    assert stopped["usage"]["request_status"] == "not_sent"
    assert len(catalog_calls) == 1
    reader.client.close()
