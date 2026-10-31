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
    if "cursor" in args:
        return (
            "fabric-page",
            args.get("provider"),
            args.get("source_id"),
            tuple(_terms(args.get("terms", []))),
            args.get("limit"),
            _hash(args.get("scope")),
            args.get("catalog_discovery", False),
            args.get("cursor"),
        )
    return (
        action["kind"],
        args.get("provider"),
        args.get("source_id"),
        tuple(_terms(args.get("terms", []))),
        " ".join(args.get("query", "").casefold().split()),
    )


def _repeat_catalog(view, trace, decisions, position):
    """Verify source-version receipts independently of the producer's candidate marker."""
    decision = decisions[position]
    action, request = decision["action"], decision.get("policy_request") or {}
    args, basis = action["arguments"], action.get("discovery_basis") or {}
    version = "bounded-required-gap-discovery-v1"
    _require(
        trace.get("discovery_version") == request.get("discovery_version") == version
        and basis.get("version") == version
        and basis.get("kind") == "required_gap_repeat_search"
        and set(basis) == {"version", "kind", "query_index", "check_ids"},
        "partial catalog lacks its frozen discovery policy and basis",
    )
    _require(
        any(c == action for c in request.get("candidates", [])),
        "partial catalog was not the recorded candidate",
    )
    checks = {c["id"]: c for c in request.get("checks", [])}
    ids, index = basis.get("check_ids"), basis.get("query_index")
    _require(
        isinstance(ids, list)
        and ids
        and len(ids) == len(set(ids))
        and set(ids) <= set(action.get("covers", []))
        and all(
            cid in checks
            and cid not in {"source_materials", "answer_scope"}
            and not cid.startswith("kdd_")
            and checks[cid].get("required", True) is True
            and type(checks[cid].get("importance", 1)) in (int, float)
            and checks[cid].get("importance", 1) > 0
            and checks[cid].get("status") != "passed"
            for cid in ids
        ),
        "partial catalog has no important unresolved check",
    )
    history, queries = request.get("history", []), view.get("queries", [])
    _require(
        type(index) is int and 0 <= index < len(history) <= len(queries),
        "partial catalog basis is not a global saved query index",
    )
    _require(
        not any(h.get("status") in {"prepared", "sent", "unknown", "unconfirmed"} for h in history)
        and bool(request.get("reading_manifest"))
        and all(r.get("status") == "read" for r in request["reading_manifest"]),
        "partial catalog bypassed pending or unread work",
    )
    evidence_ids = {e["id"] for e in request.get("evidence", [])}
    adopted = set()
    for evidence in view.get("evidence", []):
        if evidence.get("key") not in evidence_ids:
            continue
        ref = evidence.get("record_ref") or {}
        try:
            identity = json.loads(ref["object_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            ref.get("provider") == "data_fabric_history"
            and isinstance(identity, list)
            and len(identity) == 3
            and identity[0] == evidence.get("source_id")
            and identity[2] is view["is_demo"]
            and isinstance(ref.get("adopted_version"), str)
        ):
            adopted.add((identity[0], identity[1], ref["adopted_version"]))
    prior = {
        d["receipt"]["query_index"]: d
        for d in decisions[:position]
        if isinstance(d.get("receipt"), dict) and type(d["receipt"].get("query_index")) is int
    }
    declared = set()
    for earlier in decisions[:position]:
        choices = [
            c
            for c in (earlier.get("policy_request") or {}).get("candidates", [])
            if c.get("kind") == "document_search"
            and c.get("arguments", {}).get("provider") == "fabric"
            and c["arguments"].get("source_id") == args["source_id"]
            and c["arguments"].get("phase") == "initial"
            and not c["arguments"].get("catalog_discovery")
            and not c["arguments"].get("relaxed_from")
        ]
        if choices:
            declared = {_search_key(c) for c in choices}
            break
    completed, closed, open_pages = set(), set(), []
    for i, row in enumerate(history):
        actual_args = row.get("arguments") or {}
        source = actual_args.get("source_id") or (actual_args.get("parameters") or {}).get("source_id")
        if source != args["source_id"]:
            continue
        _require(not actual_args.get("catalog_discovery"), "same source catalog was scanned twice")
        if row.get("kind") != "document_search" or actual_args.get("provider") != "fabric":
            if i >= index:
                query, saved = queries[i], prior.get(i)
                usage = query.get("usage") or {}
                _require(
                    saved is not None
                    and query.get("action_id") == row["action_id"]
                    and saved.get("action", {}).get("id") == row["action_id"]
                    and row.get("status") == "completed"
                    and row.get("new_evidence_ids") == []
                    and query.get("status") in {"read", "empty"}
                    and type(query.get("new_versions")) is int
                    and query["new_versions"] == 0
                    and not query.get("evidence_keys")
                    and type((query.get("cost") or {}).get("tool_calls")) is int
                    and query["cost"]["tool_calls"] >= 1
                    and not usage.get("adoption_stop_reason")
                    and not (usage.get("fetch") or {}).get("adoption_stop_reason")
                    and usage.get("request_status") not in {"not_sent", "unknown", "unconfirmed"},
                    "new or unconfirmed same-source work invalidated repeat basis",
                )
            continue
        query, saved = queries[i], prior.get(i)
        if i < index and (row.get("status") != "completed" or query.get("status") not in {"read", "empty"}):
            continue
        _require(
            saved is not None
            and saved.get("action", {}).get("id") == row["action_id"]
            and query.get("action_id") == row["action_id"]
            and saved["receipt"].get("status") == query.get("status") in {"read", "empty"}
            and row.get("status") == "completed"
            and query.get("provider") == "fabric"
            and query.get("plan", {}).get("source_id") == args["source_id"]
            and _terms(query["plan"].get("terms", [])) == _terms(actual_args.get("terms", []))
            and all(actual_args.get(k) == args[k] for k in ("mode", "as_of", "is_demo", "scope"))
            and type((query.get("cost") or {}).get("tool_calls")) is int
            and query["cost"]["tool_calls"] >= 1
            and (query.get("usage") or {}).get("request_status") not in {"not_sent", "unknown", "unconfirmed"}
            and not query.get("adoption_stop_reason")
            and not (query.get("usage") or {}).get("adoption_stop_reason")
            and not ((query.get("usage") or {}).get("fetch") or {}).get("adoption_stop_reason"),
            "partial catalog was based on an unbound or unsuccessful source receipt",
        )
        versions = query.get("returned_versions")
        _require(
            isinstance(versions, list)
            and type(query.get("returned")) is int
            and len(versions) == query["returned"]
            and (query["status"] == "empty") == (query["returned"] == 0)
            and all(
                set(v) == {"source_id", "source_record_id", "version_id"}
                and all(isinstance(v[k], str) and v[k] for k in v)
                and v["source_id"] == args["source_id"]
                for v in versions
            ),
            "partial catalog lacks actual returned fixed-version identities",
        )
        completed.add(_search_key(saved["action"]))
        if actual_args.get("previous_action_id"):
            closed.add(actual_args["previous_action_id"])
        if (row.get("pagination") or {}).get("has_more"):
            open_pages.append(row["action_id"])
        if i < index:
            continue
        _require(
            type(query.get("new_versions")) is int
            and query["new_versions"] == 0
            and not query.get("evidence_keys")
            and row.get("new_evidence_ids") == []
            and all((v["source_id"], v["source_record_id"], v["version_id"]) in adopted for v in versions),
            "zero increment did not prove all returned originals were already read",
        )
        if i == index:
            _require(
                query["status"] == "read" and query["returned"] > 0,
                "repeat basis must point to an actual nonempty duplicate hit",
            )
    _require(declared and declared <= completed, "partial catalog skipped a frozen initial alternative")
    _require(all(aid in closed for aid in open_pages), "partial catalog bypassed a received continuation")


def _catalog_result(view, decision):
    args = decision["action"]["arguments"]
    queries = view.get("queries", [])
    receipt = decision.get("receipt")
    if receipt is None:
        return
    index = receipt.get("query_index")
    _require(type(index) is int and 0 <= index < len(queries), "catalog receipt is not a saved query")
    query = queries[index]
    _require(query.get("plan", {}).get("terms") == [], "catalog actual query retained keyword terms")
    _require(
        query.get("scope") == "fabric_bounded_original_catalog_not_keyword_match",
        "catalog originals were represented as keyword matches",
    )
    if query.get("status") in {"read", "empty"}:
        usage = query.get("usage") or {}
        coverage = usage.get("coverage") or {}
        _require(
            usage.get("catalog_discovery") is True
            and coverage.get("complete") is False
            and coverage.get("search_scope") == "bounded_original_catalog_not_keyword_match"
            and coverage.get("followup_limit") == args["limit"]
            and coverage.get("has_more") is query.get("has_more"),
            "catalog receipt lost the actual one-page scope or truncation",
        )


def _catalog_discovery(view, trace, decisions, position):
    decision = decisions[position]
    action = decision["action"]
    args = action["arguments"]
    request = decision.get("policy_request") or {}
    fixed_time = trace.get("evidence_time") or {}
    _require(
        trace.get("schema_version") == "query-planner.external.v2"
        and trace.get("effective") == "query_planner"
        and trace.get("research_contract_version") == "bioradar-research-input-v1"
        and (
            trace.get("initial_retrieval_version") == "literal-language-alternatives-v1"
            or trace.get("initial_retrieval_version") == "literal-language-budget-v2"
            and trace.get("pagination_version") == "fabric-receipt-bound-pagination-v1"
        )
        and args.get("provider") == "fabric"
        and args.get("catalog_discovery") is True
        and args.get("terms") == []
        and "query" not in args,
        "empty terms are only valid for the new explicit Fabric catalog contract",
    )
    _require(
        args.get("source_id") in view.get("search_scope", [])
        and type(view.get("is_demo")) is bool
        and args.get("is_demo") is view["is_demo"]
        and args.get("mode") == fixed_time.get("mode")
        and args.get("as_of") == fixed_time.get("cutoff")
        and args.get("scope")
        == {
            "direction": trace.get("direction"),
            "mode": fixed_time.get("mode"),
            "as_of": fixed_time.get("cutoff"),
            "is_demo": view["is_demo"],
        }
        and type(args.get("limit")) is int
        and 1 <= args["limit"] <= 20
        and action.get("estimated_cost", {}).get("tool_calls") == 1,
        "catalog discovery changed its fixed source, time, data class or one-page limit",
    )
    identity = {
        key: args[key] for key in ("provider", "source_id", "mode", "as_of", "is_demo", "catalog_discovery")
    }
    _require(
        action.get("id") == "catalog:" + _hash(["document_search", identity])[:32],
        "catalog identity changed across a phase or page-size retry",
    )
    if request.get("evidence") and trace.get("discovery_version") == "bounded-required-gap-discovery-v1":
        _repeat_catalog(view, trace, decisions, position)
        _catalog_result(view, decision)
        return
    _require(
        request.get("schema_version") == "query-planner.external.v2"
        and request.get("evidence") == []
        and any(
            item.get("id") == action["id"] and item.get("arguments") == args
            for item in request.get("candidates", [])
        ),
        "catalog discovery lacks the actual evidence-free candidate request",
    )
    declared = set()
    for earlier in decisions[:position]:
        candidates = (earlier.get("policy_request") or {}).get("candidates", [])
        same_source = [
            item
            for item in candidates
            if item.get("kind") == "document_search"
            and item.get("arguments", {}).get("provider") == "fabric"
            and item["arguments"].get("source_id") == args["source_id"]
            and item["arguments"].get("phase") == "initial"
            and not item["arguments"].get("catalog_discovery")
            and not item["arguments"].get("relaxed_from")
        ]
        if same_source:
            declared = {_search_key(item) for item in same_source}
            break
    _require(bool(declared), "catalog discovery has no frozen initial language alternatives")
    empty = set()
    queries = view.get("queries", [])
    for earlier in decisions[:position]:
        prior = earlier.get("action") or {}
        prior_args = prior.get("arguments") or {}
        if prior.get("kind") != "document_search" or prior_args.get("provider") != "fabric":
            continue
        if prior_args.get("source_id") != args["source_id"]:
            continue
        _require(prior_args.get("catalog_discovery") is not True, "same source catalog was scanned twice")
        key = _search_key(prior)
        if key not in declared:
            continue
        saved = earlier.get("receipt") or {}
        index = saved.get("query_index")
        _require(type(index) is int and 0 <= index < len(queries), "initial empty response is not durable")
        query = queries[index]
        actual_cost = (query.get("cost") or {}).get("tool_calls")
        _require(
            query.get("action_id") == prior.get("id")
            and query.get("provider") == "fabric"
            and query.get("plan", {}).get("source_id") == args["source_id"]
            and _terms(query.get("plan", {}).get("terms", [])) == _terms(prior_args.get("terms", []))
            and saved.get("status") == query.get("status") == "empty"
            and type(query.get("returned")) is int
            and query["returned"] == 0
            and type(actual_cost) is int
            and actual_cost >= 1
            and (query.get("usage") or {}).get("request_status") not in {"not_sent", "unknown", "unconfirmed"}
            and not query.get("evidence_keys")
            and not saved.get("evidence_ids"),
            "catalog discovery was unlocked by a failed, unmeasured or nonempty initial response",
        )
        empty.add(key)
    _require(declared <= empty, "catalog discovery skipped an initial same-source language alternative")
    _catalog_result(view, decision)


def _pagination(view, trace, decisions, position):
    """A page is an exact continuation of a previously measured Fabric receipt."""
    decision = decisions[position]
    action, queries = decision["action"], view.get("queries", [])
    args, request = action["arguments"], decision.get("policy_request") or {}
    temporal = trace.get("evidence_time") or {}
    _require(
        trace.get("schema_version") == "query-planner.external.v2"
        and trace.get("effective") == "query_planner"
        and trace.get("research_contract_version") == "bioradar-research-input-v1"
        and trace.get("pagination_version") == "fabric-receipt-bound-pagination-v1"
        and args.get("provider") == "fabric"
        and args.get("source_id") in view.get("search_scope", [])
        and isinstance(args.get("cursor"), str)
        and 0 < len(args["cursor"]) <= 4096
        and type(args.get("limit")) is int
        and 1 <= args["limit"] <= 20
        and type(args.get("max_seq")) is int
        and args["max_seq"] >= 0
        and type(args.get("page_number")) is int
        and 2 <= args["page_number"] <= 8
        and type(view.get("is_demo")) is bool
        and args.get("is_demo") is view["is_demo"]
        and args.get("mode") == temporal.get("mode")
        and args.get("as_of") == temporal.get("cutoff")
        and args.get("scope")
        == {
            "direction": trace.get("direction"),
            "mode": temporal.get("mode"),
            "as_of": temporal.get("cutoff"),
            "is_demo": view["is_demo"],
        }
        and isinstance(args.get("terms"), list)
        and all(isinstance(term, str) and term.strip() for term in args["terms"])
        and "query" not in args
        and ("catalog_discovery" not in args or args["catalog_discovery"] is True)
        and (args.get("terms") == [] if args.get("catalog_discovery") is True else bool(args.get("terms"))),
        "pagination lacks the frozen new contract, cursor or legal source/time binding",
    )
    previous = next(
        (
            prior
            for prior in decisions[:position]
            if (prior.get("action") or {}).get("id") == args.get("previous_action_id")
        ),
        None,
    )
    _require(previous is not None, "page has no earlier actual predecessor action")
    prior_action, saved = previous["action"], previous.get("receipt") or {}
    prior_args = prior_action.get("arguments") or {}
    index = saved.get("query_index")
    _require(type(index) is int and 0 <= index < len(queries), "page predecessor has no durable query")
    prior_query = queries[index]
    page = prior_query.get("pagination") or {}
    _require(
        prior_action.get("kind") == "document_search"
        and prior_args.get("provider") == "fabric"
        and saved.get("status") == prior_query.get("status")
        and prior_query.get("status") in {"read", "empty"}
        and prior_query.get("action_id") == prior_action.get("id")
        and prior_query.get("provider") == "fabric"
        and prior_query.get("plan", {}).get("source_id") == prior_args.get("source_id")
        and prior_query.get("plan", {}).get("terms") == prior_args.get("terms")
        and type((prior_query.get("cost") or {}).get("tool_calls")) is int
        and prior_query["cost"]["tool_calls"] >= 1
        and (prior_query.get("usage") or {}).get("request_status") not in UNCONFIRMED | {"not_sent"}
        and prior_query.get("has_more") is True
        and page.get("has_more") is True
        and page.get("next_cursor") == args["cursor"]
        and type(page.get("max_seq")) is int
        and page["max_seq"] == args["max_seq"]
        and type(page.get("page_number")) is int
        and page["page_number"] == prior_args.get("page_number", 1)
        and args["page_number"] == page["page_number"] + 1,
        "page was not unlocked by the measured completed predecessor and exact returned cursor",
    )
    _require(
        all(
            args.get(key) == prior_args.get(key)
            for key in (
                "provider",
                "source_id",
                "terms",
                "limit",
                "mode",
                "as_of",
                "is_demo",
                "scope",
                "plan",
            )
        )
        and args.get("catalog_discovery", False) == prior_args.get("catalog_discovery", False)
        and ("max_seq" not in prior_args or prior_args["max_seq"] == args["max_seq"]),
        "page changed the original source/query/size/sequence binding",
    )
    identity = {
        "provider": "fabric",
        "source_id": args["source_id"],
        "terms": sorted({term.strip() for term in args["terms"]}),
        "limit": args["limit"],
        "scope": args["scope"],
        "catalog_discovery": args.get("catalog_discovery", False),
        "cursor": args["cursor"],
    }
    _require(
        action.get("id") == "page:" + _hash(["document_search", identity])[:32],
        "page identity changed across metadata-only retries",
    )
    actual_history = next(
        (item for item in request.get("history", []) if item.get("action_id") == prior_action["id"]), None
    )
    _require(
        request.get("schema_version") == "query-planner.external.v2"
        and any(
            item.get("id") == action["id"] and item.get("arguments") == args
            for item in request.get("candidates", [])
        )
        and actual_history is not None
        and actual_history.get("kind") == "document_search"
        and actual_history.get("status") == "completed"
        and actual_history.get("arguments") == prior_args
        and actual_history.get("pagination") == page,
        "page decision did not observe its actual fixed predecessor receipt",
    )
    receipt = decision.get("receipt")
    if receipt is None:
        return
    index = receipt.get("query_index")
    _require(type(index) is int and 0 <= index < len(queries), "page receipt has no saved query")
    query = queries[index]
    _require(
        query.get("plan", {}).get("terms") == args["terms"], "page actual terms differ from selected terms"
    )
    if query.get("status") in {"read", "empty"}:
        current = query.get("pagination") or {}
        _require(
            current.get("page_number") == args["page_number"]
            and type(current.get("max_seq")) is int
            and current["max_seq"] == args["max_seq"]
            and type(current.get("has_more")) is bool
            and current["has_more"] is query.get("has_more")
            and (
                isinstance(current.get("next_cursor"), str)
                and bool(current["next_cursor"])
                and current["next_cursor"] != args["cursor"]
                if current["has_more"]
                else current.get("next_cursor") is None
            ),
            "received page lost its actual sequence, page number or truncation",
        )
        if args.get("catalog_discovery") is True:
            usage = query.get("usage") or {}
            coverage = usage.get("coverage") or {}
            _require(
                query.get("scope") == "fabric_bounded_original_catalog_not_keyword_match"
                and usage.get("catalog_discovery") is True
                and coverage.get("complete") is False
                and coverage.get("search_scope") == "bounded_original_catalog_not_keyword_match"
                and coverage.get("followup_limit") == args["limit"]
                and coverage.get("has_more") is query.get("has_more"),
                "catalog page was represented as a keyword match or complete source inventory",
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
    _require(
        payload.get("schema_version") in {"bioradar-analysis-dataset-v1", "bioradar-analysis-dataset-v2"},
        "unknown dataset schema",
    )
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
    _require(
        payload.get("compute_version")
        in {
            "bioradar-mining-compute-v0.3",
            "bioradar-mining-compute-v0.4",
            "bioradar-mining-compute-v0.5",
        },
        "not a saved KDD computation",
    )
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


def _fetch_known_prefix(query):
    cost = query.get("cost") or {}
    if "known_tool_calls" not in cost:
        return 0
    known = cost["known_tool_calls"]
    fetch = (query.get("usage") or {}).get("fetch") or {}
    _require(
        query.get("kind") == "source_fetch"
        and query.get("provider") == "fabric"
        and query.get("status") in UNCONFIRMED
        and cost.get("tool_calls") is None
        and type(known) is int
        and known >= 0
        and type(fetch.get("known_tool_calls")) is int
        and known == fetch["known_tool_calls"]
        and cost.get("reserved_tool_calls") == 1,
        "partial known cost is not bound to the actual source-fetch receipt",
    )
    if known:
        _require(
            fetch.get("submission_status") == "received"
            and isinstance(fetch.get("run_id"), str)
            and bool(fetch["run_id"]),
            "known fetch prefix has no received run identity",
        )
    return known


def _source_fetch_receipt(view, trace, action, query):
    args = action["arguments"]
    fetch = (query.get("usage") or {}).get("fetch") or {}
    _require(
        query.get("provider") == "fabric"
        and query.get("kind") == "source_fetch"
        and query.get("plan") == args
        and args.get("capability_id") == "fabric.history.fetch"
        and args.get("capability_version") == "datafabric.history.fetch.v1",
        "actual source fetch differs from the selected fixed capability",
    )
    _require(
        view.get("is_demo") is False
        and args.get("scope")
        == {
            "direction": trace.get("direction"),
            "mode": "current",
            "as_of": (trace.get("evidence_time") or {}).get("cutoff"),
            "is_demo": False,
        }
        and (trace.get("evidence_time") or {}).get("mode") == "current",
        "source fetch crossed its actual current/real/direction scope",
    )
    parameters = args.get("parameters")
    _require(isinstance(parameters, dict), "source-fetch parameters missing")
    expected = {
        "source_record_ids": [],
        "max_records": 10,
        "max_pages": 3,
        "deadline_seconds": 60,
        **{key: value for key, value in parameters.items() if value is not None},
    }
    request = fetch.get("request")
    _require(
        request == expected
        and fetch.get("request_hash") == _hash(request)
        and fetch.get("call_key") == "analysis-fetch:" + _hash([view.get("id"), action["id"]]),
        "source fetch lost its normalized request, hash or stable acceptance key",
    )
    run = fetch.get("run")
    records = query.get("records") or []
    _require(isinstance(records, list), "fetch returned-record receipt is not a list")
    if run is None:
        _require(not fetch.get("run_id") and not records, "unconfirmed fetch invented a run or versions")
        return
    _require(
        isinstance(run, dict)
        and run.get("id") == fetch.get("run_id")
        and bool(run.get("id"))
        and run.get("source_id") == request.get("source_id")
        and run.get("is_demo") is False
        and run.get("kind") == "fetch",
        "source fetch run does not match its received source and data class",
    )
    versions = run.get("version_ids")
    _require(
        isinstance(versions, list)
        and len(versions) == len(set(versions))
        and all(isinstance(value, str) and len(value) == 64 for value in versions),
        "fetch run version identities are invalid",
    )
    for record in records:
        _require(
            record.get("source_id") == request["source_id"]
            and record.get("is_demo") is False
            and record.get("version_id") in versions
            and isinstance(record.get("source_record_id"), str)
            and bool(record["source_record_id"])
            and isinstance(record.get("raw_content"), str)
            and record.get("raw_sha256") == hashlib.sha256(record["raw_content"].encode()).hexdigest(),
            "returned original is not bound to an actual source-fetch run version",
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
        else:
            route_known += _fetch_known_prefix(query)
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
            if "cursor" in args:
                _pagination(view, trace, decisions, position)
            elif args.get("catalog_discovery") is True:
                _catalog_discovery(view, trace, decisions, position)
            else:
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
        elif v2 and action["kind"] == "source_fetch":
            _source_fetch_receipt(view, trace, action, query)
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
            measured_calls += _fetch_known_prefix(query)
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
