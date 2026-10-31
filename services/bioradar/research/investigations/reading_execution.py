"""Fixed-document reads and the one recorded structural retry, using the host ledger."""

import copy
import re
from datetime import UTC, datetime

from pydantic import ValidationError

from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import (
    SYSTEM_PROMPT,
    analysis_budget,
    normalize_policy_synthesis,
)
from bioradar.research.investigations.analysis_models import AnalysisQueryReceipt, PolicyReading
from bioradar.research.investigations.execution_budget import reading_limits, request_estimate
from bioradar.research.investigations.research_assessment import claim_assessment_errors
from bioradar.research.mining.models import canonical_hash

_REPAIR_PROMPT = (
    "只修复所列JSON字段的结构或引用标识，返回changes。只能修改field_paths中的精确路径，"
    "不能改写未出错字段、命题正文、结论或删去反证。引用只能使用给定合法编号；"
    "没有足够信息就返回空changes，保留原输出待核，不猜原文。"
)
_REPAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changes"],
    "properties": {
        "changes": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "operation"],
                "properties": {
                    "path": {"type": "array", "minItems": 1, "items": {"type": ["string", "integer"]}},
                    "operation": {"type": "string", "enum": ["replace", "remove"]},
                    "value": {},
                },
            },
        }
    },
}


def _reference_feedback(raw, payload):
    """Use only supplied IDs, never the evidence text or whole rejected claim."""
    allowed = {
        item["evidence_key"]: [passage["id"] for passage in item["passages"]] for item in payload["evidence"]
    }
    errors = []
    for claim_index, claim in enumerate(raw.get("claims", [])):
        for link_index, link in enumerate(claim.get("evidence_links", [])):
            permitted = allowed.get(link["evidence_key"], [])
            if link["passage_id"] not in permitted:
                errors.append(
                    {
                        "path": ["claims", claim_index, "evidence_links", link_index],
                        "claim_id": claim["id"],
                        "evidence_key": link["evidence_key"],
                        "passage_id": link["passage_id"],
                        "reason": "unknown_evidence"
                        if link["evidence_key"] not in allowed
                        else "unknown_passage",
                        "allowed_passage_ids": permitted,
                    }
                )
    return errors, allowed


def _at_path(value, path):
    for part in path:
        value = value[part]
    return value


def _repair_fields(raw, payload, code):
    errors, allowed, fields = [], {}, []
    if code == "analysis_output_invalid":
        normalized, _ = normalize_policy_synthesis(raw)
        errors = [
            {"path": item["path"], "reason": item["reason"]} for item in claim_assessment_errors(normalized)
        ]
        # Only a proven scalar ID fault may use this slot; semantic faults remain hard failures.
        if errors and all(item["reason"] == "unknown_claim" for item in errors):
            fields = [{"path": item["path"], "error_code": item["reason"]} for item in errors]
    elif code in {"analysis_reference_invalid", "analysis_passage_invalid"}:
        errors, allowed = _reference_feedback(raw, payload)
        for error in errors:
            names = (
                ["evidence_key", "passage_id"] if error["reason"] == "unknown_evidence" else ["passage_id"]
            )
            for name in names:
                fields.append({"path": [*error["path"], name], "error_code": error["reason"]})
    else:
        try:
            PolicyReading.model_validate(raw)
        except ValidationError as error:
            fields = [
                {"path": list(item["loc"]), "error_code": item["type"]}
                for item in error.errors(include_url=False, include_context=False, include_input=False)
                if item["loc"]
            ]
    unique = []
    for field in fields:
        if any(field["path"] == item["path"] for item in unique):
            continue
        # Only safe scalar identifiers/enums are repeated. Prose, quotes, arrays
        # and objects are represented by their type/hash, never sent in full.
        try:
            value = _at_path(raw, field["path"])
        except (KeyError, IndexError, TypeError):
            field["invalid_part"] = {"type": "missing"}
        else:
            safe = field["error_code"] == "unknown_claim" or field["path"][-1] in {
                "id",
                "evidence_key",
                "passage_id",
                "status",
                "kind",
                "role",
                "gap_id",
                "source_id",
            }
            field["invalid_part"] = (
                value
                if safe and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:_.-]{1,80}", value)
                else {"type": type(value).__name__, "sha256": canonical_hash(value)}
            )
        unique.append(field)
    return unique[:8], errors[:8], allowed


def _apply_reading_patches(raw, output, paths, *, allowed_claim_ids=None):
    if not isinstance(output, dict) or set(output) != {"changes"}:
        raise DomainError("analysis_reading_invalid", "引用修复未返回受限字段补丁，原输出保留待核", 422)
    changes = output["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 8:
        raise DomainError("analysis_reading_invalid", "引用修复没有有效的受限字段补丁", 422)
    result, used = copy.deepcopy(raw), []
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"path", "operation", "value"}:
            raise DomainError("analysis_reading_invalid", "引用修复字段格式无效", 422)
        path = change.get("path")
        if (
            not isinstance(path, list)
            or not path
            or path not in paths
            or path in used
            or any(type(part) not in {str, int} for part in path)
            or any(path[: len(prior)] == prior or prior[: len(path)] == path for prior in used)
        ):
            raise DomainError("analysis_reading_invalid", "修复试图修改未诊断字段，原输出保留待核", 422)
        if allowed_claim_ids is not None and (
            change.get("operation") != "replace"
            or not isinstance(change.get("value"), str)
            or change["value"] not in allowed_claim_ids
        ):
            raise DomainError(
                "analysis_reading_invalid", "命题引用只能替换为本版合法编号，不能删除或改写", 422
            )
        try:
            parent = _at_path(result, path[:-1])
            if change.get("operation") == "replace" and "value" in change:
                parent[path[-1]] = copy.deepcopy(change["value"])
            elif change.get("operation") == "remove" and "value" not in change:
                del parent[path[-1]]
            else:
                raise TypeError
        except (KeyError, IndexError, TypeError):
            raise DomainError("analysis_reading_invalid", "引用修复路径或操作无效", 422) from None
        used.append(path)
    return result


def _reading_repair_estimates(service, fixed, original, payload):
    from bioradar.adapters.reasoning_model import encode_request

    receipt = original.get("receipt") or {}
    parameters = original.get("model_parameters") or fixed["model_parameters"]
    original_bytes = original.get("request_bytes") or receipt.get("request_bytes")
    if original_bytes is None:
        original_bytes = len(
            encode_request(
                service.settings.model_copy(
                    update={"semantic_llm_max_output_tokens": parameters["max_output_tokens"]}
                ),
                stage=original["stage"],
                system_prompt=original.get("system_prompt") or SYSTEM_PROMPT,
                payload=original["payload"],
                response_schema=original["response_schema"],
            )
        )
    actual = (receipt.get("usage") or {}).get("prompt_tokens")
    if type(actual) is int and actual >= 0:
        original_input = {"tokens": actual, "basis": "received_prompt_tokens"}
    else:
        estimate = request_estimate(fixed, original["stage"], original_bytes)
        original_input = {"tokens": estimate["estimated_input_tokens"], "basis": estimate["basis"]}
    repair_bytes = len(
        encode_request(
            service.settings.model_copy(
                update={
                    "semantic_llm_max_output_tokens": min(
                        2048, fixed["model_parameters"]["max_output_tokens"]
                    )
                }
            ),
            stage="read_plan_repair",
            system_prompt=_REPAIR_PROMPT,
            payload=payload,
            response_schema=_REPAIR_SCHEMA,
        )
    )
    estimate = request_estimate(fixed, "read_plan_repair", repair_bytes)
    return {
        "original_request_bytes": original_bytes,
        "repair_request_bytes": repair_bytes,
        "original_input_token_estimate": original_input,
        "repair_input_token_estimate": {
            "tokens": estimate["estimated_input_tokens"],
            "basis": estimate["basis"],
        },
        "max_input_ratio": 0.2,
    }


def validated_policy_output(
    service,
    identifier,
    token,
    fixed,
    progress,
    raw,
    payload,
    key,
    declared,
    *,
    optional=False,
    following_tokens=0,
):
    from bioradar.research.investigations.analysis import parse_policy_reading, validate_policy_reading
    from bioradar.research.investigations.query_policy import normalize_policy_coverage

    strict = fixed.get("query_policy", {}).get("research_contract_version") == "bioradar-research-input-v1"

    def parse(value):
        normalized, adjustments = parse_policy_reading(value)
        if strict:
            normalized, reference_adjustments = normalize_policy_synthesis(normalized)
            adjustments.extend(reference_adjustments)
        normalized, issues = normalize_policy_coverage(normalized, declared, progress["evidence"])
        reading = validate_policy_reading(
            normalized, progress["evidence"], fixed["search_scope"], strict_references=strict
        )
        return reading, adjustments, issues

    try:
        result = parse(raw)
    except DomainError as exc:
        fields, errors, allowed = _repair_fields(raw, payload, exc.code)
        field_errors = [{"path": item["path"], "error_code": item["reason"]} for item in errors] or [
            {"path": item["path"], "error_code": item["error_code"]} for item in fields
        ]
        progress.setdefault("reading_validation_errors", {})[key] = {
            "error_code": exc.code,
            "field_errors": field_errors,
            "original_request_sha256": canonical_hash(payload),
            "original_output_sha256": canonical_hash(raw),
        }
        service._checkpoint(identifier, token, progress)
        reference_fault = (
            exc.code == "analysis_output_invalid"
            and bool(fields)
            and bool(errors)
            and all(item["reason"] == "unknown_claim" for item in errors)
        )
        if not strict or not (
            reference_fault
            or exc.code
            in {"analysis_reading_invalid", "analysis_reference_invalid", "analysis_passage_invalid"}
        ):
            raise
        repairs = progress.setdefault("reading_repairs", {})
        entry = repairs.get(key)
        if entry is None:
            # A second evidence refresh does not create another repair allowance.
            if repairs:
                repair_keys = {item.get("repair_call_key") for item in repairs.values()}
                if any(item.get("status") in {"sent", "unconfirmed"} for item in repairs.values()) or any(
                    call.get("call_key") in repair_keys and call.get("status") in {"sent", "unconfirmed"}
                    for call in progress["calls"]
                ):
                    raise DomainError(
                        "model_call_outcome_unconfirmed", "既有读取修复结果未知，未再发请求", 409
                    )
                repairs[key] = {
                    "attempts": 0,
                    "reason": exc.code,
                    "original_call_key": key,
                    "status": "invalid",
                    "repair_error_code": "analysis_reading_repair_exhausted",
                }
                service._checkpoint(identifier, token, progress)
                raise
            entry = repairs[key] = {
                "attempts": 1,
                "reason": exc.code,
                "error_code": exc.code,
                "original_call_key": key,
                "repair_call_key": key + ":structure-repair",
                "status": "prepared",
                "response_format": "field_patches",
                "field_paths": [field["path"] for field in fields],
                "reference_errors": errors,
                "allowed_reference_ids": allowed,
                "original_request_sha256": canonical_hash(payload),
                "original_output_sha256": canonical_hash(raw),
            }
            entry["repair_payload"] = {
                "task": "repair_structured_reading_fields",
                "error_code": exc.code,
                "field_paths": entry["field_paths"],
                "invalid_fields": fields,
                "allowed_reference_ids": allowed,
            }
            if errors:
                entry["repair_payload"]["reference_errors"] = errors
            if reference_fault:
                entry["allowed_claim_ids"] = [claim["id"] for claim in raw["claims"]]
                entry["repair_payload"]["allowed_claim_ids"] = entry["allowed_claim_ids"]
            entry["repair_payload_sha256"] = canonical_hash(entry["repair_payload"])
        saved = next(
            (call for call in progress["calls"] if call.get("call_key") == entry.get("repair_call_key")), None
        )
        if not entry.get("repair_call_key"):
            raise
        try:
            compact = entry.get("response_format") == "field_patches"
            if compact and (
                entry.get("original_request_sha256") != canonical_hash(payload)
                or entry.get("original_output_sha256") != canonical_hash(raw)
                or entry.get("repair_payload_sha256") != canonical_hash(entry["repair_payload"])
            ):
                raise DomainError("analysis_call_input_changed", "读取修复原输入发生变化，未复用输出", 409)
            if entry["status"] == "not_sent" and saved is None:
                raise DomainError(entry["repair_error_code"], "本次有界修复已停止，原解读与回执保留", 409)
            if saved is not None and saved["status"] != "prepared":
                # Old full-reading receipts and new field-patch receipts both
                # replay their exact stored envelope before any fresh budget check.
                repaired, _ = service._call(
                    identifier,
                    token,
                    "read_plan_repair",
                    saved.get("response_schema", PolicyReading),
                    fixed,
                    progress,
                    saved["payload"],
                    call_key=entry["repair_call_key"],
                    system_prompt=saved.get("system_prompt"),
                )
            else:
                if not compact:
                    if saved is None:
                        raise DomainError(
                            "analysis_reading_invalid", "旧读取修复缺少原调用，原输出保留待核", 422
                        )
                    service._not_sent_call(
                        identifier,
                        token,
                        fixed,
                        progress,
                        saved,
                        "analysis_token_budget",
                        "旧整份读取修复未发送；不得重新发送原文或绕过20%上限",
                    )
                original = next(call for call in progress["calls"] if call.get("call_key") == key)
                entry.update(_reading_repair_estimates(service, fixed, original, entry["repair_payload"]))
                service._checkpoint(identifier, token, progress)
                required = entry["repair_input_token_estimate"]["tokens"]
                if (
                    not entry["field_paths"]
                    or required * 5 > entry["original_input_token_estimate"]["tokens"]
                ):
                    entry["stop_reason"] = "repair_input_over_twenty_percent"
                    if saved is not None:
                        service._not_sent_call(
                            identifier,
                            token,
                            fixed,
                            progress,
                            saved,
                            "analysis_token_budget",
                            "读取修复超过原请求输入20%，未发送；原输出及错误保留待核",
                        )
                    raise DomainError("analysis_token_budget", "读取修复超过原请求输入20%，未发送", 409)
                if (
                    optional
                    and saved is None
                    and not service._optional_budget(
                        fixed,
                        progress,
                        1,
                        request_tokens=required + min(2048, fixed["model_parameters"]["max_output_tokens"]),
                    )
                ):
                    raise DomainError(
                        "analysis_finishing_budget_reserved", "本次实际修复请求额度不足，未发送", 409
                    )
                repaired, _ = service._call(
                    identifier,
                    token,
                    "read_plan_repair",
                    _REPAIR_SCHEMA,
                    fixed,
                    progress,
                    entry["repair_payload"],
                    call_key=entry["repair_call_key"],
                    system_prompt=_REPAIR_PROMPT,
                )
            saved = next(
                call for call in progress["calls"] if call.get("call_key") == entry["repair_call_key"]
            )
            entry["repair_call_id"] = saved["call_id"]
            corrected = (
                _apply_reading_patches(
                    raw,
                    repaired,
                    entry["field_paths"],
                    allowed_claim_ids=entry["repair_payload"].get("allowed_claim_ids"),
                )
                if compact
                else repaired
            )
            result = parse(corrected)
        except DomainError as failure:
            saved = next(
                (call for call in progress["calls"] if call.get("call_key") == entry.get("repair_call_key")),
                None,
            )
            entry["status"] = (
                "unconfirmed"
                if saved is not None and saved["status"] in {"sent", "unconfirmed"}
                else "not_sent"
                if service._report_budget_stop(failure)
                or failure.code == "analysis_finishing_budget_reserved"
                else "invalid"
                if failure.code
                in {
                    "analysis_reading_invalid",
                    "analysis_reference_invalid",
                    "analysis_passage_invalid",
                    "analysis_output_invalid",
                }
                else "failed"
            )
            entry["repair_error_code"] = failure.code
            if saved is not None:
                entry["repair_call_id"] = saved["call_id"]
            service._checkpoint(identifier, token, progress)
            raise
        entry["status"] = "validated"
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
        if (
            fixed.get("query_policy", {}).get("schema_version") != "query-planner.external.v2"
            and len(progress["queries"]) >= fixed["budget"]["max_queries"]
        ):
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


def retained_policy_reading(fixed, progress):
    """Return only a reading tied to its received input, fixed versions and exact text."""
    from bioradar.research.investigations.analysis import (
        model_evidence,
        parse_policy_reading,
        semantic_reading_state,
        validate_policy_reading,
        validate_selection,
    )
    from bioradar.research.investigations.query_policy import normalize_policy_coverage

    try:
        state = progress.get("query_policy") or {}
        fingerprint = state.get("reading_evidence_hash")
        if (
            state.get("reading_kind") != "policy"
            or fixed.get("query_context_version") != "semantic-v2"
            or fixed.get("query_policy", {}).get("effective") != "query_planner"
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            return None
        key = "policy-read:" + fingerprint
        originals = [call for call in progress.get("calls", []) if call.get("call_key") == key]
        if len(originals) != 1:
            return None
        original = originals[0]

        def received(call):
            receipt = call.get("receipt") or {}
            return call.get("status") == "received" and receipt.get("status") == "completed"

        if original.get("stage") != "read_plan" or not received(original):
            return None
        payload = original["payload"]
        if (
            payload.get("task") != "read_plan"
            or payload.get("question") != fixed["question"]
            or payload.get("as_of") != fixed["as_of"]
            or payload.get("search_scope") != fixed["search_scope"]
        ):
            return None
        source = original
        repair = progress.get("reading_repairs", {}).get(key)
        if repair is not None:
            repairs = [
                call
                for call in progress.get("calls", [])
                if call.get("call_key") == repair.get("repair_call_key")
            ]
            if repair.get("status") != "validated" or len(repairs) != 1:
                return None
            source = repairs[0]
            if (
                repair.get("original_call_key") != key
                or source.get("stage") != "read_plan_repair"
                or not received(source)
                or source.get("call_id") != repair.get("repair_call_id")
            ):
                return None
            if repair.get("response_format") == "field_patches":
                if (
                    repair.get("original_request_sha256") != canonical_hash(payload)
                    or repair.get("original_output_sha256") != canonical_hash(original.get("output"))
                    or repair.get("repair_payload_sha256") != canonical_hash(source["payload"])
                    or source["payload"].get("field_paths") != repair.get("field_paths")
                ):
                    return None
                reading_output = _apply_reading_patches(
                    original["output"],
                    source["output"],
                    repair["field_paths"],
                    allowed_claim_ids=source["payload"].get("allowed_claim_ids"),
                )
            elif source["payload"].get("original_request") != payload or source["payload"].get(
                "invalid_output"
            ) != original.get("output"):
                return None
            else:
                reading_output = source["output"]
        else:
            reading_output = source["output"]
        rows = progress["evidence"]
        by_key = {item["key"]: item for item in rows}
        requested = [item["evidence_key"] for item in payload["evidence"]]
        if len(by_key) != len(rows) or len(set(requested)) != len(requested):
            return None
        selected = [by_key[key] for key in requested]
        for item in selected:
            validate_selection(item)
        # The model projection has no standalone version field. Full projection
        # equality checks its actual words/context; the fingerprint below binds
        # selection_hash, which also contains the complete adopted record_ref.
        if (
            model_evidence(
                selected,
                include_source_context="research_task" in payload,
                evidence_time=fixed.get("evidence_time"),
            )
            != payload["evidence"]
        ):
            return None
        if payload.get("mechanism_query_context") or payload.get("mechanism_evaluation"):
            # The compact mechanism query omits part of its semantic preimage.
            # Never substitute the current professional context to guess it.
            return None
        context = {
            "graph_context": payload.get("graph_context", []),
            "analysis_results": payload.get("analysis_results", []),
            "quality_feedback": payload.get("quality_feedback"),
            "mechanism_evaluation": None,
        }
        if canonical_hash(semantic_reading_state(selected, context)) != fingerprint:
            return None
        normalized, _ = parse_policy_reading(reading_output)
        if fixed.get("query_policy", {}).get("research_contract_version") == "bioradar-research-input-v1":
            normalized, _ = normalize_policy_synthesis(normalized)
        normalized, _ = normalize_policy_coverage(normalized, payload.get("coverage_checks", []), selected)
        parsed = validate_policy_reading(
            normalized,
            selected,
            fixed["search_scope"],
            strict_references=fixed.get("query_policy", {}).get("research_contract_version")
            == "bioradar-research-input-v1",
        )
        if parsed.model_dump(mode="json") != state.get("reading"):
            return None
        return parsed, original
    except DomainError as exc:
        if hasattr(exc, "receipt") or exc.code not in {
            "analysis_selection_mismatch",
            "analysis_reading_invalid",
            "analysis_reference_invalid",
            "analysis_passage_invalid",
            "analysis_unbound_claim",
            "analysis_query_invalid",
            "analysis_output_invalid",
            "analysis_policy_coverage_invalid",
        }:
            raise
        return None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
