"""Offline fault controls for E2E provenance assertions; no service or model calls."""

from __future__ import annotations

from copy import deepcopy

from query_policy_assertions import assert_query_policy


def saved_view():
    action = {
        "id": "search-1",
        "kind": "document_search",
        "arguments": {"provider": "fabric", "source_id": "europe_pmc", "terms": ["BCMA"]},
        "covers": ["counterevidence"],
        "estimated_cost": {"tool_calls": 1},
    }
    trace = {
        "execution_version": "query-step-v1",
        "requested": "query_planner",
        "effective": "query_planner",
        "version": "offline-control",
        "selector": "heuristic",
        "stop": "rule",
        "training_status": "heuristic_untrained",
        "status": "insufficient",
        "pending_action": None,
        "checks": [{"id": "counterevidence", "status": "unknown", "evidence_ids": []}],
        "decisions": [
            {
                "step": 0,
                "decision": "continue",
                "action": action,
                "reason": "selected_by_heuristic",
                "state_summary": {"observed_history": []},
                "receipt": {
                    "query_index": 0,
                    "status": "read",
                    "evidence_ids": ["e1"],
                    "cost": {"tool_calls": 3, "tokens": None},
                },
            },
            {
                "step": 1,
                "decision": "insufficient",
                "action": None,
                "reason": "no_legal_action",
                "state_summary": {
                    "observed_history": [
                        {"action_id": "search-1", "status": "read", "query_index": 0, "evidence_ids": ["e1"]}
                    ]
                },
                "receipt": None,
            },
        ],
        "budget": {
            "limits": {"tool_calls": 10},
            "usage": {
                "tool_calls": 5,
                "known_tool_calls": 5,
                "unknown_tool_calls": 0,
                "reserved_tool_calls": 0,
                "budgeted_tool_calls": 5,
                "preparation_tool_calls": 2,
                "tokens": None,
            },
            "remaining": {"tool_calls": 5},
        },
    }
    return {
        "query_policy": trace,
        "result": {"query_policy": trace},
        "evidence": [{"key": "e1"}],
        "queries": [
            {
                "action_id": "search-1",
                "provider": "fabric",
                "status": "read",
                "plan": {"source_id": "europe_pmc", "terms": ["BCMA"]},
                "evidence_keys": ["e1"],
                "cost": trace["decisions"][0]["receipt"]["cost"],
            }
        ],
    }


def rejects(view, name):
    try:
        assert_query_policy(view)
    except AssertionError:
        return name
    raise AssertionError("Fault control was incorrectly accepted: " + name)


def main():
    accepted = saved_view()
    summary = assert_query_policy(accepted, expected_policy="query_planner")
    assert summary["action_tool_calls"] == 3 and summary["preparation_tool_calls"] == 2
    assert accepted["query_policy"]["budget"]["usage"]["tokens"] is None

    baseline = deepcopy(accepted)
    policy = baseline["query_policy"]
    policy.update(requested="baseline", effective="baseline")
    first = policy["decisions"][0]
    first["action"]["arguments"]["phase"] = "initial"
    baseline["queries"][0]["phase"] = "initial"
    second = deepcopy(first)
    second["step"] = 1
    second["action"]["id"] = "search-2"
    second["action"]["arguments"]["phase"] = "followup"
    second["state_summary"] = deepcopy(policy["decisions"][1]["state_summary"])
    second["receipt"]["query_index"] = 1
    last = deepcopy(policy["decisions"][1])
    last["step"] = 2
    last["state_summary"]["observed_history"].append(
        {"action_id": "search-2", "status": "read", "query_index": 1, "evidence_ids": ["e1"]}
    )
    policy["decisions"] = [first, second, last]
    query = deepcopy(baseline["queries"][0])
    query.update(action_id="search-2", phase="followup", cost=second["receipt"]["cost"])
    baseline["queries"].append(query)
    policy["budget"]["usage"].update(tool_calls=8, known_tool_calls=8, budgeted_tool_calls=8)
    policy["budget"]["remaining"]["tool_calls"] = 2
    assert_query_policy(baseline, expected_policy="baseline")

    faults = []
    changed = deepcopy(baseline)
    changed["query_policy"].update(requested="query_planner", effective="query_planner")
    faults.append(rejects(changed, "planner_repeats_completed_search_across_phases"))
    changed = deepcopy(accepted)
    changed["query_policy"]["decisions"][1]["state_summary"]["observed_history"] = []
    faults.append(rejects(changed, "preplanned_steps_without_observed_receipts"))
    changed = deepcopy(accepted)
    changed["query_policy"]["decisions"][1]["state_summary"]["observed_history"][0]["status"] = "empty"
    faults.append(rejects(changed, "observed_status_differs_from_actual_read"))
    changed = deepcopy(accepted)
    changed["query_policy"]["checks"][0]["status"] = "passed"
    faults.append(rejects(changed, "possible_coverage_mislabeled_as_supported"))
    changed = deepcopy(accepted)
    changed["queries"][0]["provider"] = "weknora"
    faults.append(rejects(changed, "provider_switched_after_selection"))
    changed = deepcopy(accepted)
    changed["queries"].append(deepcopy(changed["queries"][0]))
    faults.append(rejects(changed, "untraced_query_bypassed_policy"))
    changed = deepcopy(accepted)
    changed["query_policy"]["budget"]["usage"]["tool_calls"] = 3
    faults.append(rejects(changed, "preparation_cost_omitted"))

    blocked = deepcopy(accepted)
    trace = blocked["query_policy"]
    trace.update(status="blocked", reason="analysis_query_outcome_unconfirmed")
    trace["pending_action"] = trace["decisions"][0]["action"]
    trace["decisions"][0]["receipt"].update(
        status="unconfirmed", evidence_ids=[], cost={"tool_calls": 1, "tokens": None}
    )
    trace["decisions"][1]["reason"] = "pending_execution_or_reconciliation"
    trace["decisions"][1]["state_summary"]["observed_history"][0].update(
        status="unconfirmed", evidence_ids=[]
    )
    trace["budget"]["usage"].update(tool_calls=3, known_tool_calls=3, budgeted_tool_calls=3)
    trace["budget"]["remaining"]["tool_calls"] = 7
    blocked["queries"][0].update(
        status="unconfirmed", evidence_keys=[], cost=trace["decisions"][0]["receipt"]["cost"]
    )
    assert_query_policy(blocked)
    interrupted = deepcopy(blocked)
    policy = interrupted["query_policy"]
    unknown_cost = {"tool_calls": None, "reserved_tool_calls": 4, "tokens": None}
    policy["decisions"][0]["receipt"]["cost"] = unknown_cost
    interrupted["queries"][0]["cost"] = unknown_cost
    policy["budget"]["usage"].update(
        tool_calls=None,
        known_tool_calls=2,
        unknown_tool_calls=1,
        reserved_tool_calls=4,
        budgeted_tool_calls=6,
    )
    policy["budget"]["remaining"]["tool_calls"] = 4
    assert_query_policy(interrupted)
    interrupted_without_stop = deepcopy(interrupted)
    interrupted_without_stop["query_policy"]["decisions"] = interrupted_without_stop["query_policy"][
        "decisions"
    ][:1]
    assert_query_policy(interrupted_without_stop)

    not_sent = deepcopy(accepted)
    policy = not_sent["query_policy"]
    policy.update(status="blocked", reason="history_source_revoked")
    policy["decisions"] = policy["decisions"][:1]
    policy["decisions"][0]["receipt"].update(
        status="failed", evidence_ids=[], cost={"tool_calls": 0, "tokens": None}
    )
    not_sent["queries"][0].update(
        status="failed",
        evidence_keys=[],
        usage={"request_status": "not_sent"},
        cost=policy["decisions"][0]["receipt"]["cost"],
    )
    policy["budget"]["usage"].update(tool_calls=2, known_tool_calls=2, budgeted_tool_calls=2)
    policy["budget"]["remaining"]["tool_calls"] = 8
    assert assert_query_policy(not_sent)["stop_reason"] == "history_source_revoked"
    changed = deepcopy(not_sent)
    changed["query_policy"].pop("reason")
    faults.append(rejects(changed, "known_blocked_receipt_has_no_actual_block_reason"))
    changed = deepcopy(not_sent)
    changed["query_policy"]["decisions"][0]["receipt"] = None
    faults.append(rejects(changed, "known_failed_query_omitted_from_trace"))
    changed = deepcopy(interrupted)
    changed["query_policy"]["budget"]["usage"]["tool_calls"] = 2
    faults.append(rejects(changed, "unknown_intent_cost_invented_as_a_known_total"))
    changed = deepcopy(blocked)
    changed["query_policy"]["pending_action"] = {"id": "replacement-id"}
    faults.append(rejects(changed, "unknown_outcome_lost_original_action_identity"))
    changed = deepcopy(blocked)
    decision = changed["query_policy"]["decisions"][1]
    action = deepcopy(changed["query_policy"]["decisions"][0]["action"])
    action["id"] = "replacement-id"
    decision.update(decision="continue", action=action)
    faults.append(rejects(changed, "unknown_outcome_retried_with_a_new_id"))
    print(
        {
            "status": "passed",
            "accepted_controls": 6,
            "rejected_faults": faults,
            "qualification": "Offline assertion controls only; no scientific or integration result.",
        }
    )


if __name__ == "__main__":
    main()
