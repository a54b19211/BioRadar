"""Exact claim binding for reading and the future final research contract."""

import copy
import json

from pydantic import BaseModel, ValidationError

from bioradar.api.errors import DomainError

FINAL_ASSESSMENT_VERSION = "bioradar-research-final-v1"
ASSESSMENT_SCOPE = "current_question_general_action_with_targeted_next_actions"


def _plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def synthesis_schema(fixed):
    """Only an explicit future frozen contract can select the enhanced wire schema."""
    from bioradar.research.investigations.analysis_models import FinalResearchSynthesis, ModelSynthesis

    policy = fixed.get("query_policy", {})
    version = policy.get("final_assessment_version")
    if version is None:
        return ModelSynthesis
    if (
        version != FINAL_ASSESSMENT_VERSION
        or policy.get("schema_version") != "query-planner.external.v2"
        or policy.get("effective") != "query_planner"
        or policy.get("research_contract_version") != "bioradar-research-input-v1"
    ):
        raise DomainError("analysis_final_assessment_contract_invalid", "最终研究判断契约不匹配", 422)
    return FinalResearchSynthesis


def final_assessment_context(fixed):
    """A future-only instruction; never insert it into an existing frozen model request."""
    from bioradar.research.investigations.analysis_models import ModelSynthesis

    if synthesis_schema(fixed) is ModelSynthesis:
        return {}
    payload = {
        "version": FINAL_ASSESSMENT_VERSION,
        "scope": ASSESSMENT_SCOPE,
        "decision_instruction": (
            "warning_assessment.decision是本轮问题与当前证据范围的总体行动建议，"
            "next_actions可包含对特定对象的定向核实；总体观察与定向立即核实可以并存。"
            "在reason中说明主次和对象。该建议不是真实性概率，不证明传言成立；不要按措辞自动升级。"
        ),
        "claim_instruction": (
            "claims、warning_assessment和competing_hypotheses在本次综合输出中共同生成。"
            "所有支持、反对和决定命题编号必须逐字引用本次claims已有id，不能沿用阅读阶段旧编号、"
            "按编号相似度猜对应关系，或用宿主替换编号。不适用的warning设null，合理竞争解释可为空。"
        ),
    }
    topic_scope = (fixed.get("origin") or {}).get("topic_risk_scope")
    if topic_scope is not None:
        payload["topic_risk_scope"] = copy.deepcopy(_plain(topic_scope))
        definition = (fixed.get("origin") or {}).get("topic_risk_definition")
        if definition is not None:
            payload["topic_risk_definition"] = copy.deepcopy(_plain(definition))
        payload["topic_risk_instruction"] = (
            "可选topic_risk_reassessment只能复核给定scope，不能自造或扩大范围。"
            "decision_statement必须逐字等于本次decision_claim_id命题text，并在最终报告正文中逐字呈现。"
            "保留支持、范围与控制依据、反证、条件和关键缺口；部分覆盖、缺口或专业核验PASS本身不等于风险已解除。"
            "没有实际topic_risk_definition时不能仅凭hash宣称完整范围，须保留未知或部分覆盖。"
            "没有对应已核正文和本次固定scope绑定时，不得据未核侧项解除风险。"
        )
    return payload


def claim_assessment_errors(value, claims=None, *, topic_risk_scope=None, check_topic_scope=True):
    """Return every protocol issue, containing IDs/paths only, without rewriting the input."""
    value = _plain(value)
    value = value if isinstance(value, dict) else {}
    supplied = claims if claims is not None else value.get("claims", [])
    rows = [_plain(claim) for claim in supplied] if isinstance(supplied, (list, tuple)) else []
    rows = [claim for claim in rows if isinstance(claim, dict) and isinstance(claim.get("id"), str)]
    by_id = {claim["id"]: claim for claim in rows}
    allowed = list(by_id)
    errors = []

    def issue(path, reason, claim_id=None):
        item = {"path": path, "reason": reason}
        if claim_id is not None:
            item["claim_id"] = claim_id
        if reason == "unknown_claim":
            item["allowed_claim_ids"] = allowed.copy()
        errors.append(item)

    if len(rows) != len(by_id):
        issue(["claims"], "duplicate_claim_id")

    def references(item, path, fields, *, unique=False, allow_overlap=False):
        found = {}
        for field in fields:
            identifiers = item.get(field, [])
            if not isinstance(identifiers, list):
                issue([*path, field], "invalid_reference_list")
                continue
            for index, identifier in enumerate(identifiers):
                if not isinstance(identifier, str):
                    issue([*path, field, index], "invalid_claim_id")
                elif identifier not in by_id:
                    issue([*path, field, index], "unknown_claim", identifier)
                if unique and isinstance(identifier, str) and identifier in found:
                    issue([*path, field, index], "duplicate_risk_reference", identifier)
                if isinstance(identifier, str):
                    found[identifier] = field
        support, oppose = item.get("supporting_claim_ids", []), item.get("opposing_claim_ids", [])
        if not allow_overlap and isinstance(support, list) and isinstance(oppose, list):
            for identifier in sorted(
                set(x for x in support if isinstance(x, str)) & set(x for x in oppose if isinstance(x, str))
            ):
                issue(path, "support_opposition_overlap", identifier)
        return found

    explanations = value.get("explanations", [])
    if isinstance(explanations, list):
        for index, explanation in enumerate(explanations):
            if isinstance(explanation, dict):
                references(explanation, ["explanations", index], ("for_claim_ids", "against_claim_ids"))

    warning = value.get("warning_assessment")
    if warning is not None:
        if not isinstance(warning, dict):
            issue(["warning_assessment"], "invalid_assessment_structure")
        else:
            references(warning, ["warning_assessment"], ("supporting_claim_ids", "opposing_claim_ids"))
            if warning.get("decision") == "alert":
                supporting = warning.get("supporting_claim_ids", [])
                supporting = supporting if isinstance(supporting, list) else []
                if not any(
                    by_id.get(key, {}).get("evidence_links") for key in supporting if isinstance(key, str)
                ):
                    issue(["warning_assessment", "supporting_claim_ids"], "alert_without_bound_evidence")
                if not warning.get("critical_unknowns") or not warning.get("next_actions"):
                    issue(["warning_assessment"], "alert_without_unknowns_or_actions")
    hypotheses = value.get("competing_hypotheses") or []
    seen = set()
    if not isinstance(hypotheses, list):
        issue(["competing_hypotheses"], "invalid_assessment_structure")
        hypotheses = []
    for index, hypothesis in enumerate(hypotheses):
        path = ["competing_hypotheses", index]
        if not isinstance(hypothesis, dict):
            issue(path, "invalid_assessment_structure")
            continue
        identifier = hypothesis.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                issue([*path, "id"], "duplicate_hypothesis_id")
            seen.add(identifier)
        # A compound source claim may contain both a positive result and its limitation.
        # Preserve both links; the explanation and downstream verification assess their meaning.
        references(hypothesis, path, ("supporting_claim_ids", "opposing_claim_ids"), allow_overlap=True)

    risk = value.get("topic_risk_reassessment")
    if risk is not None:
        path = ["topic_risk_reassessment"]
        if not isinstance(risk, dict):
            issue(path, "invalid_assessment_structure")
            return errors
        decision = risk.get("decision_claim_id")
        if not isinstance(decision, str):
            issue([*path, "decision_claim_id"], "invalid_claim_id")
        elif decision not in by_id:
            issue([*path, "decision_claim_id"], "unknown_claim", decision)
        elif risk.get("decision_statement") != by_id[decision].get("text"):
            issue([*path, "decision_statement"], "decision_statement_mismatch", decision)
        basis = references(
            risk, path, ("supporting_claim_ids", "scope_control_claim_ids", "opposing_claim_ids"), unique=True
        )
        if isinstance(decision, str) and decision in basis:
            issue([*path, "decision_claim_id"], "decision_claim_is_own_basis", decision)
        if risk.get("decision") == "resolved_within_scope" and (
            risk.get("scope_coverage") != "full_declared_scope" or risk.get("remaining_critical_gaps")
        ):
            issue(path, "resolution_without_full_scope")
        if check_topic_scope:
            from bioradar.topics.risk import TopicRiskScopeStamp

            try:
                expected = TopicRiskScopeStamp.model_validate(_plain(topic_risk_scope))
                actual = TopicRiskScopeStamp.model_validate(risk.get("scope"))
            except ValidationError:
                issue([*path, "scope"], "missing_or_invalid_frozen_scope")
            else:
                if actual != expected:
                    issue([*path, "scope"], "frozen_scope_mismatch")
    return errors


def validate_claim_assessments(value, claims, *, error_code="analysis_output_invalid", topic_risk_scope=None):
    errors = claim_assessment_errors(value, claims, topic_risk_scope=topic_risk_scope)
    if errors:
        messages = {
            "duplicate_hypothesis_id": "竞争假设编号重复",
            "unknown_claim": "研究判断引用不存在的命题",
            "support_opposition_overlap": "同一命题不能同时标为同一假设的支持与反证",
            "alert_without_bound_evidence": "预警判断须绑定实际固定原文命题",
            "alert_without_unknowns_or_actions": "预警判断须保留关键未知与核实动作",
        }
        raise DomainError(
            error_code, messages.get(errors[0]["reason"], "研究判断未绑定本版命题或固定适用范围"), 422
        )


def assessment_binding(value):
    """Read-only legacy projection: exact binding is not a scientific score or new decision."""
    plain = _plain(value)
    plain = plain if isinstance(plain, dict) else {}
    present = any(
        plain.get(key) for key in ("warning_assessment", "competing_hypotheses", "topic_risk_reassessment")
    )
    errors = claim_assessment_errors(plain, check_topic_scope=False)
    return {
        "status": "unbound" if errors else "bound" if present else "not_provided",
        "scope": "current_result_claim_ids",
        "can_use_bound_claim_refs": bool(present and not errors),
        "unbound_claim_ids": list(
            dict.fromkeys(
                item["claim_id"]
                for item in errors
                if item["reason"] == "unknown_claim" and "claim_id" in item
            )
        ),
        "issues": errors,
        "qualification": (
            "仅检查本版命题编号绑定；不代表专业通过、风险已解除或科学评分。未绑定判断不得作为已绑定依据使用。"
        ),
    }


def assessment_result_view(value):
    """Annotate a copy for API/export; never write a reading judgment into a final result."""
    plain = copy.deepcopy(_plain(value))
    if not isinstance(plain, dict):
        return plain
    names = ("warning_assessment", "competing_hypotheses", "topic_risk_reassessment")
    if any(key in plain and plain[key] is not None for key in names):
        plain["assessment_binding"] = assessment_binding(plain)
    else:
        plain.pop("assessment_binding", None)
    return plain


def assessment_binding_note(value):
    if assessment_binding(value)["status"] == "unbound":
        return "系统记录：结构化研究判断未绑定本版命题；原稿与原判断保留，不作为已绑定评分或自动行动依据。"
    return ""


def assessment_repair_payload(
    original_request, invalid_output, *, validation_error="analysis_assessment_invalid", topic_risk_scope=None
):
    """A shared-ledger caller may send this once; this function never chooses replacement IDs."""
    plain = _plain(invalid_output)
    plain = plain if isinstance(plain, dict) else {}
    claims = plain.get("claims", [])
    claims = claims if isinstance(claims, list) else []
    payload = {
        "task": "repair_final_assessment",
        "original_request": copy.deepcopy(original_request),
        "invalid_output": copy.deepcopy(_plain(invalid_output)),
        "validation_error": validation_error,
        "allowed_claim_ids": [
            claim["id"] for claim in claims if isinstance(claim, dict) and isinstance(claim.get("id"), str)
        ],
        "reference_errors": claim_assessment_errors(plain, topic_risk_scope=topic_risk_scope),
        "instruction": (
            "只允许reference_errors中unknown_claim路径的原无效命题编号，改为allowed_claim_ids中的实际编号。"
            "重新对照同一原文与当前claims，不沿用旧阅读编号，不按位置或相似度猜编号；宿主未替换任何链接。"
            "其余JSON必须逐字段保持，包括claims和文本、行动等级、支持反对关系、关键未知、缺口、条件与scope。"
            "无法确定合法编号时不得猜测、删除字段或改写结论；修复不会放宽完整校验。"
            "完整输出同一增强JSON，不得输出新的原文或自造专题scope。"
        ),
    }
    if topic_risk_scope is not None:
        payload["required_topic_risk_scope"] = copy.deepcopy(_plain(topic_risk_scope))
    return payload


def validate_assessment_repair(original, repaired):
    """Allow only unknown-ID substitutions; the caller must then run full validation."""
    before, after = _plain(original), _plain(repaired)
    errors = claim_assessment_errors(before, check_topic_scope=False)

    def reject():
        raise DomainError(
            "analysis_assessment_repair_invalid",
            "本次修复仅允许原无效命题编号改为实际已有编号；其余结构和文字必须逐字段保持",
            422,
        )

    if not isinstance(before, dict) or not isinstance(after, dict) or not errors:
        reject()
    if any(error["reason"] != "unknown_claim" for error in errors):
        reject()
    expected = copy.deepcopy(before)
    try:
        for error in errors:
            path = error["path"]
            source, target, unchanged = before, after, expected
            for part in path[:-1]:
                source, target, unchanged = source[part], target[part], unchanged[part]
            old, new = source[path[-1]], target[path[-1]]
            allowed = error["allowed_claim_ids"]
            if not isinstance(old, str) or old in allowed or not isinstance(new, str) or new not in allowed:
                reject()
            unchanged[path[-1]] = new

        def canonical(value):
            return json.dumps(
                value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )

        if canonical(expected) != canonical(after):
            reject()
    except (KeyError, IndexError, TypeError, ValueError):
        reject()


FINAL_REFERENCE_REPAIR_VERSION = "bioradar-synthesis-reference-repair-v1"
_FINAL_REFERENCE_ERRORS = {"unknown_claim", "unknown_evidence", "unknown_passage"}


def _final_source_reference_ids(original_request):
    """Use only the exact original model input, never later accumulated evidence."""
    request = _plain(original_request)
    rows = request.get("evidence") if isinstance(request, dict) else None
    if not isinstance(rows, list):
        raise DomainError("analysis_reference_repair_context_invalid", "缺少原综合输入引用清单", 422)
    allowed, owners = {}, {}
    for row in rows:
        key = row.get("evidence_key") if isinstance(row, dict) else None
        passages = row.get("passages") if isinstance(row, dict) else None
        if not isinstance(key, str) or not key or key in allowed or not isinstance(passages, list):
            raise DomainError("analysis_reference_repair_context_invalid", "原综合输入引用身份不唯一", 422)
        identifiers = []
        for passage in passages:
            identifier = passage.get("id") if isinstance(passage, dict) else None
            if not isinstance(identifier, str) or not identifier or identifier in owners:
                raise DomainError("analysis_reference_repair_context_invalid", "原综合片段身份不唯一", 422)
            identifiers.append(identifier)
            owners[identifier] = key
        if not identifiers:
            raise DomainError("analysis_reference_repair_context_invalid", "原综合材料没有固定片段", 422)
        allowed[key] = identifiers
    return allowed, owners


def final_reference_errors(value, original_request, *, topic_risk_scope=None):
    """Describe scalar ID faults; semantic/structural faults cannot enter this repair."""
    from bioradar.research.investigations.analysis_models import FinalResearchSynthesis

    plain = _plain(value)
    try:
        FinalResearchSynthesis.model_validate(plain)
    except ValidationError as exc:
        return [
            {"path": list(item["loc"]), "reason": "invalid_synthesis_structure", "type": item["type"]}
            for item in exc.errors(include_input=False, include_context=False)
        ]
    allowed, owners = _final_source_reference_ids(original_request)
    errors = claim_assessment_errors(plain, topic_risk_scope=topic_risk_scope)
    for error in errors:
        if error["reason"] == "unknown_claim":
            error["allowed_id_values"] = error["allowed_claim_ids"].copy()
    for claim_index, claim in enumerate(plain["claims"]):
        for link_index, link in enumerate(claim["evidence_links"]):
            key, passage = link["evidence_key"], link["passage_id"]
            path = ["claims", claim_index, "evidence_links", link_index]
            if key not in allowed:
                errors.append(
                    {
                        "path": [*path, "evidence_key"],
                        "reason": "unknown_evidence",
                        "claim_id": claim["id"],
                        "rejected_id": key,
                        "allowed_id_values": [owners[passage]] if passage in owners else list(allowed),
                    }
                )
            # A real evidence owner is immutable. A globally valid passage also
            # remains immutable when only its owner key was invented.
            permitted = allowed[key] if key in allowed else list(owners)
            if passage not in permitted:
                errors.append(
                    {
                        "path": [*path, "passage_id"],
                        "reason": "unknown_passage",
                        "claim_id": claim["id"],
                        "evidence_key": key,
                        "rejected_id": passage,
                        "allowed_id_values": permitted.copy(),
                    }
                )
    if any(error.get("allowed_id_values") == [] for error in errors):
        errors.append({"path": [], "reason": "no_legal_reference_replacement"})
    return errors


def _require_final_reference_errors(errors):
    if not errors or any(error["reason"] not in _FINAL_REFERENCE_ERRORS for error in errors):
        raise DomainError(
            "analysis_reference_repair_invalid", "仅本轮确定无效引用可进入一次综合引用修复", 422
        )


def final_reference_repair_payload(
    original_request, invalid_output, *, validation_error, topic_risk_scope=None
):
    """Prepare one shared synthesis-repair slot; the host owns receipts and budget."""
    import hashlib

    plain = _plain(invalid_output)
    errors = final_reference_errors(plain, original_request, topic_risk_scope=topic_risk_scope)
    _require_final_reference_errors(errors)
    allowed, _ = _final_source_reference_ids(original_request)
    encoded = json.dumps(plain, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    payload = {
        "task": "repair_final_references",
        "repair_version": FINAL_REFERENCE_REPAIR_VERSION,
        "maximum_attempts": 1,
        "original_request": copy.deepcopy(_plain(original_request)),
        "invalid_output": copy.deepcopy(plain),
        "original_output_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "validation_error": validation_error,
        "reference_errors": errors,
        "allowed_claim_ids": [claim["id"] for claim in plain["claims"]],
        "allowed_reference_ids": allowed,
        "instruction": (
            "本次综合仅有一次共用引用修复名额，不区分命题、材料或片段另开重试。"
            "只允许reference_errors列出的无效标识字段改为该路径allowed_id_values中的已有编号。"
            "有效材料owner和有效片段编号不能换；最终材料与片段必须是原输入的真实配对。"
            "逐条重读同一原文判断能否支持原命题，不按编号相似度、列表位置或仅因只有一个候选而猜链接。"
            "其他JSON逐字段原样保留，包括命题编号与文字、role/reason、行动等级、条件、反证、缺口、"
            "范围与列表次序/数量。无法确认支持时保留原无效编号供校验拒绝，不删除链接或改写结论求通过。"
            "输出同一完整增强JSON；合法编号不代表事实已核验或风险已解除，修复后仍须全部校验。"
        ),
    }
    if topic_risk_scope is not None:
        payload["required_topic_risk_scope"] = copy.deepcopy(_plain(topic_risk_scope))
    return payload


def validate_final_reference_repair(original, repaired, original_request, *, topic_risk_scope=None):
    """Permit only known-invalid scalar IDs, then require complete host validation."""
    before, after = _plain(original), _plain(repaired)
    errors = final_reference_errors(before, original_request, topic_risk_scope=topic_risk_scope)
    _require_final_reference_errors(errors)

    def reject():
        raise DomainError(
            "analysis_reference_repair_invalid",
            "引用修复仅可改列出的无效编号；有效owner、全文、角色、条件与反证必须原样保留",
            422,
        )

    if not isinstance(after, dict):
        reject()
    expected = copy.deepcopy(before)
    try:
        for error in errors:
            source, target, unchanged = before, after, expected
            path = error["path"]
            for part in path[:-1]:
                source, target, unchanged = source[part], target[part], unchanged[part]
            old, new = source[path[-1]], target[path[-1]]
            allowed = error["allowed_id_values"]
            if not isinstance(old, str) or old in allowed or not isinstance(new, str) or new not in allowed:
                reject()
            unchanged[path[-1]] = new

        def canonical(value):
            return json.dumps(
                value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )

        if canonical(expected) != canonical(after):
            reject()
        allowed, _ = _final_source_reference_ids(original_request)
        for claim in after["claims"]:
            for link in claim["evidence_links"]:
                if (
                    link["evidence_key"] not in allowed
                    or link["passage_id"] not in allowed[link["evidence_key"]]
                ):
                    reject()
    except (KeyError, IndexError, TypeError, ValueError):
        reject()
