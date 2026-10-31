"""Independent public-receipt checks for scoped discovery, using synthetic originals."""

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from bioradar.research.investigations.analysis_models import ANALYSIS_VERSION
from bioradar.research.investigations.analysis_service import AnalysisService
from test_optional_policy_reading import harness
from test_required_gap_discovery import adopted_original, context, save, setup_case

from scripts.query_policy_assertions import _catalog_discovery


def public_case():
    fixed, state, plans = setup_case()
    decisions = state["query_policy"]["decisions"]
    for index, records, count in ((0, [adopted_original()], 1), (1, [adopted_original()], 0), (2, [], 0)):
        request = context(fixed, state, plans)
        action = next(
            c
            for c in request["candidates"]
            if c["kind"] == "document_search"
            and c["arguments"]["provider"] == "fabric"
            and c["arguments"]["index"] == index
            and not c["arguments"].get("catalog_discovery")
        )
        query = save(state, action, records=records, new_versions=count)
        decisions[-1].update(
            policy_request=request, receipt={"query_index": index, "status": query["status"]}
        )
    request = context(fixed, state, plans)
    action = next(c for c in request["candidates"] if c["arguments"].get("catalog_discovery"))
    decisions.append({"action": action, "policy_request": request, "receipt": None})
    queries = []
    for query in state["queries"]:
        public = {k: copy.deepcopy(v) for k, v in query.items() if k != "records"}
        public["returned_versions"] = [
            {k: r[k] for k in ("source_id", "source_record_id", "version_id")} for r in query["records"]
        ]
        queries.append(public)
    trace = {
        **fixed["query_policy"],
        "direction": fixed["query_policy"].get("direction", "research"),
        "evidence_time": fixed["evidence_time"],
        "decisions": decisions,
    }
    view = {
        "is_demo": False,
        "search_scope": fixed["search_scope"],
        "queries": queries,
        "evidence": state["evidence"],
    }
    return view, trace


def verify(view, trace):
    _catalog_discovery(view, trace, trace["decisions"], len(trace["decisions"]) - 1)


def test_actual_returned_versions_support_partial_catalog_and_old_missing_flag_does_not():
    view, trace = public_case()
    verify(view, trace)
    legacy = copy.deepcopy(trace)
    legacy.pop("discovery_version")
    with pytest.raises(AssertionError):
        verify(view, legacy)


@pytest.mark.parametrize(
    "change",
    [
        "foreign_version",
        "missing_versions",
        "new_adoption",
        "unknown_receipt",
        "stop_reason",
        "fetch_stop",
        "wrong_index",
        "unresolved_removed",
        "future_evidence",
        "wrong_scope",
        "unfinished_page",
        "unread_manifest",
    ],
)
def test_candidate_label_cannot_replace_saved_version_scope_or_receipt_proof(change):
    view, trace = public_case()
    decision = trace["decisions"][-1]
    request = decision["policy_request"]
    if change == "foreign_version":
        view["queries"][1]["returned_versions"][0]["version_id"] = "b" * 64
    elif change == "missing_versions":
        view["queries"][1].pop("returned_versions")
    elif change == "new_adoption":
        view["queries"][2]["new_versions"] = 1
    elif change == "unknown_receipt":
        view["queries"][1]["usage"]["request_status"] = "unknown"
    elif change == "stop_reason":
        view["queries"][1]["usage"]["adoption_stop_reason"] = "budget"
    elif change == "fetch_stop":
        view["queries"][1]["usage"]["fetch"] = {"adoption_stop_reason": "budget"}
    elif change == "wrong_index":
        decision["action"]["discovery_basis"]["query_index"] = True
    elif change == "unresolved_removed":
        request["checks"][-1].update(status="passed", evidence_ids=["E1"])
    elif change == "future_evidence":
        request["evidence"] = [{"id": "not-the-adopted-original"}]
    elif change == "wrong_scope":
        request["history"][1]["arguments"]["as_of"] = "2099-01-01"
    elif change == "unfinished_page":
        request["history"][1]["pagination"].update(has_more=True, next_cursor="SYNTHETIC-next")
    else:
        request["reading_manifest"][0]["status"] = "unread"
    with pytest.raises(AssertionError):
        verify(view, trace)


@pytest.mark.parametrize("change", ["new_versions", "unknown_cost", "zero_cost", "stopped_adoption"])
def test_real_nested_fetch_shape_after_repeat_must_not_hide_failed_adoption(change):
    view, trace = public_case()
    final = trace["decisions"].pop()
    args = {
        "capability_id": "fabric.history.fetch",
        "capability_version": "SYNTHETIC-v1",
        "input_refs": [],
        "scope": final["action"]["arguments"]["scope"],
        "parameters": {"source_id": "papers", "query": "SYNTHETIC"},
    }
    action = {"id": "SYNTHETIC-fetch", "kind": "source_fetch", "arguments": args}
    trace["decisions"].append({"action": action, "receipt": {"query_index": 3, "status": "read"}})
    row = {
        "action_id": action["id"],
        "kind": "source_fetch",
        "arguments": args,
        "status": "completed",
        "new_evidence_ids": [],
        "actual_cost": {"tool_calls": 1},
    }
    final["policy_request"]["history"].append(row)
    trace["decisions"].append(final)
    q = {
        "action_id": action["id"],
        "kind": "source_fetch",
        "provider": "fabric",
        "status": "read",
        "new_versions": 0,
        "evidence_keys": [],
        "cost": {"tool_calls": 1},
        "usage": {"request_status": "received"},
    }
    view["queries"].append(q)
    verify(view, trace)
    if change == "new_versions":
        q["new_versions"] = 1
    elif change == "unknown_cost":
        q["cost"]["tool_calls"] = None
    elif change == "zero_cost":
        q["cost"]["tool_calls"] = 0
    else:
        q["usage"]["fetch"] = {"adoption_stop_reason": "budget"}
    with pytest.raises(AssertionError):
        verify(view, trace)


def test_analysis_view_accepts_fixed_version_projection_without_leaking_raw_source(monkeypatch):
    _, fixed, state = harness(monkeypatch)
    fixed.update(
        version=ANALYSIS_VERSION,
        evidence=[],
        limitations=[],
        is_demo=False,
        search_scope=["synthetic-source"],
    )
    fixed["query_policy"]["discovery_version"] = "bounded-required-gap-discovery-v1"
    state["evidence"] = []
    original = {
        "source_id": "synthetic-source",
        "source_record_id": "SYNTHETIC-original",
        "version_id": "a" * 64,
        "raw_content": "PRIVATE_RAW_SENTINEL",
    }
    state["queries"] = [
        {
            "index": 0,
            "kind": "document_search",
            "provider": "fabric",
            "phase": "initial",
            "status": "read",
            "plan": {"source_id": "synthetic-source", "terms": ["synthetic"]},
            "returned": 1,
            "new_versions": 0,
            "cost": {"tool_calls": 1},
            "records": [original],
        }
    ]
    row = SimpleNamespace(
        id="SYNTHETIC-analysis",
        investigation_id="SYNTHETIC-investigation",
        parent_analysis_id=None,
        fixed_input=fixed,
        progress=state,
        state="running",
        trigger="manual",
        created_at=datetime.now(UTC),
        completed_at=None,
        based_on_revision=1,
        error_code=None,
        result=None,
        is_demo=False,
        input_hash="a" * 64,
        note_entry_id=None,
    )
    actual = AnalysisService._view(row).model_dump(mode="json")
    assert actual["queries"][0]["returned_versions"] == [
        {k: original[k] for k in ("source_id", "source_record_id", "version_id")}
    ]
    assert "PRIVATE_RAW_SENTINEL" not in json.dumps(actual)
    fixed["query_policy"].pop("discovery_version")
    legacy = AnalysisService._view(row).model_dump(mode="json")
    assert "returned_versions" not in legacy["queries"][0]
    assert state["queries"][0]["records"][0] == original
