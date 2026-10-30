"""Host-owned KDD capabilities: fixed inputs, pure computation, and durable action receipts."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from bioradar.api.errors import DomainError
from bioradar.research.datasets.build import DATASET_VERSION, build_dataset
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.mining.kdd import kdd_versions, run_kdd
from bioradar.research.mining.models import KddPolicy, NormalizedSnapshot, SnapshotCoverage, canonical_hash

SCHEMA = "query-planner.external.v2"
KINDS = {"dataset_prepare", "analysis_run", "professional_assess"}
DIRECTIONS = {"research", "pipeline", "business", "public_health"}


def direction_for(question, explicit=None):
    if explicit in DIRECTIONS:
        return explicit
    text = question.casefold()
    if any(term in text for term in ("收入", "营收", "销售", "利润", "revenue", "commercial", "business")):
        return "business"
    if any(term in text for term in ("疫情", "公共卫生", "通报", "发病", "outbreak", "surveillance")):
        return "public_health"
    if any(term in text for term in ("管线", "竞争", "布局", "pipeline", "competitor")):
        return "pipeline"
    return "research"


def policy_for(fixed):
    direction = fixed["query_policy"]["direction"]
    narrative_version = fixed["query_policy"].get("narrative_contract_version")
    saved = fixed.get("kdd_mining_policy")
    if saved:
        policy = KddPolicy.model_validate(saved)
        if policy.kdd_direction != direction:
            raise DomainError("analysis_dataset_scope_changed", "分析方向与固定计算策略不一致", 409)
        if policy.narrative_contract_version != narrative_version:
            raise DomainError("analysis_dataset_scope_changed", "数值提取契约与固定计算策略不一致", 409)
        return policy
    return KddPolicy(
        as_of=datetime.fromisoformat(fixed["evidence_time"]["cutoff"]),
        kdd_direction=direction,
        narrative_contract_version=narrative_version,
        time_mode="availability_replay"
        if fixed.get("evidence_time", {}).get("mode") in {
            "operational_replay", "public_history_reconstruction"
        }
        else "publication_retrospective",
        availability_basis=fixed["evidence_time"]["mode"]
        if fixed.get("evidence_time", {}).get("mode") in {
            "operational_replay", "public_history_reconstruction"
        }
        else None,
    )


def _store(progress, artifact, payload=None):
    artifacts = progress.setdefault("kdd_artifacts", [])
    old = next((x for x in artifacts if x["id"] == artifact["id"]), None)
    if old is not None and old != artifact:
        raise DomainError("analysis_artifact_changed", "同一分析产物的版本或出处发生变化", 409)
    if old is None:
        artifacts.append(copy.deepcopy(artifact))
    if payload is not None:
        progress.setdefault("kdd_results", {})[artifact["id"]] = copy.deepcopy(payload)
    return artifact


def _sources(service, fixed):
    actor = DevelopmentAccess(service.settings).actor()
    snapshots = []
    with Session(service.engine.database) as session:
        for identifier in fixed.get("snapshot_ids", []):
            row = service.engine._snapshot(session, actor, identifier)
            snapshots.append((row.id, row.snapshot_hash, NormalizedSnapshot.model_validate(row.payload)))
    return snapshots


def source_snapshot(service, fixed, artifact):
    snapshots = _sources(service, fixed)
    requested = set(artifact["snapshot_ids"])
    chosen = [entry for entry in snapshots if entry[0] in requested]
    if len(chosen) != len(requested):
        raise DomainError("analysis_artifact_missing", "分析队列引用的固定资料未找到", 409)
    binding = sorted([[identifier, digest] for identifier, digest, _ in chosen])
    if canonical_hash(binding) != artifact["version"]:
        raise DomainError("analysis_artifact_changed", "固定分析成员版本已改变", 409)
    records = {}
    coverage = []
    gaps = {"explicit_adopted_snapshots_not_search_top_k_population"}
    instances = set()
    for _, _, snapshot in chosen:
        instances.add(snapshot.source_instance)
        gaps.update(snapshot.metadata_gaps)
        for record in snapshot.records:
            records[(record.source_instance, *record.record_ref.version_key)] = record
    if len(instances) != 1:
        raise DomainError("analysis_dataset_source_scope", "本轮合并仅支持同一明确来源实例", 422)
    if not records or len(records) > service.settings.engine_max_records:
        raise DomainError("analysis_dataset_budget", "固定分析成员为空或超过本轮计算范围", 422)
    ordered = [records[key] for key in sorted(records)]
    for source in sorted({row.source_id for row in ordered}):
        count = sum(row.source_id == source for row in ordered)
        coverage.append(
            SnapshotCoverage(
                source_instance=next(iter(instances)),
                source_id=source,
                mode="bounded_sample",
                scope_description="本调查已明确采用快照的固定成员并集；不是全行业总体或检索排名分母。",
                record_count=count,
                reasons=("explicit_snapshot_union",),
            )
        )
    return NormalizedSnapshot(
        snapshot_id=artifact["id"],
        source_instance=next(iter(instances)),
        is_demo=fixed["is_demo"],
        observed_at=max(record.observed_at for record in ordered),
        coverage_mode="bounded_sample",
        records=tuple(ordered),
        coverage=tuple(coverage),
        metadata_gaps=tuple(sorted(gaps)),
    )


def ensure_artifacts(service, identifier, token, fixed, progress):
    """Record actual adopted snapshot identities; no model or upstream source search."""
    if fixed.get("query_policy", {}).get("schema_version") != SCHEMA:
        return
    snapshots = _sources(service, fixed)
    if not snapshots:
        return
    # Keep the complete fixed input identities outside planner artifacts. Current-head
    # computation manifests can legitimately omit superseded source versions.
    fixed_refs = {
        (record.source_instance, *record.record_ref.version_key): record.record_ref.model_dump(mode="json")
        for _, _, snapshot in snapshots
        for record in snapshot.records
    }
    progress["kdd_source_version_refs"] = [fixed_refs[key] for key in sorted(fixed_refs)]
    if fixed.get("query_policy", {}).get("research_contract_version") == "bioradar-research-input-v1":
        catalog = {}
        for _, _, snapshot in snapshots:
            for record in snapshot.records:
                item = {
                    "record_ref": record.record_ref.model_dump(mode="json"), "source_id": record.source_id,
                }
                if record.signal_context is not None:
                    context = record.signal_context.model_dump(mode="json")
                    item["signal_context"] = context
                    if context.get("document_evidence") is not None:
                        item["source_context"] = copy.deepcopy(context["document_evidence"])
                catalog[canonical_hash(item["record_ref"])] = item
        progress["kdd_source_catalog"] = [catalog[key] for key in sorted(catalog)]
    binding = sorted([[sid, digest] for sid, digest, _ in snapshots])
    version = canonical_hash(binding)
    source = {
        "id": "sources:" + version[:24],
        "version": version,
        "kind": "source_documents",
        "snapshot_ids": [sid for sid, _ in binding],
        "record_count": sum(len(snapshot.records) for _, _, snapshot in snapshots),
        "scope": {"direction": fixed["query_policy"]["direction"], **fixed["evidence_time"]},
        "qualification": "Fixed adopted snapshot members; record count may include repeated versions.",
    }
    _store(progress, source)
    progress["kdd_source_artifact_id"] = source["id"]
    origin = fixed.get("kdd_origin")
    if origin and origin.get("run_id"):
        actor = DevelopmentAccess(service.settings).actor()
        with Session(service.engine.database) as session:
            row = service.engine._run(session, actor, origin["run_id"])
            preparation = (row.result or {}).get("data_preparation")
            if (
                row.result_hash == origin.get("result_hash")
                and preparation
                and preparation.get("direction") == fixed["query_policy"]["direction"]
                and set(source["snapshot_ids"]) == {row.snapshot_id}
                and canonical_hash(row.policy) == canonical_hash(policy_for(fixed))
                and row.method_versions == kdd_versions(policy_for(fixed))
            ):
                artifact = {
                    "id": "run:" + row.id,
                    "version": row.result_hash,
                    "kind": "pattern_result",
                    "source_artifact_id": source["id"],
                    "source_version": source["version"],
                    "dataset_hash": preparation["dataset_hash"],
                    "scope": source["scope"],
                    "candidate_keys": [x["candidate_key"] for x in row.result["candidates"]],
                    "method_statuses": [x["execution_status"] for x in row.result["method_results"]],
                    "run_id": row.id,
                    "policy": copy.deepcopy(row.policy),
                }
                _store(progress, artifact, row.result)
    service._checkpoint(identifier, token, progress)


def _artifact_refs(values):
    return [{"id": value["id"], "version": value["version"]} for value in values]


def extend_context(fixed, progress, context, reading=None):
    """Add only capabilities with host-validated prerequisites; never execute while selecting."""
    if fixed.get("query_policy", {}).get("schema_version") != SCHEMA:
        return context
    value = copy.deepcopy(context)
    direction = fixed["query_policy"]["direction"]
    artifacts = progress.get("kdd_artifacts", [])
    source = next((a for a in artifacts if a["id"] == progress.get("kdd_source_artifact_id")), None)
    current = [
        a
        for a in artifacts
        if source
        and (
            a["id"] == source["id"]
            or a.get("source_artifact_id") == source["id"]
            and a.get("source_version") == source["version"]
        )
    ]
    datasets = [a for a in current if a["kind"] == "analysis_dataset"]
    patterns = [a for a in current if a["kind"] == "pattern_result"]
    assessments = [a for a in current if a["kind"] == "professional_assessment"]
    policy = policy_for(fixed)
    version = canonical_hash(kdd_versions(policy))
    capabilities = [
        {
            "id": "radar.dataset.prepare",
            "kind": "dataset_prepare",
            "version": DATASET_VERSION,
            "directions": sorted(DIRECTIONS),
            "input_requirements": ["source_documents"],
        },
        {
            "id": "radar.analysis." + direction,
            "kind": "analysis_run",
            "version": version,
            "directions": [direction],
            "input_requirements": ["analysis_dataset"],
        },
    ]
    checks = [item for item in value["checks"] if not item["id"].startswith("kdd_")]
    value["checks"] = checks
    computed = any(a.get("qualified_count", 0) > 0 for a in datasets) or any(
        progress.get("kdd_results", {}).get(a["id"], {}).get("data_preparation", {}).get("qualified_count", 0)
        > 0
        for a in patterns
    )
    evaluated = any(set(a.get("method_statuses", [])) & {"success", "partial"} for a in patterns)
    checks.extend(
        [
            {
                "id": "kdd_dataset",
                "question": "按明确单位构成合格分析数据，并保留排除与缺失。",
                "status": "passed" if computed else "unknown",
                "required": True,
                "importance": 1,
                "artifact_refs": _artifact_refs(patterns or datasets) if computed else [],
                "evidence_ids": [],
                "reason": "实际计算资料资格；不是科学结论。",
            },
            {
                "id": "kdd_pattern",
                "question": "运行适用计算并保存全候选、价值依据与未入选原因。",
                "status": "passed" if evaluated else "unknown",
                "required": True,
                "importance": 1,
                "artifact_refs": _artifact_refs(patterns),
                "evidence_ids": [],
                "reason": "已保存计算结果才算执行；无有价值模式也是实际结果。",
            },
        ]
    )

    def candidate(capability, inputs, covers, reason):
        args = {
            "capability_id": capability["id"],
            "capability_version": capability["version"],
            "input_refs": _artifact_refs(inputs),
            "scope": {
                "direction": direction,
                "as_of": fixed["evidence_time"]["cutoff"],
                "mode": fixed["evidence_time"]["mode"],
                "is_demo": fixed["is_demo"],
            },
            "parameters": {},
        }
        return {
            "id": "kdd:" + canonical_hash([capability["kind"], args])[:32],
            "kind": capability["kind"],
            "capability_id": capability["id"],
            "arguments": args,
            "covers": covers,
            "reason": reason,
            "applicability": {"status": "eligible", "reason": reason, "scope_match": 1},
            "estimated_cost": {
                "tool_calls": 1,
                "tokens": None if capability["kind"] == "professional_assess" else 0,
            },
        }

    candidates = value["candidates"]
    if source and not datasets and not patterns:
        candidates.append(
            candidate(
                capabilities[0],
                [source],
                ["kdd_dataset"],
                "已有明确采用的固定原件，按本方向实际字段检查队列资格。",
            )
        )
    qualified = [a for a in datasets if a.get("qualified_count", 0) > 0]
    if qualified and not patterns:
        candidates.append(
            candidate(
                capabilities[1],
                qualified[-1:],
                ["kdd_pattern"],
                "已有合格分析单位；运行本方向固定方法与独立价值筛选。",
            )
        )
    mechanism = (reading or {}).get("mechanism_candidate")
    provider = fixed.get("providers", {}).get("mech_evidence", {})
    if direction in {"research", "pipeline"} and mechanism and provider.get("available") and source:
        cap = {
            "id": "mech_evidence.evaluate",
            "kind": "professional_assess",
            "version": provider["version"],
            "directions": ["research", "pipeline"],
            "input_requirements": ["source_documents"],
        }
        capabilities.append(cap)
        completed = any(item.get("status") == "completed" for item in assessments)
        checks.append(
            {
                "id": "kdd_mechanism_review",
                "question": "对本轮明确机制候选执行专业评价。",
                "status": "passed" if completed else "unknown",
                "required": True,
                "artifact_refs": _artifact_refs(assessments),
                "evidence_ids": [],
                "reason": "评价已执行与机制已成立是不同结果。",
                "importance": 1,
            }
        )
        if not completed:
            candidates.append(
                candidate(
                    cap,
                    [source],
                    ["kdd_mechanism_review"],
                    "本轮有明确生医机制候选与固定材料，机制方法适用。",
                )
            )
    fetch_enabled = (
        fixed["query_policy"].get("research_contract_version") == "bioradar-research-input-v1"
        and not fixed["is_demo"] and fixed["evidence_time"]["mode"] == "current"
        and fixed.get("providers", {}).get("fabric_fetch", {}).get("available")
    )
    if fetch_enabled:
        from bioradar.adapters.fabric_fetch import FETCH_VERSION, normalize_fetch_request

        capabilities.append({
            "id": "fabric.history.fetch", "kind": "source_fetch", "version": FETCH_VERSION,
            "directions": [direction], "input_requirements": [],
        })
        for item in list(candidates):
            query = item.get("arguments", {})
            if (
                item["kind"] != "document_search" or query.get("provider") != "fabric"
                or query.get("catalog_discovery") is True or "cursor" in query
            ):
                continue
            if query.get("source_id") not in {
                "clinicaltrials", "pubmed", "fda_crl", "europe_pmc", "who_don", "sec_companyfacts",
            }:
                continue
            parameters = normalize_fetch_request({
                "source_id": query["source_id"], "query": " ".join(query["terms"]),
                "max_records": fixed.get("reading_limits", {}).get("search_limit", 8),
                "max_pages": 1, "deadline_seconds": 30,
            })
            args = {
                "capability_id": "fabric.history.fetch", "capability_version": FETCH_VERSION,
                "input_refs": _artifact_refs([source]) if source else [],
                "scope": {
                    "direction": direction, "as_of": fixed["evidence_time"]["cutoff"],
                    "mode": "current", "is_demo": False,
                },
                "parameters": parameters,
            }
            candidates.append({
                "id": "fetch:" + canonical_hash(args)[:32], "kind": "source_fetch",
                "capability_id": "fabric.history.fetch", "arguments": args,
                "covers": copy.deepcopy(item["covers"]),
                "reason": "按当前可见的具体问题词请求来源增量采集；不代表已取得或覆盖。",
                "applicability": {"status": "eligible", "scope_match": 1,
                                  "reason": "已配置真实当前补采，查询词来自本轮实际计划。"},
                "estimated_cost": {"tool_calls": 1, "tokens": 0},
            })
    attempted = {canonical_hash([item["kind"], item["arguments"]]) for item in value["history"]}
    value["candidates"] = [
        item
        for item in candidates
        if item["kind"] not in KINDS or canonical_hash([item["kind"], item["arguments"]]) not in attempted
    ]
    for item in value["history"]:
        receipt = next((q for q in progress["queries"] if q.get("action_id") == item["action_id"]), None)
        if receipt:
            item["new_artifact_refs"] = copy.deepcopy(receipt.get("artifact_refs", []))
            item["check_updates"] = copy.deepcopy(receipt.get("check_updates", []))
    value.update(
        schema_version=SCHEMA,
        direction=direction,
        capabilities=capabilities,
        artifacts=copy.deepcopy(current),
        scope={
            "direction": direction, "mode": fixed["evidence_time"]["mode"],
            "as_of": fixed["evidence_time"]["cutoff"], "is_demo": fixed["is_demo"],
        },
        selector=fixed["query_policy"]["selector"],
        **(
            {"action_limits": copy.deepcopy(fixed["query_policy"]["action_limits"])}
            if "action_limits" in fixed["query_policy"] else {}
        ),
    )
    return value


def execute_action(service, identifier, token, fixed, progress, action, step):
    """Replay pure computation or a professional step through the existing analysis job."""
    from bioradar.research.investigations.analysis_models import AnalysisQueryReceipt
    from bioradar.research.investigations.query_policy import semantic_key

    args = action["arguments"]
    receipt = next((q for q in progress["queries"] if q.get("action_id") == action["id"]), None)
    if receipt is None:
        if len(progress["queries"]) >= fixed["budget"]["max_queries"]:
            raise DomainError("analysis_query_budget", "本轮研究动作预算已用尽", 422)
        receipt = AnalysisQueryReceipt(
            index=len(progress["queries"]),
            phase="followup",
            provider="professional" if action["kind"] == "professional_assess" else "radar",
            kind=action["kind"],
            plan=copy.deepcopy(args),
            action_id=action["id"],
            semantic_key=semantic_key(action["kind"], args),
            decision_step=step,
            status="prepared",
            scope="fixed_analysis_artifacts_not_external_population",
            cost={"tool_calls": 0, "tokens": 0},
        ).model_dump(mode="json")
        progress["queries"].append(receipt)
        service._checkpoint(identifier, token, progress)
    if receipt["status"] != "prepared":
        return receipt
    service._guard(identifier, token, check_deadline=action["kind"] != "professional_assess")
    if (
        action["kind"] != "professional_assess"
        and service._policy_budget(fixed, progress)["remaining"]["tool_calls"] < 1
    ):
        raise DomainError("analysis_tool_budget", "本轮已无剩余计算动作预算", 422)
    inputs = []
    for ref in args["input_refs"]:
        artifact = next(
            (
                a
                for a in progress.get("kdd_artifacts", [])
                if a["id"] == ref["id"] and a["version"] == ref["version"]
            ),
            None,
        )
        if artifact is None:
            raise DomainError("analysis_artifact_missing", "动作引用了未持久保存的分析产物", 422)
        inputs.append(artifact)
    if args["scope"].get("direction") != fixed["query_policy"]["direction"]:
        raise DomainError("analysis_dataset_scope_changed", "动作改变了本轮业务方向", 409)
    source = next(
        (
            a
            for a in progress["kdd_artifacts"]
            if a["id"]
            == (
                inputs[0]["id"]
                if inputs[0]["kind"] == "source_documents"
                else inputs[0]["source_artifact_id"]
            )
        ),
        None,
    )
    if source is None:
        raise DomainError("analysis_artifact_missing", "分析产物缺少原始固定成员", 422)
    try:
        snapshot = source_snapshot(service, fixed, source)
        if action["kind"] in {"dataset_prepare", "analysis_run"}:
            from bioradar.research.investigations.narrative_execution import prepare_narrative_snapshot

            snapshot = prepare_narrative_snapshot(
                service, identifier, token, fixed, progress, snapshot,
                allow_model=action["kind"] == "dataset_prepare",
            )
        policy = policy_for(fixed)
        base = {
            "scope": source["scope"],
            "source_artifact_id": source["id"],
            "source_version": source["version"],
            "snapshot_ids": source["snapshot_ids"],
        }
        if action["kind"] == "dataset_prepare":
            if args["capability_version"] != DATASET_VERSION:
                raise DomainError("analysis_method_changed", "数据加工版本与固定动作不同", 409)
            data = build_dataset(snapshot, policy).manifest()
            if progress.get("narrative_extractions"):
                refs = {
                    canonical_hash(record.record_ref.model_dump(mode="json")) for record in snapshot.records
                }
                data["narrative_extractions"] = [
                    copy.deepcopy(value)
                    for value in progress["narrative_extractions"].values()
                    if value.get("record_ref") is not None
                    and canonical_hash(value["record_ref"]) in refs
                ]
            artifact = {
                **base,
                "id": "dataset:" + data["dataset_hash"][:24],
                "version": data["dataset_hash"],
                "kind": "analysis_dataset",
                "qualified_count": data["qualified_count"],
                "policy": policy.model_dump(mode="json"),
            }
            _store(progress, artifact, data)
        elif action["kind"] == "analysis_run":
            if args["capability_version"] != canonical_hash(kdd_versions(policy)):
                raise DomainError("analysis_method_changed", "计算方法与固定动作不同", 409)
            result = run_kdd(snapshot, policy)
            if result.data_preparation["dataset_hash"] != inputs[0]["version"]:
                raise DomainError("analysis_dataset_scope_changed", "运行输入不再是已采用的分析数据", 409)
            artifact = {
                **base,
                "id": "pattern:" + result.result_hash[:24],
                "version": result.result_hash,
                "kind": "pattern_result",
                "dataset_hash": result.data_preparation["dataset_hash"],
                "candidate_keys": [x.candidate_key for x in result.candidates],
                "method_statuses": [x.execution_status for x in result.method_results],
                "policy": policy.model_dump(mode="json"),
            }
            _store(progress, artifact, result.model_dump(mode="json"))
        elif action["kind"] == "professional_assess":
            reading = service._saved_policy_reading(progress["query_policy"])
            basis = canonical_hash([source["version"], reading.mechanism_candidate])
            previous = progress.get("mechanism_evaluation")
            if previous is not None and progress.get("kdd_mechanism_basis") != basis:
                progress.setdefault("mechanism_history", []).append(
                    {
                        "basis": progress.get("kdd_mechanism_basis"),
                        "result": copy.deepcopy(previous),
                    }
                )
                progress.pop("mechanism_evaluation", None)
            progress["kdd_mechanism_basis"] = basis
            progress["active_professional_action"] = action["id"]
            service._checkpoint(identifier, token, progress, fixed=fixed)
            service._mechanism(identifier, token, fixed, progress, reading)
            assessment = progress["mechanism_evaluation"]
            digest = canonical_hash(assessment)
            artifact = {
                **base,
                "id": "professional:" + digest[:24],
                "version": digest,
                "kind": "professional_assessment",
                "provider": "mech_evidence",
                "status": assessment["status"],
                "qualification": "Professional result, not scientific approval.",
            }
            _store(progress, artifact, assessment)
            progress.pop("active_professional_action", None)
        else:
            raise DomainError("analysis_query_invalid", "此执行器不支持该分析动作", 422)
        if action["kind"] == "professional_assess":
            _professional_cost(fixed, progress, receipt)
        else:
            # One executed local capability is one tool invocation; it sends no model request.
            usage = progress.setdefault("tool_usage", {})
            usage["tool_calls"] = usage.get("tool_calls", 0) + 1
            receipt["cost"] = {"tool_calls": 1, "tokens": 0, "model_calls": 0}
        receipt.update(
            status="read",
            returned=1,
            artifact_refs=_artifact_refs([artifact]),
            usage={
                **(receipt.get("usage") or {}),
                "execution": "host_capability",
                "paid_calls": "see_model_ledger",
            },
        )
        progress.setdefault("kdd_execution", []).append(
            {
                "action_id": action["id"],
                "kind": action["kind"],
                "input_refs": copy.deepcopy(args["input_refs"]),
                "output_refs": _artifact_refs([artifact]),
            }
        )
        service._checkpoint(identifier, token, progress, fixed=fixed)
        return receipt
    except DomainError as exc:
        uncertain = "unconfirmed" in exc.code or "unknown" in exc.code
        if action["kind"] == "professional_assess":
            _professional_cost(fixed, progress, receipt)
        receipt.update(status="unconfirmed" if uncertain else "failed", error_code=exc.code)
        service._checkpoint(identifier, token, progress, fixed=fixed)
        raise


def report_summary(progress):
    results = []
    for artifact in progress.get("kdd_artifacts", []):
        if artifact["kind"] != "pattern_result":
            continue
        result = progress.get("kdd_results", {}).get(artifact["id"], {})
        results.append(
            {
                "artifact_ref": {"id": artifact["id"], "version": artifact["version"]},
                "dataset": result.get("data_preparation"),
                "methods": [
                    {
                        "id": m["method_id"],
                        "status": m["execution_status"],
                        "readiness": m["readiness"],
                        "statistics": m["statistics"],
                        "limitations": m["limitations"],
                    }
                    for m in result.get("method_results", [])
                ],
                "candidates": result.get("candidates", []),
                "pattern_evaluations": result.get("pattern_evaluations", []),
                "selection_decisions": result.get("selection_decisions", []),
                "qualification": (
                    "程序对固定成员的实际计算和筛选；描述性模式，不自动等于因果、显著性或业务价值已证实。"
                ),
            }
        )
    return results


def analysis_results(progress):
    """Use BioCheck's calculation contract without relabelling calculations as papers."""
    analyses = []
    current_source = progress.get("kdd_source_artifact_id")
    for artifact in progress.get("kdd_artifacts", []):
        if artifact["kind"] != "pattern_result" or artifact.get("source_artifact_id") != current_source:
            continue
        result = progress.get("kdd_results", {}).get(artifact["id"], {})
        data = result.get("data_preparation") or {}
        if not data:
            continue
        selected = {item["candidate_key"] for item in result.get("candidates", [])}
        decisions = {item["candidate_key"]: item for item in result.get("selection_decisions", [])}
        evaluations = {item["candidate_key"]: item for item in result.get("pattern_evaluations", [])}
        # WHO methods share one fixed scope. Apply the existing detail sampling
        # to their combined pattern payload, without changing the stored result.
        who_compact = (
            data["direction"] == "public_health"
            and len(
                json.dumps(
                    [
                        {"metrics": item["metrics"], "data_gaps": item.get("data_gaps", [])}
                        for method in result.get("method_results", [])
                        for item in method.get("candidates", [])
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            )
            > 16_000
        )
        for method_index, method in enumerate(result.get("method_results", [])):
            candidates = method.get("candidates", [])
            candidate_positions = {item["candidate_key"]: index for index, item in enumerate(candidates)}
            # Keep the actual saved selection order; this is a report projection,
            # never another ranking or a rewrite of the accepted algorithm result.
            selected_patterns = [
                item for item in result.get("candidates", []) if item["candidate_key"] in candidate_positions
            ]
            retained = list(candidates)
            pattern_bytes = len(
                json.dumps(
                    [
                        {"metrics": item["metrics"], "data_gaps": item.get("data_gaps", [])}
                        for item in candidates
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            )
            if pattern_bytes > 16_000 or who_compact:
                retained = selected_patterns[:2]
                samples = {}
                for item in candidates:
                    if item["candidate_key"] in selected:
                        continue
                    state = decisions.get(item["candidate_key"], {}).get("decision", "not_classified")
                    bucket = samples.setdefault(state, [])
                    if len(bucket) < 1:
                        bucket.append(item)
                retained.extend(item for group in samples.values() for item in group)
            outputs = [
                {
                    "candidate_key": item["candidate_key"],
                    "title": item["title"],
                    "metrics": copy.deepcopy(item["metrics"]),
                    "data_gaps": copy.deepcopy(item.get("data_gaps", [])),
                    "selected": item["candidate_key"] in selected,
                    "pattern_evaluation": {
                        key: copy.deepcopy(value)
                        for key, value in (evaluations.get(item["candidate_key"]) or {}).items()
                        if key
                        in {
                            "eligible",
                            "qualification",
                            "value_score",
                            "value_reasons",
                            "eligibility_kind",
                            "required_evidence",
                            "incremental_context",
                        }
                    },
                }
                for item in retained
            ]
            analyses.append(
                {
                    "id": "calc:" + canonical_hash([artifact["id"], method["method_id"]])[:24],
                    "dataset_ref": {"id": data["dataset_hash"], "version": artifact["version"]},
                    "method_version": method["method_version"],
                    "parameters": {
                        "policy": copy.deepcopy(artifact.get("policy", {})),
                        "policy_hash": result["policy_hash"],
                        "direction": data["direction"],
                        "as_of": data["as_of"],
                        "time_mode": data["time_mode"],
                        "versions": copy.deepcopy(data.get("versions", {})),
                    },
                    "output": {
                        "method": method["method_id"],
                        "status": method["execution_status"],
                        "readiness": copy.deepcopy(method.get("readiness")),
                        "reasons": copy.deepcopy(method.get("reasons", [])),
                        "truncated_count": method.get("truncated_count", 0),
                        "statistics": copy.deepcopy(method.get("statistics", {})),
                        "patterns": outputs,
                        "report_projection": {
                            "version": "selected_index_and_representative_details_v3",
                            "full_result_ref": {"id": artifact["id"], "version": artifact["version"]},
                            "generated_count": len(candidates),
                            "included_count": len(retained),
                            "omitted_count": len(candidates) - len(retained),
                            "included_selected_count": sum(
                                item["candidate_key"] in selected for item in retained
                            ),
                            "omitted_selected_detail_count": len(selected_patterns)
                            - sum(item["candidate_key"] in selected for item in retained),
                            "selected_index": [
                                {
                                    "candidate_key": item["candidate_key"],
                                    "title": item["title"],
                                    **(
                                        {
                                            "value_score": copy.deepcopy(
                                                evaluations[item["candidate_key"]]["value_score"]
                                            )
                                        }
                                        if "value_score" in evaluations.get(item["candidate_key"], {})
                                        else {}
                                    ),
                                    "full_result_ref": {
                                        "id": artifact["id"],
                                        "version": artifact["version"],
                                        "output_path": "/method_results/"
                                        + str(method_index)
                                        + "/candidates/"
                                        + str(candidate_positions[item["candidate_key"]]),
                                    },
                                }
                                for item in selected_patterns
                            ],
                            "qualification": (
                                "报告详情样例，不是重新筛选：模式数据超过16KB时，详情仅含按原入选顺序的"
                                "最多两个已选模式和各未选类别最多一个代表。全部已选简要索引及完整结果仍保存；"
                                "未传详情不代表未入选、没有价值或不存在，不能据样例推断全体模式。"
                            ),
                            "all_pattern_gaps": sorted(
                                {gap for item in candidates for gap in item.get("data_gaps", [])}
                            ),
                            "all_required_evidence": sorted(
                                {
                                    requirement
                                    for item in candidates
                                    for requirement in [
                                        *item.get("next_data_requirements", []),
                                        *evaluations.get(item["candidate_key"], {}).get(
                                            "required_evidence", []
                                        ),
                                    ]
                                }
                            ),
                        },
                        "selection_summary": {
                            key: copy.deepcopy(value)
                            for key, value in data.get("selection_summary", {}).items()
                            if key != "deferred_candidate_keys"
                        },
                        "qualified_count": data["qualified_count"],
                        "input_count": data["input_count"],
                    },
                    "input_refs": [
                        {"dataset_ref": artifact["source_artifact_id"], "version": artifact["source_version"]}
                    ],
                    "calculation_basis": {
                        "analysis_unit": data["unit_contract"],
                        "unit": data.get("qualified_count_unit", "analysis_units"),
                        "period": {"as_of": data["as_of"], "time_mode": data["time_mode"]},
                        "population": copy.deepcopy(data["coverage"]),
                        "denominator": copy.deepcopy(data["denominator"]),
                    },
                    "limitations": list(
                        dict.fromkeys(
                            [
                                *method.get("limitations", []),
                                *data.get("unknown_reasons", []),
                                "实际固定成员计算，未证明全领域覆盖、研究独立性、因果或预测有效性。",
                            ]
                        )
                    ),
                }
            )
            if who_compact:
                projected = analyses[-1]
                # The immutable result and policy_hash retain complete parameters.
                # Every method keeps its own full unit/period/population/denominator.
                projected["parameters"].pop("policy", None)
                projection = projected["output"]["report_projection"]
                projection["version"] = "who_shared_result_identity_v4"
                for selected_item in projection["selected_index"]:
                    selected_item["full_result_ref"] = {
                        "output_path": selected_item["full_result_ref"]["output_path"]
                    }
                projection["selected_index_ref_basis"] = (
                    "每项full_result_ref.output_path与本report_projection.full_result_ref的"
                    "实际id和version共同构成完整结果定位；没有生成新的结果或重新筛选。"
                )
                projection["qualification"] = (
                    "报告详情样例，不是重新筛选：WHO组合模式数据超过16KB，详情仅含原入选顺序"
                    "最多两个已选模式和各未选类别最多一个代表。全部已选索引、所有缺口和补证要求"
                    "保留；完整算法参数按policy_hash及完整结果引用回查，未传详情不代表不存在。"
                )
    return analyses


def _professional_cost(fixed, progress, receipt):
    from bioradar.research.investigations.analysis import analysis_budget

    calls = [call for call in progress["calls"] if call.get("action_id") == receipt["action_id"]]
    usage = analysis_budget(fixed, {"calls": calls}, datetime.now(UTC))["usage"]
    costs = [
        state.get("actual_cost", {})
        for state in progress.get("professional_operations", {}).values()
        if state.get("action_id") == receipt["action_id"]
    ]
    known_tools = all(cost.get("tool_calls") is not None for cost in costs)
    receipt["cost"] = {
        **(receipt.get("cost") or {}),
        "tool_calls": sum(cost["tool_calls"] for cost in costs) if known_tools else None,
        "reserved_tool_calls": sum(cost.get("reserved_tool_calls", 0) for cost in costs),
        "tokens": usage["total_tokens"],
        "model_calls": usage["calls"],
        "budgeted_tokens": usage["budgeted_tokens"],
    }
    receipt["usage"] = dict(receipt.get("usage") or {})
    receipt["usage"]["model_call_ids"] = [call["call_id"] for call in calls]


def source_update_context(fixed, progress):
    """Keep actual correction/retraction targets exact, including unread replacements."""
    if fixed.get("query_policy", {}).get("research_contract_version") != "bioradar-research-input-v1":
        return {}
    catalog = (
        progress.get("kdd_source_catalog", [])
        + fixed.get("evidence", []) + progress.get("evidence", [])
    )
    updates, unresolved = {}, {}
    for item in catalog:
        context = item.get("source_context")
        if context is None:
            context = (item.get("signal_context") or {}).get("document_evidence")
        if not context:
            continue
        ref = item.get("record_ref") or {}
        try:
            source_id, record_id, demo = json.loads(ref["object_id"])
        except (KeyError, ValueError, TypeError):
            continue
        if demo is not fixed.get("is_demo", False):
            continue
        replacement = {"record_id": record_id, "version": ref.get("adopted_version")}
        if not replacement["version"]:
            continue
        if item.get("key"):
            replacement["evidence_id"] = item["key"]
        for relation in context.get("document_relations", []):
            kind = {"corrects": "correction", "retracts": "retraction"}.get(relation["relation_type"])
            if kind is None:
                continue
            target = {
                "record_id": relation["target_source_record_id"],
                "version": relation.get("target_version_id"),
            }
            if target["version"] is None:
                unresolved[canonical_hash([source_id, record_id, relation])] = {
                    "kind": kind, "target": target, "replacement": replacement,
                    "reason": "来源未给出目标固定版本；未猜测或认定完成替代。",
                }
                continue
            key = canonical_hash([
                kind, relation["target_source_id"], target, record_id, replacement["version"],
            ])
            previous = updates.get(key)
            update = {
                "kind": kind, "target": target, "replacement": replacement,
                "reason": "固定来源明确声明更正或撤回；不是科学真实性批准。",
            }
            if previous is None or replacement.get("evidence_id"):
                updates[key] = update
    return {
        "source_updates": [updates[key] for key in sorted(updates)],
        "unresolved_source_updates": [unresolved[key] for key in sorted(unresolved)],
    }


def material_scope(fixed, progress, *, fixed_version_refs=None):
    """Visible factual scope; these counts describe inputs, never a source population."""
    if fixed.get("query_policy", {}).get("schema_version") != SCHEMA:
        return None
    declared = {}
    if fixed_version_refs is None:
        fixed_version_refs = progress.get("kdd_source_version_refs", [])
    for ref in fixed_version_refs:
        declared[canonical_hash(ref)] = ref
    for item in fixed.get("evidence", []):
        declared[canonical_hash(item["record_ref"])] = item["record_ref"]
    source = progress.get("kdd_source_artifact_id")
    for artifact in progress.get("kdd_artifacts", []):
        if artifact.get("source_artifact_id") != source:
            continue
        payload = progress.get("kdd_results", {}).get(artifact["id"], {})
        data = payload.get("data_preparation", payload if artifact["kind"] == "analysis_dataset" else {})
        if not data:
            continue
        for unit in data.get("units", []):
            for member in unit["members"]:
                declared[canonical_hash(member["record_ref"])] = member["record_ref"]
        for item in data.get("excluded_records", []):
            declared[canonical_hash(item["record_ref"])] = item["record_ref"]
    read = {canonical_hash(item["record_ref"]): item["record_ref"] for item in progress.get("evidence", [])}
    declared.update(read)

    def describe(ref):
        try:
            native = json.loads(ref["object_id"])
            source_id, record_id = native[0], native[1]
        except (ValueError, TypeError, IndexError):
            source_id, record_id = ref["provider"], ref["object_id"]
        return {
            "source_id": source_id,
            "source_record_id": record_id,
            "version_id": ref["adopted_version"],
            "record_ref": copy.deepcopy(ref),
        }

    def identity(item):
        return item["source_id"], item["source_record_id"], item["version_id"]

    known = {identity(describe(ref)) for ref in declared.values()}
    read_versions = {identity(describe(ref)) for ref in read.values()}
    unread = {
        identity(item): item
        for key, ref in sorted(declared.items())
        for item in [describe(ref)]
        if identity(item) not in read_versions
    }
    for query in progress.get("queries", []):
        if query.get("status") != "read":
            continue
        for record in query.get("records", []):
            if not isinstance(record, dict) or not all(
                isinstance(record.get(key), str) and record[key]
                for key in ("source_id", "source_record_id", "version_id", "raw_sha256")
            ):
                continue
            if any(
                len(record[key]) != 64 or any(char not in "0123456789abcdef" for char in record[key])
                for key in ("version_id", "raw_sha256")
            ):
                continue
            item = {
                "source_id": record["source_id"],
                "source_record_id": record["source_record_id"],
                "version_id": record["version_id"],
                "raw_sha256": record["raw_sha256"],
            }
            key = identity(item)
            known.add(key)
            if key in read_versions:
                continue
            # The receipt fixes a native version; it supplies no new adoption or authority.
            item = unread.setdefault(key, item)
            item["query_hit"] = True
            if query.get("action_id"):
                actions = item.setdefault("query_action_ids", [])
                if query["action_id"] not in actions:
                    actions.append(query["action_id"])
            reason = (query.get("usage") or {}).get("adoption_stop_reason")
            if reason:
                item["adoption_stop_reason"] = reason

    failures = [
        {
            "provider": q.get("provider"),
            "source_id": q.get("plan", {}).get("source_id"),
            "status": q["status"],
            "error_code": q.get("error_code"),
        }
        for q in progress.get("queries", [])
        if q["status"] in {"failed", "unavailable", "unconfirmed"}
    ]
    return {
        "fixed_version_count": len(known),
        "read_version_count": len(read_versions),
        "read_scope": "selected_passages_not_full_documents",
        "unread_versions": list(unread.values()),
        "source_failures": failures,
        "qualification": "固定输入、计算成员及成功补查命中的版本与实际已读片段分别保留；"
        "固定版本包括同一原件的历史版，计算单位可仅采用当期版本。"
        "命中未读不代表来源失败，未读材料及失败补查不代表没有相关证据。",
    }


def scope_markdown(scope):
    """Delivery metadata stays outside the unchanged verified report body."""
    if not scope:
        return ""
    lines = [
        "## 本轮资料范围（系统记录）",
        "",
        f"固定资料 {scope['fixed_version_count']} 个版本；"
        f"研判实际读取其中 {scope['read_version_count']} 个版本的选定片段。",
        "这些数字不是全文阅读量、独立研究数或全领域覆盖率。",
    ]
    if scope["unread_versions"]:
        labels = [x["source_id"] + ":" + x["source_record_id"] for x in scope["unread_versions"]]
        lines.append("未进入本轮原文片段：" + "；".join(labels) + "。")
    hits = [item for item in scope["unread_versions"] if item.get("query_hit")]
    if hits:
        labels = []
        for item in hits:
            label = item["source_id"] + ":" + item["source_record_id"]
            reason = item.get("adoption_stop_reason")
            if reason:
                description = "本轮原文片段容量已满" if reason == "analysis_evidence_budget" else reason
                label += "（" + description + "；" + reason + "）"
            labels.append(label)
        lines.append("成功补查命中但未读：" + "；".join(labels) + "。")
    if scope["source_failures"]:
        labels = [
            (x.get("source_id") or x.get("provider") or "") + ":" + (x.get("error_code") or x["status"])
            for x in scope["source_failures"]
        ]
        lines.append("补查未完成：" + "；".join(labels) + "。")
    return "\n".join(lines) + "\n\n---\n\n"
