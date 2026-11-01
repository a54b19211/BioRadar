"""Use installed BioCheck methods with the caller's durable model callback."""

import ast
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from types import ModuleType

from bioradar.adapters.reasoning_model import ModelCallError
from bioradar.api.errors import DomainError

_PROVIDER_FILES = (
    "source_context.py",
    "evidence.py",
    "contracts.py",
    "prompts.py",
    "core.py",
    "claims.py",
    "rpc.py",
)
_VERSION_ONLY_FILES = ("analysis.py", "service.py")
_METHODS = ("generate", "verify", "revise")
_STAGES = {
    "plan_bound": "biocheck_plan",
    "plan_plain": "biocheck_plan",
    "replan_bound": "biocheck_plan_repair",
    "replan_plain": "biocheck_plan_repair",
    "write_bound": "biocheck_write",
    "write_plain": "biocheck_write",
    "write_direct": "biocheck_write",
    "revise": "biocheck_revise",
    "revise_claims": "biocheck_revise_claims",
    "support_audit": "biocheck_support_audit",
}
_CITATION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*")


def _sources(settings):
    directory = settings.biocheck_project_dir
    if directory is None:
        return None
    root = Path(directory) / "app"
    try:
        # Execute pure modules in dependency order. The native RPC provider hash
        # also depends on optional analysis/service sources, which are hash-only.
        names = [
            name
            for name in _PROVIDER_FILES
            if name in {"evidence.py", "prompts.py", "core.py"} or (root / name).is_file()
        ]
        if "rpc.py" in names:
            names.extend(name for name in _VERSION_ONLY_FILES if (root / name).is_file())
        sources = {name: (root / name).read_text(encoding="utf-8") for name in names}
        methods = {
            node.name for node in ast.parse(sources["core.py"]).body if isinstance(node, ast.FunctionDef)
        }
        if not set(_METHODS).issubset(methods):
            return None
        for source in sources.values():
            ast.parse(source)
    except (OSError, UnicodeError, SyntaxError):
        return None
    version = hashlib.sha256("".join(name + "\0" + sources[name] for name in sources).encode()).hexdigest()
    return sources, version


def provider_status(settings):
    available = _sources(settings)
    return {
        "provider": "biocheck",
        "configured": settings.biocheck_project_dir is not None,
        "available": available is not None,
        "status": "ready" if available else "unavailable",
        "version": available[1] if available else None,
        "error_code": None if available else "biocheck_not_available",
        "qualification": "方法源码可读取；不表示本轮已执行或核验通过。",
    }


def load_biocheck(settings):
    """Return one in-memory method snapshot and its actual content version."""
    source = _sources(settings)
    if source is None:
        return None, None
    sources, version = source
    package_name = "_bioradar_biocheck_" + version
    module_name = package_name + ".core"
    if module_name in sys.modules:
        return sys.modules[module_name], version
    # Load pure evidence, methods and prompts without app/__init__, model or CLI.
    # Compiling in memory avoids writing bytecode into the upstream project.
    package = ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    try:
        for filename in sources:
            if filename in _VERSION_ONLY_FILES:
                continue
            name = package_name + "." + filename.removesuffix(".py")
            module = ModuleType(name)
            module.__package__ = package_name
            module.__file__ = str(Path(settings.biocheck_project_dir) / "app" / filename)
            sys.modules[name] = module
            exec(compile(sources[filename], module.__file__, "exec"), module.__dict__)
        core = sys.modules[module_name]
        if not all(callable(getattr(core, method, None)) for method in _METHODS):
            raise ValueError("missing method")
    except Exception:
        for name in (package_name, *(package_name + "." + f[:-3] for f in sources)):
            sys.modules.pop(name, None)
        return None, version
    return core, version


def _value(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else copy.deepcopy(value)


BIOCHECK_INPUT_VERSION = "adopted-window-declared-audit-v1"


def _adopted_input_version(value):
    if value is None:
        return False
    if value != BIOCHECK_INPUT_VERSION:
        raise DomainError("biocheck_input_version_invalid", "固定专业输入版本不受支持", 422)
    return True


def _adopted_source_window(item, text):
    start, end, length = item.get("start"), item.get("end"), item.get("source_length")
    if start is not None or end is not None:
        if type(start) is not int or type(end) is not int or start < 0 or end - start != len(text):
            raise DomainError("provider_input_invalid", "采用窗口未对应实际文本区间", 422)
    if length is not None and (
        type(length) is not int or length < len(text) or end is not None and end > length
    ):
        raise DomainError("provider_input_invalid", "采用窗口越过已知原文长度", 422)
    return {
        "start": start,
        "end": end,
        "source_length": length,
        "offset_basis": "unicode_code_points" if start is not None else None,
        "text_basis": item.get("text_basis"),
        "adopted_text_length": len(text),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_text_sha256": item.get("text_sha256"),
        "hash_scope": "adopted_packet_text",
    }


def _adopted_source_context(entry, item, facts, text):
    context = copy.deepcopy(entry.get("source_context") or {})
    level = context.get(
        "original_text_level",
        context.get(
            "text_level", item.get("text_level", facts.get("text_level") if isinstance(facts, dict) else None)
        ),
    )
    if context.get("source_window") is not None:
        context.setdefault("original_source_window", copy.deepcopy(context["source_window"]))
    context.update(
        original_text_level=level,
        adopted_text_level="selected_passages",
        text_level="selected_passages",
        source_window=_adopted_source_window(item, text),
    )
    return context


def build_packet(question, evidence, assessment=None, *, input_version=None):
    """Keep exact selected text, versions and a reversible Radar reference mapping."""
    versioned = _adopted_input_version(input_version)
    if not isinstance(question, str) or not question.strip():
        raise DomainError("provider_input_invalid", "共享研究输入缺少问题", 422)
    items, references = [], {}
    for index, original in enumerate(evidence, 1):
        item = _value(original)
        if not isinstance(item, dict):
            raise DomainError("provider_input_invalid", "共享研究材料不是有效对象", 422)
        key = item.get("key", item.get("id", f"E{index}"))
        reference = _value(item.get("record_ref", {}))
        if not isinstance(reference, dict):
            raise DomainError("provider_input_invalid", "共享研究材料引用格式无效", 422)
        version = reference.get("adopted_version") or item.get("version")
        text = item.get("text")
        if (
            not isinstance(key, str)
            or not _CITATION_ID.fullmatch(key)
            or key in references
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(version, str)
            or not version
        ):
            raise DomainError("provider_input_invalid", "共享研究材料缺少唯一引用、固定版本或原文", 422)
        entry = {"id": key, "text": text, "version": version}
        for name in ("title", "source_url", "publication_time", "limitations", "text_basis"):
            if name in item:
                entry[name] = copy.deepcopy(item[name])
        facts = item.get("facts") or {}
        source_context = item.get("source_context")
        if source_context is not None:
            from bioradar.adapters.fabric_document import context_at

            interpretation = _value(assessment) if assessment is not None else {}
            temporal = (interpretation.get("research_scope") or {}).get("evidence_time") or {}
            entry["source_context"] = context_at(
                source_context,
                mode=temporal.get("mode", "current"),
                cutoff=temporal.get("cutoff"),
                text=text,
            )
            try:
                source_id, native_id, demo = json.loads(reference["object_id"])
            except (KeyError, ValueError, TypeError):
                source_id, native_id, demo = item.get("source_id", ""), "", None
            entry.update(source_id=source_id, record_id=native_id)
            if type(demo) is bool:
                entry["is_demo"] = demo
            if source_context.get("material_kind"):
                entry["source_kind"] = source_context["material_kind"]
            if entry["source_context"].get("information_events"):
                entry["source_context"]["information_environment_event"] = True
            entry["verification_status"] = (
                "unverified"
                if source_context.get("statement_status") == "unverified_report"
                or source_context.get("material_kind") in {"social_media", "repost"}
                else "unknown"
            )
            if source_context.get("text_level") is not None:
                entry["text_level"] = "selected_passages"
        for name in (
            "source_kind",
            "publication_type",
            "publication_types",
            "pub_type_list",
            "text_level",
            "source_id",
            "source_record_id",
            "source_group",
            "source_site",
        ):
            if name in item:
                entry[name] = copy.deepcopy(item[name])
            elif isinstance(facts, dict) and name in facts:
                entry[name] = copy.deepcopy(facts[name])
        if source_context is not None:
            # Explicit document type wins over stale legacy source_kind metadata.
            if source_context.get("material_kind"):
                entry["source_kind"] = source_context["material_kind"]
            entry["text_level"] = "selected_passages"
        for name in ("pmid", "doi"):
            if isinstance(facts, dict) and name in facts:
                entry[name] = copy.deepcopy(facts[name])
            elif name in item:
                entry[name] = copy.deepcopy(item[name])
        if versioned:
            entry["source_context"] = _adopted_source_context(entry, item, facts, text)
            entry["text_level"] = "selected_passages"
        items.append(entry)
        references[key] = {
            name: copy.deepcopy(item[name])
            for name in (
                "key",
                "record_ref",
                "history_binding",
                "snapshot_id",
                "material_entry_id",
                "text_basis",
                "start",
                "end",
                "text_sha256",
                "selection_hash",
            )
            if name in item
        }
        references[key]["version"] = version
        references[key]["selected_text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    if not items:
        raise DomainError("provider_input_invalid", "共享研究输入至少需要一份固定原文", 422)
    packet = {"question": question, "evidence": items, "evidence_map": references}
    if assessment is not None:
        packet["assessment"] = _value(assessment)
    return packet


def _retry_kwargs(value, name):
    if type(value) is not int or value not in {0, 1}:
        raise DomainError("biocheck_retry_invalid", "专业结构修复仅允许明确的0或1", 422)
    return {name: 1} if value else {}


def supports_bounded_retry(core, kind):
    """Trust only the capability snapshot pinned to this loaded HTTP method version."""
    if kind not in {"plan", "verify"}:
        return False
    caps = getattr(core, "capabilities", {})
    if not isinstance(caps, dict):
        return False
    features = caps.get("features")
    policy = caps.get(kind + "_retry_policy")
    feature = "bounded_plan_repair" if kind == "plan" else "bounded_verify_contract_repair"
    stages = ["replan_plain", "replan_bound"] if kind == "plan" else ["verify_repair"]
    return bool(
        caps.get("schema_version") == "bioradar.professional.v1"
        and caps.get("provider") == "biocheck"
        and isinstance(caps.get("provider_version"), str)
        and bool(caps["provider_version"])
        and caps["provider_version"] == getattr(core, "provider_version", None)
        and isinstance(features, list)
        and all(isinstance(item, str) for item in features)
        and feature in features
        and isinstance(policy, dict)
        and policy.get("argument") == "max_" + kind + "_retries"
        and type(policy.get("default")) is int
        and policy["default"] == 0
        and type(policy.get("maximum")) is int
        and policy["maximum"] == 1
        and policy.get("stages") == stages
        and type(policy.get("diagnostic_limit")) is int
        and policy["diagnostic_limit"] == 8
    )


def supports_paragraph_verification(core):
    caps = getattr(core, "capabilities", {})
    if isinstance(caps, dict) and caps:
        policy = caps.get("paragraph_verification", {})
        return (
            caps.get("provider_version") == getattr(core, "provider_version", None)
            and policy.get("argument") == "mode"
            and policy.get("value") == "paragraph"
            and policy.get("retry_on") == "length"
            and (
                policy.get("max_retries_per_paragraph") == 1
                or policy.get("grain") == "cited_claims"
                and policy.get("maximum_claims_per_batch") == 3
                and policy.get("max_retries_per_batch") == 1
            )
        )
    import inspect

    verify = getattr(core, "verify", None)
    return callable(verify) and "mode" in inspect.signature(verify).parameters


def _unsent_verification_budget(exc, max_verify_retries):
    if max_verify_retries != 1 or exc.code not in {
        "analysis_model_budget",
        "analysis_token_budget",
        "analysis_time_budget",
        "analysis_tool_budget",
        "model_input_budget",
    }:
        return False
    return not isinstance(exc, ModelCallError) or exc.receipt.status == "not_sent"


def _contract_call(core, operation, *args, **kwargs):
    """Give local typed rejections the same safe codes as completed HTTP replies."""
    try:
        return getattr(core, operation)(*args, **kwargs)
    except ValueError as exc:
        completion_error = getattr(core, "VerificationCompletionError", None)
        if isinstance(completion_error, type) and isinstance(exc, completion_error):
            raise DomainError(
                "verification_incomplete", "段落核验未完整结束；正文及各段回执保留。", 502
            ) from exc
        for name, initial, exhausted in (
            ("PlanReferenceError", "plan_reference_invalid", "plan_repair_exhausted"),
            ("VerificationContractError", "verification_contract_invalid", "verification_repair_exhausted"),
        ):
            error_type = getattr(core, name, None)
            if isinstance(error_type, type) and error_type is not ValueError and isinstance(exc, error_type):
                code = exhausted if getattr(exc, "repair_attempted", 0) else initial
                raise DomainError(code, "专业结构回执未通过契约；原稿与原回执保留，等待核验。", 502) from exc
        raise


def _callback(call, *, verify_target=None, allow_plan_repair=False):
    if verify_target not in {None, "biocheck_check", "biocheck_recheck"}:
        raise DomainError("biocheck_stage_invalid", "核验目标阶段不符合接入约定", 422)
    checks = 0

    def invoke(stage, instruction, payload, response_schema=None, *, call_id=None):
        nonlocal checks
        paragraph_stage = stage in {
            "verify_paragraph",
            "verify_paragraph_retry",
            "verify_coverage",
            "verify_claim",
            "verify_claim_retry",
        }
        envelope_stage = paragraph_stage or stage == "revise_claims"
        if paragraph_stage:
            prefix = "biocheck_recheck" if verify_target == "biocheck_recheck" else "biocheck_verify"
            target = prefix + stage.removeprefix("verify")
        elif stage == "verify":
            checks += 1
            if checks > (1 if verify_target is not None else 2):
                raise DomainError("biocheck_stage_invalid", "BioCheck 超过一次修订复查范围", 502)
            target = verify_target or ("biocheck_check" if checks == 1 else "biocheck_recheck")
        elif stage == "verify_repair" and verify_target is not None:
            target = (
                "biocheck_verify_repair" if verify_target == "biocheck_check" else "biocheck_recheck_repair"
            )
        else:
            target = _STAGES.get(stage)
            if stage in {"replan_plain", "replan_bound"} and not allow_plan_repair:
                target = None
        if target is None or not isinstance(instruction, str) or not isinstance(payload, dict):
            raise DomainError("biocheck_stage_invalid", "BioCheck 请求阶段不符合接入约定", 502)
        if envelope_stage and call_id is None:
            call_id = (
                target
                + ":"
                + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            )
        keywords = {"call_id": call_id} if call_id is not None else {}
        try:
            output = (
                call(target, instruction, payload, response_schema, **keywords)
                if response_schema is not None
                else call(target, instruction, payload, **keywords)
            )
        except ModelCallError as exc:
            if envelope_stage and exc.receipt.status == "failed" and exc.receipt.finish_reason == "length":
                return {"finish_reason": "length", "output": None}
            raise
        if not isinstance(output, dict):
            raise DomainError("biocheck_response_invalid", "BioCheck 模型回执不是JSON对象", 502)
        return {"finish_reason": "stop", "output": output} if envelope_stage else output

    return invoke


def _unavailable(packet, draft=None, *, version=None, status="unavailable"):
    return {
        "provider": "biocheck",
        "provider_version": version,
        "status": status,
        "final_text": draft,
        "verification": None,
        "initial_verification": None,
        "revision": None,
        "evidence_map": copy.deepcopy(packet.get("evidence_map", {})),
        "error_code": "biocheck_not_available" if status == "unavailable" else "biocheck_result_invalid",
    }


def _result(packet, draft, review, initial, revision, version, **extra):
    if (
        not isinstance(draft, str)
        or not draft.strip()
        or not isinstance(review, dict)
        or review.get("draft") != draft
        or review.get("status") not in {"PASS", "REVIEW", "FAIL"}
    ):
        raise ValueError("verification is not bound to delivered text")
    return {
        "provider": "biocheck",
        "provider_version": version,
        "status": review["status"],
        "final_text": draft,
        "verification": review,
        "initial_verification": initial,
        "revision": revision,
        "evidence_map": copy.deepcopy(packet.get("evidence_map", {})),
        "text_sha256": hashlib.sha256(draft.encode()).hexdigest(),
        "position_basis": (
            "Unicode code points; source quotes use evidence[id].text; analysis_result quotes use "
            "output_path and selected_value_json value_start/value_end (half-open offsets)."
        ),
        "error_code": None,
        **extra,
    }


def _optional_repair(
    core,
    packet,
    draft,
    initial,
    invoke,
    repair,
    *,
    max_verify_retries=0,
    recheck_invoke=None,
    verification_mode="whole",
):
    if not repair or not initial.get("problem_units"):
        return draft, initial, None, {}
    if initial.get("verification_errors"):
        return (
            draft,
            initial,
            None,
            {
                "repair_error_code": "biocheck_verification_errors",
                "repair_stop_reason": "核验回执的原文或陈述位置未通过检查，已保留初稿，未据此修订。",
            },
        )
    revision = None
    try:
        revision = _contract_call(core, "revise", copy.deepcopy(packet), initial, invoke)
        candidate = revision["draft"]
        if (
            initial.get("verification_method") == "cited_claims"
            and revision.get("requires_verification") is False
            and candidate == draft
        ):
            return draft, initial, revision, {"repair_stop_reason": revision.get("reason")}
        review = _contract_call(
            core,
            "verify",
            copy.deepcopy(packet),
            candidate,
            recheck_invoke or invoke,
            **_retry_kwargs(max_verify_retries, "max_verify_retries"),
            **({"mode": "paragraph"} if verification_mode == "paragraph" else {}),
            **(
                {"previous_review": copy.deepcopy(initial)}
                if verification_mode == "paragraph" and initial.get("verification_method") == "cited_claims"
                else {}
            ),
        )
        return candidate, review, revision, {}
    except DomainError as exc:
        budget_stop = exc.code in {
            "analysis_model_budget",
            "analysis_token_budget",
            "analysis_time_budget",
        } or _unsent_verification_budget(exc, max_verify_retries)
        if isinstance(exc, ModelCallError):
            budget_stop = exc.receipt.status == "not_sent" and (
                budget_stop or exc.code == "model_input_budget"
            )
        contract_stop = (
            not isinstance(exc, ModelCallError)
            and (revision is not None or initial.get("verification_method") == "cited_claims")
            and exc.code
            in {
                "verification_contract_invalid",
                "verification_repair_exhausted",
                "verification_incomplete",
                "method_input_invalid",
                "biocheck_response_invalid",
            }
        )
        if not budget_stop and not contract_stop:
            raise
        # A revision without its own check cannot replace an already checked draft.
        return (
            draft,
            initial,
            None,
            {
                "repair_error_code": exc.code,
                "repair_stopped_at": "biocheck_revise" if revision is None else "biocheck_recheck",
                "repair_stop_reason": (
                    "修订稿核验结构未形成有效回执，已保留原核验稿，等待核验。"
                    if contract_stop
                    else "可选修订或复核在请求发送前达到预算，已保留完成核验的初稿。"
                ),
                "unverified_revision": revision,
            },
        )
    except ValueError:
        if initial.get("verification_method") != "cited_claims":
            raise
        return (
            draft,
            initial,
            None,
            {
                "repair_error_code": "biocheck_revision_invalid",
                "repair_stop_reason": "修订回执未通过失败句与引用范围检查，已保留原核验稿。",
                "unverified_revision": revision,
            },
        )


def check_biocheck(
    settings,
    packet,
    draft,
    call,
    *,
    repair=True,
    loaded=None,
    max_verify_retries=0,
    verify_target=None,
    verification_mode="whole",
    previous_review=None,
):
    """Check this exact draft; one optional local revision is rechecked in full."""
    core, version = load_biocheck(settings) if loaded is None else loaded
    if core is None:
        return _unavailable(packet, draft, version=version)
    verify_options = _retry_kwargs(max_verify_retries, "max_verify_retries")
    if verification_mode == "paragraph":
        verify_options = {"mode": "paragraph"}
        if previous_review is not None:
            verify_options["previous_review"] = copy.deepcopy(previous_review)

    invoke = _callback(call, verify_target=(verify_target or "biocheck_check") if verify_options else None)
    recheck_invoke = _callback(call, verify_target="biocheck_recheck") if verify_options else None
    try:
        try:
            initial = _contract_call(core, "verify", copy.deepcopy(packet), draft, invoke, **verify_options)
        except ModelCallError as exc:
            budget_stop = _unsent_verification_budget(
                exc, 1 if verification_mode == "paragraph" else max_verify_retries
            )
            if not budget_stop and (
                exc.receipt.status != "failed"
                or exc.code
                not in {
                    "model_output_invalid",
                    "model_output_truncated",
                }
            ):
                raise
            # The provider answered, but supplied no usable verification. Preserve
            # the exact draft as failed/unverified; never revise it from this error.
            return {
                **_unavailable(packet, draft, version=version, status="failed"),
                "error_code": exc.code,
                "verification_error": (
                    "专业核验在发送前达到预算；已生成正文保留，核验未完成。"
                    if budget_stop
                    else "核验服务返回的内容无法使用；正文保留，等待核验。"
                ),
                **({"verification_stop_reason": "not_sent_budget"} if budget_stop else {}),
            }
        except DomainError as exc:
            budget_stop = _unsent_verification_budget(
                exc, 1 if verification_mode == "paragraph" else max_verify_retries
            )
            if not budget_stop and exc.code not in {
                "method_input_invalid",
                "verification_contract_invalid",
                "verification_repair_exhausted",
                "verification_incomplete",
            }:
                raise
            # A completed HTTP method rejection is a known failed verification,
            # unlike a missing/unknown transport response.
            return {
                **_unavailable(packet, draft, version=version, status="failed"),
                "error_code": exc.code,
                "verification_error": (
                    "专业核验在发送前达到预算；已生成正文保留，核验未完成。"
                    if budget_stop
                    else "专业核验未接受本次回执结构；正文保留，等待核验。"
                ),
                **({"verification_stop_reason": "not_sent_budget"} if budget_stop else {}),
            }
        draft, review, revision, repair_state = _optional_repair(
            core,
            packet,
            draft,
            initial,
            invoke,
            repair,
            max_verify_retries=max_verify_retries,
            recheck_invoke=recheck_invoke,
            verification_mode=verification_mode,
        )
        return _result(
            packet,
            draft,
            review,
            initial,
            revision,
            getattr(core, "provider_version", version),
            **repair_state,
        )
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        # Provider validation errors may include restricted source text.
        return _unavailable(packet, draft, version=version, status="failed")


def run_biocheck(
    settings,
    packet,
    call,
    *,
    repair=True,
    loaded=None,
    max_plan_retries=0,
    max_verify_retries=0,
    verification_mode="whole",
):
    """Compose using the upstream bound plan, verify, and revise at most once."""
    core, version = load_biocheck(settings) if loaded is None else loaded
    if core is None:
        return _unavailable(packet, version=version)
    plan_options = _retry_kwargs(max_plan_retries, "max_plan_retries")
    verify_options = _retry_kwargs(max_verify_retries, "max_verify_retries")
    if verification_mode == "paragraph":
        verify_options = {"mode": "paragraph"}

    invoke = _callback(
        call,
        verify_target="biocheck_check" if verify_options else None,
        allow_plan_repair=bool(plan_options),
    )
    recheck_invoke = _callback(call, verify_target="biocheck_recheck") if verify_options else None
    draft = None
    try:
        generated = _contract_call(
            core, "generate", copy.deepcopy(packet), invoke, method="bound", **plan_options
        )
        draft = generated["draft"]
        try:
            initial = _contract_call(core, "verify", copy.deepcopy(packet), draft, invoke, **verify_options)
        except ModelCallError as exc:
            budget_stop = _unsent_verification_budget(
                exc, 1 if verification_mode == "paragraph" else max_verify_retries
            )
            if not budget_stop and (
                exc.receipt.status != "failed"
                or exc.code
                not in {
                    "model_output_invalid",
                    "model_output_truncated",
                }
            ):
                raise
            # The provider answered, but supplied no usable verification. Preserve
            # the exact draft as failed/unverified; never revise it from this error.
            return {
                **_unavailable(packet, draft, version=version, status="failed"),
                "error_code": exc.code,
                "verification_error": (
                    "专业核验在发送前达到预算；已生成正文保留，核验未完成。"
                    if budget_stop
                    else "核验服务返回的内容无法使用；正文保留，等待核验。"
                ),
                **({"verification_stop_reason": "not_sent_budget"} if budget_stop else {}),
            }
        except DomainError as exc:
            budget_stop = _unsent_verification_budget(
                exc, 1 if verification_mode == "paragraph" else max_verify_retries
            )
            if not budget_stop and exc.code not in {
                "method_input_invalid",
                "verification_contract_invalid",
                "verification_repair_exhausted",
                "verification_incomplete",
            }:
                raise
            # A completed HTTP method rejection is a known failed verification,
            # unlike a missing/unknown transport response.
            return {
                **_unavailable(packet, draft, version=version, status="failed"),
                "error_code": exc.code,
                "verification_error": (
                    "专业核验在发送前达到预算；已生成正文保留，核验未完成。"
                    if budget_stop
                    else "专业核验未接受本次回执结构；正文保留，等待核验。"
                ),
                **({"verification_stop_reason": "not_sent_budget"} if budget_stop else {}),
            }
        draft, review, revision, repair_state = _optional_repair(
            core,
            packet,
            draft,
            initial,
            invoke,
            repair,
            max_verify_retries=max_verify_retries,
            recheck_invoke=recheck_invoke,
            verification_mode=verification_mode,
        )
        return _result(
            packet,
            draft,
            review,
            initial,
            revision,
            getattr(core, "provider_version", version),
            generated=generated,
            **repair_state,
        )
    except DomainError as exc:
        known_failed = (
            not isinstance(exc, ModelCallError)
            and exc.code in {"method_input_invalid", "plan_reference_invalid", "plan_repair_exhausted"}
        ) or (
            isinstance(exc, ModelCallError)
            and exc.receipt.status == "failed"
            and exc.code in {"model_output_invalid", "model_output_truncated"}
        )
        if not known_failed or draft is not None:
            raise
        # No professional draft exists yet. The host can still deliver its saved
        # structured analysis, explicitly marked as an unfinished report.
        return {
            **_unavailable(packet, version=version, status="failed"),
            "error_code": exc.code,
            "generation_error": "专业报告生成未完成；保留已有分析草稿，尚未经过正文核验。",
        }
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return _unavailable(packet, draft, version=version, status="failed")


def support_audit_targets(review):
    """Freeze only the already supported fact identifiers; this does not judge support."""
    if review.get("verification_errors"):
        return []
    return [
        [unit["unit_id"], index]
        for unit in review["units"]
        for index, claim in enumerate(unit["claims"])
        if claim.get("kind", "fact") == "fact" and claim["verdict"] == "supported"
    ]


def audit_biocheck_support(settings, packet, review, call, *, targets=None, loaded=None, input_version=None):
    """Audit selected support independently, preserving the exact draft and old verification."""
    versioned = _adopted_input_version(input_version)
    core, version = load_biocheck(settings) if loaded is None else loaded
    binding = {
        "provider": "biocheck",
        "provider_version": version,
        "original_status": review.get("status") if isinstance(review, dict) else None,
        "targets": [],
        "text_sha256": None,
        "review_sha256": None,
        "evidence_map": copy.deepcopy(packet.get("evidence_map", {})) if isinstance(packet, dict) else {},
        "evidence_versions": {},
    }

    def stopped(status, code, reason):
        return {
            "status": status,
            "checks": [],
            "skipped": [],
            "verification_errors": [],
            "scope": "仅复核固定目标的所选支持片段；不判断事实整体真伪，不改写正文，PASS不代表整稿通过。",
            **binding,
            "error_code": code,
            "not_run_reason": reason,
        }

    callback_failure = None
    guarded_call = _callback(call)

    def invoke(stage, instruction, payload, response_schema=None, *, call_id=None):
        nonlocal callback_failure
        try:
            assessment = packet.get("assessment")
            if (
                not versioned
                and stage == "support_audit"
                and isinstance(payload, dict)
                and isinstance(assessment, dict)
                and ("research_scope" in assessment or "source_updates" in assessment)
            ):
                # Upstream audit sends only claims; keep the same frozen scope as full verification.
                payload = copy.deepcopy(payload)
                for field in ("research_scope", "source_updates", "analysis_results"):
                    if field in assessment:
                        payload.setdefault("assessment", {})[field] = copy.deepcopy(assessment[field])
            return guarded_call(stage, instruction, payload, response_schema, call_id=call_id)
        except Exception as exc:
            callback_failure = exc
            raise

    try:
        draft = review["draft"]
        if not isinstance(draft, str) or not draft.strip():
            raise ValueError("missing checked text")
        binding["text_sha256"] = hashlib.sha256(draft.encode()).hexdigest()
        binding["review_sha256"] = hashlib.sha256(
            json.dumps(review, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        binding["evidence_versions"] = {
            item["id"]: {
                "version": item["version"],
                "selected_text_sha256": hashlib.sha256(item["text"].encode()).hexdigest(),
            }
            for item in packet["evidence"]
        }
        if review.get("verification_errors"):
            result = stopped(
                "NOT_RUN",
                "biocheck_verification_errors",
                "原核验回执存在位置或材料错误，未发送所选片段复核请求。",
            )
            result["verification_errors"] = copy.deepcopy(review["verification_errors"])
            return result
        eligible = [tuple(target) for target in support_audit_targets(review)]
        requested = eligible if targets is None else [tuple(target) for target in targets]
        if len(set(requested)) != len(requested) or any(
            len(target) != 2
            or not isinstance(target[0], str)
            or type(target[1]) is not int
            or target not in eligible
            for target in requested
        ):
            raise ValueError("invalid support targets")
        binding["targets"] = [list(target) for target in requested]
        if not requested:
            return stopped("NOT_RUN", "biocheck_support_no_targets", "原核验没有已判支持的事实目标。")
        if core is None or not callable(getattr(core, "audit_support", None)):
            return stopped("NOT_RUN", "biocheck_support_unavailable", "当前共享方法未提供所选片段复核。")
        audit = core.audit_support(copy.deepcopy(packet), copy.deepcopy(review), invoke, targets=requested)
        binding["provider_version"] = getattr(core, "provider_version", version)
        if (
            not isinstance(audit, dict)
            or audit.get("status") not in {"PASS", "REVIEW", "NOT_RUN"}
            or audit.get("original_status") != review["status"]
        ):
            raise ValueError("invalid support audit")
        result = {**audit, **binding, "error_code": None}
        if audit["status"] == "NOT_RUN":
            result.update(
                error_code="biocheck_support_no_targets",
                not_run_reason="共享方法未形成可发送的有效片段复核目标。",
            )
        return result
    except DomainError as exc:
        budget_stop = exc.code in {"analysis_model_budget", "analysis_token_budget", "analysis_time_budget"}
        if isinstance(exc, ModelCallError):
            budget_stop = exc.receipt.status == "not_sent" and (
                budget_stop or exc.code == "model_input_budget"
            )
            if (
                exc.receipt.status == "failed"
                and exc.receipt.http_status == 200
                and exc.code in {"model_output_invalid", "model_protocol_error"}
            ):
                result = stopped("REVIEW", exc.code, "所选片段复核已收到响应，但格式无效，需人工核对。")
                result["model_receipt_status"] = exc.receipt.status
                return result
        if budget_stop:
            result = stopped("NOT_RUN", exc.code, "请求发送前达到统一预算，所选片段复核未执行。")
            result["model_receipt_status"] = (
                exc.receipt.status if isinstance(exc, ModelCallError) else "not_sent"
            )
            return result
        if exc.code == "biocheck_response_invalid" and not isinstance(exc, ModelCallError):
            return stopped("REVIEW", exc.code, "所选片段复核响应不是JSON对象，需人工核对。")
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        if callback_failure is not None:
            raise
        return stopped("REVIEW", "biocheck_support_invalid", "所选片段复核输入或回执无效，需人工核对。")


def _feedback_binding(packet, review, audit):
    """Bind feedback to a saved draft and its currently fixed evidence, without semantic rechecking."""
    draft = review.get("draft") if isinstance(review, dict) else None
    binding = {
        "provider": "biocheck",
        "text_sha256": hashlib.sha256(draft.encode()).hexdigest() if isinstance(draft, str) else None,
        "review_sha256": hashlib.sha256(
            json.dumps(review, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "evidence_versions": {
            item["id"]: {
                "version": item["version"],
                "selected_text_sha256": hashlib.sha256(item["text"].encode()).hexdigest(),
            }
            for item in packet["evidence"]
        },
    }
    if isinstance(review, dict):
        for field in ("evidence_catalog", "source_updates", "analysis_signature"):
            if field in review:
                binding[field] = copy.deepcopy(review[field])
    errors = []
    if not isinstance(draft, str) or not draft.strip():
        errors.append({"kind": "verification_unavailable", "reason": "缺少已核验的固定正文。"})
        return binding, errors
    units = review.get("units", [])
    identifiers = set()
    for unit in units:
        key, start, end = unit["unit_id"], unit["start"], unit["end"]
        if (
            key in identifiers
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(draft)
            or draft[start:end] != unit["text"]
        ):
            errors.append({"kind": "verification_mismatch", "unit_id": key, "reason": "核验段落不属于本稿。"})
        identifiers.add(key)
        for claim in [*unit.get("claims", []), *unit.get("issues", [])]:
            for reference in claim.get("evidence", []):
                expected = binding["evidence_versions"].get(reference.get("evidence_id"))
                if expected is None or reference.get("version") != expected["version"]:
                    errors.append(
                        {
                            "kind": "source_mismatch",
                            "unit_id": key,
                            "reason": "核验所用原件与当前固定原件版本不同。",
                        }
                    )
    if audit is not None:
        if audit.get("original_status") is not None and audit["original_status"] != review.get("status"):
            errors.append({"kind": "audit_mismatch", "reason": "片段复核的原核验状态与当前回执不同。"})
        for key in (
            "text_sha256",
            "review_sha256",
            "evidence_versions",
            "evidence_catalog",
            "source_updates",
            "analysis_signature",
        ):
            if key in audit and key in binding and audit[key] != binding[key]:
                errors.append({"kind": "audit_mismatch", "reason": "片段复核不属于本稿、本核验或固定原件。"})
        if audit.get("error_code") and audit["error_code"] != "biocheck_support_no_targets":
            errors.append(
                {
                    "kind": "audit_error",
                    "error_code": audit["error_code"],
                    "reason": audit.get("not_run_reason", "片段复核存在未解决错误。"),
                }
            )
        elif audit.get("status") == "NOT_RUN" and support_audit_targets(review):
            errors.append({"kind": "audit_not_run", "reason": "本稿存在支持片段目标，但片段复核尚未执行。"})
    return binding, errors


def quality_feedback_biocheck(settings, packet, review, *, audit=None, source_errors=(), loaded=None):
    """Route saved verification and audit findings with zero model calls."""
    core, version = load_biocheck(settings) if loaded is None else loaded
    result = {
        "provider": "biocheck",
        "provider_version": version,
        "machine_status": review.get("status") if isinstance(review, dict) else None,
        "audit_status": audit.get("status") if isinstance(audit, dict) else "not_requested",
        "evidence_gaps": [],
        "repair_units": [],
        "review_required": [],
        "error_code": None,
        "qualification": "已有核验与片段复核的机器反馈；缺口不是事实，修订仍须重新核验。",
    }
    try:
        binding, errors = _feedback_binding(packet, review, audit)
        result.update(binding)
        if not isinstance(review, dict) or not review.get("draft"):
            result["review_required"] = errors
            result["error_code"] = "biocheck_verification_unavailable"
            return result
        if core is None or not callable(getattr(core, "quality_feedback", None)):
            result.update(error_code="biocheck_feedback_unavailable")
            result["review_required"].append(
                {
                    "kind": "feedback_unavailable",
                    "reason": "当前共享方法未提供成稿质量反馈。",
                }
            )
            return result
        feedback = core.quality_feedback(
            copy.deepcopy(review), copy.deepcopy(audit), source_errors=copy.deepcopy(tuple(source_errors))
        )
        if not isinstance(feedback, dict) or any(
            not isinstance(feedback.get(key), list)
            for key in ("evidence_gaps", "repair_units", "review_required")
        ):
            raise ValueError("invalid quality feedback")
        result.update(copy.deepcopy(feedback))
        result["provider_version"] = getattr(core, "provider_version", version)
        result["review_required"].extend(errors)
        if (source_errors or review.get("verification_errors")) and not result["review_required"]:
            result["review_required"].append(
                {
                    "kind": "unresolved_input_error",
                    "reason": "来源或原核验错误尚未解决，禁止自动修订。",
                }
            )
        declared = {unit["unit_id"] for unit in review.get("units", [])}
        if any(not isinstance(key, str) or key not in declared for key in result["repair_units"]) or len(
            set(result["repair_units"])
        ) != len(result["repair_units"]):
            raise ValueError("invalid repair units")
        if result["review_required"]:
            result["repair_units"] = []
        return result
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        result.update(error_code="biocheck_feedback_invalid", repair_units=[])
        result["review_required"].append(
            {
                "kind": "feedback_invalid",
                "reason": "质量反馈或其固定正文、原件绑定无效，保留待核。",
            }
        )
        return result


def revise_biocheck(settings, packet, review, call, *, audit=None, source_errors=(), loaded=None):
    """Revise only method-approved units; the caller must durably save and verify any changed draft."""
    loaded = load_biocheck(settings) if loaded is None else loaded
    core, version = loaded
    feedback = quality_feedback_biocheck(
        settings, packet, review, audit=audit, source_errors=source_errors, loaded=loaded
    )
    original = review.get("draft") if isinstance(review, dict) else None
    result = {
        "provider": "biocheck",
        "provider_version": getattr(core, "provider_version", version),
        "draft": original,
        "status": "blocked" if feedback["review_required"] else "not_changed",
        "changed_units": [],
        "feedback": feedback,
        "needs_verification": False,
        "verification": None,
        "support_audit": None,
        "error_code": feedback.get("error_code"),
    }
    if feedback["review_required"] or not feedback["repair_units"]:
        result["reason"] = "没有已获允许的自动修订段落；保留原核验文字与补查或待核问题。"
        return result
    callback_failure = None
    guarded_call = _callback(call)

    def invoke(stage, instruction, payload, response_schema=None, *, call_id=None):
        nonlocal callback_failure
        try:
            return guarded_call(stage, instruction, payload, response_schema, call_id=call_id)
        except Exception as exc:
            callback_failure = exc
            raise

    try:
        revision = core.revise(
            copy.deepcopy(packet),
            copy.deepcopy(review),
            invoke,
            audit=copy.deepcopy(audit),
            source_errors=copy.deepcopy(tuple(source_errors)),
        )
        candidate, changes = revision["draft"], revision["changed_units"]
        if not isinstance(candidate, str) or not candidate.strip() or not isinstance(changes, list):
            raise ValueError("invalid revised draft")
        units = {unit["unit_id"]: unit for unit in review["units"]}
        expected, seen, actual = original, set(), []
        for change in changes:
            key = change["unit_id"]
            if key not in feedback["repair_units"] or key in seen:
                raise ValueError("revision changed an unapproved unit")
            unit = units[key]
            if (
                change["before"] != unit["text"]
                or not isinstance(change["after"], str)
                or not change["after"].strip()
            ):
                raise ValueError("revision before/after does not match the saved unit")
            seen.add(key)
            if change["before"] != change["after"]:
                actual.append(copy.deepcopy(change))
        for change in sorted(actual, key=lambda item: units[item["unit_id"]]["start"], reverse=True):
            unit = units[change["unit_id"]]
            expected = expected[: unit["start"]] + change["after"] + expected[unit["end"] :]
        if candidate != expected:
            raise ValueError("revision body does not match the declared changes")
        if review.get("verification_method") == "cited_claims":
            if (
                not isinstance(revision.get("changed_claims"), list)
                or type(revision.get("requires_verification")) is not bool
                or bool(actual) != revision["requires_verification"]
            ):
                raise ValueError("claim revision metadata differs from actual changed text")
            result.update(
                changed_claims=copy.deepcopy(revision["changed_claims"]),
                requires_verification=revision["requires_verification"],
            )
            if "verification_execution" in revision:
                result["verification_execution"] = copy.deepcopy(revision["verification_execution"])
        if not actual or candidate == original:
            result["reason"] = "修订回执没有产生实际文字改动，未记为修订成功。"
            return result
        result.update(
            draft=candidate,
            changed_units=actual,
            status="changed",
            needs_verification=True,
            reason="新文字已产生，尚须整稿核验及本稿所选片段复核。",
        )
        return result
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        if callback_failure is not None:
            raise
        result.update(status="failed", error_code="biocheck_revision_invalid")
        result["reason"] = "修订回执未严格对应允许段落，保留原核验文字。"
        return result


class _HTTPMethods:
    """Expose native method signatures; transport and durable hooks are supplied by the host."""

    def __init__(self, execute, provider_version, *, capabilities=None):
        self.execute = execute
        self.provider_version = provider_version
        self.capabilities = copy.deepcopy(capabilities) if isinstance(capabilities, dict) else {}
        self.features = copy.deepcopy(self.capabilities.get("features", []))
        self.plan_retry_policy = copy.deepcopy(self.capabilities.get("plan_retry_policy"))
        self.verify_retry_policy = copy.deepcopy(self.capabilities.get("verify_retry_policy"))

    def _run(self, operation, arguments, call=None):
        response = self.execute(operation, arguments, call=call)
        self.provider_version = response["provider_version"]
        return response["result"]

    def _retry_options(self, kind, value):
        options = _retry_kwargs(value, "max_" + kind + "_retries")
        if options and not supports_bounded_retry(self, kind):
            raise DomainError("biocheck_retry_unavailable", "固定专业方法未声明该有界结构修复", 422)
        return options

    def generate(self, packet, call, method="bound", *, max_plan_retries=0):
        return self._run(
            "generate",
            {"packet": packet, "method": method, **self._retry_options("plan", max_plan_retries)},
            call,
        )

    def verify(self, packet, draft, call, *, max_verify_retries=0, mode="whole", previous_review=None):
        if previous_review is not None and (mode != "paragraph" or not isinstance(previous_review, dict)):
            raise DomainError("biocheck_input_invalid", "复用须提供同方法完整的主张核验回执", 422)
        return self._run(
            "verify",
            {
                "packet": packet,
                "draft": draft,
                **self._retry_options("verify", max_verify_retries),
                **({"mode": "paragraph"} if mode == "paragraph" else {}),
                **(
                    {"previous_review": copy.deepcopy(previous_review)} if previous_review is not None else {}
                ),
            },
            call,
        )

    def revise(self, packet, review, call, *, audit=None, source_errors=()):
        return self._run(
            "revise",
            {"packet": packet, "review": review, "audit": audit, "source_errors": list(source_errors)},
            call,
        )

    def audit_support(self, packet, review, call, *, targets=None):
        return self._run("audit_support", {"packet": packet, "review": review, "targets": targets}, call)

    def quality_feedback(self, review, audit=None, *, source_errors=()):
        return self._run(
            "quality_feedback", {"review": review, "audit": audit, "source_errors": list(source_errors)}
        )


def load_biocheck_http(execute, provider_version=None, *, capabilities=None):
    """Bind the host's operation executor; HTTP mode never falls back to a local import."""
    return _HTTPMethods(execute, provider_version, capabilities=capabilities), provider_version
