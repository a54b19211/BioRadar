"""Fixed-document reads and the one recorded structural retry, using the host ledger."""

import copy
from datetime import UTC, datetime

from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import analysis_budget
from bioradar.research.investigations.analysis_models import AnalysisQueryReceipt, PolicyReading
from bioradar.research.investigations.execution_budget import reading_limits
from bioradar.research.mining.models import canonical_hash


def validated_policy_output(service, identifier, token, fixed, progress, raw, payload, key, declared):
    from bioradar.research.investigations.analysis import parse_policy_reading, validate_policy_reading
    from bioradar.research.investigations.query_policy import normalize_policy_coverage

    def parse(value):
        normalized, adjustments = parse_policy_reading(value)
        normalized, issues = normalize_policy_coverage(normalized, declared, progress["evidence"])
        reading = validate_policy_reading(normalized, progress["evidence"], fixed["search_scope"])
        return reading, adjustments, issues

    try:
        result = parse(raw)
    except DomainError as exc:
        if (
            exc.code != "analysis_reading_invalid"
            or fixed.get("query_policy", {}).get("research_contract_version") != "bioradar-research-input-v1"
        ):
            raise
        repairs = progress.setdefault("reading_repairs", {})
        entry = repairs.setdefault(
            key,
            {
                "attempts": 1,
                "reason": exc.code,
                "original_call_key": key,
                "repair_call_key": key + ":structure-repair",
                "status": "prepared",
            },
        )
        service._checkpoint(identifier, token, progress)
        repair_payload = {
            "task": "repair_structured_reading",
            "original_request": copy.deepcopy(payload),
            "invalid_output": copy.deepcopy(raw),
            "validation_error": exc.code,
            "instruction": (
                "上一份阅读结果结构不符合给定协议。重新对照同一批固定原文完成一次阅读，"
                "只输出指定JSON。原文、引用编号与范围不可改造；未知保持未知，不猜片段位置，"
                "不能靠删除矛盾或更正来规避格式错误。"
            ),
        }
        repaired, _ = service._call(
            identifier,
            token,
            "read_plan_repair",
            PolicyReading,
            fixed,
            progress,
            repair_payload,
            call_key=entry["repair_call_key"],
        )
        try:
            result = parse(repaired)
        except DomainError:
            entry["status"] = "invalid"
            service._checkpoint(identifier, token, progress)
            raise
        entry["status"] = "validated"
        entry["repair_call_id"] = next(
            c["call_id"] for c in progress["calls"] if c.get("call_key") == entry["repair_call_key"]
        )
    reading, adjustments, issues = result
    if adjustments:
        progress.setdefault("model_output_adjustments", {})[key] = adjustments
    known = progress["query_policy"].setdefault("coverage_issues", [])
    for issue in issues:
        if issue not in known:
            known.append(issue)
    if issues:
        limitation = "模型报告了未声明或重复的覆盖编号，已保留为未知；未据此扩大权限或判为覆盖完成。"
        if limitation not in progress["limitations"]:
            progress["limitations"].append(limitation)
    service._checkpoint(identifier, token, progress)
    return reading


def _visible_requests(fixed, progress, action, step):
    args = action["arguments"]
    scope = args.get("scope", {})
    declared_scope = {
        "direction": fixed["query_policy"]["direction"],
        "as_of": fixed["evidence_time"]["cutoff"],
        "mode": fixed["evidence_time"]["mode"],
        "is_demo": fixed["is_demo"],
    }
    if scope != declared_scope or args.get("provider") != "fabric":
        raise DomainError("analysis_query_invalid", "记录点读范围与本轮冻结范围不一致", 422)
    decisions = progress["query_policy"]["decisions"]
    manifest = decisions[step].get("policy_request", {}).get("reading_manifest", [])
    allowed = {
        (item["source_id"], item["source_record_id"], item["version_id"], item.get("window_start", 0))
        for item in manifest
        if item.get("status") == "unread"
    }
    records = args.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= reading_limits(fixed)["search_limit"]:
        raise DomainError("analysis_query_invalid", "记录点读数量不符合本轮范围", 422)
    requested = []
    for record in records:
        if not isinstance(record, dict) or set(record) - {"source_record_id", "version_id", "window_start"}:
            raise DomainError("analysis_query_invalid", "记录点读参数无效", 422)
        start = record.get("window_start", 0)
        key = (args.get("source_id"), record.get("source_record_id"), record.get("version_id"), start)
        if type(start) is not int or start < 0 or key not in allowed or key in requested:
            raise DomainError("analysis_query_invalid", "记录点读没有匹配当前可见的固定窗口", 422)
        requested.append(key)
    return requested


def execute_document_read(service, identifier, token, fixed, progress, action, step):
    from bioradar.research.investigations.query_policy import semantic_key

    receipt = next((q for q in progress["queries"] if q.get("action_id") == action["id"]), None)
    requested = _visible_requests(fixed, progress, action, step)
    if receipt is None:
        if len(progress["queries"]) >= fixed["budget"]["max_queries"]:
            raise DomainError("analysis_query_budget", "本轮取证动作额度已用完", 422)
        receipt = AnalysisQueryReceipt(
            index=len(progress["queries"]),
            provider="fabric",
            kind="document_read",
            plan=copy.deepcopy(action["arguments"]),
            action_id=action["id"],
            semantic_key=semantic_key("document_read", action["arguments"]),
            decision_step=step,
            status="prepared",
            scope="authorized_fixed_record_windows",
        ).model_dump(mode="json")
        progress["queries"].append(receipt)
        service._checkpoint(identifier, token, progress)
    if receipt["status"] in {"sent", "unconfirmed"}:
        receipt.update(status="unconfirmed", error_code="analysis_query_outcome_unconfirmed")
        service._checkpoint(identifier, token, progress)
        return receipt
    if receipt["status"] == "prepared":
        service._authorize_query_sources(fixed, action)
        service._guard(identifier, token, check_deadline=True)
        if service.reader is None:
            receipt.update(status="unavailable", error_code="history_not_connected", records=[])
            receipt["cost"] = {"tool_calls": 0, "tokens": 0}
            service._checkpoint(identifier, token, progress)
            return receipt
        before = service._tool_start(fixed, progress)
        receipt.update(
            status="sent",
            records=[],
            cost={"tool_calls": None, "tokens": 0, "reserved_tool_calls": len(requested)},
        )
        service._checkpoint(identifier, token, progress)
        mode, cutoff = service._evidence_time(fixed)
        try:
            seconds = analysis_budget(fixed, progress, datetime.now(UTC))["usage"]["remaining_seconds"]
            if seconds <= 0:
                receipt["usage"] = {"request_status": "not_sent"}
                raise DomainError("analysis_time_budget", "点读意图保存后时限已到，请求未发送", 409)
            for source, native, version, _ in requested:
                service._guard(identifier, token, check_deadline=True)
                original = service.reader.read_version(
                    {
                        "source_id": source,
                        "source_record_id": native,
                        "version_id": version,
                        "is_demo": fixed["is_demo"],
                    },
                    mode=mode,
                    cutoff=cutoff,
                )
                receipt["records"].append(original)
            receipt.update(status="read" if receipt["records"] else "empty", returned=len(receipt["records"]))
        except DomainError as exc:
            receipt.update(status="failed", error_code=exc.code)
            raise
        finally:
            receipt["cost"] = {"tool_calls": service._tool_finish(progress, before), "tokens": 0}
            service._checkpoint(identifier, token, progress)
    if receipt["status"] != "read":
        return receipt
    for original, (_, _, _, start) in zip(receipt.get("records", []), requested, strict=True):
        ref = service.reader.ref(original).model_dump(mode="json")
        if any(e["record_ref"] == ref and e["start"] == start for e in progress["evidence"]):
            continue
        try:
            entry = service._adopt_original(
                identifier,
                token,
                fixed,
                progress,
                original,
                "read-window:" + canonical_hash([action["id"], ref, start]),
                start=start,
            )
        except DomainError as exc:
            if exc.code != "analysis_evidence_budget":
                raise
            receipt["usage"] = {"adoption_stop_reason": exc.code}
            service._checkpoint(identifier, token, progress)
            break
        was_known = any(e["record_ref"] == ref for e in progress["evidence"])
        progress["evidence"].append(entry)
        receipt["evidence_keys"].append(entry["key"])
        receipt["new_versions"] += int(not was_known)
        usage = receipt.setdefault("usage", {}) or {}
        usage["read_windows"] = usage.get("read_windows", 0) + 1
        receipt["usage"] = usage
        dependency = {
            "instance": service._source_instance(fixed["is_demo"]),
            "source_id": original["source_id"],
            "history": True,
        }
        if dependency not in fixed["dependency_sources"]:
            fixed["dependency_sources"].append(dependency)
        service._checkpoint(identifier, token, progress, fixed=fixed)
    return receipt
