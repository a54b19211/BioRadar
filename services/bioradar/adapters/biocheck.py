"""Use installed BioCheck methods with the caller's durable model callback."""

import ast
import copy
import hashlib
import re
import sys
from pathlib import Path
from types import ModuleType

from bioradar.adapters.reasoning_model import ModelCallError
from bioradar.api.errors import DomainError

_PROVIDER_FILES = ("evidence.py", "prompts.py", "core.py")
_METHODS = ("generate", "verify", "revise")
_STAGES = {
    "plan_bound": "biocheck_plan",
    "plan_plain": "biocheck_plan",
    "write_bound": "biocheck_write",
    "write_plain": "biocheck_write",
    "write_direct": "biocheck_write",
    "revise": "biocheck_revise",
}
_CITATION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*")


def _sources(settings):
    directory = settings.biocheck_project_dir
    if directory is None:
        return None
    root = Path(directory) / "app"
    try:
        sources = {name: (root / name).read_text(encoding="utf-8") for name in _PROVIDER_FILES}
        methods = {
            node.name for node in ast.parse(sources["core.py"]).body if isinstance(node, ast.FunctionDef)
        }
        if not set(_METHODS).issubset(methods):
            return None
        for source in sources.values():
            ast.parse(source)
    except (OSError, UnicodeError, SyntaxError):
        return None
    version = hashlib.sha256(
        "".join(name + "\0" + sources[name] for name in _PROVIDER_FILES).encode()
    ).hexdigest()
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
        for filename in _PROVIDER_FILES:
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
        for name in (package_name, *(package_name + "." + f[:-3] for f in _PROVIDER_FILES)):
            sys.modules.pop(name, None)
        return None, version
    return core, version


def _value(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else copy.deepcopy(value)


def build_packet(question, evidence, assessment=None):
    """Keep exact selected text, versions and a reversible Radar reference mapping."""
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


def _callback(call):
    checks = 0

    def invoke(stage, instruction, payload):
        nonlocal checks
        if stage == "verify":
            checks += 1
            if checks > 2:
                raise DomainError("biocheck_stage_invalid", "BioCheck 超过一次修订复查范围", 502)
            target = "biocheck_check" if checks == 1 else "biocheck_recheck"
        else:
            target = _STAGES.get(stage)
        if target is None or not isinstance(instruction, str) or not isinstance(payload, dict):
            raise DomainError("biocheck_stage_invalid", "BioCheck 请求阶段不符合接入约定", 502)
        output = call(target, instruction, payload)
        if not isinstance(output, dict):
            raise DomainError("biocheck_response_invalid", "BioCheck 模型回执不是JSON对象", 502)
        return output

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
        "position_basis": "Unicode code points; half-open offsets in each evidence[id].text",
        "error_code": None,
        **extra,
    }


def _optional_repair(core, packet, draft, initial, invoke, repair):
    if not repair or not initial.get("problem_units"):
        return draft, initial, None, {}
    revision = None
    try:
        revision = core.revise(copy.deepcopy(packet), initial, invoke)
        candidate = revision["draft"]
        review = core.verify(copy.deepcopy(packet), candidate, invoke)
        return candidate, review, revision, {}
    except DomainError as exc:
        not_sent_input_limit = (
            isinstance(exc, ModelCallError)
            and exc.code == "model_input_budget"
            and exc.receipt.status == "not_sent"
        )
        if exc.code not in {"analysis_model_budget", "analysis_token_budget"} and not not_sent_input_limit:
            raise
        # A revision without its own check cannot replace an already checked draft.
        return (
            draft,
            initial,
            None,
            {
                "repair_error_code": exc.code,
                "unverified_revision": revision,
            },
        )


def check_biocheck(settings, packet, draft, call, *, repair=True, loaded=None):
    """Check this exact draft; one optional local revision is rechecked in full."""
    core, version = load_biocheck(settings) if loaded is None else loaded
    if core is None:
        return _unavailable(packet, draft, version=version)
    invoke = _callback(call)
    try:
        initial = core.verify(copy.deepcopy(packet), draft, invoke)
        draft, review, revision, repair_state = _optional_repair(core, packet, draft, initial, invoke, repair)
        return _result(packet, draft, review, initial, revision, version, **repair_state)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        # Provider validation errors may include restricted source text.
        return _unavailable(packet, draft, version=version, status="failed")


def run_biocheck(settings, packet, call, *, repair=True, loaded=None):
    """Compose using the upstream bound plan, verify, and revise at most once."""
    core, version = load_biocheck(settings) if loaded is None else loaded
    if core is None:
        return _unavailable(packet, version=version)
    invoke = _callback(call)
    draft = None
    try:
        generated = core.generate(copy.deepcopy(packet), invoke, method="bound")
        draft = generated["draft"]
        initial = core.verify(copy.deepcopy(packet), draft, invoke)
        draft, review, revision, repair_state = _optional_repair(core, packet, draft, initial, invoke, repair)
        return _result(packet, draft, review, initial, revision, version, generated=generated, **repair_state)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return _unavailable(packet, draft, version=version, status="failed")
