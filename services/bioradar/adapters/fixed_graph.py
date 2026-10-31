"""Navigate only already adopted, immutable graph packages, including literal Chinese names."""

import copy
import hashlib
import time
from types import SimpleNamespace

from pydantic import ValidationError

from bioradar.adapters.fabric_graph import (
    list_graph_packages,
    read_graph_version,
    read_neighborhood,
    resolve_graph_names,
)
from bioradar.adapters.fabric_history import time_upper
from bioradar.api.errors import DomainError
from bioradar.research.investigations.graph import GraphBundle, GraphReference
from bioradar.research.mining.models import canonical_hash

QUALIFICATION = "固定图的来源关系背景；reviewed包及resolved名称不自动确认其中实体、关系或科学结论。"


def remember_graph_package(fixed, progress, original):
    """The caller has read/validated this original already; this function sends no request."""
    if original.get("record_kind") != "graph":
        return None
    try:
        bundle = GraphBundle.model_validate_json(original["raw_content"])
        reference = GraphReference(
            **{key: original[key] for key in ("source_id", "source_record_id", "version_id", "raw_sha256")},
            is_demo=original["is_demo"],
            first_seen_at=time_upper(original["first_seen_at"]),
        )
        if (
            bundle.is_demo is not fixed["is_demo"]
            or reference.is_demo is not fixed["is_demo"]
            or hashlib.sha256(original["raw_content"].encode()).hexdigest() != reference.raw_sha256
        ):
            raise ValueError("fixed graph data class or raw hash changed")
    except (ValueError, TypeError, KeyError, ValidationError):
        raise DomainError("graph_package_invalid", "已读图包与其真实固定绑定不同", 502) from None
    package = {
        "graph_ref": reference.model_dump(mode="json"),
        "bundle": bundle.model_dump(mode="json"),
    }
    packages = progress.setdefault("graph_packages", [])
    previous = next((item for item in packages if item["graph_ref"] == package["graph_ref"]), None)
    if previous is not None and previous != package:
        raise DomainError("graph_package_changed", "同固定图版本内容发生变化", 409)
    if previous is None:
        packages.append(package)
    return package


def build_fixed_graph_candidates(settings, fixed, progress, reading, *, checks):
    if fixed.get("query_policy", {}).get("research_contract_version") != "bioradar-research-input-v1":
        return []
    is_demo = fixed["is_demo"]
    instance = (
        getattr(settings, "data_fabric_demo_source_instance", None) or "fixture"
        if is_demo
        else getattr(settings, "data_fabric_source_instance", "datafabric-test")
    )
    permitted = getattr(settings, "engine_source_id_allowlist", {}).get(instance, [])
    fixed_sources = set(fixed.get("graph_scope", [])) | {
        item["source_id"]
        for item in fixed.get("dependency_sources", [])
        if item.get("history") and item.get("instance") == instance
    }
    anchored = " ".join(
        [
            fixed.get("question", ""),
            *(
                str(item.get("title", "")) + " " + str(item.get("text", ""))
                for item in progress.get("evidence", [])
            ),
        ]
    ).casefold()
    covers = [
        item["id"]
        for item in checks
        if (
            item["id"] in {"technology", "competing_hypotheses"}
            or item["id"].startswith("question_relation:")
        )
        and item.get("status") != "passed"
    ]
    contexts = progress.get("graph_context", [])
    contexts = [contexts] if isinstance(contexts, dict) else contexts
    result = _package_candidates(fixed, progress, reading, covers, permitted)
    for package in [*fixed.get("graph_packages", []), *progress.get("graph_packages", [])]:
        reference = GraphReference.model_validate(package["graph_ref"])
        bundle = GraphBundle.model_validate(package["bundle"])
        if reference.source_id not in fixed_sources or reference.source_id not in permitted:
            continue
        if reference.is_demo is not is_demo or bundle.is_demo is not is_demo:
            continue
        mode = fixed["evidence_time"]["mode"]
        if mode == "operational_replay" and reference.first_seen_at > time_upper(
            fixed["evidence_time"]["cutoff"]
        ):
            continue
        binding = reference.model_dump(mode="json")
        resolved = [
            item
            for item in contexts
            if isinstance(item, dict)
            and item.get("operation") == "fixed_resolve"
            and item.get("binding") == binding
        ]
        names_done = {item.get("name") for item in resolved}
        for node in bundle.nodes:
            for name in (node.name, *node.aliases):
                if len(name) > 120 or name.casefold() not in anchored or name in names_done:
                    continue
                args = {
                    "provider": "fabric_graph",
                    "source_id": reference.source_id,
                    "operation": "resolve",
                    "binding": binding,
                    "name": name,
                }
                result.append(_candidate("entity_relations", args, covers))
        for item in resolved:
            seeds = item.get("seed_ids", [])
            if not seeds:
                continue
            args = {
                "provider": "fabric_graph",
                "source_id": reference.source_id,
                "operation": "neighbors",
                "binding": binding,
                "seed_ids": copy.deepcopy(seeds),
                "relation_types": [],
                "direction": "both",
                "max_depth": 1,
                "max_nodes": 40,
                "max_edges": 40,
            }
            result.append(_candidate("graph_neighbors", args, covers))
    return list({item["id"]: item for item in result}.values())[:12]


def _package_candidates(fixed, progress, reading, covers, permitted):
    """Offer a finite authorized catalog before guessing any graph identifiers."""
    names = progress.get("query_policy", {}).get("named_objects", [])
    explicit = (reading or {}).get("graph_names", []) if isinstance(reading, dict) else []
    if not covers or not (names or explicit):
        return []
    known = [
        *fixed.get("graph_packages", []),
        *progress.get("graph_packages", []),
    ]
    contexts = progress.get("graph_context", [])
    contexts = [contexts] if isinstance(contexts, dict) else contexts
    candidates = []
    for source in fixed.get("graph_scope", []):
        if source not in permitted:
            continue
        catalogs = [
            c
            for c in contexts
            if isinstance(c, dict) and c.get("operation") == "fixed_catalog" and c.get("source_id") == source
        ]
        if not catalogs and not any(p["graph_ref"]["source_id"] == source for p in known):
            args = {
                "provider": "fabric_graph",
                "source_id": source,
                "operation": "catalog",
                "is_demo": fixed["is_demo"],
                "limit": 4,
            }
            candidate = _candidate("entity_relations", args, covers)
            candidate.update(
                reason="问题有待查对象关系，先列出授权来源的一页图包，再选择固定版本读取。",
                applicability={
                    "status": "eligible",
                    "scope_match": 1,
                    "reason": "对象关系有缺口且本来源图包尚未发现；不自动认定相关。",
                },
                estimated_cost={"tool_calls": 1, "tokens": 0, "relation_access": 0},
            )
            candidates.append(candidate)
        for catalog in catalogs:
            for package in catalog.get("packages", []):
                if any(
                    p["graph_ref"]["source_id"] == source
                    and p["graph_ref"]["source_record_id"] == package["source_record_id"]
                    and p["graph_ref"]["version_id"] == package["version_id"]
                    for p in known
                ):
                    continue
                args = {
                    "provider": "fabric_graph",
                    "source_id": source,
                    "operation": "adopt",
                    "source_record_id": package["source_record_id"],
                    "version_id": package["version_id"],
                    "is_demo": fixed["is_demo"],
                }
                candidate = _candidate("entity_relations", args, covers)
                candidate.update(
                    reason="读取真实目录回执中的固定图包以核对对象；目录标题不作为研究证据。",
                    applicability={
                        "status": "eligible",
                        "scope_match": 1,
                        "reason": "真实授权目录返回的版本，尚未读取包内关系。",
                    },
                    estimated_cost={"tool_calls": 1, "tokens": 0, "relation_access": 0},
                )
                candidates.append(candidate)
    return candidates


def _candidate(kind, arguments, covers):
    return {
        "id": "fixed-graph:" + canonical_hash([kind, arguments])[:28],
        "kind": kind,
        "arguments": arguments,
        "covers": copy.deepcopy(covers),
        "reason": "在已采用的固定图包中解析原文出现的名称或读取同版本邻域；仅作背景。",
        "applicability": {"status": "eligible", "scope_match": 1, "reason": "已采用固定图及原文逐字名称。"},
        "estimated_cost": {"tool_calls": 2, "tokens": 0, "relation_access": 0},
    }


def execute_fixed_graph_action(reader, arguments, *, mode, cutoff, timeout_seconds):
    before = reader.requests_made
    old_deadline = reader.deadline
    context = None
    original = None
    try:
        if timeout_seconds <= 0:
            raise DomainError("analysis_time_budget", "固定图读取时限已到，请求未发送", 409)
        reader.deadline = min(old_deadline, time.monotonic() + timeout_seconds)
        operation = arguments.get("operation")
        if operation in {"catalog", "adopt"}:
            if arguments.get("provider") != "fabric_graph" or type(arguments.get("is_demo")) is not bool:
                raise ValueError("invalid graph catalog identity")
            if operation == "catalog":
                if arguments.get("limit") != 4:
                    raise ValueError("catalog must be a finite host-generated page")
                catalog = list_graph_packages(
                    reader,
                    source_id=arguments["source_id"],
                    is_demo=arguments["is_demo"],
                    cutoff=cutoff,
                    limit=4,
                    mode=mode,
                )
                context = {
                    "provider": "fabric_graph",
                    "operation": "fixed_catalog",
                    "source_id": arguments["source_id"],
                    "packages": [p.model_dump(mode="json") for p in catalog.items],
                    "next_cursor": catalog.next_cursor,
                    "unavailable_packages": [p.model_dump(mode="json") for p in catalog.unavailable_packages],
                    "limitations": catalog.limitations,
                    "qualification": QUALIFICATION,
                }
            else:
                request = SimpleNamespace(
                    source_id=arguments["source_id"],
                    source_record_id=arguments["source_record_id"],
                    graph_version_id=arguments["version_id"],
                    is_demo=arguments["is_demo"],
                    mode=mode,
                )
                original, bundle = read_graph_version(reader, request, cutoff)
                context = {
                    "provider": "fabric_graph",
                    "operation": "fixed_adopt",
                    "binding": {
                        key: original[key]
                        for key in (
                            "source_id",
                            "source_record_id",
                            "version_id",
                            "raw_sha256",
                            "is_demo",
                            "first_seen_at",
                        )
                    },
                    "qualification": QUALIFICATION,
                }
        else:
            reference = GraphReference.model_validate(arguments["binding"])
            if (
                reference.source_id != arguments.get("source_id")
                or arguments.get("provider") != "fabric_graph"
            ):
                raise ValueError("fixed graph source conflicts")
            request = SimpleNamespace(
                source_id=reference.source_id,
                source_record_id=reference.source_record_id,
                graph_version_id=reference.version_id,
                is_demo=reference.is_demo,
                mode=mode,
                review_scope="with_candidates",
            )
            original, bundle = read_graph_version(reader, request, cutoff)
            if original["raw_sha256"] != reference.raw_sha256:
                raise ValueError("fixed graph hash changed")
            operation = arguments.get("operation")
            context = {
                "provider": "fabric_graph",
                "operation": "fixed_" + str(operation),
                "binding": reference.model_dump(mode="json"),
                "qualification": QUALIFICATION,
            }
            if operation == "resolve":
                name = arguments["name"]
                if not any(name in (node.name, *node.aliases) for node in bundle.nodes):
                    raise ValueError("name is not in this immutable graph")
                request.seed_names = (name,)
                seeds, resolutions = resolve_graph_names(reader, original, bundle, request, cutoff)
                context.update(
                    name=name,
                    seed_ids=list(seeds),
                    resolutions=[value.model_dump(mode="json") for value in resolutions],
                )
            elif operation == "neighbors":
                original_nodes = {node.wire for node in bundle.nodes}
                seeds = arguments["seed_ids"]
                if not isinstance(seeds, list) or not seeds or not set(seeds).issubset(original_nodes):
                    raise ValueError("seed does not belong to the immutable graph")
                for field in (
                    "seed_ids",
                    "relation_types",
                    "direction",
                    "max_depth",
                    "max_nodes",
                    "max_edges",
                ):
                    setattr(request, field, arguments[field])
                neighborhood = read_neighborhood(reader, original, bundle, request, cutoff)
                context.update(
                    seed_ids=copy.deepcopy(seeds), neighborhood=neighborhood.model_dump(mode="json")
                )
            else:
                raise ValueError("unsupported fixed graph operation")
        status, error = "read", None
    except (ValueError, ValidationError, KeyError, TypeError):
        status, error = "failed", "graph_action_invalid"
    except DomainError as exc:
        status, error = "failed", exc.code
    finally:
        reader.deadline = old_deadline
    calls = max(0, reader.requests_made - before)
    return {
        "status": status,
        "records": [],
        "graph_context": context,
        "graph_records": [original] if original is not None and status == "read" else [],
        "error_code": error,
        "actual_cost": {"tool_calls": calls, "tokens": 0, "relation_access": 0},
        "tool_requests": 0,
        "usage": {"request_status": "received" if calls else "not_sent", "qualification": QUALIFICATION},
    }
