"""SYNTHETIC fixed originals only; pure required-gap discovery, with no HTTP/model calls."""

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from bioradar.adapters.query_planner import decide_query
from bioradar.research.investigations.kdd import extend_context
from bioradar.research.investigations.query_policy import (
    BOUNDED_INITIAL_VERSION,
    build_policy_context,
    catalog_discovery_candidates,
    freeze_policy,
    pagination_candidates,
    question_plan_payload,
    validate_initial_plans,
)
from test_initial_language_retrieval import fixed as language_fixed
from test_query_policy import plan, progress, receipt, write_planner

pytestmark = pytest.mark.BOOT
VERSION = "bounded-required-gap-discovery-v1"
GAP = "validation_measurement"


def setup_case(*, enabled=True, source_id="papers"):
    fixed = language_fixed()
    fixed["question"] = "SYNTHETIC membrane filtration validation measurements"
    fixed["search_scope"] = [source_id]
    fixed["query_policy"]["initial_retrieval_version"] = BOUNDED_INITIAL_VERSION
    fixed["query_policy"]["pagination_version"] = "fabric-receipt-bound-pagination-v1"
    if enabled:
        fixed["query_policy"]["discovery_version"] = VERSION
    items = [plan(term, source_id=source_id) for term in ("membrane", "filtration", "validation")]
    validate_initial_plans({"queries": items}, fixed)
    state = progress()
    state["query_policy"]["initial_plans"] = copy.deepcopy(items)
    state["evidence"] = [
        {
            "key": "E1",
            "source_id": source_id,
            "title": "SYNTHETIC adopted original",
            "record_ref": {
                "provider": "data_fabric_history",
                "object_type": "source_record",
                "object_id": json.dumps([source_id, "SYNTHETIC-original", False]),
                "adopted_version": "a" * 64,
            },
        }
    ]
    state["query_policy"]["checks"] = [
        {
            "id": key,
            "question": "SYNTHETIC " + key,
            "status": status,
            "required": True,
            "importance": 1,
            "evidence_ids": refs,
        }
        for key, status, refs in (
            ("source_materials", "passed", ["E1"]),
            ("answer_scope", "passed", ["E1"]),
            (GAP, "unknown", []),
        )
    ]
    return fixed, state, items


def context(fixed, state, items):
    return build_policy_context(fixed, state, phase="initial", plans=items)


def adopted_original(source_id="papers"):
    return {
        "source_id": source_id,
        "source_record_id": "SYNTHETIC-original",
        "version_id": "a" * 64,
        "is_demo": False,
    }


def save(state, action, *, records=(), new_versions=0):
    query = receipt(
        action,
        status="read" if records else "empty",
        returned=len(records),
        records=copy.deepcopy(list(records)),
        new_versions=new_versions,
        cost={"tool_calls": 1, "tokens": 0, "relation_access": 0},
        usage={"request_status": "received"},
        pagination={"has_more": False, "next_cursor": None, "max_seq": 4, "page_number": 1},
    )
    state["queries"].append(query)
    state["query_policy"]["decisions"].append({"action": copy.deepcopy(action)})
    return query


def partial_case(*, evidence_time=None, source_id="papers"):
    fixed, state, items = setup_case(source_id=source_id)
    if evidence_time is not None:
        fixed["evidence_time"] = evidence_time
    for index, records, count in (
        (0, [adopted_original(source_id)], 1),
        (1, [adopted_original(source_id)], 0),
        (2, [], 0),
    ):
        action = next(
            x
            for x in context(fixed, state, items)["candidates"]
            if x["kind"] == "document_search"
            and x["arguments"]["index"] == index
            and not x["arguments"].get("catalog_discovery")
        )
        save(state, action, records=records, new_versions=count)
    return fixed, state, items


def directory(fixed, state, items):
    checks = context(fixed, state, items)["checks"]
    return catalog_discovery_candidates(fixed, state, items, checks)


def selector():
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory or not (Path(directory) / "src/integration/kdd.py").exists():
        pytest.skip("native pure query-planner project directory is not configured")
    return SimpleNamespace(query_planner_project_dir=Path(directory))


def test_freeze_limits_new_discovery_to_new_external_v2_and_keeps_prompt_payload(tmp_path):
    settings = write_planner(tmp_path)
    assert freeze_policy(settings)["discovery_version"] == VERSION
    assert "discovery_version" not in freeze_policy(settings, "baseline")
    assert freeze_policy(settings)["discovery_version"] == VERSION
    current, _, _ = setup_case()
    legacy = copy.deepcopy(current)
    legacy["query_policy"].pop("discovery_version")
    assert question_plan_payload(current) == question_plan_payload(legacy)


def test_unexecuted_initial_keeps_real_gap_value_when_general_coverage_is_passed():
    fixed, state, items = setup_case()
    before = copy.deepcopy((fixed, state))
    request = context(fixed, state, items)
    assert (fixed, state) == before
    assert all(GAP in x["covers"] for x in request["candidates"])
    assert all("待检索" in x["reason"] for x in request["candidates"])
    assert next(x for x in request["checks"] if x["id"] == GAP)["status"] == "unknown"
    selected = decide_query(selector(), extend_context(fixed, state, request))
    assert selected["decision"] == "continue", selected
    assert GAP in selected["action"]["covers"]


def test_partial_directory_accepts_confirmed_duplicate_then_empty_with_exact_scope():
    fixed, state, items = partial_case(
        evidence_time={
            "mode": "public_history_reconstruction",
            "cutoff": "2020-01-02T23:59:59+00:00",
        }
    )
    before = copy.deepcopy((fixed, state))
    candidates = directory(fixed, state, items)
    assert len(candidates) == 1
    assert (fixed, state) == before
    action = candidates[0]
    assert action["discovery_basis"] == {
        "version": VERSION,
        "kind": "required_gap_repeat_search",
        "query_index": 1,
        "check_ids": [GAP],
    }
    args = action["arguments"]
    assert args["source_id"] == "papers" and args["terms"] == []
    assert args["catalog_discovery"] is True and args["limit"] == 8
    assert args["as_of"] == args["scope"]["as_of"] == fixed["evidence_time"]["cutoff"]
    assert args["mode"] == args["scope"]["mode"] == "public_history_reconstruction"
    assert args["is_demo"] is args["scope"]["is_demo"] is False
    assert GAP in action["covers"]
    request = extend_context(fixed, state, context(fixed, state, items))
    assert request["discovery_version"] == VERSION
    selected = decide_query(selector(), request)
    assert selected["decision"] == "continue", selected
    assert selected["action"] == action
    limited = copy.deepcopy(request)
    limited["action_limits"]["document_search"] = 3
    limited_decision = decide_query(selector(), limited)
    assert limited_decision["decision"] == "insufficient"
    assert limited_decision["action"] is None
    save(state, action)
    assert directory(fixed, state, items) == []


def test_partial_directory_never_bypasses_unexecuted_initial_or_unread_window():
    fixed, state, items = partial_case()
    state["queries"].pop()
    state["query_policy"]["decisions"].pop()
    assert directory(fixed, state, items) == []
    action = next(x for x in context(fixed, state, items)["candidates"] if x["arguments"].get("index") == 2)
    save(state, action)
    state["evidence"][0].update(end=5, source_length=10)
    assert directory(fixed, state, items) == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"records": [adopted_original() | {"version_id": "b" * 64}]},
        {"records": [adopted_original() | {"is_demo": True}]},
        {"records": []},
        {"new_versions": 1},
        {"usage": {"request_status": "unknown"}},
        {"cost": {"tool_calls": 0}},
    ],
)
def test_zero_new_versions_is_insufficient_without_a_confirmed_adopted_duplicate(mutation):
    fixed, state, items = partial_case()
    state["queries"][1].update(copy.deepcopy(mutation))
    assert directory(fixed, state, items) == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "failed"},
        {"usage": {"request_status": "not_sent"}},
        {"returned": 1, "status": "read", "records": [adopted_original()], "new_versions": 1},
    ],
)
def test_later_unconfirmed_or_new_information_breaks_the_duplicate_segment(mutation):
    fixed, state, items = partial_case()
    state["queries"][-1].update(copy.deepcopy(mutation))
    assert directory(fixed, state, items) == []


def test_partial_directory_preserves_pending_and_real_cursor_obligations():
    fixed, state, items = partial_case()
    state["queries"][1]["pagination"].update(has_more=True, next_cursor="SYNTHETIC-next")
    assert directory(fixed, state, items) == []
    checks = context(fixed, state, items)["checks"]
    pages = pagination_candidates(fixed, state, checks)
    assert len(pages) == 1 and pages[0]["arguments"]["cursor"] == "SYNTHETIC-next"
    save(state, pages[0])
    assert directory(fixed, state, items)
    state["query_policy"]["pending_action"] = directory(fixed, state, items)[0]
    assert directory(fixed, state, items) == []


@pytest.mark.parametrize(
    "change",
    [
        {"status": "passed", "evidence_ids": ["E1"]},
        {"required": False},
        {"importance": 0},
    ],
)
def test_only_specific_important_required_unresolved_checks_enable_discovery(change):
    fixed, state, items = partial_case()
    state["query_policy"]["checks"][-1].update(change)
    assert directory(fixed, state, items) == []
    initial = progress()
    initial.update(
        evidence=copy.deepcopy(state["evidence"]), query_policy=copy.deepcopy(state["query_policy"])
    )
    initial["query_policy"]["decisions"] = []
    assert all(GAP not in x["covers"] for x in context(fixed, initial, items)["candidates"])


def test_baseline_keeps_existing_partial_directory_behavior():
    fixed, state, items = partial_case()
    fixed["query_policy"].update(discovery_version=VERSION, effective="baseline")
    assert directory(fixed, state, items) == []


def test_duplicate_adoption_stop_is_not_confirmed_exhaustion():
    fixed, state, items = partial_case()
    state["queries"][1]["usage"]["adoption_stop_reason"] = "analysis_evidence_budget"
    assert directory(fixed, state, items) == []


@pytest.mark.parametrize("outcome", ["new_version", "failed", "unknown_cost", "adoption_stopped"])
def test_later_source_fetch_invalidates_old_duplicate_basis(outcome):
    fixed, state, items = partial_case(source_id="pubmed")
    fixed["providers"] = {"fabric_fetch": {"available": True}}
    empty = progress()
    request = extend_context(fixed, empty, context(fixed, empty, items))
    action = next(candidate for candidate in request["candidates"] if candidate["kind"] == "source_fetch")
    arguments = action["arguments"]
    assert "source_id" not in arguments
    assert arguments["parameters"]["source_id"] == "pubmed"
    assert set(arguments) == {"capability_id", "capability_version", "input_refs", "scope", "parameters"}
    query = {
        "action_id": action["id"],
        "kind": "source_fetch",
        "provider": "fabric",
        "index": 3,
        "phase": "followup",
        "plan": copy.deepcopy(arguments),
        "status": "empty",
        "new_versions": 0,
        "evidence_keys": [],
        "records": [],
        "returned": 0,
        "cost": {"tool_calls": 2, "tokens": 0, "relation_access": 0},
        "usage": {"request_status": "received", "fetch": {"phase": "completed"}},
    }
    state["queries"].append(query)
    state["query_policy"]["decisions"].append({"action": action})
    assert directory(fixed, state, items)  # A confirmed zero-result fetch is not a new duplicate.
    if outcome == "new_version":
        new_record = adopted_original("pubmed") | {
            "source_record_id": "SYNTHETIC-new",
            "version_id": "b" * 64,
        }
        query.update(status="read", records=[new_record], returned=1, new_versions=1, evidence_keys=["E2"])
        new_evidence = copy.deepcopy(state["evidence"][0])
        new_evidence["key"] = "E2"
        new_evidence["record_ref"].update(
            object_id=json.dumps(["pubmed", "SYNTHETIC-new", False]),
            adopted_version="b" * 64,
        )
        state["evidence"].append(new_evidence)
    elif outcome == "failed":
        query["status"] = "failed"
    elif outcome == "unknown_cost":
        query["cost"]["tool_calls"] = None
    else:
        query["usage"]["fetch"]["adoption_stop_reason"] = "analysis_evidence_budget"
    assert directory(fixed, state, items) == []
