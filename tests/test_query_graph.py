"""Isolated graph adapter probes. Every source response here is synthetic."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.query_graph import build_graph_candidates, execute_graph_action
from bioradar.research.investigations.graph import GraphBundle
from pydantic import SecretStr
from test_graph import source

pytestmark = pytest.mark.BOOT
CUTOFF = datetime(2026, 10, 30, tzinfo=UTC)
SETTINGS = SimpleNamespace(
    data_fabric_url="http://synthetic.test",
    data_fabric_graph_query_url="http://synthetic.test",
    data_fabric_graph_query_token=SecretStr("query-only"),
    engine_source_id_allowlist={"datafabric-test": ["primekg_graph"]},
)
FIXED = {
    "question": "How do MARKERX and MARKERY relate?",
    "search_scope": ["primekg_graph"],
    "is_demo": False,
    "evidence_time": {"mode": "current", "cutoff": CUTOFF.isoformat()},
}
CHECKS = [
    {"id": "answer_scope", "status": "unknown"},
    {"id": "source_materials", "status": "unknown"},
    {"id": "technology", "status": "unknown"},
]


def lookup(status="resolved"):
    candidate = {
        "name": "MARKERX",
        "node_type": "gene/protein",
        "node_source": "TEST",
        "seed_id": "42",
        "identity_status": "background",
    }
    return {
        "query": {"name": "MARKERX", "node_type": "gene/protein", "limit": 5},
        "status": status,
        "candidates": [] if status == "not_found" else [candidate],
        "truncated": False,
        "observed_at": "2026-10-30T00:00:00Z",
        "evidence_status": "current_identity_candidates_only",
        "limitations": ["synthetic_test"],
    }


def world():
    review = {"status": "background", "reviewed_by": None, "reviewed_at": None}
    nodes = [
        {
            "namespace": "primekg.TEST",
            "id": i,
            "entity_type": "gene/protein",
            "name": n,
            "identity_status": "background",
            "review": review,
        }
        for i, n in (("42", "MARKERX"), ("43", "MARKERY"))
    ]
    edge = {
        "id": "synthetic-edge",
        "source": {"namespace": "primekg.TEST", "id": "42"},
        "target": {"namespace": "primekg.TEST", "id": "43"},
        "relation_type": "protein_protein",
        "directed": True,
        "review": review,
        "binding_status": "background",
    }
    bundle = GraphBundle.model_validate(
        {
            "schema_version": "datafabric.graph.bundle.v1",
            "graph_kind": "background",
            "namespace": "primekg",
            "is_demo": False,
            "title": "SYNTHETIC test, not biomedical evidence",
            "origin": "primekg_current_capture",
            "scope_description": "synthetic pair",
            "nodes": nodes,
            "edges": [edge],
        }
    )
    reader, graph, calls = source(bundle, mutate=lambda v: v["coverage"].update(max_depth=1))
    reader.client.headers["Authorization"] = "Bearer history-only"
    return graph, reader, calls


def install_posts(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        "bioradar.adapters.query_graph.httpx.Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )


def execute(arguments, reader=None, **changes):
    kwargs = {"mode": "current", "cutoff": CUTOFF, "idempotency_key": "stable-test-key", "timeout_seconds": 5}
    kwargs.update(changes)
    return execute_graph_action(SETTINGS, reader, arguments, **kwargs)


def initial():
    return build_graph_candidates(SETTINGS, FIXED, {}, None, checks=CHECKS)[0]["arguments"]


def capture_args():
    context = {"provider": "fabric_graph", "operation": "lookup", "lookup": lookup()}
    candidates = build_graph_candidates(SETTINGS, FIXED, {"graph_context": [context]}, None, checks=CHECKS)
    return next(c["arguments"] for c in candidates if c["arguments"]["operation"] == "capture")


def capture_response(graph, reader):
    params = {"seed_ids": ["primekg.TEST|42"], "max_depth": 1}
    neighborhood = reader._get("/api/history/graph/" + graph["version_id"] + "/neighborhood", params)
    args = capture_args()
    body = {k: args[k] for k in ("node_type", "node_source", "seed_id", "relation_types", "limit")}
    body["neighbor_term"] = ""
    return {
        "run": {
            "id": "synthetic-run",
            "state": "completed",
            "source_id": "primekg_graph",
            "is_demo": False,
            "version_ids": [graph["version_id"]],
        },
        "query": body,
        "read_cutoff": CUTOFF.isoformat(),
        "neighborhood": neighborhood,
    }


def test_candidates_use_question_and_read_materials_not_invented_native_ids():
    progress = {"evidence": [{"text": "The source mentions Drugmab and OTHER1.", "title": "source"}]}
    original = copy.deepcopy(progress)
    candidates = build_graph_candidates(
        SETTINGS, FIXED, progress, {"graph_names": [{"name": "invented", "node_type": "drug"}]}, checks=CHECKS
    )
    names = {c["arguments"]["name"] for c in candidates}
    assert names == {"MARKERX", "MARKERY", "Drugmab", "OTHER1"}
    assert all("seed_id" not in c["arguments"] for c in candidates)
    assert all(c["covers"] == ["technology"] for c in candidates)
    assert (
        next(c for c in candidates if c["arguments"]["name"] == "Drugmab")["arguments"]["node_type"] == "drug"
    )
    assert progress == original


def test_each_feedback_enables_only_its_next_real_operation(monkeypatch):
    graph, reader, _ = world()
    response = capture_response(graph, reader)
    reader.requests_made = 0
    posts = []

    def post(req):
        posts.append(req)
        assert req.headers["Authorization"] == "Bearer query-only"
        if req.url.path.endswith("lookup"):
            return httpx.Response(200, json=lookup())
        assert req.headers["Idempotency-Key"] == "stable-test-key"
        return httpx.Response(200, json=response)

    install_posts(monkeypatch, post)
    first = execute(initial(), reader)
    assert first["status"] == "read" and first["actual_cost"]["tool_calls"] == 1
    assert reader.requests_made == 0
    second = execute(capture_args(), reader)
    assert second["status"] == "read" and len(posts) == 2 and reader.requests_made == 0
    assert second["actual_cost"]["relation_access"] is None
    assert second["usage"]["returned_edges"] == 1
    candidates = build_graph_candidates(
        SETTINGS, FIXED, {"graph_context": [second["graph_context"]]}, None, checks=CHECKS
    )
    neighbors = next(c for c in candidates if c["arguments"]["operation"] == "neighbors")
    assert neighbors["estimated_cost"]["tool_calls"] == 2
    third = execute(neighbors["arguments"], reader)
    assert third["status"] == "read" and third["actual_cost"]["tool_calls"] == 2
    assert third["tool_requests"] == 0 and reader.requests_made == 2
    assert len(third["graph_records"]) == 1
    assert first["records"] == second["records"] == third["records"] == []
    reader.client.close()


def test_not_found_is_completed_empty_and_ambiguity_never_auto_selects(monkeypatch):
    install_posts(monkeypatch, lambda req: httpx.Response(200, json=lookup("not_found")))
    out = execute(initial())
    assert out["status"] == "empty" and out["error_code"] is None
    assert out["actual_cost"]["tool_calls"] == 1
    ambiguous = lookup("ambiguous")
    ambiguous["candidates"].append({**ambiguous["candidates"][0], "seed_id": "different"})
    context = {"provider": "fabric_graph", "operation": "lookup", "lookup": ambiguous}
    candidates = build_graph_candidates(SETTINGS, FIXED, {"graph_context": [context]}, None, checks=CHECKS)
    assert not any(c["arguments"]["operation"] == "capture" for c in candidates)
    args = capture_args()
    args["lookup_receipt"] = ambiguous
    assert execute(args)["usage"]["request_status"] == "not_sent"


def test_guessed_native_and_mismatched_receipts_never_dispatch(monkeypatch):
    install_posts(monkeypatch, lambda req: pytest.fail("must not dispatch"))
    args = capture_args()
    args["seed_id"] = "guessed"
    assert execute(args)["error_code"] == "graph_action_invalid"
    args = capture_args()
    args["lookup_receipt"]["truncated"] = True
    assert execute(args)["usage"]["request_status"] == "not_sent"
    args = initial()
    del args["node_type"]
    assert execute(args)["usage"]["request_status"] == "not_sent"


def test_historical_study_never_uses_current_lookup_or_capture(monkeypatch):
    install_posts(monkeypatch, lambda req: pytest.fail("must not dispatch"))
    fixed = copy.deepcopy(FIXED)
    fixed["evidence_time"]["study_id"] = "historical-study"
    assert build_graph_candidates(SETTINGS, fixed, {}, None, checks=CHECKS) == []
    for args in (initial(), capture_args()):
        result = execute(args, mode="historical")
        assert result["error_code"] == "history_current_graph_unavailable"
        assert result["actual_cost"]["tool_calls"] == 0


def test_timeout_exhaustion_is_not_sent_and_missing_configuration_is_not_empty(monkeypatch):
    install_posts(monkeypatch, lambda req: pytest.fail("must not dispatch"))
    result = execute(initial(), timeout_seconds=0)
    assert result["usage"]["request_status"] == "not_sent" and result["actual_cost"]["tool_calls"] == 0
    settings = copy.copy(SETTINGS)
    settings.data_fabric_graph_query_token = None
    result = execute_graph_action(
        settings, None, initial(), mode="current", cutoff=CUTOFF, idempotency_key="test", timeout_seconds=5
    )
    assert result["status"] == "unavailable" and result["error_code"] == "graph_query_not_configured"


@pytest.mark.parametrize("failure", ["read_timeout", "server_error", "invalid_receipt"])
def test_uncertain_capture_is_never_reported_not_sent_or_zero_cost(monkeypatch, failure):
    def post(req):
        if failure == "read_timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=req)
        return httpx.Response(500) if failure == "server_error" else httpx.Response(200, json={})

    install_posts(monkeypatch, post)
    result = execute(capture_args())
    assert result["status"] == "unconfirmed" and result["usage"]["request_status"] == "unknown"
    assert result["actual_cost"]["tool_calls"] == 1 and result["actual_cost"]["relation_access"] is None


def test_fixed_neighbors_retains_old_cutoff_and_checks_remaining_http_budget(monkeypatch):
    graph, reader, calls = world()
    response = capture_response(graph, reader)
    reader.requests_made = 0
    calls.clear()
    binding = response["neighborhood"]["graph_ref"]
    args = {
        "provider": "fabric_graph",
        "source_id": "primekg_graph",
        "operation": "neighbors",
        "binding": binding,
        "seed_ids": ["primekg.TEST|42"],
        "read_cutoff": CUTOFF.isoformat(),
    }
    old = datetime(2026, 10, 1, tzinfo=UTC)
    result = execute(args, reader, mode="historical", cutoff=old)
    assert result["error_code"] == "graph_version_after_cutoff" and not calls
    reader.max_requests = 1
    result = execute(args, reader)
    assert result["error_code"] == "analysis_tool_budget" and not calls
    reader.client.close()


def test_lookup_sent_query_and_remaining_timeout_are_explicit(monkeypatch):
    seen = []

    def post(req):
        import json

        seen.append(req)
        body = json.loads(req.content)
        assert body == {"name": "MARKERX", "node_type": "gene/protein", "limit": 5}
        assert 0 < req.extensions["timeout"]["read"] <= 0.25
        return httpx.Response(200, json=lookup())

    install_posts(monkeypatch, post)
    result = execute(initial(), timeout_seconds=0.25)
    assert result["status"] == "read" and len(seen) == 1


def test_expired_deadline_after_preparation_never_posts(monkeypatch):
    from bioradar.adapters import query_graph

    values = iter([100.0, 101.0])
    monkeypatch.setattr(query_graph.time, "monotonic", lambda: next(values))
    install_posts(monkeypatch, lambda req: pytest.fail("deadline expired before dispatch"))
    result = execute(initial(), timeout_seconds=0.5)
    assert result["usage"]["request_status"] == "not_sent"
    assert result["actual_cost"]["tool_calls"] == 0


def test_mismatched_lookup_and_capture_seed_remain_visible_failures(monkeypatch):
    wrong = lookup()
    wrong["query"]["name"] = "another name"
    install_posts(monkeypatch, lambda req: httpx.Response(200, json=wrong))
    result = execute(initial())
    assert result["status"] == "failed" and result["error_code"] == "graph_source_invalid"
    assert result["actual_cost"]["tool_calls"] == 1


def test_fixed_neighborhood_invalid_response_does_not_report_measured_zero_relations():
    graph, reader, _ = world()
    response = capture_response(graph, reader)
    binding = response["neighborhood"]["graph_ref"]
    reader.requests_made = 0
    original_get = reader._get

    def changed(path, params=None, **kwargs):
        value = original_get(path, params, **kwargs)
        if path.endswith("/neighborhood"):
            value["edges"][0]["relation_type"] = "invented"
        return value

    reader._get = changed
    args = {
        "provider": "fabric_graph",
        "source_id": "primekg_graph",
        "operation": "neighbors",
        "binding": binding,
        "seed_ids": ["primekg.TEST|42"],
        "read_cutoff": CUTOFF.isoformat(),
    }
    result = execute(args, reader)
    assert result["status"] == "failed"
    assert result["actual_cost"]["tool_calls"] == 2
    assert result["actual_cost"]["relation_access"] is None
    assert result["records"] == []
    reader.client.close()


@pytest.mark.parametrize(
    ("scopes", "enabled"),
    [
        ({"search_scope": ["clinicaltrials", "europe_pmc"], "graph_scope": ["primekg_graph"]}, True),
        ({"search_scope": ["primekg_graph"], "graph_scope": []}, False),
        ({"search_scope": ["primekg_graph"]}, True),
        ({"search_scope": ["clinicaltrials", "europe_pmc"]}, False),
        ({}, False),
    ],
)
def test_graph_candidates_respect_frozen_graph_scope_and_legacy_fallback(scopes, enabled):
    fixed = {key: value for key, value in copy.deepcopy(FIXED).items() if key != "search_scope"}
    fixed.update(scopes)
    context = {"provider": "fabric_graph", "operation": "lookup", "lookup": lookup()}
    candidates = build_graph_candidates(SETTINGS, fixed, {"graph_context": [context]}, None, checks=CHECKS)
    assert bool(candidates) is enabled
    assert any(item["arguments"]["operation"] == "capture" for item in candidates) is enabled


@pytest.mark.parametrize("technology_status", ["unknown", "passed", None])
def test_graph_expected_coverage_is_only_explicit_unmet_technology(technology_status):
    checks = [
        {"id": name, "status": "unknown", "question": "graph relationship and technology research"}
        for name in (
            "competition",
            "analogous_failure",
            "answer_scope",
            "support",
            "patent",
            "gap:technology_clinical_comparison",
            "mech:gap:graph_relation",
        )
    ]
    if technology_status is not None:
        checks.append({"id": "technology", "status": technology_status})
    original = copy.deepcopy(checks)
    context = {"provider": "fabric_graph", "operation": "lookup", "lookup": lookup()}
    candidates = build_graph_candidates(SETTINGS, FIXED, {"graph_context": [context]}, None, checks=checks)
    assert candidates  # Background remains selectable even without an expected coverage bonus.
    assert {item["arguments"]["operation"] for item in candidates} == {"lookup", "capture"}
    expected = ["technology"] if technology_status == "unknown" else []
    assert all(item["covers"] == expected for item in candidates)
    assert checks == original  # Candidate creation never marks any scientific check passed.
