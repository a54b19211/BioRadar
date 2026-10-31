"""Ordinary query functions shared by the legacy baseline and the installed selector."""

import copy
import hashlib
import json
import re

from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_models import InitialQuery, QuestionPlan

SCHEMA_VERSION = "query-planner.external.v1"
BASELINE_VERSION = "bioradar-query-baseline-v1"
INITIAL_RETRIEVAL_VERSION = "literal-language-alternatives-v1"
BOUNDED_INITIAL_VERSION = "literal-language-budget-v2"
PAGINATION_VERSION = "fabric-receipt-bound-pagination-v1"
PAGINATION_COVER_VERSION = "fabric-parent-gap-pagination-v1"
_DIMENSIONS = {"tool_calls", "tokens", "relation_access"}
_GRAPH_KINDS = {"entity_relations", "graph_neighbors", "graph_path"}
_PENDING = {"prepared", "sent", "unknown", "unconfirmed"}
_HOST_ARGUMENTS = {
    "phase",
    "index",
    "plan",
    "reason",
    "relaxed_from",
    "relaxation_reason",
    "decision_step",
}
_PIPELINE_CHECKS = {
    "support": "支持该管线或多靶标方向的原始研究",
    "counterevidence": "反证、否定结果与限制条件",
    "competition": "竞争项目及其实际阶段和结果",
    "analogous_failure": "类似技术或项目的失败及可比条件",
    "technology": "技术可行性、适用条件和关键实验",
    "patent": "专利申请、权利范围和公开时间",
}
_CATEGORY_WORDS = {
    "support": ("支持", "原始", "clinical", "evidence", "support"),
    "counterevidence": ("反证", "反对", "否定", "冲突", "opposing", "contradict", "negative"),
    "competition": ("竞争", "竞品", "competitor", "competing"),
    "analogous_failure": ("类似失败", "失败", "failure", "failed"),
    "technology": ("技术", "实验", "机制", "experiment", "technology", "mechanism"),
    "patent": ("专利", "patent", "intellectual property"),
}


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return copy.deepcopy(value)


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _canonical(value, name=""):
    if isinstance(value, dict):
        return {key: _canonical(item, key) for key, item in sorted(value.items())}
    if isinstance(value, list):
        items = [_canonical(item, name) for item in value]
        if name in {"terms", "source_ids", "knowledge_base_ids", "seed_ids"} and all(
            isinstance(item, str) for item in items
        ):
            return sorted(set(items))
        return items
    if isinstance(value, str) and name in {"terms", "query"}:
        return " ".join(value.casefold().split())
    return value


def semantic_key(kind, arguments):
    """Hash real tool meaning; changing a stage, reason or action ID cannot enable a retry."""
    if kind not in {
        "document_search",
        "document_read",
        "source_fetch",
        *_GRAPH_KINDS,
        "dataset_prepare",
        "analysis_run",
        "professional_assess",
    } or not isinstance(arguments, dict):
        raise DomainError("query_policy_action_invalid", "查询动作种类或参数无效", 422)
    effective = {key: value for key, value in arguments.items() if key not in _HOST_ARGUMENTS}
    if kind == "document_search" and "cursor" in arguments:
        # A changed display ID, page label or predecessor cannot resend an opaque cursor.
        required = ("source_id", "terms", "limit", "scope", "cursor")
        if arguments.get("provider") != "fabric" or any(k not in arguments for k in required):
            raise DomainError("query_policy_action_invalid", "续页缺少真实游标或固定检索范围", 422)
        return _hash(
            [
                kind,
                {
                    "provider": "fabric",
                    "source_id": arguments["source_id"],
                    "terms": sorted({term.strip() for term in arguments["terms"]}),
                    "limit": arguments["limit"],
                    "scope": arguments["scope"],
                    "catalog_discovery": arguments.get("catalog_discovery", False),
                    "cursor": arguments["cursor"],
                },
            ]
        )
    if kind == "document_search" and arguments.get("catalog_discovery") is True:
        if (
            arguments.get("provider") != "fabric"
            or arguments.get("terms") != []
            or "query" in arguments
            or not isinstance(arguments.get("source_id"), str)
            or not arguments["source_id"]
            or type(arguments.get("is_demo")) is not bool
            or arguments.get("mode")
            not in {
                "current",
                "operational_replay",
                "public_history_reconstruction",
            }
            or not isinstance(arguments.get("as_of"), str)
            or not arguments["as_of"]
        ):
            raise DomainError("query_policy_action_invalid", "目录发现缺少明确来源或固定时点", 422)
        effective = {
            key: arguments[key]
            for key in ("provider", "source_id", "mode", "as_of", "is_demo", "catalog_discovery")
        }
    if kind == "document_read":
        records = copy.deepcopy(effective.get("records"))
        if not isinstance(records, list) or not records:
            raise DomainError("query_policy_action_invalid", "固定读取动作缺少原件窗口", 422)
        for record in records:
            if not isinstance(record, dict) or any(
                not isinstance(record.get(key), str) or not record[key]
                for key in ("source_record_id", "version_id")
            ):
                raise DomainError("query_policy_action_invalid", "固定读取原件身份无效", 422)
            window = record.get("window_start", 0)
            if type(window) is not int or window < 0:
                raise DomainError("query_policy_action_invalid", "固定读取窗口无效", 422)
            if window == 0:
                record.pop("window_start", None)
        records.sort(key=lambda r: (r["source_record_id"], r["version_id"], r.get("window_start", 0)))
        effective["records"] = records
    try:
        return _hash([kind, _canonical(effective)])
    except (TypeError, ValueError, RecursionError):
        raise DomainError("query_policy_action_invalid", "查询动作参数不是有限JSON对象", 422) from None


def _baseline_source():
    from pathlib import Path

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def freeze_policy(settings, requested=None):
    """Freeze the selected method and actual local code version with this research input."""
    from bioradar.adapters.query_planner import load_query_planner, provider_status

    selected = requested or getattr(settings, "analysis_query_policy", "baseline")
    if selected not in {"baseline", "query_planner"}:
        raise DomainError("query_policy_invalid", "查询策略只能选baseline或query_planner", 422)
    status = provider_status(settings)
    version = BASELINE_VERSION
    source = _baseline_source()
    if selected == "query_planner":
        _, version = load_query_planner(settings)
        source = version
    kdd = selected == "query_planner" and getattr(settings, "analysis_kdd_enabled", False)
    return {
        "requested": selected,
        "effective": selected,
        "version": version,
        "source_sha256": source,
        "schema_version": "query-planner.external.v2" if kdd else SCHEMA_VERSION,
        "selector": getattr(settings, "analysis_kdd_selector", "information_gain_v1")
        if kdd
        else "legacy_sequence"
        if selected == "baseline"
        else "heuristic",
        "stop": "observed_checks_reading_pages_objects_v4"
        if kdd
        else "legacy_bounded_sequence"
        if selected == "baseline"
        else "rule",
        "training_status": "llm_rules_baseline" if selected == "baseline" else "heuristic_untrained",
        "max_steps": getattr(settings, "analysis_query_policy_v2_max_steps", 32)
        if kdd
        else getattr(settings, "analysis_query_policy_max_steps", 12),
        **(
            {
                "research_contract_version": "bioradar-research-input-v1",
                "initial_retrieval_version": BOUNDED_INITIAL_VERSION,
                "pagination_version": PAGINATION_VERSION,
                "pagination_cover_version": PAGINATION_COVER_VERSION,
                "professional_retry_version": "biocheck-contract-repair-v1",
                "biocheck_input_version": "adopted-window-declared-audit-v1",
                "narrative_contract_version": "literal-narrative-observations-v2",
                "narrative_proposal_version": "literal-narrative-proposal-v1",
                "source_intelligence_version": "public-health-source-intelligence-v1",
                "material_impact_version": "dependency-aware-material-update-v1",
                "final_assessment_version": "bioradar-research-final-v1",
                "final_reference_repair_version": "bioradar-synthesis-reference-repair-v1",
                "retrieval_allocation": {
                    "automatic_initial_queries": 3,
                    "explicit_queries": "preserved_within_existing_limits",
                    "qualification": (
                        "初查、目录和新补查计document_search首查总额；续页计工具与步骤，每条最多五页。"
                    ),
                },
                "action_limits": copy.deepcopy(getattr(settings, "analysis_v2_action_limits", {})),
            }
            if kdd
            else {}
        ),
        "provider_status": status,
        "qualification": "取证覆盖决策，不代表科学结论或人工审批。",
    }


def validate_frozen_policy(settings, frozen):
    from bioradar.adapters.query_planner import provider_status

    if frozen.get("effective") == "baseline":
        current = _baseline_source()
        expected = frozen.get("source_sha256")
    elif frozen.get("effective") == "query_planner":
        current = provider_status(settings).get("version")
        expected = frozen.get("source_sha256") or frozen.get("version")
    else:
        raise DomainError("query_policy_invalid", "保存的查询策略不可识别", 409)
    if not current or current != expected:
        raise DomainError("query_policy_version_changed", "查询策略源码与本轮固定版本不同，未重选动作", 409)


def _language_alternatives(fixed):
    policy = fixed.get("query_policy", {})
    return (
        policy.get("effective") == "query_planner"
        and policy.get("schema_version") == "query-planner.external.v2"
        and policy.get("research_contract_version") == "bioradar-research-input-v1"
        and policy.get("initial_retrieval_version") in {INITIAL_RETRIEVAL_VERSION, BOUNDED_INITIAL_VERSION}
    )


def pagination_enabled(fixed):
    return (
        fixed.get("query_policy", {}).get("schema_version") == "query-planner.external.v2"
        and fixed["query_policy"].get("pagination_version") == PAGINATION_VERSION
    )


def question_plan_payload(fixed):
    if _language_alternatives(fixed):
        bounded = fixed["query_policy"]["initial_retrieval_version"] == BOUNDED_INITIAL_VERSION
        return {
            "task": "question_plan",
            "question": fixed["question"],
            "search_scope": fixed["search_scope"],
            "max_queries": min(3 if bounded else 6, fixed["budget"]["max_queries"]),
            "retrieval_strategy_version": fixed["query_policy"]["initial_retrieval_version"],
            "instruction": (
                (
                    "在允许来源内规划最多三项独立字面查询，每项最多三个词，预算更小时按max_queries。"
                    "初查与目录、后续补查和续页共享总额，应保留后续余量。"
                    if bounded
                    else "在允许来源内规划最多六项独立字面查询，每项最多三个词，预算更小时按max_queries。"
                )
                + "不同queries是依次可执行的OR备选；同一query的terms仍须全部字面同时满足（AND）。"
                "根据问题中的实际对象或核心现象，安排原问题语言、英语等义表达及少量常用同义变体。"
                "问题不是英语时，至少安排一个独立英语等义备选；"
                "若对象只有固定专名或缩写且无需翻译，则保留原名并在reason说明。"
                "绝不把不同语言或同义词强行放进同一terms要求同时出现。"
                "优先仅用一个辨识力强的对象或核心现象词，不叠加试验类型、机制等额外限定。"
                "只能翻译或改写问题已有含义，禁止补入问题未给的疾病、地点、年份、病例答案或文档编号。"
                "reason说明原词与等义表达或同义变体的关系，并标明待检索假设、尚非原始事实或证据。"
                "当前没有已读证据，不输出事实、命题或结论；一次字面查询为空不否定其余语言备选。"
                + (
                    "named_objects列出问题实际点名的对象；name必须逐字出现在问题里，"
                    "question_span使用Unicode码点半开区间start/end。不要补入答案、材料编号或推测对象。"
                    if fixed.get("query_policy", {}).get("research_contract_version")
                    == "bioradar-research-input-v1"
                    else ""
                )
            ),
        }
    return {
        "task": "question_plan",
        "question": fixed["question"],
        "search_scope": fixed["search_scope"],
        "max_queries": min(2, fixed["budget"]["max_queries"]),
        "instruction": (
            "仅规划在允许来源内的最多两项字面查询，每项最多三个词。"
            "terms中的每个词是必须同时满足的字面条件（AND），不是任选词或语义扩展。"
            "先用辨识力强且问题中确实出现的研究对象词；对象词放第一项。"
            "首查通常只需一个药物、靶点、疾病或登记编号，不叠加机制、试验类型等修饰词。"
            "当前没有已读证据，不输出事实、命题或结论。"
        ),
    }


def validate_initial_plans(raw, fixed):
    explicit = isinstance(raw, list)
    try:
        plans = (
            [InitialQuery.model_validate(item) for item in raw]
            if explicit
            else QuestionPlan.model_validate({k: v for k, v in raw.items() if k != "named_objects"}).queries
        )
    except ValueError:
        raise DomainError("analysis_question_plan_invalid", "初查规划格式无效，未补造查询", 422) from None
    initial_limit = 6 if explicit or _language_alternatives(fixed) else 2
    if (
        not explicit
        and fixed.get("query_policy", {}).get("initial_retrieval_version") == BOUNDED_INITIAL_VERSION
    ):
        initial_limit = 3
    maximum = min(initial_limit, fixed["budget"]["max_queries"])
    if len(plans) > maximum:
        raise DomainError("analysis_query_budget", "初查规划超过本轮固定查询预算", 422)
    if any(plan.source_id not in fixed["search_scope"] for plan in plans):
        raise DomainError("analysis_query_invalid", "初查规划越过本次授权来源", 422)
    return [plan.model_dump(mode="json") for plan in plans]


def mechanism_query_context(mechanism):
    """Reuse the compact professional result so every gap ID remains dereferenceable."""
    from bioradar.research.investigations.analysis import compact_mechanism

    value = _plain(mechanism) if mechanism else {}
    if not isinstance(value, dict) or not value:
        return {}
    context = compact_mechanism(value) or {}
    for key in ("missing_experiment", "observations", "evidence_map"):
        if key in value:
            context[key] = copy.deepcopy(value[key])
    opposing = copy.deepcopy(value.get("opposing_observations", []))
    for field, identifier in (("claims", "claim_id"), ("joins", "join_id")):
        obligations = value.get(field, [])
        for obligation in obligations.values() if isinstance(obligations, dict) else obligations:
            if isinstance(obligation, dict) and obligation.get("opposing_observation_ids"):
                opposing.append(
                    {
                        identifier: obligation.get("id", obligation.get(identifier)),
                        "text": obligation.get("text"),
                        "opposing_observation_ids": copy.deepcopy(obligation["opposing_observation_ids"]),
                    }
                )
    context.update(
        opposing_observations=opposing,
        qualification=(
            "专业方法提出的缺口及反向观察须进入补查规划；空conflicts不表示没有反证。"
            "claims/joins与evidence保留可解引用编号及原件版本；全文仅在固定原文输入中提供。"
            "缺失实验是待查需要，不是已经完成的研究结果。"
        ),
    )
    return context


def _check(identifier, question, *, reason=None):
    return {
        "id": identifier,
        "question": question,
        "required": True,
        "status": "unknown",
        "evidence_ids": [],
        "reason": reason or "本轮尚未取得明确覆盖判断。",
        "qualification": "机器取证覆盖判断，不是科学结论或人工审批。",
    }


def _pipeline_question(question):
    lower = question.casefold()
    return any(
        word in lower
        for word in (
            "管线",
            "布局",
            "多靶",
            "双靶",
            "pipeline",
            "multi-target",
            "multitarget",
            "multi target",
        )
    )


def _gap_check_key(identifier, question=None, checks=None):
    while identifier.startswith("gap:"):
        identifier = identifier[4:]
    canonical = "gap:" + identifier
    if len(canonical) > 80:
        canonical = canonical[:63] + ":" + _hash(canonical)[:16]
    key, attempt = canonical, 0
    while checks and key in checks and checks[key].get("question") != question:
        suffix = ":" + _hash([canonical, question, attempt])[:16]
        key = canonical[: 80 - len(suffix)] + suffix
        attempt += 1
    return key


_DIRECTION_CHECKS = {
    "public_health": {
        "warning_judgment": "是否预警、立即核实或继续观察及其条件；不以官方是否发文替代判断",
        "independent_confirmation": "来源独立性、机构确认与转载去重",
        "competing_hypotheses": "各竞争假设的支持、反证、未检验条件与区分证据",
        "counterevidence": "反证、否定边界、替代解释与来源限制",
        "count_revision": "人数去重、累计与净增以及更正替代关系",
        "pathogen_uncertainty": "病原未确认、疑似与已排除的区别",
        "transmission_uncertainty": "传播方式和持续人传人证据及未确认范围",
    },
    "business": {
        "orders": "毛订单、净订单、退订及原始口径",
        "corrections": "已知更正、撤回及其明确替代目标",
        "comparable_periods": "同主体、指标、期间、币种与范围的可比性",
        "revenue_recognition": "验收、收入确认、销售额的区别",
        "cash_receipts": "现金、预收款和收入确认的区别",
        "capacity_utilization": "产能利用率的实际口径与程序计算",
        "sample_scope": "已采用样本和不能外推的总体范围",
    },
}
_CLINICAL_CHECKS = {
    "population": "限定人群、队列、入组与实际评价人数",
    "endpoints": "预设终点、PFS与OS及不能外推的获益",
    "safety": "早期和当前安全性、毒性以及不良事件上修",
    "corrections": "同研究的更正、撤回和补充，不能当新研究",
    "regulatory_status": "实际监管状态与批准边界",
    "publication_status": "论文、会议、新闻稿与正式发表状态",
}


def normalize_policy_coverage(reading, declared_checks, evidence):
    """Only model coverage decoration is recoverable; source/action checks stay strict."""
    value = _plain(reading)
    declared = {}
    for check in declared_checks:
        key = check.get("id")
        if not isinstance(key, str) or not key or key in declared:
            raise DomainError("analysis_policy_coverage_invalid", "覆盖检查声明编号无效或重复", 422)
        declared[key] = check
    if not isinstance(value, dict):
        return value, []
    rows = value.get("coverage_checks", [])
    if not isinstance(rows, list):
        return value, []
    issues, reported = [], {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            return value, []
        reported.setdefault(row["id"], []).append(row)
    normalized = []
    for key, updates in reported.items():
        if key not in declared:
            issues.append({"check_id": key, "source": "model_coverage", "status": "unknown"})
            continue
        if len(updates) != 1:
            normalized.append(
                {
                    "id": key,
                    "status": "unknown",
                    "evidence_ids": [],
                    "reason": "模型重复报告同一覆盖编号，状态保留未知。",
                }
            )
            issues.append({"check_id": key, "source": "model_coverage", "status": "unknown"})
        else:
            normalized.append(copy.deepcopy(updates[0]))
    available = {row["key"] for row in evidence}
    for row in normalized:
        refs = row.get("evidence_ids", [])
        if row.get("status") == "passed" and (
            not refs or not isinstance(refs, list) or any(ref not in available for ref in refs)
        ):
            row.update(status="unknown", evidence_ids=[], reason="覆盖声明未绑定本轮已读证据。")
            issues.append({"check_id": row["id"], "source": "model_coverage", "status": "unknown"})
    value["coverage_checks"] = normalized
    for plan in value.get("next_queries", []):
        if not isinstance(plan, dict) or not isinstance(plan.get("coverage_check_ids", []), list):
            continue
        identifiers = plan.get("coverage_check_ids", [])
        if any(not isinstance(key, str) for key in identifiers):
            continue
        valid = []
        for key in identifiers:
            if key not in declared or key in valid:
                issues.append({"check_id": key, "source": "model_query_coverage", "status": "unknown"})
            else:
                valid.append(key)
        if "coverage_check_ids" in plan:
            plan["coverage_check_ids"] = valid
    unique = {(item["source"], item["check_id"]): item for item in issues}
    return value, list(unique.values())


def _coverage_question_text(value):
    """Keep structured provider gaps lossless while exposing a textual check question."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise DomainError(
            "analysis_policy_coverage_invalid", "取证检查问题须为字符串或有限JSON值", 422
        ) from None


def build_coverage_checks(fixed, progress, *, reading=None):
    evidence_ids = {item["key"] for item in progress.get("evidence", [])}
    saved = progress.get("query_policy", {}).get("checks", [])
    external = fixed.get("query_policy", {}).get("effective") == "query_planner"
    checks, aliases = {}, {}
    saved_ids = set()
    for original in saved:
        if original.get("id") in saved_ids:
            raise DomainError("analysis_policy_coverage_invalid", "已声明覆盖检查编号重复", 422)
        saved_ids.add(original.get("id"))
        item = copy.deepcopy(original)
        old_key = item["id"]
        key = (
            _gap_check_key(old_key, item.get("question"), checks)
            if external and old_key.startswith("gap:")
            else old_key
        )
        aliases[old_key] = key
        item["id"] = key
        if external and old_key.startswith("gap:") and key in checks:
            previous = checks[key]
            if previous.get("status") != item.get("status"):
                previous.update(
                    status="unknown", evidence_ids=[], reason="重复命名的覆盖状态不一致，保留未知。"
                )
            elif previous.get("evidence_ids") != item.get("evidence_ids"):
                previous["evidence_ids"] = list(
                    dict.fromkeys(previous.get("evidence_ids", []) + item.get("evidence_ids", []))
                )
        else:
            checks[key] = item
    checks.setdefault("source_materials", _check("source_materials", "取得本轮可解读的授权固定原文"))
    checks.setdefault(
        "answer_scope",
        _check("answer_scope", "当前原文是否足以在明确条件和范围内回答本轮问题：" + fixed["question"]),
    )
    if _pipeline_question(fixed["question"]):
        for key, question in _PIPELINE_CHECKS.items():
            checks.setdefault(key, _check(key, question))
    contract = fixed.get("query_policy", {}).get("research_contract_version")
    if external and contract == "bioradar-research-input-v1":
        direction = fixed.get("query_policy", {}).get("direction", "research")
        extra = dict(_DIRECTION_CHECKS.get(direction, {}))
        if direction in {"research", "pipeline"} and any(
            word in fixed["question"].casefold()
            for word in ("药", "临床", "治疗", "试验", "trial", "therapy", "drug")
        ):
            extra.update(_CLINICAL_CHECKS)
        for key, question in extra.items():
            checks.setdefault(key, _check(key, question))
    value = _plain(reading) if reading else {}
    for gap in value.get("gaps", []):
        question = gap["question"]
        identifier = _gap_check_key(gap["id"], question, checks) if external else "gap:" + gap["id"]
        if identifier not in checks or checks[identifier].get("question") != question:
            checks[identifier] = _check(identifier, question, reason=gap.get("why_it_matters"))
    mechanism = mechanism_query_context(
        progress.get("mechanism_evaluation") or fixed.get("mechanism_evaluation")
    )
    for gap in mechanism.get("critical_gaps", []):
        if not isinstance(gap, dict):
            gap = {"reason": str(gap)}
        question = "；".join(str(gap[key]) for key in ("reason", "missing_experiment") if gap.get(key))
        if question:
            key = "mech:gap:" + _hash(question)[:12]
            checks.setdefault(key, _check(key, question, reason="MechEvidence指出的专业取证缺口。"))
    missing = mechanism.get("missing_experiment")
    for question in missing if isinstance(missing, list) else ([missing] if missing else []):
        question = question if isinstance(question, str) else json.dumps(question, ensure_ascii=False)
        key = "mech:experiment:" + _hash(question)[:12]
        checks.setdefault(key, _check(key, question, reason="MechEvidence指出尚缺的关键实验。"))
    for claim in mechanism.get("opposing_observations", []):
        identifier = claim.get("claim_id") or claim.get("join_id") or _hash(claim)[:12]
        prefix = "join:" if claim.get("join_id") else ""
        key = "mech:opposing:" + prefix + str(identifier)
        question = "补查命题或连接{}的反向观察{}及其原始依据".format(
            identifier, "、".join(claim["opposing_observation_ids"])
        )
        checks.setdefault(key, _check(key, question, reason="反向观察存在；不能用空conflicts排除反证。"))
    quality = progress.get("quality_feedback") or fixed.get("quality_feedback") or {}
    for gap in quality.get("evidence_gaps", []):
        key = (
            "quality:"
            + _hash(
                {field: gap.get(field) for field in ("kind", "unit_id", "claim_index", "span", "reason")}
            )[:16]
        )
        checks.setdefault(
            key,
            _check(
                key,
                gap.get("reason") or gap.get("span") or "BioCheck提出的材料或选段缺口",
                reason="成稿核验或所选片段复核提出的取证缺口；先检查已读原文，再规划必要补查。",
            ),
        )
    value, _ = normalize_policy_coverage(value, list(checks.values()), progress.get("evidence", []))
    seen = set()
    for update in value.get("coverage_checks", []):
        update = _plain(update)
        key = update.get("id")
        if external and key not in checks:
            key = aliases.get(key, key)
        if key not in checks:
            continue
        if key in seen or update.get("status") not in {"passed", "failed", "unknown"}:
            checks[key].update(status="unknown", evidence_ids=[], reason="模型覆盖声明无效，保留未知。")
            continue
        seen.add(key)
        checks[key].update(
            {k: copy.deepcopy(update[k]) for k in ("status", "evidence_ids", "reason") if k in update}
        )
    for item in checks.values():
        refs = item.get("evidence_ids", [])
        valid_refs = isinstance(refs, list) and all(
            isinstance(ref, str) and ref in evidence_ids for ref in refs
        )
        if item.get("status") == "passed" and (not refs or not valid_refs):
            item.update(
                status="unknown",
                evidence_ids=[],
                reason="覆盖判断未绑定本轮有效固定证据，保留未知。",
            )
        elif not valid_refs:
            item["evidence_ids"] = []
        item["qualification"] = "机器取证覆盖判断，不是科学结论或人工审批。"
    if evidence_ids:
        checks["source_materials"].update(
            status="passed",
            evidence_ids=sorted(evidence_ids),
            reason="本轮已取得可解读的固定原文；此项只检查资料取得，不表示问题已回答。",
        )
    else:
        checks["source_materials"].update(
            status="unknown", evidence_ids=[], reason="尚未取得可解读固定原文。"
        )
    if len(checks) > 64:
        raise DomainError("analysis_policy_input_limit", "本轮取证覆盖检查超过策略接口上限", 422)
    for item in checks.values():
        # Also cover saved checks received before structured quality gaps were supported.
        item["question"] = _coverage_question_text(item.get("question"))
    return list(checks.values())


def coverage_instruction():
    return (
        "coverage_checks只判断输入声明的取证覆盖检查，id须沿用coverage_checks中的编号。"
        "passed必须给当前evidence_key组成的evidence_ids，并说明原文怎样覆盖该项；"
        "查询已执行、covers预测、空结果或没有发现冲突都不能视为覆盖。"
        "没有来源或只见候选时设unknown；这是机器取证覆盖判断，不是科学或人工审批。"
        "answer_scope须根据当前原文明确判断：可回答的有限命题可设passed并绑定证据，"
        "同时写清条件与未知范围；不要求消除所有扩展问题。只有取得资料或没有列出gaps仍设unknown。"
        "管线或多靶标布局须分别考虑支持、反证、竞争项目、类似失败、技术和专利。"
        "优先将关键未覆盖项与MechEvidence critical_gaps、missing_experiment、"
        "claims.opposing_observation_ids或opposing_observations转为有依据的next_queries；"
        "空conflicts不代表无反证。每条next_query可用coverage_check_ids标预计覆盖的已声明编号，"
        "预计覆盖不改变检查状态。只能使用已授权来源；无来源须明确unknown，不编造检索工具。"
    )


def _action_arguments(plan, provider, phase, index, fixed):
    return {
        "provider": provider,
        "source_id": plan["source_id"],
        "terms": copy.deepcopy(plan["terms"]),
        "phase": phase,
        "index": index,
        "plan": copy.deepcopy(plan),
        "as_of": fixed.get("evidence_time", {}).get("cutoff", fixed.get("as_of"))
        if fixed.get("query_policy", {}).get("research_contract_version")
        else fixed.get("as_of"),
        "mode": fixed.get("evidence_time", {}).get("mode", "current"),
        **(
            {
                "limit": fixed.get("reading_limits", {}).get("search_limit", 8),
                "is_demo": fixed.get("is_demo", False),
                "scope": {
                    "direction": fixed["query_policy"].get("direction", "research"),
                    "mode": fixed.get("evidence_time", {}).get("mode", "current"),
                    "as_of": fixed.get("evidence_time", {}).get("cutoff", fixed.get("as_of")),
                    "is_demo": fixed.get("is_demo", False),
                },
            }
            if provider == "fabric" and pagination_enabled(fixed)
            else {}
        ),
    }


def _category_covers(plan, checks, reading, *, normalize_gaps=False):
    declared = {check["id"] for check in checks}
    requested = plan.get("coverage_check_ids", [])
    if normalize_gaps:
        requested = [
            _gap_check_key(key) if key not in declared and key.startswith("gap:") else key
            for key in requested
        ]
    if any(key not in declared for key in requested):
        raise DomainError("analysis_policy_coverage_invalid", "追加查询引用未声明的覆盖检查", 422)
    covers = list(requested)
    gap_id = plan.get("gap_id")
    gap_key = "gap:" + gap_id if gap_id else None
    if gap_id and normalize_gaps:
        gap = next((item for item in reading.get("gaps", []) if item["id"] == gap_id), None)
        gap_key = (
            _gap_check_key(gap_id, gap["question"], {check["id"]: check for check in checks})
            if gap is not None
            else _gap_check_key(gap_id)
        )
    if gap_key in declared:
        covers.append(gap_key)
    text = " ".join(str(plan.get(key, "")) for key in ("reason", "expected_discrimination"))
    for gap in reading.get("gaps", []):
        if gap["id"] == gap_id:
            text += " " + gap["question"] + " " + gap.get("why_it_matters", "")
    for key, words in _CATEGORY_WORDS.items():
        if key in declared and any(word in text.casefold() for word in words):
            covers.append(key)
    for check in checks:
        if check["id"].startswith("mech:") and check["question"] in text:
            covers.append(check["id"])
    if not covers:
        covers.append("support" if "support" in declared else "source_materials")
    covers.append("answer_scope")
    return list(dict.fromkeys(covers))


def _candidate(plan, provider, phase, index, fixed, checks, reading):
    arguments = _action_arguments(plan, provider, phase, index, fixed)
    hypothesis = _language_alternatives(fixed) and phase == "initial"
    if hypothesis:
        arguments["plan"]["reason"] = "待检索假设（非原始事实或证据）：" + plan["reason"]
    key = semantic_key("document_search", arguments)
    return {
        "id": f"{phase}-{provider}-{index}-" + key[:16],
        "kind": "document_search",
        "arguments": arguments,
        **(
            {
                "reason": arguments["plan"]["reason"],
                "applicability": {
                    "status": "eligible",
                    "scope_match": 1,
                    "reason": "固定问题规划的独立字面备选，执行前仍核当前来源许可。",
                },
            }
            if hypothesis
            else {}
        ),
        "covers": _category_covers(
            plan,
            checks,
            reading,
            normalize_gaps=fixed.get("query_policy", {}).get("effective") == "query_planner",
        ),
        "estimated_cost": {
            "tool_calls": 1,
            "tokens": 0 if provider == "fabric" else None,
            "relation_access": 0,
        },
    }


def _receipt_matches(query, candidate):
    arguments = candidate["arguments"]
    return (
        query.get("kind", "document_search") == candidate["kind"]
        and query.get("provider", "fabric") == arguments["provider"]
        and query.get("phase", "followup") == arguments["phase"]
        and query["index"] == arguments["index"]
    )


def _indices(plans, phase, queries, fixed):
    if phase == "initial":
        return list(range(len(plans)))
    previous = [
        query
        for query in queries
        if query.get("kind", "document_search") == "document_search"
        and query.get("phase", "followup") == phase
        and query.get("provider", "fabric") == "fabric"
    ]
    next_index = max((query["index"] for query in previous), default=-1) + 1
    result = []
    for plan in plans:
        key = semantic_key("document_search", _action_arguments(plan, "fabric", phase, 0, fixed))
        matched = next(
            (
                query["index"]
                for query in previous
                if semantic_key(
                    "document_search",
                    _action_arguments(query["plan"], "fabric", phase, query["index"], fixed),
                )
                == key
            ),
            None,
        )
        result.append(next_index if matched is None else matched)
        if matched is None:
            next_index += 1
    return result


def _relaxation_candidate(fixed, progress, plans, phase, checks, reading, originals):
    from bioradar.research.investigations.analysis import relaxed_query_terms

    saved = progress.get("query_relaxation") or progress.get("initial_relaxation")
    if saved:
        if saved.get("phase", "initial") != phase:
            return None
        plan = copy.deepcopy(saved["plan"])
        candidate = _candidate(plan, "fabric", phase, saved["query_index"], fixed, checks, reading)
        candidate["arguments"].update(
            relaxed_from=copy.deepcopy(saved["original_plan"]), relaxation_reason=saved["reason"]
        )
        candidate["relaxation"] = copy.deepcopy(saved)
        return candidate
    for original in originals:
        receipt = next(
            (query for query in progress.get("queries", []) if _receipt_matches(query, original)), None
        )
        plan = original["arguments"]["plan"]
        terms = relaxed_query_terms(fixed["question"], plan["terms"])
        if receipt is None or receipt["status"] != "empty" or terms is None:
            continue
        relaxed = copy.deepcopy(plan)
        reason = (
            "原查询的字面条件须同时满足且结果为空；在同一授权来源仅减词补查一次，"
            "优先保留问题中出现的原对象词。原terms：" + json.dumps(plan["terms"], ensure_ascii=False)
        )
        relaxed.update(terms=terms, reason=reason)
        index = max((item["arguments"]["index"] for item in originals), default=-1) + 1
        candidate = _candidate(relaxed, "fabric", phase, index, fixed, checks, reading)
        key = semantic_key(candidate["kind"], candidate["arguments"])
        if any(
            query.get("kind", "document_search") == "document_search"
            and query.get("provider", "fabric") == "fabric"
            and semantic_key(
                "document_search",
                _action_arguments(
                    query["plan"], "fabric", query.get("phase", "followup"), query["index"], fixed
                ),
            )
            == key
            for query in progress.get("queries", [])
        ):
            continue
        candidate["arguments"].update(relaxed_from=copy.deepcopy(plan), relaxation_reason=reason)
        candidate["relaxation"] = {
            "phase": phase,
            "query_index": index,
            "original_plan": copy.deepcopy(plan),
            "plan": relaxed,
            "reason": reason,
        }
        return candidate
    return None


def _selected_action(entry):
    if not isinstance(entry, dict):
        return None
    if entry.get("kind") and isinstance(entry.get("arguments"), dict):
        return entry
    if isinstance(entry.get("action"), dict):
        return entry["action"]
    for name in ("output", "result", "decision"):
        value = entry.get(name)
        if isinstance(value, dict) and isinstance(value.get("action"), dict):
            return value["action"]
    return None


def _history(fixed, progress):
    state = progress.get("query_policy", {})
    selected = [_selected_action(entry) for entry in state.get("decisions", [])]
    by_id = {action["id"]: action for action in selected if action}
    current = {item["key"] for item in progress.get("evidence", [])}
    history = []
    for query in progress.get("queries", []):
        provider, phase = query.get("provider", "fabric"), query.get("phase", "followup")
        aid = query.get("action_id") or f"legacy-{phase}-{provider}-{query['index']}"
        action = by_id.get(aid)
        arguments = (
            copy.deepcopy(action["arguments"])
            if action
            else _action_arguments(query["plan"], provider, phase, query["index"], fixed)
        )
        status = query["status"]
        if status in {"read", "empty"}:
            status = "completed"
        elif status == "unavailable":
            status = "failed"
        usage = query.get("usage") or {}
        if usage.get("request_status") == "not_sent":
            status = "not_sent"
        actual = query.get("cost") or query.get("actual_cost") or usage.get("actual_cost")
        if actual is None:
            actual = {key: 0 if status == "not_sent" else None for key in _DIMENSIONS}
        history.append(
            {
                "action_id": aid,
                "kind": action["kind"] if action else query.get("kind", "document_search"),
                "arguments": arguments,
                "status": status,
                "new_evidence_ids": [key for key in query.get("evidence_keys", []) if key in current],
                "actual_cost": copy.deepcopy(actual),
                **(
                    {"pagination": copy.deepcopy(query["pagination"])}
                    if query.get("pagination") is not None
                    else {}
                ),
            }
        )
    pending = _selected_action(state.get("pending_action"))
    if pending:
        previous = next((entry for entry in history if entry["action_id"] == pending["id"]), None)
        if previous is None:
            history.append(
                {
                    "action_id": pending["id"],
                    "kind": pending["kind"],
                    "arguments": copy.deepcopy(pending["arguments"]),
                    "status": "prepared",
                    "new_evidence_ids": [],
                    "actual_cost": {key: None for key in _DIMENSIONS},
                }
            )
        elif previous["status"] not in _PENDING:
            previous["status"] = "prepared"
    if len(history) > 256:
        raise DomainError("analysis_policy_input_limit", "本轮动作历史超过策略接口上限", 422)
    return history


def search_query_count(fixed, progress):
    """KDD first-page searches have their own limit; every page still costs tools/steps."""
    queries = progress.get("queries", [])
    if fixed.get("query_policy", {}).get("schema_version") != "query-planner.external.v2":
        return len(queries)
    actions = {
        action["id"]: action
        for entry in progress.get("query_policy", {}).get("decisions", [])
        if (action := _selected_action(entry))
    }
    return sum(
        query.get("kind", "document_search") == "document_search"
        and not actions.get(query.get("action_id"), {}).get("arguments", {}).get("cursor")
        and not (query.get("usage") or {}).get("search_cursor")
        and (query.get("pagination") or {}).get("page_number", 1) == 1
        for query in queries
    )


def _policy_budget(fixed, progress):
    supplied = progress.get("query_policy", {}).get("budget")
    if supplied:
        limits = {key: value for key, value in supplied["limits"].items() if key in _DIMENSIONS}
        remaining = {key: supplied["remaining"].get(key) for key in limits}
    else:
        maximum = fixed["budget"].get("max_tool_calls", fixed["budget"]["max_queries"])
        limits = {"tool_calls": maximum}
        remaining = {"tool_calls": max(0, maximum - len(progress.get("queries", [])))}
    if any(
        type(limits[key]) is not int
        or type(remaining[key]) is not int
        or not 0 <= remaining[key] <= limits[key] <= 10_000_000
        for key in limits
    ):
        raise DomainError(
            "analysis_policy_budget_unknown", "策略剩余预算尚未确认，未按零费用或无限额处理", 409
        )
    return {"limits": limits, "remaining": remaining}


def build_read_manifest(fixed, progress, *, fixed_version_refs=(), checks=None):
    """Only fixed/adopted originals and actual successful query receipts may name a read."""
    if fixed.get("query_policy", {}).get("research_contract_version") != "bioradar-research-input-v1":
        return []
    scope = set(fixed.get("search_scope", [])) | {
        item["source_id"] for item in fixed.get("dependency_sources", []) if item.get("history")
    }
    known, metadata, read, windows = {}, {}, set(), {}

    def bind(value):
        value = _plain(value)
        if not isinstance(value, dict):
            return None
        if "is_demo" in value and value["is_demo"] is not fixed.get("is_demo", False):
            return None
        ref = value.get("record_ref", value)
        if isinstance(ref, dict) and isinstance(ref.get("object_id"), str):
            try:
                native = json.loads(ref["object_id"])
            except (ValueError, TypeError):
                return None
            if (
                not isinstance(native, list)
                or len(native) != 3
                or native[2] is not fixed.get("is_demo", False)
            ):
                return None
            sid, rid, version = native[0], native[1], ref.get("adopted_version")
        else:
            sid = value.get("source_id")
            rid = value.get("source_record_id", value.get("record_id"))
            version = value.get("version_id", value.get("version"))
        if sid not in scope or not all(isinstance(x, str) and x for x in (sid, rid, version)):
            return None
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            return None
        return sid, rid, version

    def adopt(value):
        key = bind(value)
        if key is not None:
            known[key] = {
                "source_id": key[0],
                "source_record_id": key[1],
                "version_id": key[2],
                "status": "unread",
                "priority": "normal",
                "reason": "scope",
                "covers": ["answer_scope"],
            }
            metadata.setdefault(key, []).append(_plain(value))
        return key

    fixed_members = (
        list(fixed_version_refs)
        + progress.get("kdd_source_version_refs", [])
        + progress.get("kdd_source_catalog", [])
        + fixed.get("evidence", [])
    )
    for value in fixed_members:
        adopt(value)
    for value in progress.get("evidence", []):
        key = adopt(value)
        if key is not None:
            read.add(key)
            if (
                type(value.get("end")) is int
                and type(value.get("source_length")) is int
                and 0 <= value["end"] <= value["source_length"]
            ):
                old = windows.get(key, (0, value["source_length"]))
                windows[key] = (max(old[0], value["end"]), value["source_length"])
    for query in progress.get("queries", []):
        if query.get("status") == "read":
            for value in query.get("records", []):
                adopt(value)
    declared = {
        x["id"]
        for x in (checks if checks is not None else progress.get("query_policy", {}).get("checks", []))
    }
    declared.add("answer_scope")

    def critical(key, reason, covers=(), *, basis=None):
        if key in known:
            if basis is None:
                return
            known[key].update(priority="critical", reason=reason, basis=copy.deepcopy(basis))
            known[key]["covers"] = list(
                dict.fromkeys([x for x in covers if x in declared] + known[key]["covers"])
            )

    for key, items in metadata.items():
        for item in items:
            context = item.get("signal_context") or {}
            relations = context.get("source_relations", [])
            for relation in relations:
                if relation.get("kind") in {"correction", "retraction"}:
                    basis = {"kind": "visible_source_relation", "reference_ids": [key[1]]}
                    critical(
                        key,
                        "correction",
                        ["corrections", "count_revision", "counterevidence"],
                        basis=basis,
                    )
                    target = bind(relation.get("target_ref", {}))
                    critical(
                        target,
                        "correction",
                        ["corrections", "count_revision", "counterevidence"],
                        basis=basis,
                    )
            facts = item.get("facts") or {}
            document = (
                item.get("source_context")
                or item.get("document_evidence")
                or facts.get("document_evidence")
                or context.get("document_evidence")
                or {}
            )
            for relation in document.get("document_relations", []):
                if relation.get("relation_type") not in {"corrects", "retracts"}:
                    continue
                basis = {"kind": "visible_source_relation", "reference_ids": [key[1]]}
                critical(key, "correction", ["corrections", "count_revision", "counterevidence"], basis=basis)
                target = bind(
                    {
                        "source_id": relation.get("target_source_id"),
                        "source_record_id": relation.get("target_source_record_id"),
                        "version_id": relation.get("target_version_id"),
                    }
                )
                critical(
                    target,
                    "correction",
                    ["corrections", "count_revision", "counterevidence"],
                    basis=basis,
                )
            if set(item.get("record_flags", [])) & {"retracted", "corrected"}:
                critical(
                    key,
                    "counterevidence",
                    ["counterevidence", "corrections"],
                    basis={"kind": "visible_source_relation", "reference_ids": [key[1]]},
                )
    quality = progress.get("quality_feedback") or {}
    requests = list(quality.get("read_requests") or [])
    for gap in quality.get("evidence_gaps", []):
        requests.extend(gap.get("read_requests") or [])
    for request in requests:
        if not isinstance(request, dict):
            continue
        matches = [
            key
            for key in known
            if key[1] == request.get("record_id")
            and key[2] == request.get("version")
            and (not request.get("source_id") or key[0] == request["source_id"])
        ]
        if len(matches) == 1:
            quality_checks = [x for x in declared if x.startswith("quality:")]
            if quality_checks:
                critical(
                    matches[0],
                    "counterevidence",
                    quality_checks,
                    basis={"kind": "observed_gap", "reference_ids": quality_checks},
                )
    for key in read:
        known[key]["status"] = "read"
        if key in windows:
            end, length = windows[key]
            if end < length:
                known[key].update(status="unread", window_start=end)
    return sorted(
        known.values(),
        key=lambda x: (x["priority"] != "critical", x["source_id"], x["source_record_id"], x["version_id"]),
    )


def document_read_candidates(fixed, progress, manifest, checks):
    """A one-record fixed read has the same legality and retry identity as its receipt."""
    if fixed.get("query_policy", {}).get("research_contract_version") != "bioradar-research-input-v1":
        return []
    declared = {item["id"] for item in checks}
    result = []
    for item in manifest:
        if item["status"] != "unread":
            continue
        args = {
            "provider": "fabric",
            "source_id": item["source_id"],
            "records": [
                {
                    "source_record_id": item["source_record_id"],
                    "version_id": item["version_id"],
                    **({"window_start": item["window_start"]} if "window_start" in item else {}),
                }
            ],
            "scope": {
                "direction": fixed["query_policy"].get("direction", "research"),
                "as_of": fixed.get("evidence_time", {}).get("cutoff", fixed.get("as_of")),
                "mode": fixed.get("evidence_time", {}).get("mode", "current"),
                "is_demo": fixed.get("is_demo", False),
            },
        }
        result.append(
            {
                "id": "read:" + semantic_key("document_read", args)[:32],
                "kind": "document_read",
                "arguments": args,
                "covers": [key for key in item["covers"] if key in declared],
                "reason": item["reason"],
                "applicability": {
                    "status": "eligible",
                    "scope_match": 1,
                    "reason": "原件版本及Unicode窗口来自本轮已采用manifest；执行前仍核当前许可。",
                },
                "estimated_cost": {"tool_calls": 1, "tokens": 0, "relation_access": 0},
            }
        )
    return result


def _pagination_covers(fixed, progress, previous, checks):
    """A continuation retains only still-unmet checks of its recorded parent action."""
    declared = {item["id"]: item for item in checks}
    covers = [key for key in ("source_materials", "answer_scope") if key in declared]
    if fixed.get("query_policy", {}).get("pagination_cover_version") != PAGINATION_COVER_VERSION:
        return covers
    entries = [
        entry
        for entry in progress.get("query_policy", {}).get("decisions", [])
        if (_selected_action(entry) or {}).get("id") == previous["action_id"]
    ]
    queries = [q for q in progress.get("queries", []) if q.get("action_id") == previous["action_id"]]
    if len(entries) != 1 or len(queries) != 1:
        return covers
    parent = _selected_action(entries[0])
    query, args = queries[0], parent["arguments"]
    if (
        parent["kind"] != "document_search"
        or not _receipt_matches(query, parent)
        or any(query.get("plan", {}).get(key) != args.get(key) for key in ("source_id", "terms"))
        or semantic_key(parent["kind"], args) != semantic_key(previous["kind"], previous["arguments"])
    ):
        return covers
    original_checks = {item["id"] for item in (entries[0].get("policy_request") or {}).get("checks", [])}
    for key in parent.get("covers", []):
        if (
            key in original_checks
            and key in declared
            and declared[key]["status"] != "passed"
            and key not in covers
        ):
            covers.append(key)
    return covers


def pagination_candidates(fixed, progress, checks):
    """Offer only the exact next page returned by a completed, fixed Fabric query."""
    if not pagination_enabled(fixed):
        return []
    history = _history(fixed, progress)
    if any(entry["status"] in _PENDING for entry in history):
        return []
    attempted = {semantic_key(entry["kind"], entry["arguments"]) for entry in history}
    candidates = []
    for previous in history:
        page, args = previous.get("pagination") or {}, previous["arguments"]
        if (
            previous["kind"] != "document_search"
            or previous["status"] != "completed"
            or args.get("provider") != "fabric"
            or not page.get("has_more")
            or not isinstance(page.get("next_cursor"), str)
            or not page["next_cursor"]
            or type(page.get("max_seq")) is not int
            or page["max_seq"] < 0
            or type(page.get("page_number")) is not int
            or not 1 <= page["page_number"] < 5
            or args.get("source_id") not in fixed["search_scope"]
        ):
            continue
        original = args.get("plan") or {
            "source_id": args["source_id"],
            "terms": args["terms"],
            "reason": "读取实际后续页",
        }
        next_args = _action_arguments(
            original,
            "fabric",
            args.get("phase", "followup"),
            len(history) + len(candidates),
            fixed,
        )
        # A frozen page is tied to the exact original query, page size and source scope.
        for key in ("provider", "source_id", "terms", "limit", "mode", "as_of", "is_demo", "scope"):
            if key not in args or args[key] != next_args[key]:
                raise DomainError("analysis_query_invalid", "已存分页回执与固定查询范围不符", 422)
        next_args.update(
            cursor=page["next_cursor"],
            previous_action_id=previous["action_id"],
            page_number=page["page_number"] + 1,
            max_seq=page["max_seq"],
        )
        if args.get("catalog_discovery") is True:
            next_args["catalog_discovery"] = True
        identity = semantic_key("document_search", next_args)
        if identity in attempted:
            continue
        reason = "上一页真实回执仍有后续资料；按同一来源、查询、时点和页大小继续一页。"
        candidates.append(
            {
                "id": "page:" + identity[:32],
                "kind": "document_search",
                "arguments": next_args,
                "covers": _pagination_covers(fixed, progress, previous, checks),
                "reason": reason,
                "applicability": {"status": "eligible", "scope_match": 1, "reason": reason},
                "estimated_cost": {"tool_calls": 1, "tokens": 0, "relation_access": 0},
            }
        )
    return candidates


def catalog_discovery_candidates(fixed, progress, plans, checks):
    """One bounded source directory page only after its actual literal alternatives are empty."""
    if not _language_alternatives(fixed) or progress.get("evidence"):
        return []
    history = _history(fixed, progress)
    if any(entry["status"] in _PENDING for entry in history):
        return []
    initial = [_plain(plan) for plan in plans]
    by_source = {}
    for plan in initial:
        if plan["source_id"] not in fixed["search_scope"]:
            raise DomainError("analysis_query_invalid", "目录发现越过本轮授权来源", 422)
        key = semantic_key("document_search", _action_arguments(plan, "fabric", "initial", 0, fixed))
        by_source.setdefault(plan["source_id"], set()).add(key)
    observed_empty, attempted_directory = set(), set()
    for query, entry in zip(progress.get("queries", []), history, strict=False):
        args = entry["arguments"]
        if entry["kind"] != "document_search" or args.get("provider") != "fabric":
            continue
        if args.get("catalog_discovery") is True:
            attempted_directory.add(args.get("source_id"))
            continue
        cost = (query.get("cost") or {}).get("tool_calls")
        request_status = (query.get("usage") or {}).get("request_status")
        if (
            query.get("status") == "empty"
            and type(query.get("returned")) is int
            and query["returned"] == 0
            and type(cost) is int
            and cost >= 1
            and request_status not in {"not_sent", "unknown", "unconfirmed"}
        ):
            observed_empty.add(semantic_key("document_search", args))
    declared = {item["id"] for item in checks}
    limit = fixed.get("reading_limits", {}).get("search_limit", 8)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise DomainError("analysis_query_invalid", "目录发现页大小不在固定读取范围内", 422)
    result = []
    for offset, (source_id, required) in enumerate(sorted(by_source.items())):
        if source_id in attempted_directory or not required.issubset(observed_empty):
            continue
        reason = "该来源的独立语言或同义备选均实际返回空，且本轮尚无已读证据；" + (
            "先读取同一授权来源、同一时点的一页固定原件；真实后续页仍逐页占用查询额度，所得候选仍非证据。"
            if pagination_enabled(fixed)
            else "仅发现同一授权来源、同一时点的一页固定原件，不翻页、不搜全网，所得候选仍非证据。"
        )
        plan = {"source_id": source_id, "terms": [], "reason": reason}
        args = _action_arguments(plan, "fabric", "initial", len(initial) + 1 + offset, fixed)
        args.update(
            catalog_discovery=True,
            limit=limit,
            is_demo=fixed.get("is_demo", False),
            scope={
                "direction": fixed["query_policy"].get("direction", "research"),
                "as_of": fixed.get("evidence_time", {}).get("cutoff", fixed.get("as_of")),
                "mode": fixed.get("evidence_time", {}).get("mode", "current"),
                "is_demo": fixed.get("is_demo", False),
            },
        )
        result.append(
            {
                "id": "catalog:" + semantic_key("document_search", args)[:32],
                "kind": "document_search",
                "arguments": args,
                "covers": [key for key in ("source_materials", "answer_scope") if key in declared],
                "reason": reason,
                "applicability": {
                    "status": "eligible",
                    "scope_match": 1,
                    "reason": "仅本来源已有全部字面空回执；发送前仍核当前许可与固定时点。",
                },
                "estimated_cost": {"tool_calls": 1, "tokens": 0, "relation_access": 0},
            }
        )
    return result


def question_named_objects(fixed, raw):
    """Accept only exact spans of the user's question, never inferred answer objects."""
    result, seen = [], set()
    for item in raw:
        span = item.get("question_span", {})
        start, end = span.get("start"), span.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(fixed["question"])
            or item.get("name") != fixed["question"][start:end]
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or item["id"] in seen
        ):
            raise DomainError("analysis_question_object_invalid", "点名对象未绑定原问题的准确位置", 422)
        seen.add(item["id"])
        result.append({key: copy.deepcopy(item[key]) for key in ("id", "name", "question_span")})
    return result


def _object_occurs(name, text):
    left = r"(?<![A-Za-z0-9_-])" if name and name[0].isascii() and name[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_-])" if name and name[-1].isascii() and name[-1].isalnum() else ""
    return bool(name and re.search(left + re.escape(name) + right, text, re.IGNORECASE))


def named_object_context(fixed, progress, candidates, checks):
    objects = question_named_objects(fixed, progress.get("query_policy", {}).get("named_objects", []))
    history = _history(fixed, progress)
    attempted = {semantic_key(item["kind"], item["arguments"]) for item in history}
    output = []
    for item in objects:
        refs = [
            e["key"] for e in progress.get("evidence", []) if _object_occurs(item["name"], e.get("text", ""))
        ]
        ids = []
        if not refs:
            for source in fixed["search_scope"]:
                plan = {
                    "source_id": source,
                    "terms": [item["name"]],
                    "reason": "补查问题点名但已读正文尚未覆盖的对象",
                }
                candidate = _candidate(plan, "fabric", "followup", len(history), fixed, checks, {})
                identity = semantic_key(candidate["kind"], candidate["arguments"])
                if identity not in attempted and not any(c["id"] == candidate["id"] for c in candidates):
                    candidates.append(candidate)
            for candidate in candidates:
                args = candidate["arguments"]
                terms = args.get("terms", [])
                query = args.get("parameters", {}).get("query", "")
                if any(item["name"].casefold() in term.casefold() for term in terms) or (
                    query and item["name"].casefold() in query.casefold()
                ):
                    ids.append(candidate["id"])
        output.append(
            {
                **item,
                "status": "read" if refs else "unread" if ids else "unavailable",
                "evidence_ids": refs,
                "candidate_ids": ids,
                **(
                    {"reason": "本轮已尝试的查询未取得该对象正文，且没有可执行候选。"}
                    if not refs and not ids
                    else {}
                ),
            }
        )
    return output


def build_policy_context(
    fixed, progress, *, phase, plans, reading=None, provider_status=None, graph_candidates=None
):
    """Build observable input only; no network, model, wall clock, or progress mutation."""
    state = progress.get("query_policy", {})
    policy = fixed.get("query_policy", {}).get("effective", "baseline")
    value = _plain(reading) if reading else {}
    plans = [_plain(plan) for plan in plans]
    checks = build_coverage_checks(fixed, progress, reading=value)
    coverage_issues = []
    if policy == "query_planner":
        value, coverage_issues = normalize_policy_coverage(value, checks, progress.get("evidence", []))
        normalized_plans, plan_issues = normalize_policy_coverage(
            {"next_queries": plans}, checks, progress.get("evidence", [])
        )
        plans = normalized_plans["next_queries"]
        coverage_issues = list(
            {(item["source"], item["check_id"]): item for item in coverage_issues + plan_issues}.values()
        )
    queries = progress.get("queries", [])
    history = _history(fixed, progress)
    stages = [(phase, plans)]
    if policy == "query_planner" and phase != "initial":
        initial = state.get("initial_plans") or fixed.get("retrieval", {}).get("initial_queries", [])
        if initial:
            stages.insert(0, ("initial", [_plain(plan) for plan in initial]))
    reading_manifest = build_read_manifest(fixed, progress, checks=checks)
    candidates = document_read_candidates(fixed, progress, reading_manifest, checks)
    for stage, items in stages:
        originals = [
            _candidate(plan, "fabric", stage, index, fixed, checks, value)
            for plan, index in zip(items, _indices(items, stage, queries, fixed), strict=True)
        ]
        if any(item["arguments"]["source_id"] not in fixed["search_scope"] for item in originals):
            raise DomainError("analysis_query_invalid", "候选查询越过本轮授权来源", 422)
        unexecuted = [
            item for item in originals if not any(_receipt_matches(query, item) for query in queries)
        ]
        candidates.extend(unexecuted)
        if policy == "query_planner" or not unexecuted:
            relaxed = _relaxation_candidate(fixed, progress, items, stage, checks, value, originals)
            if relaxed and not any(_receipt_matches(query, relaxed) for query in queries):
                candidates.append(relaxed)
    initial = next((items for stage, items in stages if stage == "initial"), [])
    candidates.extend(catalog_discovery_candidates(fixed, progress, initial, checks))
    candidates.extend(pagination_candidates(fixed, progress, checks))
    status = provider_status or fixed.get("providers", {})
    weknora = status.get("weknora", {})
    available = weknora.get("available", False) if isinstance(weknora, dict) else bool(weknora)
    mode = fixed.get("evidence_time", {}).get("mode", "current")
    if fixed.get("is_demo"):
        available = bool(
            policy == "query_planner"
            and fixed.get("query_policy", {}).get("research_contract_version") == "bioradar-research-input-v1"
            and isinstance(weknora, dict)
            and weknora.get("is_demo") is True
            and weknora.get("knowledge_base_id")
            and weknora.get("source_scope")
            and available
        )
    if (
        plans
        and available
        and mode == "current"
        and not any(query.get("provider") == "weknora" for query in queries)
        and len({_hash(item["record_ref"]) for item in progress.get("evidence", [])})
        < fixed.get("reading_limits", {}).get("max_versions", 10)
        and (policy == "query_planner" or not candidates)
    ):
        candidates.append(_candidate(plans[0], "weknora", phase, 0, fixed, checks, value))
    named = []
    if fixed.get("query_policy", {}).get("schema_version") == "query-planner.external.v2":
        named = named_object_context(fixed, progress, candidates, checks)
    if policy == "query_planner":
        for candidate in graph_candidates or []:
            item = _plain(candidate)
            if item.get("kind") not in _GRAPH_KINDS or not isinstance(item.get("arguments"), dict):
                raise DomainError("query_policy_action_invalid", "图查询候选必须来自可用的固定图工具", 422)
            if not set(item.get("covers", [])).issubset({check["id"] for check in checks}):
                raise DomainError("analysis_policy_coverage_invalid", "图查询引用未声明的覆盖检查", 422)
            candidates.append(item)
        attempted = {semantic_key(entry["kind"], entry["arguments"]) for entry in history}
        seen, unique = set(), []
        for item in candidates:
            key = semantic_key(item["kind"], item["arguments"])
            if key not in attempted and key not in seen:
                unique.append(item)
            seen.add(key)
        candidates = unique
    if len(candidates) > 128:
        raise DomainError("analysis_policy_input_limit", "本轮候选超过策略接口上限", 422)
    return {
        "schema_version": SCHEMA_VERSION,
        "question": fixed["question"],
        **({"named_objects": named} if named else {}),
        "checks": checks,
        "evidence": [
            {
                "id": item["key"],
                "source_id": item["source_id"],
                "record_ref": copy.deepcopy(item["record_ref"]),
                "history_binding": copy.deepcopy(item.get("history_binding")),
                "title": item.get("title"),
            }
            for item in progress.get("evidence", [])
        ],
        "history": history,
        "candidates": candidates,
        "budget": _policy_budget(fixed, progress),
        "max_steps": fixed.get("query_policy", {}).get("max_steps", 12),
        **(
            {"action_limits": copy.deepcopy(fixed["query_policy"]["action_limits"])}
            if "action_limits" in fixed.get("query_policy", {})
            else {}
        ),
        **({"reading_manifest": reading_manifest} if reading_manifest else {}),
        "host_budget": copy.deepcopy(state.get("budget")),
        "phase": phase,
        "mechanism_context": mechanism_query_context(
            progress.get("mechanism_evaluation") or fixed.get("mechanism_evaluation")
        ),
        "quality_feedback": copy.deepcopy(progress.get("quality_feedback") or fixed.get("quality_feedback")),
        "graph_context": copy.deepcopy(state.get("graph_context", progress.get("graph_context", []))),
        "graph_history": copy.deepcopy(state.get("graph_history", progress.get("graph_history", []))),
        **(
            {
                "coverage_issues": coverage_issues,
                "warnings": [
                    "模型覆盖检查编号未知或重复，保留未知：" + item["check_id"] for item in coverage_issues
                ],
            }
            if coverage_issues
            else {}
        ),
        "coverage_qualification": "取证覆盖与预计查询方向，不代表科学结论或人工审批。",
    }


def baseline_decide(context):
    """Preserve the bounded old plan order, one receipt and decision at a time."""
    history = context["history"]
    pending = [entry["action_id"] for entry in history if entry["status"] in _PENDING]
    decision, action, reason = "insufficient", None, "no_legal_action"
    excluded = []
    if pending:
        reason = "pending_execution_or_reconciliation"
    else:
        for candidate in context["candidates"]:
            if candidate["kind"] != "document_search":
                continue
            costs = candidate["estimated_cost"]
            if any(
                type(costs.get(dimension)) is not int or costs[dimension] > remaining
                for dimension, remaining in context["budget"]["remaining"].items()
            ):
                excluded.append({"id": candidate["id"], "reason": "cost_unknown_or_budget_exhausted"})
                continue
            decision, action, reason = "continue", copy.deepcopy(candidate), "legacy_sequence"
            break
        if action is None:
            checks = [item for item in context["checks"] if item.get("required", True)]
            if checks and all(item["status"] == "passed" and item.get("evidence_ids") for item in checks):
                decision, reason = "complete", "retrieval_checks_satisfied"
            elif excluded:
                reason = "budget_exhausted_or_unknown_cost"
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "action": action,
        "reason": reason,
        "policy": {
            "version": BASELINE_VERSION,
            "selector": "legacy_sequence",
            "stop": "legacy_bounded_sequence",
            "training_status": "llm_rules_baseline",
            "legacy_sequence": True,
            "scope": "retrieval_coverage_only_not_scientific_approval",
        },
        "state_summary": {
            "checks": copy.deepcopy(context["checks"]),
            "evidence_count": len(context["evidence"]),
            "step": len(history),
            "remaining": copy.deepcopy(context["budget"]["remaining"]),
            "pending_action_ids": pending,
            "excluded_candidates": excluded,
        },
    }


def decide(context, *, policy, planner=None):
    if policy == "baseline":
        return baseline_decide(context)
    if policy != "query_planner":
        raise DomainError("query_policy_invalid", "查询策略不符合本轮固定配置", 422)
    if planner is None:
        raise DomainError("query_planner_not_available", "本轮选择的查询方法尚不可用", 422)
    return planner(copy.deepcopy(context))
