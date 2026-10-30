"""PrimeKG name lookup, capture and immutable neighborhood as separate host actions.

Only names occurring in the question/read materials generate lookup candidates.
Native identities come from saved Fabric lookup receipts. PrimeKG observations
remain background context and never become independent research evidence.
"""

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
from pydantic import ValidationError

from bioradar.adapters.fabric_graph import read_graph_version, read_neighborhood
from bioradar.adapters.fabric_history import time_upper
from bioradar.adapters.fixed_graph import (
    build_fixed_graph_candidates,
    execute_fixed_graph_action,
)
from bioradar.adapters.fixed_graph import (
    remember_graph_package as remember_graph_package,
)
from bioradar.api.errors import DomainError
from bioradar.research.investigations.graph import GraphNeighborhood

SOURCE = "primekg_graph"
PROVIDER = "fabric_graph"
QUALIFICATION = "current_or_fixed_primekg_background_not_research_evidence_or_professional_assessment"
_NAMES = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9-]{1,29}|[A-Za-z][A-Za-z0-9-]{1,40}(?:mab|nib))(?![A-Za-z0-9_])"
)


def _plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else copy.deepcopy(value)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _text(value, maximum=400):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid graph text")
    return value


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("invalid graph bound")
    return value


def _secret(settings):
    secret = getattr(settings, "data_fabric_graph_query_token", None)
    return secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret


def _url(settings):
    return getattr(settings, "data_fabric_graph_query_url", None) or getattr(
        settings, "data_fabric_url", None
    )


def _lookup(value, query=None):
    if not isinstance(value, dict) or value.get("status") not in {"resolved", "ambiguous", "not_found"}:
        raise ValueError("invalid lookup receipt")
    declared = value.get("query", {})
    _text(declared.get("name"), 120)
    _integer(declared.get("limit"), 1, 20)
    if declared.get("node_type") not in {"drug", "disease", "gene/protein"}:
        raise ValueError("lookup requires a supported node_type")
    if query is not None and any(declared.get(k) != v for k, v in query.items()):
        raise ValueError("lookup query mismatch")
    if value.get("evidence_status") != "current_identity_candidates_only":
        raise ValueError("lookup is not a current candidate receipt")
    if time_upper(value.get("observed_at")) is None or type(value.get("truncated")) is not bool:
        raise ValueError("lookup provenance missing")
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > declared["limit"]:
        raise ValueError("invalid lookup candidates")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("invalid lookup candidate")
        for key, maximum in (("name", 120), ("node_type", 120), ("node_source", 152), ("seed_id", 400)):
            _text(candidate.get(key), maximum)
        if (
            candidate.get("identity_status") != "background"
            or candidate["name"].casefold() != declared["name"].casefold()
            or (declared.get("node_type") and candidate["node_type"] != declared["node_type"])
        ):
            raise ValueError("lookup candidate differs from exact requested name/type")
    if value["status"] == "resolved" and (len(candidates) != 1 or value["truncated"]):
        raise ValueError("ambiguous identities cannot be selected")
    if value["status"] == "not_found" and (candidates or value["truncated"]):
        raise ValueError("invalid not_found receipt")
    return value


def _binding(value):
    if not isinstance(value, dict) or value.get("source_id") != SOURCE:
        raise ValueError("unsupported fixed graph source")
    _text(value.get("source_record_id"))
    for field in ("version_id", "raw_sha256"):
        if not isinstance(value.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            raise ValueError("invalid fixed graph hash")
    if type(value.get("is_demo")) is not bool or time_upper(value.get("first_seen_at")) is None:
        raise ValueError("missing fixed graph time/data class")
    return copy.deepcopy(value)


def _candidate(operation, arguments, covers, calls, relations=0):
    args = {"provider": PROVIDER, "source_id": SOURCE, "operation": operation, **arguments}
    # The receipt proves a native identity; action identity denotes the actual query.
    meaning = {key: value for key, value in args.items() if key != "lookup_receipt"}
    return {
        "id": "graph-" + _hash(meaning)[:24],
        "kind": "entity_relations" if operation == "lookup" else "graph_neighbors",
        "arguments": args,
        "covers": covers,
        "estimated_cost": {"tool_calls": calls, "tokens": 0, "relation_access": relations},
        "reason": "从问题或已读资料中的名称/真实图回执补充背景和后续检索方向；不认证研究结论。",
    }


def build_graph_candidates(settings, fixed, progress, reading, *, checks):
    """Pure candidate construction. No guessed IDs, implicit HTTP, or mutation."""
    fixed_candidates = build_fixed_graph_candidates(settings, fixed, progress, reading, checks=checks)
    scope = fixed["graph_scope"] if "graph_scope" in fixed else fixed.get("search_scope", [])
    if SOURCE not in scope:
        return fixed_candidates
    instance = (
        (getattr(settings, "data_fabric_demo_source_instance", None) or "fixture")
        if fixed.get("is_demo")
        else getattr(settings, "data_fabric_source_instance", "datafabric-test")
    )
    if SOURCE not in getattr(settings, "engine_source_id_allowlist", {}).get(instance, []):
        return fixed_candidates
    history = fixed.get("evidence_time", {})
    current = history.get("mode", "current") == "current" and not history.get("study_id")
    # Background relationships can suggest technical follow-ups. They cannot
    # prospectively cover clinical comparisons, failures or arbitrary research gaps.
    covers = (
        ["technology"]
        if any(c.get("id") == "technology" and c.get("status") != "passed" for c in checks)
        else []
    )
    contexts = []
    for owner in (fixed, progress, progress.get("query_policy", {})):
        contexts.extend(owner.get("graph_context", []) or [])
    result, names_seen = [], set()
    for context in contexts:
        if not isinstance(context, dict) or context.get("provider") != PROVIDER:
            continue
        if context.get("operation") == "lookup":
            try:
                lookup = _lookup(context["lookup"])
            except (ValueError, TypeError, KeyError):
                continue
            names_seen.add((lookup["query"]["name"].casefold(), lookup["query"]["node_type"]))
            if current and _url(settings) and _secret(settings) and lookup["status"] == "resolved":
                native = lookup["candidates"][0]
                result.append(
                    _candidate(
                        "capture",
                        {
                            **{k: native[k] for k in ("node_type", "node_source", "seed_id")},
                            "lookup_receipt": lookup,
                            "relation_types": [],
                            "limit": 10,
                        },
                        covers,
                        1,
                        None,
                    )
                )
        elif context.get("operation") == "capture":
            try:
                binding = _binding(context["binding"])
                seeds = context["seed_ids"]
                if not seeds:
                    continue
                if not current and time_upper(binding["first_seen_at"]) > time_upper(history["cutoff"]):
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            result.append(
                _candidate(
                    "neighbors",
                    {
                        "binding": binding,
                        "seed_ids": copy.deepcopy(seeds),
                        "relation_types": [],
                        "direction": "both",
                        "max_depth": 1,
                        "max_nodes": 40,
                        "max_edges": 40,
                        "read_cutoff": context.get("read_cutoff"),
                    },
                    covers,
                    2,
                    None,
                )
            )
    if current and not fixed.get("is_demo") and _url(settings) and _secret(settings):
        texts = [fixed.get("question", "")]
        texts.extend(
            str(e.get("title", "")) + " " + str(e.get("text", "")) for e in progress.get("evidence", [])[:10]
        )
        value = _plain(reading) if reading is not None else {}
        # Structured names are accepted only when literally present in known text.
        anchored = " ".join(texts)
        explicit = value.get("graph_names", []) if isinstance(value, dict) else []
        if not isinstance(explicit, list):
            explicit = []
        names = [
            (n["name"], n["node_type"])
            for n in explicit
            if isinstance(n, dict)
            and isinstance(n.get("name"), str)
            and n["name"] in anchored
            and n.get("node_type") in {"drug", "disease", "gene/protein"}
            and 1 <= len(n["name"]) <= 120
        ]
        names.extend(
            (match.group(), "drug" if match.group().lower().endswith(("mab", "nib")) else "gene/protein")
            for text in texts
            for match in _NAMES.finditer(text[:40000])
        )
        for name, node_type in names:
            if (name.casefold(), node_type) in names_seen:
                continue
            names_seen.add((name.casefold(), node_type))
            result.append(_candidate("lookup", {"name": name, "node_type": node_type, "limit": 5}, covers, 1))
            if sum(item["arguments"]["operation"] == "lookup" for item in result) >= 6:
                break
    unique = {item["id"]: item for item in result}
    return list({item["id"]: item for item in [*fixed_candidates, *unique.values()]}.values())[:12]


def _result(
    status,
    *,
    calls=0,
    extra=0,
    relations=0,
    request_status="not_sent",
    error=None,
    context=None,
    graph_records=None,
    returned_edges=None,
):
    return {
        "status": status,
        "records": [],
        "graph_context": context,
        "graph_records": graph_records or [],
        "error_code": error,
        "actual_cost": {"tool_calls": calls, "tokens": 0, "relation_access": relations},
        "tool_requests": extra,
        "usage": {
            "request_status": request_status,
            "returned_edges": returned_edges,
            "relation_access_measurement": "not_measured" if relations is None else "no_relation_query",
        },
    }


def _post(settings, operation, body, key, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DomainError("analysis_time_budget", "图查询时限已到，请求未发送", 409)
    headers = {"Authorization": "Bearer " + _secret(settings)}
    if operation == "capture":
        headers["Idempotency-Key"] = key
    with httpx.Client(base_url=_url(settings), trust_env=False, timeout=remaining, headers=headers) as client:
        with client.stream("POST", "/api/history/graph/primekg/" + operation, json=body) as response:
            if not response.is_success:
                return response.status_code, None
            raw = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout("graph operation deadline exceeded")
                raw.extend(chunk)
                if len(raw) > 4_000_000:
                    raise ValueError("graph response exceeds bounded size")
            return response.status_code, json.loads(raw)


def execute_graph_action(settings, reader, arguments, *, mode, cutoff, idempotency_key, timeout_seconds):
    """Execute one visible graph action, reporting observed HTTP/read counts.

    Unknown POST outcomes stay unconfirmed. The host must persist and reconcile
    the same idempotency key; this function never automatically retries.
    """
    if isinstance(arguments, dict) and (
        arguments.get("operation") == "resolve"
        or isinstance(arguments.get("binding"), dict)
        and arguments["binding"].get("source_id") != SOURCE
    ):
        return execute_fixed_graph_action(
            reader,
            arguments,
            mode=mode,
            cutoff=cutoff,
            timeout_seconds=timeout_seconds,
        )
    start = time.monotonic()
    extra = 0
    before = getattr(reader, "requests_made", 0)
    old_deadline = getattr(reader, "deadline", None)
    operation = arguments.get("operation") if isinstance(arguments, dict) else None
    try:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            return _result("failed", error="analysis_time_budget")
        deadline = start + timeout_seconds
        if (
            not isinstance(arguments, dict)
            or arguments.get("provider") != PROVIDER
            or arguments.get("source_id") != SOURCE
            or operation not in {"lookup", "capture", "neighbors"}
        ):
            return _result("failed", error="graph_action_invalid")
        if mode != "current" and operation in {"lookup", "capture"}:
            return _result("unavailable", error="history_current_graph_unavailable")
        if operation in {"lookup", "capture"}:
            if not _url(settings) or not _secret(settings):
                return _result("unavailable", error="graph_query_not_configured")
            if operation == "lookup":
                body = {
                    "name": _text(arguments.get("name"), 120),
                    "limit": _integer(arguments.get("limit", 5), 1, 20),
                }
                if arguments.get("node_type") not in {"drug", "disease", "gene/protein"}:
                    raise ValueError("lookup requires a supported node_type")
                body["node_type"] = arguments["node_type"]
            else:
                lookup = _lookup(arguments.get("lookup_receipt"))
                if lookup["status"] != "resolved":
                    raise ValueError("capture needs one actual resolved lookup candidate")
                native = lookup["candidates"][0]
                body = {
                    k: _text(arguments.get(k), maximum)
                    for k, maximum in (("node_type", 120), ("node_source", 152), ("seed_id", 400))
                }
                if any(body[k] != native[k] for k in body):
                    raise ValueError("native identity differs from saved lookup")
                _text(idempotency_key, 200)
                body["limit"] = _integer(arguments.get("limit", 10), 1, 200)
                relations = arguments.get("relation_types", [])
                if not isinstance(relations, list) or len(relations) > 16:
                    raise ValueError("invalid relation types")
                body["relation_types"] = [_text(item, 120) for item in relations]
                body["neighbor_term"] = ""
                if arguments.get("neighbor_term"):
                    body["neighbor_term"] = _text(arguments["neighbor_term"], 120)
            if time.monotonic() >= deadline:
                return _result("failed", error="analysis_time_budget")
            extra = 1
            code, value = _post(settings, operation, body, idempotency_key, deadline)
            if code >= 400:
                # Capture may already have published before an unexpected server failure.
                unknown = operation == "capture" and code >= 500
                error = (
                    "graph_permission_denied"
                    if code in {401, 403}
                    else ("graph_idempotency_conflict" if code == 409 else "graph_provider_unavailable")
                )
                return _result(
                    "unconfirmed" if unknown else "failed",
                    calls=1,
                    extra=1,
                    relations=None if unknown else 0,
                    request_status="unknown" if unknown else "received",
                    error=error,
                )
            if operation == "lookup":
                value = _lookup(value, body)
                context = {
                    "provider": PROVIDER,
                    "operation": operation,
                    "lookup": value,
                    "qualification": QUALIFICATION,
                }
                return _result(
                    "empty" if value["status"] == "not_found" else "read",
                    calls=1,
                    extra=1,
                    request_status="received",
                    context=context,
                )
            neighborhood = GraphNeighborhood.model_validate(value["neighborhood"])
            binding = _binding(neighborhood.graph_ref.model_dump(mode="json"))
            read_cutoff = time_upper(value["read_cutoff"])
            run = value["run"]
            if not isinstance(run, dict):
                raise ValueError("capture run receipt missing")
            _text(run.get("id"), 200)
            if (
                value.get("query") != body
                or run.get("state") != "completed"
                or run.get("source_id") != SOURCE
                or run.get("is_demo") is not False
                or binding["version_id"] not in run.get("version_ids", [])
                or binding["is_demo"]
                or read_cutoff < time_upper(binding["first_seen_at"])
            ):
                raise ValueError("capture receipt not bound to completed immutable publication")
            expected_seed = ("primekg." + body["node_source"], body["seed_id"])
            if {n.key for n in (*neighborhood.seeds_found, *neighborhood.seeds_missing)} != {expected_seed}:
                raise ValueError("capture seed differs from resolved native identity")
            nodes = {node.key: node for node in neighborhood.nodes}
            if (
                len(nodes) != len(neighborhood.nodes)
                or neighborhood.coverage.returned_nodes != len(nodes)
                or neighborhood.coverage.returned_edges != len(neighborhood.edges)
                or len(neighborhood.edges) > body["limit"]
                or any(n.key not in nodes for n in neighborhood.seeds_found)
                or any(e.source.key not in nodes or e.target.key not in nodes for e in neighborhood.edges)
            ):
                raise ValueError("capture coverage is not coherent")
            if expected_seed in nodes and nodes[expected_seed].entity_type != body["node_type"]:
                raise ValueError("capture seed type differs from resolved identity")
            if any(n.identity_status != "background" for n in neighborhood.nodes) or any(
                e.binding_status != "background" or e.evidence for e in neighborhood.edges
            ):
                raise ValueError("PrimeKG cannot assert research evidence")
            context = {
                "provider": PROVIDER,
                "operation": operation,
                "binding": binding,
                "seed_ids": [n.namespace + "|" + n.id for n in neighborhood.seeds_found],
                "read_cutoff": value["read_cutoff"],
                "run": run,
                "query": value["query"],
                "neighborhood": neighborhood.model_dump(mode="json"),
                "qualification": QUALIFICATION,
            }
            return _result(
                "read",
                calls=1,
                extra=1,
                relations=None,
                returned_edges=len(neighborhood.edges),
                request_status="received",
                context=context,
            )
        if reader is None:
            return _result("unavailable", error="graph_history_not_connected")
        binding = _binding(arguments.get("binding"))
        effective_cutoff = time_upper(cutoff.isoformat() if isinstance(cutoff, datetime) else cutoff)
        if mode == "current" and arguments.get("read_cutoff"):
            effective_cutoff = max(effective_cutoff, time_upper(arguments["read_cutoff"]))
        if effective_cutoff is None or time_upper(binding["first_seen_at"]) > effective_cutoff:
            return _result("unavailable", error="graph_version_after_cutoff")
        seeds = arguments.get("seed_ids")
        if (
            not isinstance(seeds, list)
            or not 1 <= len(seeds) <= 20
            or any(not isinstance(seed, str) or seed.count("|") != 1 for seed in seeds)
        ):
            raise ValueError("fixed neighborhood needs actual graph seed identities")
        relation_types = arguments.get("relation_types", [])
        if not isinstance(relation_types, list) or len(relation_types) > 16:
            raise ValueError("invalid relation types")
        direction = arguments.get("direction", "both")
        if direction not in {"in", "out", "both"}:
            raise ValueError("invalid direction")
        request = SimpleNamespace(
            source_id=SOURCE,
            source_record_id=binding["source_record_id"],
            graph_version_id=binding["version_id"],
            is_demo=binding["is_demo"],
            mode=mode,
            seed_ids=seeds,
            relation_types=[_text(r, 200) for r in relation_types],
            direction=direction,
            max_depth=_integer(arguments.get("max_depth", 1), 1, 2),
            max_nodes=_integer(arguments.get("max_nodes", 40), 1, 100),
            max_edges=_integer(arguments.get("max_edges", 40), 1, 200),
        )
        if getattr(reader, "max_requests", None) is not None and reader.max_requests - before < 2:
            return _result("failed", error="analysis_tool_budget")
        reader.deadline = min(old_deadline, deadline)
        record, bundle = read_graph_version(reader, request, effective_cutoff)
        if (
            record["raw_sha256"] != binding["raw_sha256"]
            or record["source_record_id"] != binding["source_record_id"]
            or time_upper(record["first_seen_at"]) != time_upper(binding["first_seen_at"])
            or bundle.graph_kind != "background"
        ):
            raise ValueError("fixed PrimeKG binding or qualification changed")
        neighborhood = read_neighborhood(reader, record, bundle, request, effective_cutoff)
        context = {
            "provider": PROVIDER,
            "operation": operation,
            "binding": binding,
            "read_cutoff": effective_cutoff.isoformat(),
            "seed_ids": seeds,
            "neighborhood": neighborhood.model_dump(mode="json"),
            "qualification": QUALIFICATION,
        }
        return _result(
            "read" if neighborhood.nodes else "empty",
            calls=reader.requests_made - before,
            relations=None,
            returned_edges=len(neighborhood.edges),
            request_status="received",
            context=context,
            graph_records=[record],
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return _result(
            "failed", calls=extra, extra=extra, error="graph_connection_failed", request_status="not_sent"
        )
    except httpx.HTTPError:
        return _result(
            "unconfirmed",
            calls=extra,
            extra=extra,
            relations=None,
            error="graph_query_outcome_unconfirmed",
            request_status="unknown",
        )
    except DomainError as exc:
        calls = getattr(reader, "requests_made", before) - before
        return _result(
            "failed",
            calls=calls,
            extra=0,
            error=exc.code,
            relations=None if operation == "neighbors" and calls > 1 else 0,
            request_status="received" if calls else "not_sent",
        )
    except (ValueError, TypeError, KeyError, ValidationError):
        calls = getattr(reader, "requests_made", before) - before + extra
        unknown = operation == "capture" and extra > 0
        return _result(
            "unconfirmed" if unknown else "failed",
            calls=calls,
            extra=extra,
            relations=None if unknown or (operation == "neighbors" and calls > 1) else 0,
            error="graph_source_invalid" if calls else "graph_action_invalid",
            request_status="unknown" if unknown else "received" if calls else "not_sent",
        )
    finally:
        if reader is not None and old_deadline is not None:
            reader.deadline = old_deadline
