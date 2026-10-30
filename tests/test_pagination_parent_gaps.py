"""Continuation candidates retain a real parent gap without changing cursor identity or scores."""

import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from bioradar.adapters.query_planner import decide_query
from bioradar.research.investigations.kdd import extend_context
from bioradar.research.investigations.query_policy import (
    PAGINATION_COVER_VERSION,
    PAGINATION_VERSION,
    pagination_candidates,
    semantic_key,
)
from test_initial_language_retrieval import context, fixed, plans, progress, save_empty


def paged():
    data, state, items = fixed(), progress(), plans()[:1]
    data["query_policy"].update(
        pagination_version=PAGINATION_VERSION, pagination_cover_version=PAGINATION_COVER_VERSION,
    )
    request = context(data, state, items)
    checks = copy.deepcopy(request["checks"])
    gap = {
        "id": "gap:target", "question": "SYNTHETIC specific unsolved comparison",
        "required": True, "status": "unknown", "evidence_ids": [],
    }
    checks.append(gap)
    parent = copy.deepcopy(request["candidates"][0])
    parent["covers"].append(gap["id"])
    query = save_empty(data, state, parent)
    state["query_policy"]["decisions"][-1]["policy_request"] = {"checks": copy.deepcopy(checks)}
    state["query_policy"]["checks"] = copy.deepcopy(checks)
    query.update(
        has_more=True,
        pagination={
            "has_more": True, "next_cursor": "SYNTHETIC-opaque-next", "max_seq": 29, "page_number": 1,
        },
    )
    return data, state, checks, parent, query


def test_continuation_inherits_only_current_unmet_parent_gaps_and_keeps_exact_query_identity():
    data, state, checks, parent, query = paged()
    before = copy.deepcopy((data, state, checks))
    action, = pagination_candidates(data, state, checks)
    assert "gap:target" in action["covers"]
    args = action["arguments"]
    assert args["cursor"] == query["pagination"]["next_cursor"]
    assert args["previous_action_id"] == parent["id"]
    assert args["max_seq"] == 29 and args["page_number"] == 2
    for key in ("source_id", "terms", "limit", "scope", "as_of", "mode", "is_demo"):
        assert args[key] == parent["arguments"][key]
    assert (data, state, checks) == before
    old = copy.deepcopy(data)
    old["query_policy"].pop("pagination_cover_version")
    legacy, = pagination_candidates(old, state, checks)
    assert "gap:target" not in legacy["covers"]
    assert action["id"] == legacy["id"]
    assert semantic_key(action["kind"], action["arguments"]) == semantic_key(
        legacy["kind"], legacy["arguments"]
    )


@pytest.mark.parametrize("change", [
    "passed", "no_longer_declared", "not_originally_declared", "unknown_parent",
    "mismatched_query", "duplicated_parent", "unknown_receipt",
])
def test_gap_is_never_invented_from_a_missing_invalid_or_satisfied_parent(change):
    data, state, checks, _, query = paged()
    decision = state["query_policy"]["decisions"][0]
    if change == "passed":
        checks[-1]["status"] = "passed"
    elif change == "no_longer_declared":
        checks.pop()
    elif change == "not_originally_declared":
        decision["policy_request"]["checks"].pop()
    elif change == "unknown_parent":
        state["query_policy"]["decisions"] = []
    elif change == "mismatched_query":
        query["plan"]["terms"] = ["SYNTHETIC a different query"]
    elif change == "duplicated_parent":
        state["query_policy"]["decisions"].append(copy.deepcopy(decision))
    else:
        query["status"] = "unconfirmed"
    candidates = pagination_candidates(data, state, checks)
    assert all("gap:target" not in candidate["covers"] for candidate in candidates)
    if change == "unknown_receipt":
        assert candidates == []


def test_native_score_uses_parent_gap_without_forcing_a_page_ahead_of_an_equivalent_new_query():
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.fail("Native query-planner is required")
    data, state, checks, _, _ = paged()
    page, = pagination_candidates(data, state, checks)
    ordinary = copy.deepcopy(page)
    ordinary["id"] = "SYNTHETIC-ordinary-query"
    args = ordinary["arguments"]
    for name in ("cursor", "previous_action_id", "page_number", "max_seq"):
        args.pop(name, None)
    args["terms"] = args["plan"]["terms"] = ["SYNTHETIC another real search"]
    args["index"] = 8
    request = extend_context(data, state, context(data, state, plans()[:1]))
    request["candidates"] = [page, ordinary]
    result = decide_query(SimpleNamespace(query_planner_project_dir=Path(directory)), request)
    scores = {item["id"]: item for item in result["state_summary"]["candidate_scores"]}
    assert set(scores) == {page["id"], ordinary["id"]}
    for score in scores.values():
        gap = next(item for item in score["checks"] if item["check_id"] == "gap:target")
        assert gap["expected_contribution"] > 0
    assert scores[page["id"]]["score"] == scores[ordinary["id"]]["score"]
    assert result["action"]["id"] == sorted(scores)[0]
