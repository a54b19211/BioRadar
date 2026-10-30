"""Independent invariants for saved, receipt-driven query-policy traces.

These checks validate execution provenance, budgets and frozen versions. Scientific
correctness remains a separate review against the actual source passages.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

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


_KDD_KINDS = {"dataset_prepare", "analysis_run", "professional_assess"}
_ARTIFACT_KINDS = {"source_documents", "analysis_dataset", "pattern_result", "professional_assessment"}


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _refs(values):
    _require(isinstance(values, list), "artifact references must be a list")
    refs = []
    for ref in values:
        _require(
            isinstance(ref, dict)
            and isinstance(ref.get("id"), str)
            and bool(ref["id"])
            and isinstance(ref.get("version"), str)
            and bool(ref["version"]),
            "artifact reference lacks its fixed identity or version",
        )
        refs.append((ref["id"], ref["version"]))
    _require(len(refs) == len(set(refs)), "artifact reference was duplicated")
    return set(refs)


def _same_time(left, right):
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and datetime.fromisoformat(left.replace("Z", "+00:00"))
        == datetime.fromisoformat(right.replace("Z", "+00:00"))
    )


def _dataset(payload):
    _require(isinstance(payload, dict), "dataset payload is not visible")
    _require(payload.get("schema_version") == "bioradar-analysis-dataset-v1", "unknown dataset schema")
    unhashed = {"dataset_hash", "selection_summary", "dataset_hash_scope"}
    _require(
        payload.get("dataset_hash") == _hash({k: v for k, v in payload.items() if k not in unhashed}),
        "saved dataset content differs from its frozen hash",
    )
    units = payload.get("units")
    _require(isinstance(units, list), "dataset has no saved analysis units")
    _require(payload.get("qualified_count") == len(units), "analysis unit count differs from members")
    _require(
        payload.get("qualified_count_unit") == "analysis_units_not_documents_or_population",
        "dataset mislabeled analysis units as documents or population",
    )
    unit_ids = [unit.get("unit_id") for unit in units]
    _require(
        all(isinstance(key, str) and key for key in unit_ids) and len(set(unit_ids)) == len(unit_ids),
        "analysis unit identity missing or duplicated",
    )
    _require(all(unit.get("members") for unit in units), "analysis unit has no fixed original members")
    denominator = payload.get("denominator", {})
    _require(
        denominator.get("population_total") is None and denominator.get("population_rate_permitted") is False,
        "descriptive analysis invented population coverage or rates",
    )
    return payload


def _pattern(payload):
    _require(isinstance(payload, dict), "pattern payload is not visible")
    _require(payload.get("compute_version") == "bioradar-mining-compute-v0.3", "not a saved KDD computation")
    _require(
        payload.get("result_hash") == _hash({k: v for k, v in payload.items() if k != "result_hash"}),
        "saved pattern content differs from its frozen hash",
    )
    data = _dataset(payload.get("data_preparation"))
    generated = {}
    for method in payload.get("method_results", []):
        for candidate in method.get("candidates", []):
            key = candidate["candidate_key"]
            _require(key not in generated or generated[key] == candidate, "conflicting candidate identity")
            generated[key] = candidate
    evaluations = {row["candidate_key"]: row for row in payload.get("pattern_evaluations", [])}
    selections = {row["candidate_key"]: row for row in payload.get("selection_decisions", [])}
    _require(
        len(evaluations) == len(payload.get("pattern_evaluations", []))
        and len(selections) == len(payload.get("selection_decisions", []))
        and generated.keys() == evaluations.keys() == selections.keys(),
        "saved pattern omitted generated candidates or their evaluations",
    )
    selected = {row["candidate_key"]: row for row in payload.get("candidates", [])}
    _require(
        selected.keys() == {key for key, row in selections.items() if row["decision"] == "selected"},
        "selected patterns differ from saved selection decisions",
    )
    _require(all(row == generated[key] for key, row in selected.items()), "selected pattern content changed")
    for key, row in selections.items():
        _require(
            row.get("decision") in {"selected", "value_excluded", "capacity_deferred", "redundancy_deferred"}
            and bool(row.get("reasons")),
            "selection result or reason missing",
        )
        _require(
            evaluations[key].get("eligible") is (row["decision"] != "value_excluded"),
            "value exclusion and review capacity were conflated",
        )
        _require(
            evaluations[key].get("uncertainty", {}).get("statistical_significance") == "not_established",
            "descriptive pattern was mislabeled statistically validated",
        )
    return data


def _artifact_store(trace):
    values, payloads, executions = (
        trace.get("artifacts"),
        trace.get("artifact_results"),
        trace.get("execution"),
    )
    _require(
        isinstance(values, list) and isinstance(payloads, dict) and isinstance(executions, list),
        "v2 fixed artifacts, results or execution are not visible",
    )
    artifacts = {row["id"]: row for row in values}
    _require(len(artifacts) == len(values), "saved artifact identity duplicated")
    _require(set(payloads) <= artifacts.keys(), "artifact payload has no saved identity")
    for artifact in values:
        _require(artifact.get("kind") in _ARTIFACT_KINDS, "unknown artifact kind")
        _require(bool(artifact.get("version")), "artifact version missing")
        scope = artifact.get("scope", {})
        _require(scope.get("direction") == trace.get("direction"), "artifact changed analysis direction")
        fixed_time = trace.get("evidence_time", {})
        _require(
            all(scope.get(key) == fixed_time.get(key) for key in ("mode", "cutoff")),
            "artifact changed fixed evidence time",
        )
        kind = artifact["kind"]
        if kind == "source_documents":
            _require(bool(artifact.get("snapshot_ids")), "source artifact has no adopted snapshots")
            continue
        source = artifacts.get(artifact.get("source_artifact_id"), {})
        _require(
            source.get("kind") == "source_documents"
            and source.get("version") == artifact.get("source_version"),
            "artifact is not bound to the fixed source version",
        )
        payload = payloads.get(artifact["id"])
        if kind == "analysis_dataset":
            data = _dataset(payload)
            _require(
                artifact["version"] == data["dataset_hash"]
                and artifact.get("qualified_count") == data["qualified_count"],
                "dataset artifact differs from saved result",
            )
        elif kind == "pattern_result":
            data = _pattern(payload)
            _require(
                artifact["version"] == payload["result_hash"]
                and artifact.get("dataset_hash") == data["dataset_hash"]
                and artifact.get("candidate_keys") == [row["candidate_key"] for row in payload["candidates"]]
                and artifact.get("method_statuses")
                == [row["execution_status"] for row in payload["method_results"]],
                "pattern artifact differs from actual saved computation",
            )
        else:
            _require(
                isinstance(payload, dict)
                and artifact["version"] == _hash(payload)
                and artifact.get("provider") == payload.get("provider")
                and artifact.get("status") == payload.get("status"),
                "professional artifact differs from its actual saved assessment",
            )
        if kind in {"analysis_dataset", "pattern_result"}:
            _require(
                data.get("direction") == scope.get("direction")
                and _same_time(data.get("as_of"), scope.get("cutoff")),
                "computed dataset changed the fixed direction or cutoff",
            )
    by_action = {row["action_id"]: row for row in executions}
    _require(len(by_action) == len(executions), "capability execution identity duplicated")
    produced = set()
    known = {(row["id"], row["version"]) for row in values}
    for entry in executions:
        _require(entry.get("kind") in _KDD_KINDS, "unknown capability execution kind")
        outputs = _refs(entry.get("output_refs"))
        _require(outputs and outputs <= known, "capability output has no saved artifact version")
        _require(not produced & outputs, "artifact output was attributed to multiple executions")
        produced.update(outputs)
    return artifacts, payloads, by_action, known - produced


def _kdd_receipt(action, query, receipt, artifacts, executions, available):
    kind, args = action["kind"], action["arguments"]
    _require(query.get("kind") == kind, "actual capability kind differs from selected action")
    _require(
        query.get("provider") == ("professional" if kind == "professional_assess" else "radar"),
        "actual capability provider differs from selected action",
    )
    _require(query.get("plan") == args, "actual capability input differs from frozen action")
    _require(args.get("capability_id") and args.get("capability_version"), "capability version missing")
    inputs = _refs(args.get("input_refs"))
    _require(inputs and inputs <= available, "capability used an absent or future artifact")
    input_kinds = {
        "dataset_prepare": "source_documents",
        "analysis_run": "analysis_dataset",
        "professional_assess": "source_documents",
    }
    _require(
        len(inputs) == 1 and all(artifacts[key]["kind"] == input_kinds[kind] for key, _ in inputs),
        "capability used the wrong input artifact kind",
    )
    scope = args.get("scope", {})
    _require(
        all(
            scope.get("direction") == artifacts[key].get("scope", {}).get("direction")
            and scope.get("mode") == artifacts[key].get("scope", {}).get("mode")
            and _same_time(scope.get("as_of"), artifacts[key].get("scope", {}).get("cutoff"))
            for key, _ in inputs
        ),
        "capability changed its fixed input scope",
    )
    outputs = _refs(query.get("artifact_refs", []))
    _require(outputs == _refs(receipt.get("new_artifact_refs", [])), "trace lost actual capability outputs")
    _require(
        query.get("check_updates", []) == receipt.get("check_updates", []),
        "trace lost actual capability check updates",
    )
    _require(
        all(artifacts.get(key, {}).get("version") == version for key, version in outputs),
        "receipt refers to a different artifact version",
    )
    execution = executions.get(action["id"])
    if query["status"] == "read":
        _require(execution is not None, "successful capability has no durable execution record")
        _require(
            execution["kind"] == kind
            and _refs(execution["input_refs"]) == inputs
            and _refs(execution["output_refs"]) == outputs,
            "durable capability execution differs from selected action or receipt",
        )
        expected = {
            "dataset_prepare": "analysis_dataset",
            "analysis_run": "pattern_result",
            "professional_assess": "professional_assessment",
        }[kind]
        _require(
            len(outputs) == 1 and all(artifacts[key]["kind"] == expected for key, _ in outputs),
            "capability returned the wrong artifact kind",
        )
    else:
        _require(not outputs and execution is None, "failed or unknown capability claimed completed outputs")
    if kind == "analysis_run" and outputs:
        _require(
            all(artifacts[key].get("dataset_hash") == next(iter(inputs))[1] for key, _ in outputs),
            "pattern was not computed from the selected dataset version",
        )
    if kind in {"dataset_prepare", "analysis_run"}:
        _require(
            query.get("cost", {}).get("tool_calls") == 1
            and query.get("cost", {}).get("tokens") == 0
            and query.get("cost", {}).get("model_calls", 0) == 0,
            "local computation must count one tool action and zero model calls/tokens",
        )
    return outputs


def _capability_model_cost(view, action, query):
    calls = [row for row in view.get("calls", []) if row.get("action_id") == action["id"]]
    if action["kind"] != "professional_assess":
        _require(not calls, "pure computation unexpectedly invoked a model")
        return
    actual_ids = [row.get("call_id") for row in calls]
    saved_ids = (query.get("usage") or {}).get("model_call_ids")
    _require(
        isinstance(saved_ids, list) and len(actual_ids) == len(set(actual_ids)) and saved_ids == actual_ids,
        "professional receipt lost or changed its actual model call identities",
    )
    count, known, budgeted, unknown = 0, 0, 0, False
    pending = False
    for call in calls:
        receipt = call.get("receipt") or {}
        if call["status"] == "prepared" or receipt.get("status") == "not_sent":
            continue
        count += 1
        usage = receipt.get("usage") or {}
        tokens = usage.get("total_tokens")
        if tokens is None and all(
            usage.get(key) is not None for key in ("prompt_tokens", "completion_tokens")
        ):
            tokens = usage["prompt_tokens"] + usage["completion_tokens"]
        pending |= call["status"] in {"sent", "unconfirmed"} or receipt.get("status") == "unconfirmed"
        if tokens is None:
            unknown = True
            reserve = call.get("token_upper_bound")
            _require(
                isinstance(reserve, (int, float)) and math.isfinite(reserve) and reserve > 0,
                "unknown professional model usage has no conservative token reservation",
            )
            budgeted += reserve
        else:
            _require(
                isinstance(tokens, (int, float)) and math.isfinite(tokens) and tokens >= 0,
                "professional model token receipt is invalid",
            )
            known += tokens
            budgeted += tokens
    cost = query.get("cost") or {}
    _require(
        cost.get("model_calls") == count
        and cost.get("budgeted_tokens") == budgeted
        and (cost.get("tokens") is None if unknown else cost.get("tokens") == known),
        "professional action cost does not reconcile with its actual model ledger",
    )
    _require(
        not pending or query["status"] in UNCONFIRMED,
        "unknown professional model result was treated as a completed action",
    )


def _artifact_check(check, artifacts, payloads):
    refs = _refs(check.get("artifact_refs", []))
    _require(
        refs and all(artifacts.get(key, {}).get("version") == version for key, version in refs),
        "coverage passed without actual fixed artifact versions",
    )
    kind = check.get("id")
    allowed = {
        "kdd_dataset": {"analysis_dataset", "pattern_result"},
        "kdd_pattern": {"pattern_result"},
        "kdd_mechanism_review": {"professional_assessment"},
    }
    _require(kind in allowed, "ordinary evidence coverage was replaced with a computed artifact")
    _require(
        all(artifacts[key]["kind"] in allowed[kind] for key, _ in refs),
        "coverage refers to the wrong artifact kind",
    )
    if kind == "kdd_dataset":
        _require(
            any(
                (
                    payloads[key]
                    if artifacts[key]["kind"] == "analysis_dataset"
                    else payloads[key]["data_preparation"]
                )["qualified_count"]
                > 0
                for key, _ in refs
            ),
            "dataset coverage passed without qualified units",
        )
    elif kind == "kdd_pattern":
        _require(
            any(
                method["execution_status"] in {"success", "partial"}
                for key, _ in refs
                for method in payloads[key]["method_results"]
            ),
            "pattern coverage passed without an executed applicable method",
        )
    else:
        _require(
            any(payloads[key].get("status") == "completed" for key, _ in refs),
            "professional coverage passed without a completed assessment",
        )


def assert_professional_http_accounting(view):
    """Reconcile visible saved attempts without filling a cost residual or exposing requests."""
    trace = view["query_policy"]
    ledger = trace.get("professional_http_ledger")
    _require(isinstance(ledger, list), "professional HTTP ledger missing")
    fields = {
        "operation_id",
        "provider",
        "operation",
        "action_id",
        "exchange_index",
        "attempts",
        "http_attempted_indices",
        "outcome",
        "known_tool_calls",
        "reserved_tool_calls",
        "accounting_basis",
        "response_received",
    }
    seen, by_action, non_route_known, non_route_reserved, http_unknown = set(), {}, 0, 0, 0
    queries = {query.get("action_id"): query for query in view.get("queries", [])}
    for row in ledger:
        legacy_fields = fields - {"accounting_basis", "response_received"}
        _require(
            isinstance(row, dict) and set(row) in (fields, legacy_fields),
            "unsafe or incomplete HTTP ledger row",
        )
        key = row["operation_id"], row["exchange_index"]
        _require(
            isinstance(key[0], str) and bool(key[0]) and type(key[1]) is int and key[1] >= 0,
            "invalid HTTP exchange identity",
        )
        _require(key not in seen, "HTTP exchange was counted twice")
        seen.add(key)
        _require(
            row["provider"] in {None, "biocheck", "mech_evidence"}
            and isinstance(row["operation"], str)
            and bool(row["operation"]),
            "HTTP operation provider or name missing",
        )
        attempts, indices = row["attempts"], row["http_attempted_indices"]
        _require(
            type(attempts) is int
            and attempts >= 0
            and isinstance(indices, list)
            and all(type(index) is int and 0 < index <= attempts for index in indices)
            and indices == sorted(set(indices)),
            "HTTP actual attempt indices are invalid",
        )
        _require(
            type(row.get("response_received", False)) is bool, "HTTP received-response evidence flag missing"
        )
        legacy_received = (
            not indices
            and row.get("response_received", False)
            and row["outcome"] == "received"
            and attempts > 0
        )
        basis = (
            "http_attempted_indices"
            if indices
            else ("legacy_received_response" if legacy_received else "unmeasured_attempts")
        )
        _require(
            row.get("accounting_basis", "http_attempted_indices" if indices else "unmeasured_attempts")
            == basis,
            "HTTP accounting basis differs from provable evidence",
        )
        known = len(indices) if indices else int(legacy_received)
        reserved = attempts - known
        _require(
            row["known_tool_calls"] == known and row["reserved_tool_calls"] == reserved,
            "HTTP known and reserved counts differ from actual attempt indices",
        )
        _require(
            row["outcome"] in {"prepared", "sent", "received", "failed", "unknown", "not_sent"},
            "HTTP outcome is missing",
        )
        http_unknown += int(attempts > 0 and row["outcome"] in {"sent", "unknown"})
        action_id = row["action_id"]
        if action_id is None:
            non_route_known += known
            non_route_reserved += reserved
        else:
            _require(
                isinstance(action_id, str)
                and action_id in queries
                and queries[action_id].get("provider") == "professional",
                "HTTP route action is missing or not professional",
            )
            count = by_action.setdefault(action_id, [0, 0])
            count[0] += known
            count[1] += reserved
    route_known = route_reserved = 0
    for query in view.get("queries", []):
        cost = query.get("cost") or {}
        known = cost.get("tool_calls")
        if query.get("provider") == "professional":
            saved_known, saved_reserved = by_action.get(query.get("action_id"), [0, 0])
            _require(known is None or known == saved_known, "professional HTTP was omitted or double-counted")
            _require(
                cost.get("reserved_tool_calls", 0) == saved_reserved,
                "professional route HTTP reservation differs from actual exchange ledger",
            )
            route_known += saved_known
        elif known is not None:
            _require(type(known) is int and known >= 0, "invalid measured route request count")
            route_known += known
        if known is None:
            route_reserved += cost.get("reserved_tool_calls", 0)
    usage = trace["budget"]["usage"]
    preparation = usage["preparation_tool_calls"]
    known_total = preparation + route_known + non_route_known
    legacy_known = sum(
        row["known_tool_calls"] for row in ledger if row.get("accounting_basis") == "legacy_received_response"
    )
    recorded_known = usage.get("recorded_known_tool_calls", usage.get("known_tool_calls"))
    _require(
        type(recorded_known) is int and recorded_known <= known_total <= recorded_known + legacy_known,
        "projected known total differs from stored usage beyond proven legacy responses",
    )
    unmeasured = sum(
        row["outcome"] == "received" and row.get("accounting_basis") == "unmeasured_attempts"
        for row in ledger
    )
    _require(
        usage.get("unmeasured_professional_http_exchanges", 0) == unmeasured,
        "unmeasured received HTTP exchange was treated as fully measured",
    )
    reserved_total = route_reserved + non_route_reserved
    _require(
        usage.get("route_action_tool_calls") == route_known,
        "route action request component does not reconcile",
    )
    _require(
        usage.get("non_route_professional_tool_calls") == non_route_known,
        "non-route professional HTTP component does not reconcile",
    )
    _require(
        usage.get("known_tool_calls") == known_total,
        "preparation + route actions + non-route professional HTTP does not equal known total",
    )
    _require(
        usage.get("reserved_tool_calls") == reserved_total
        and usage.get("budgeted_tool_calls") == known_total + reserved_total,
        "HTTP conservative reservations do not reconcile",
    )
    _require(
        usage.get("professional_http_unknown_outcomes") == http_unknown,
        "HTTP unknown outcomes were lost or mixed with model outcomes",
    )
    _require(
        usage.get("model_unconfirmed_calls")
        == sum(call.get("status") in {"sent", "unconfirmed"} for call in view.get("calls", [])),
        "model unknown outcomes were lost or mixed with HTTP outcomes",
    )
    return {
        "known_total_tool_calls": known_total,
        "route_action_tool_calls": route_known,
        "non_route_professional_tool_calls": non_route_known,
        "non_route_reserved_tool_calls": non_route_reserved,
        "professional_http_unknown_outcomes": http_unknown,
        "unmeasured_professional_http_exchanges": unmeasured,
    }


def assert_query_policy(view, *, expected_policy=None, terminal=True):
    """Check a saved JSON view or validated response against every actual query receipt."""
    if callable(getattr(view, "model_dump", None)):
        view = view.model_dump(mode="json")
    trace = view.get("query_policy")
    _require(isinstance(trace, dict), "analysis has no saved query_policy")
    _require(trace.get("execution_version") == "query-step-v1", "unsupported execution version")
    schema = trace.get("schema_version", "query-planner.external.v1")
    _require(
        schema in {"query-planner.external.v1", "query-planner.external.v2"}, "unsupported policy schema"
    )
    v2 = schema == "query-planner.external.v2"
    artifacts, artifact_payloads, executions, available_artifacts = (
        _artifact_store(trace) if v2 else ({}, {}, {}, set())
    )
    result = view.get("result")
    if result is not None:
        _require(result.get("query_policy") == trace, "result and analysis refer to different traces")
    _require(trace.get("requested") in {"baseline", "query_planner"}, "requested policy missing")
    _require(trace.get("effective") in {"baseline", "query_planner"}, "actual policy missing")
    if expected_policy:
        _require(trace["requested"] == expected_policy, "request did not retain selected policy")
        _require(trace["effective"] == expected_policy, "selected policy was not actually executed")
    _require(bool(trace.get("version")), "actual policy version missing")
    _require(bool(trace.get("selector")), "actual policy selector missing")
    if not v2:
        _require(bool(trace.get("stop")), "v1 actual policy stop rule missing")
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
            if v2:
                _require(
                    _refs(prior.get("new_artifact_refs", [])) == _refs(receipt.get("new_artifact_refs", [])),
                    "next decision did not observe actual yielded artifacts",
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
        if v2 and action["kind"] in _KDD_KINDS:
            key = (
                action["kind"],
                _hash(
                    {
                        key: value
                        for key, value in action["arguments"].items()
                        if key not in {"phase", "index", "reason", "decision_step"}
                    }
                ),
            )
            _require(key not in semantic_searches, "same capability was retried with another action ID")
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
        if v2 and action["kind"] in _KDD_KINDS:
            available_artifacts.update(
                _kdd_receipt(action, query, receipt, artifacts, executions, available_artifacts)
            )
            _capability_model_cost(view, action, query)
        elif v2 and action["kind"] in {"entity_relations", "graph_neighbors", "graph_path"}:
            _require(
                query.get("provider") == "fabric_graph"
                and query.get("kind") == action["kind"]
                and query.get("plan") == args,
                "actual fixed graph operation differs from selected action",
            )
        else:
            _require(
                query.get("provider", "fabric") == args.get("provider"),
                "actual provider differs from selected provider",
            )
            plan = query.get("plan", {})
            _require(
                plan.get("source_id") == args.get("source_id"), "actual source differs from selected source"
            )
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
            if receipt["status"] in {"read", "empty", "completed", "unconfirmed", "unknown", "sent"} and not (
                v2 and action["kind"] in _KDD_KINDS
            ):
                _require(calls >= 1, "measured provider attempt was counted as free")
            measured_calls += calls
        observed[action_id] = receipt

    _require(
        seen_queries == set(range(len(queries))), "saved queries exist outside the policy decision trace"
    )
    if v2:
        _require(
            set(executions) <= seen_actions,
            "capability execution has no actual policy decision",
        )
    checks = trace.get("checks", [])
    _require(isinstance(checks, list) and bool(checks), "declared coverage checks missing")
    for check in checks:
        _require(check.get("status") in {"passed", "failed", "unknown"}, "invalid coverage status")
        if check["status"] == "passed":
            refs = set(check.get("evidence_ids", []))
            if v2 and check.get("artifact_refs"):
                _artifact_check(check, artifacts, artifact_payloads)
                _require(refs <= evidence_ids, "artifact coverage refers to missing original evidence")
            else:
                _require(bool(refs) and refs <= evidence_ids, "coverage passed without adopted evidence")
    budget = trace.get("budget", {})
    limits, usage, remaining = (budget.get(name, {}) for name in ("limits", "usage", "remaining"))
    preparation_calls = usage.get("preparation_tool_calls")
    _require(
        isinstance(preparation_calls, (int, float)) and preparation_calls >= 0,
        "preparation read cost missing",
    )
    non_route_professional_calls = 0
    unmeasured_professional_exchanges = 0
    if "professional_http_ledger" in trace:
        accounting = assert_professional_http_accounting(view)
        measured_calls = accounting["route_action_tool_calls"]
        non_route_professional_calls = accounting["non_route_professional_tool_calls"]
        reserved_calls += accounting["non_route_reserved_tool_calls"]
        unmeasured_professional_exchanges = accounting["unmeasured_professional_http_exchanges"]
    known_calls = preparation_calls + measured_calls + non_route_professional_calls
    _require(
        usage.get("known_tool_calls") == known_calls,
        "measured preparation, route and non-route professional costs do not reconcile",
    )
    _require(usage.get("unknown_tool_calls") == unknown_calls, "unknown action count was lost")
    _require(usage.get("reserved_tool_calls") == reserved_calls, "unknown cost reservation was lost")
    budgeted_calls = known_calls + reserved_calls
    _require(
        usage.get("budgeted_tool_calls") == budgeted_calls,
        "budget does not include conservative reservations",
    )
    _require(
        usage.get("tool_calls") is None
        if unknown_calls or reserved_calls or unmeasured_professional_exchanges
        else usage.get("tool_calls") == known_calls,
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
        "non_route_professional_tool_calls": non_route_professional_calls,
        "unknown_tool_calls": unknown_calls,
        "reserved_tool_calls": reserved_calls,
        "unsearched_checks": [check["id"] for check in checks if check["status"] != "passed"],
        "stop_reason": trace.get("reason") or decisions[-1].get("reason"),
        "scientific_review": "not_assessed_by_execution_checks",
    }
