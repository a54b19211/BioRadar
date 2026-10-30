"""Independent invariants for saved, receipt-driven query-policy traces.

These checks validate execution provenance, budgets and frozen versions. Scientific
correctness remains a separate review against the actual source passages.
"""

from __future__ import annotations

import math

UNCONFIRMED = {"prepared", "sent", "unconfirmed", "unknown"}


def _require(condition, message):
    if not condition:
        raise AssertionError("query policy: " + message)


def _terms(value):
    return sorted({" ".join(term.casefold().split()) for term in value})


def _search_key(action):
    args = action["arguments"]
    return (
        action["kind"],
        args.get("provider"),
        args.get("source_id"),
        tuple(_terms(args.get("terms", []))),
        " ".join(args.get("query", "").casefold().split()),
    )


def assert_query_policy(view, *, expected_policy=None, terminal=True):
    """Check a saved JSON view or validated response against every actual query receipt."""
    if callable(getattr(view, "model_dump", None)):
        view = view.model_dump(mode="json")
    trace = view.get("query_policy")
    _require(isinstance(trace, dict), "analysis has no saved query_policy")
    _require(trace.get("execution_version") == "query-step-v1", "unsupported execution version")
    result = view.get("result")
    if result is not None:
        _require(result.get("query_policy") == trace, "result and analysis refer to different traces")
    _require(trace.get("requested") in {"baseline", "query_planner"}, "requested policy missing")
    _require(trace.get("effective") in {"baseline", "query_planner"}, "actual policy missing")
    if expected_policy:
        _require(trace["requested"] == expected_policy, "request did not retain selected policy")
        _require(trace["effective"] == expected_policy, "selected policy was not actually executed")
    _require(bool(trace.get("version")), "actual policy version missing")
    _require(bool(trace.get("training_status")), "actual training status missing")
    if trace.get("selector") == "heuristic" and trace.get("stop") == "rule":
        _require(trace["training_status"] == "heuristic_untrained", "heuristic mislabeled as trained")

    decisions, queries = trace.get("decisions"), view.get("queries", [])
    _require(isinstance(decisions, list) and bool(decisions), "no actual decisions saved")
    evidence_ids = {e["key"] for e in view.get("evidence", [])}
    seen_actions, seen_queries, semantic_searches, observed = set(), set(), set(), {}
    unresolved, measured_calls = set(), 0
    unknown_calls, reserved_calls = 0, 0
    for position, decision in enumerate(decisions):
        _require(decision.get("step") == position, "decision steps are not consecutive")
        _require(decision.get("decision") in {"continue", "complete", "insufficient"}, "invalid decision")
        _require(bool(decision.get("reason")), "decision has no reason")
        history = decision.get("state_summary", {}).get("observed_history")
        _require(isinstance(history, list), "decision did not save its observed receipt history")
        history_by_id = {entry["action_id"]: entry for entry in history}
        _require(len(history_by_id) == len(history), "observed receipt identity duplicated")
        for action_id, receipt in observed.items():
            prior = history_by_id.get(action_id)
            _require(prior is not None, "next decision omitted a previous actual receipt")
            _require(
                prior.get("status") == receipt["status"],
                "observed status differs from actual receipt",
            )
            if receipt.get("query_index") is not None:
                _require(
                    prior.get("query_index") == receipt["query_index"],
                    "next decision changed query receipt identity",
                )
            prior_ids = prior.get("evidence_ids", prior.get("new_evidence_ids", []))
            _require(
                set(prior_ids) == set(receipt.get("evidence_ids", [])),
                "next decision did not observe actual yielded evidence",
            )
        _require(set(history_by_id) <= seen_actions, "decision observed an action before it was executed")
        action = decision.get("action")
        if action is None:
            _require(decision["decision"] != "continue", "continue decision has no selected action")
            _require(position == len(decisions) - 1, "execution continued after a terminal decision")
            continue
        _require(decision["decision"] == "continue", "terminal decision selected an action")
        _require(not unresolved, "a new action was selected while an earlier result was unknown")
        _require(
            isinstance(action, dict) and isinstance(action.get("arguments"), dict),
            "selected action is not one object",
        )
        action_id = action.get("id")
        _require(
            isinstance(action_id, str) and action_id not in seen_actions, "selected action ID was reused"
        )
        seen_actions.add(action_id)
        if action["kind"] == "document_search":
            args = action["arguments"]
            _require(
                args.get("provider") in {"fabric", "weknora"}, "search did not select exactly one provider"
            )
            _require(
                bool(args.get("query")) != bool(args.get("terms")),
                "search needs exactly one query or terms set",
            )
            _require(
                action.get("estimated_cost", {}).get("tool_calls") == 1,
                "one provider search must cost one estimated call",
            )
            key = _search_key(action)
            if trace["effective"] == "baseline":
                key = (*key, args.get("phase"))
            _require(key not in semantic_searches, "same provider search was retried with another action ID")
            semantic_searches.add(key)
        receipt = decision.get("receipt")
        if receipt is None:
            _require(
                not terminal and position == len(decisions) - 1, "selected action lacks a durable receipt"
            )
            unresolved.add(action_id)
            continue
        _require(isinstance(receipt, dict), "receipt must be an object")
        receipt_ids = set(receipt.get("evidence_ids", []))
        _require(receipt_ids <= evidence_ids, "receipt refers to evidence absent from this frozen analysis")
        if receipt["status"] in UNCONFIRMED:
            unresolved.add(action_id)
        query_index = receipt.get("query_index")
        _require(
            isinstance(query_index, int) and 0 <= query_index < len(queries),
            "receipt query_index is not a global saved-query index",
        )
        _require(query_index not in seen_queries, "multiple actions refer to one saved query")
        seen_queries.add(query_index)
        query = queries[query_index]
        _require(query.get("action_id") == action_id, "action does not match saved query action_id")
        _require(
            query["status"] == receipt["status"],
            "trace receipt differs from actual query status",
        )
        args = action["arguments"]
        _require(
            query.get("provider", "fabric") == args.get("provider"),
            "actual provider differs from selected provider",
        )
        plan = query.get("plan", {})
        _require(plan.get("source_id") == args.get("source_id"), "actual source differs from selected source")
        if args.get("terms"):
            _require(
                _terms(plan.get("terms", [])) == _terms(args["terms"]),
                "actual terms differ from selected terms",
            )
        _require(
            set(query.get("evidence_keys", [])) == receipt_ids, "trace did not retain actual query evidence"
        )
        cost = receipt.get("cost")
        _require(isinstance(cost, dict), "actual cost missing")
        calls = cost.get("tool_calls")
        _require(query.get("cost") == cost, "trace actual cost differs from the durable query receipt")
        if calls is None:
            _require(
                receipt["status"] in UNCONFIRMED, "known outcome has no measured or explicit unknown cost"
            )
            reserve = cost.get("reserved_tool_calls")
            _require(
                isinstance(reserve, (int, float)) and math.isfinite(reserve) and reserve > 0,
                "unknown sent intent has no conservative budget reservation",
            )
            unknown_calls += 1
            reserved_calls += reserve
        else:
            _require(
                isinstance(calls, (int, float)) and math.isfinite(calls) and calls >= 0,
                "actual call cost is invalid",
            )
            if receipt["status"] in {"read", "empty", "completed", "unconfirmed", "unknown", "sent"}:
                _require(calls >= 1, "measured provider attempt was counted as free")
            measured_calls += calls
        observed[action_id] = receipt

    _require(
        seen_queries == set(range(len(queries))), "saved queries exist outside the policy decision trace"
    )
    checks = trace.get("checks", [])
    _require(isinstance(checks, list) and bool(checks), "declared coverage checks missing")
    for check in checks:
        _require(check.get("status") in {"passed", "failed", "unknown"}, "invalid coverage status")
        if check["status"] == "passed":
            refs = set(check.get("evidence_ids", []))
            _require(bool(refs) and refs <= evidence_ids, "coverage passed without adopted evidence")
    budget = trace.get("budget", {})
    limits, usage, remaining = (budget.get(name, {}) for name in ("limits", "usage", "remaining"))
    preparation_calls = usage.get("preparation_tool_calls")
    _require(
        isinstance(preparation_calls, (int, float)) and preparation_calls >= 0,
        "preparation read cost missing",
    )
    known_calls = preparation_calls + measured_calls
    _require(
        usage.get("known_tool_calls") == known_calls, "measured preparation and action costs do not reconcile"
    )
    _require(usage.get("unknown_tool_calls") == unknown_calls, "unknown action count was lost")
    _require(usage.get("reserved_tool_calls") == reserved_calls, "unknown cost reservation was lost")
    budgeted_calls = known_calls + reserved_calls
    _require(
        usage.get("budgeted_tool_calls") == budgeted_calls,
        "budget does not include conservative reservations",
    )
    _require(
        usage.get("tool_calls") is None if unknown_calls else usage.get("tool_calls") == known_calls,
        "unknown actual cost was replaced by a measured total",
    )
    for dimension, limit in limits.items():
        used = budgeted_calls if dimension == "tool_calls" else usage.get(dimension)
        left = remaining.get(dimension)
        for value in (limit, used, left):
            _require(
                value is None or isinstance(value, (int, float)) and math.isfinite(value) and value >= 0,
                "invalid budget value",
            )
        if all(isinstance(value, (int, float)) for value in (limit, used, left)):
            _require(
                math.isclose(used + left, limit, rel_tol=1e-8, abs_tol=1e-8),
                "budget accounting does not reconcile",
            )
    if terminal:
        _require(
            trace.get("status") in {"complete", "insufficient", "blocked"},
            "terminal analysis has a running trace",
        )
        if trace["status"] == "blocked":
            _require(bool(trace.get("reason")), "blocked analysis has no actual reason")
        if unresolved:
            _require(trace["status"] == "blocked", "unknown outcome was treated as completed")
            _require(
                trace.get("pending_action", {}).get("id") in unresolved,
                "unknown action identity was not preserved",
            )
        elif trace["status"] != "blocked":
            _require(
                decisions[-1]["decision"] in {"complete", "insufficient"}, "no explicit stop decision saved"
            )
    return {
        "requested": trace["requested"],
        "effective": trace["effective"],
        "version": trace["version"],
        "training_status": trace["training_status"],
        "status": trace["status"],
        "decisions": len(decisions),
        "action_tool_calls": measured_calls,
        "preparation_tool_calls": preparation_calls,
        "unknown_tool_calls": unknown_calls,
        "reserved_tool_calls": reserved_calls,
        "unsearched_checks": [check["id"] for check in checks if check["status"] != "passed"],
        "stop_reason": trace.get("reason") or decisions[-1].get("reason"),
        "scientific_review": "not_assessed_by_execution_checks",
    }
