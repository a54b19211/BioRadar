"""Keyword indices never reinterpret a typed original-read receipt as a search."""

import copy

import pytest
from bioradar.research.investigations.query_policy import (
    _candidate,
    _indices,
    _receipt_matches,
    build_policy_context,
)
from test_query_policy import fixed, plan, progress, receipt

pytestmark = pytest.mark.ENGINE


def mixed():
    data, state = fixed("query_planner"), progress()
    search = _candidate(plan("TARGET", "absent"), "fabric", "followup", 0, data, [], {})
    state["queries"].append(receipt(search, cost={"tool_calls": 1, "tokens": 0, "relation_access": 0}))
    read_args = {
        "provider": "fabric",
        "source_id": "papers",
        "phase": "followup",
        "index": 1,
        "records": [{"source_record_id": "SYNTHETIC-native", "version_id": "1" * 64}],
        "scope": {"mode": "current", "as_of": data["as_of"], "is_demo": True},
    }
    read = {"id": "SYNTHETIC-read", "kind": "document_read", "arguments": read_args}
    state["queries"].append(
        {
            "action_id": read["id"],
            "kind": "document_read",
            "provider": "fabric",
            "phase": "followup",
            "index": 1,
            "plan": copy.deepcopy(read_args),
            "status": "read",
            "records": [],
            "returned": 0,
            "evidence_keys": [],
            "cost": {"tool_calls": 1, "tokens": 0, "relation_access": 0},
        }
    )
    state["query_policy"]["decisions"] = [{"action": search}, {"action": read}]
    return data, state


def test_mixed_search_and_read_keeps_typed_history_and_can_relax_empty_keyword_search():
    data, state = mixed()
    original = copy.deepcopy(state)
    context = build_policy_context(data, state, phase="followup", plans=[plan("TARGET", "absent")])
    assert state == original
    assert len(context["history"]) == 2
    read = next(item for item in context["history"] if item["kind"] == "document_read")
    assert read["arguments"] == original["queries"][1]["plan"]
    assert "terms" not in read["arguments"]
    relaxed = [item for item in context["candidates"] if item.get("relaxation")]
    assert len(relaxed) == 1 and relaxed[0]["arguments"]["terms"] == ["TARGET"]


def test_legacy_search_without_kind_matches_but_equal_index_read_does_not_hide_new_search():
    data, state = mixed()
    assert "kind" not in state["queries"][0]  # Preserve actual legacy keyword receipt shape.
    assert _indices([plan("TARGET", "absent")], "followup", state["queries"], data) == [0]
    assert _indices([plan("OTHER")], "followup", state["queries"], data) == [1]
    new = _candidate(plan("OTHER"), "fabric", "followup", 1, data, [], {})
    assert not _receipt_matches(state["queries"][1], new)
    context = build_policy_context(data, state, phase="followup", plans=[plan("OTHER")])
    assert any(item["arguments"].get("terms") == ["OTHER"] for item in context["candidates"])


def test_malformed_actual_search_is_not_silently_converted_to_an_empty_query():
    data, state = mixed()
    state["queries"][0]["plan"].pop("terms")
    with pytest.raises(KeyError):
        _indices([plan("TARGET")], "followup", state["queries"], data)
