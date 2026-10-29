"""Use Wang's installed mechanism method without its model or Fabric clients."""

import ast
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType

from bioradar.api.errors import DomainError

_FILES = ("models.py", "assess.py")
_STAGES = {
    "decomposition": "mech_decomposition",
    "local_review": "mech_local_review",
    "mechanism_review": "mech_mechanism_review",
}


def _sources(settings):
    if settings.mech_evidence_project_dir is None:
        return None
    root = Path(settings.mech_evidence_project_dir) / "mechevidence"
    try:
        sources = {name: (root / name).read_text(encoding="utf-8") for name in _FILES}
        parsed = {name: ast.parse(source) for name, source in sources.items()}
        if not any(
            isinstance(node, ast.FunctionDef) and node.name == "evaluate" for node in parsed["assess.py"].body
        ) or not any(
            isinstance(node, ast.ClassDef) and node.name == "Case" for node in parsed["models.py"].body
        ):
            return None
    except (OSError, UnicodeError, SyntaxError):
        return None
    version = hashlib.sha256("".join(name + "\0" + sources[name] for name in _FILES).encode()).hexdigest()
    return sources, version


def provider_status(settings):
    source = _sources(settings)
    return {
        "provider": "mech_evidence",
        "configured": settings.mech_evidence_project_dir is not None,
        "available": source is not None,
        "status": "ready" if source else "unavailable",
        "version": source[1] if source else None,
        "error_code": None if source else "mech_evidence_not_available",
        "qualification": "方法源码可读取；本轮是否适用、是否执行及评价结果另见实际回执。",
    }


def load_mech_evidence(settings):
    """Return one in-memory input/method snapshot and its actual content version."""
    source = _sources(settings)
    if source is None:
        return None, None, None
    sources, version = source
    package_name = "_bioradar_mech_" + version
    if package_name + ".assess" in sys.modules:
        return sys.modules[package_name + ".models"], sys.modules[package_name + ".assess"], version
    package = ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    try:
        for filename in _FILES:
            name = package_name + "." + filename.removesuffix(".py")
            module = ModuleType(name)
            module.__package__ = package_name
            module.__file__ = str(Path(settings.mech_evidence_project_dir) / "mechevidence" / filename)
            sys.modules[name] = module
            exec(compile(sources[filename], module.__file__, "exec"), module.__dict__)
    except Exception:
        for name in (package_name, *(package_name + "." + f[:-3] for f in _FILES)):
            sys.modules.pop(name, None)
        return None, None, version
    return sys.modules[package_name + ".models"], sys.modules[package_name + ".assess"], version


def _identity(reference):
    binding = reference.get("history_binding") or {}
    if binding.get("source_id") and binding.get("source_record_id"):
        return binding["source_id"], binding["source_record_id"]
    try:
        identity = json.loads(reference.get("record_ref", {}).get("object_id", "null"))
        if isinstance(identity, list) and len(identity) == 3:
            return identity[0], identity[1]
    except (ValueError, TypeError):
        pass
    return "", ""


def _source_kind(item, source_id, record_id):
    explicit = item.get("source_kind")
    if isinstance(explicit, str) and explicit.strip().casefold() in {"primary", "review", "graph"}:
        return explicit.strip().casefold(), "explicit_source_kind"
    types = item.get("publication_types", item.get("pub_type_list", item.get("publication_type", [])))
    if isinstance(types, dict):
        types = types.get("pubType", types.get("pub_type", []))
    if isinstance(types, str):
        types = [types]
    labels = (
        {value.strip().casefold() for value in types if isinstance(value, str)}
        if isinstance(types, list)
        else set()
    )
    if labels & {"review", "systematic review", "meta-analysis", "scoping review"}:
        return "review", "fabric_publication_types"
    if source_id in {"reviewed_graph", "primekg_graph"}:
        return "graph", "fabric_graph_source"
    site = item.get("source_site") or source_id
    if source_id == "legacy_documents" and record_id.startswith("legacy:"):
        site = record_id.split(":", 2)[1]
    if site in {"clinicaltrials", "chictr"}:
        # This is an original registry record, not evidence that a causal experiment
        # succeeded. The upstream method separately checks experimental basis.
        return "primary", "original_trial_registration_not_experimental_result"
    if labels & {
        "clinical trial",
        "randomized controlled trial",
        "controlled clinical trial",
        "clinical trial, phase i",
        "clinical trial, phase ii",
        "clinical trial, phase iii",
        "clinical trial, phase iv",
        "observational study",
        "case reports",
    }:
        return "primary", "explicit_primary_study_publication_type"
    return None, "source_kind_unresolved"


def _material_identity(packet, item):
    source_id, record_id = _identity(packet.get("evidence_map", {}).get(item["id"], {}))
    return {
        "source_id": source_id or item.get("source_id") or "",
        "record_id": record_id or item.get("source_record_id") or "",
        "source_group": item.get("source_group", ""),
        "url": item.get("source_url"),
        **{field: copy.deepcopy(item[field]) for field in ("pmid", "doi") if field in item},
    }


def _case(packet):
    assessment = packet.get("assessment") or {}
    if not isinstance(assessment, dict):
        assessment = {}
    explicit = packet.get("mechanism_case")
    if isinstance(explicit, dict):
        return copy.deepcopy(explicit), [], []
    mechanism = packet.get("mechanism") or assessment.get("mechanism")
    if not isinstance(mechanism, str) or not mechanism.strip():
        return None, [], []
    materials, missing, classification = [], [], []
    for item in packet.get("evidence", []):
        identifier = item["id"]
        identity = _material_identity(packet, item)
        kind, basis = _source_kind(item, identity["source_id"], identity["record_id"])
        classification.append({"evidence_id": identifier, "source_kind": kind, "basis": basis})
        if kind is None:
            missing.append(f"materials.{identifier}.source_kind")
            continue
        materials.append(
            {
                "id": identifier,
                "text": item["text"],
                "version": item["version"],
                "title": item.get("title", ""),
                "source_kind": kind,
                "text_level": item.get("text_level") or "selected_passages",
                **identity,
            }
        )
    return (
        {
            "question": packet["question"],
            "mechanism": mechanism,
            "scope": assessment.get("mechanism_scope") or {},
            "materials": materials,
        },
        missing,
        classification,
    )


def _state(packet, status, *, version=None, code=None, missing=()):
    return {
        "provider": "mech_evidence",
        "provider_version": version,
        "status": status,
        "error_code": code,
        "missing_fields": list(missing),
        "overall": None,
        "evidence_map": copy.deepcopy(packet.get("evidence_map", {})),
    }


def _identity_error_state(packet, version, error):
    detail = error.as_dict()
    return {
        **_state(
            packet,
            detail["status"],
            version=version,
            code=detail["error_code"],
            missing=[f"materials.{detail['material_id']}.{field}" for field in detail["fields"]],
        ),
        "identity_error": detail,
    }


def evaluate_mechanism(settings, packet, call, *, loaded=None):
    """Evaluate an explicit candidate; retain its strict schema on the common callback."""
    case, missing, classification = _case(packet)
    if case is None:
        return _state(packet, "not_requested", code="mechanism_candidate_absent")
    models, method, version = load_mech_evidence(settings) if loaded is None else loaded
    if method is None:
        return _state(packet, "unavailable", version=version, code="mech_evidence_not_available")
    if missing and not case.get("materials"):
        return _state(
            packet,
            "needs_input",
            version=version,
            code="mech_evidence_material_type_missing",
            missing=missing,
        )
    identity_error = getattr(method, "IdentityInputError", ())
    try:
        # Explicit cases must still use exactly the caller's fixed and authorized material.
        evidence = {item["id"]: item for item in packet["evidence"]}
        for material in case.get("materials", []):
            original = evidence.get(material.get("id"))
            if (
                original is None
                or material.get("text") != original["text"]
                or material.get("version") != original["version"]
            ):
                return _state(
                    packet,
                    "needs_input",
                    version=version,
                    code="mech_evidence_material_mismatch",
                )
            identity = _material_identity(packet, original)
            if any(
                name in material and material[name] != identity.get(name)
                for name in ("source_id", "record_id", "source_group", "url", "pmid", "doi")
            ):
                return _state(
                    packet, "needs_input", version=version, code="mech_evidence_material_mismatch"
                )
            material.update(identity)
        validated = models.Case.model_validate(case)
    except identity_error as exc:
        return _identity_error_state(packet, version, exc)
    except (ValueError, TypeError, KeyError, AttributeError):
        return _state(packet, "needs_input", version=version, code="mech_evidence_input_invalid")

    def invoke(stage, instruction, payload, schema):
        target = _STAGES.get(stage)
        if target is None or not isinstance(schema, dict):
            raise DomainError("mech_evidence_stage_invalid", "机制评价阶段不符合接入约定", 502)
        # Runtime accepts this optional fourth argument and dispatches the original
        # method-owned JSON Schema through its existing strict model adapter.
        return call(target, instruction, payload, schema)

    try:
        result = method.evaluate(validated, invoke)
    except Exception as exc:
        # Upstream wraps callback failures; recover the original durable budget or
        # unknown-outcome error so the runtime cannot treat it as a scientific verdict.
        cause = exc
        while cause is not None:
            if isinstance(cause, DomainError):
                raise cause
            if isinstance(cause, identity_error):
                return _identity_error_state(packet, version, cause)
            cause = cause.__cause__
        return _state(packet, "failed", version=version, code="mech_evidence_result_invalid")
    if not isinstance(result, dict) or not isinstance(result.get("overall"), dict):
        return _state(packet, "failed", version=version, code="mech_evidence_result_invalid")
    return {
        **result,
        "provider": "mech_evidence",
        "provider_version": version,
        "status": "completed",
        "error_code": None,
        "missing_fields": missing,
        "material_type_mapping": classification,
        "coverage": {
            "status": "partial" if missing else "complete",
            "evaluated_evidence_ids": [item["id"] for item in case["materials"]],
            "untyped_evidence_ids": [
                item["evidence_id"] for item in classification if item["source_kind"] is None
            ],
            "qualification": "评价仅覆盖已明确类别的本次材料；未定类别保留为补查，不计作已评价。",
        },
        "evidence_map": copy.deepcopy(packet.get("evidence_map", {})),
    }
