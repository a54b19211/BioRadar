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
    "cohort_review": "mech_cohort_review",
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
    if labels & {
        "review",
        "review-article",
        "review article",
        "systematic review",
        "meta-analysis",
        "scoping review",
    }:
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
        "research-article",
        "research article",
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


def mechanism_passage_bindings(packet, materials, *, assessment, radar_evidence=()):
    """Bind the method's actual input passages to fixed text, without resegmenting it."""
    fixed = {item["id"]: item for item in packet["evidence"]}
    radar = {item.get("key", item.get("id")): item for item in radar_evidence}
    requested = {}
    for observation in assessment.get("evidence", []):
        for reference in observation.get("references", []):
            key = (reference.get("material_id"), reference.get("version"), reference.get("id"))
            requested.setdefault(key, []).append(observation.get("id"))
    bindings, errors = {}, []
    for material in materials:
        original = fixed.get(material.get("id"))
        for passage in material.get("passages", []):
            key = (material.get("id"), material.get("version"), passage.get("id"))
            if key not in requested or key in bindings:
                continue
            start, end, quote = passage.get("start"), passage.get("end"), passage.get("text")
            if (
                original is None
                or material.get("version") != original["version"]
                or type(start) is not int
                or type(end) is not int
                or not isinstance(quote, str)
                or not 0 <= start < end <= len(original["text"])
                or original["text"][start:end] != quote
            ):
                errors.append({"id": key[2], "material_id": key[0], "reason": "fixed_passage_mismatch"})
                continue
            binding = {
                "id": key[2],
                "material_id": key[0],
                "version": key[1],
                "namespace": "mechevidence.materials.passages",
                "quote": quote,
                "start": start,
                "end": end,
                "offset_basis": "zero_based_half_open_selected_material_text",
                "selected_text_sha256": hashlib.sha256(original["text"].encode()).hexdigest(),
                "observation_ids": list(dict.fromkeys(requested[key])),
                "provenance": "derived_from_confirmed_shared_callback_materials",
            }
            parent = radar.get(key[0])
            if (
                parent is not None
                and parent.get("text") == original["text"]
                and (parent.get("record_ref", {}).get("adopted_version") or parent.get("version")) == key[1]
            ):
                binding["radar_passages"] = [
                    {
                        "id": item["id"],
                        "start": item["start"],
                        "end": item["end"],
                        "contains_reference": item["start"] <= start and item["end"] >= end,
                    }
                    for item in parent.get("passages", [])
                    if item["start"] < end and item["end"] > start
                ]
            bindings[key] = binding
    for key in requested:
        if key not in bindings:
            errors.append({"id": key[2], "material_id": key[0], "reason": "no_verified_input_passage"})
    return {
        "passage_bindings": list(bindings.values()),
        "passage_binding_errors": errors,
        "passage_namespace_qualification": (
            "MechEvidence的materials.passages编号与Radar原文片段编号是独立命名空间，不能按大小写或编号互换。"
            "本表仅从已确认共享回调的真实输入分段派生：quote/start/end对应同material/version的固定选段文字，"
            "offset为该选段内从0起、右端不含的字符位置；不是整篇未选原文的坐标。"
            "radar_passages仅列已给出的Radar片段包含或交叠关系；表中位置对应不证明机制成立或科学支持。"
        ),
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
                return _state(packet, "needs_input", version=version, code="mech_evidence_material_mismatch")
            material.update(identity)
        # The HTTP service owns its native Case schema; fixed identity checks
        # above still run here before any remote model step can be authorized.
        validated = case if isinstance(method, _HTTPMethods) else models.Case.model_validate(case)
    except identity_error as exc:
        return _identity_error_state(packet, version, exc)
    except (ValueError, TypeError, KeyError, AttributeError):
        return _state(packet, "needs_input", version=version, code="mech_evidence_input_invalid")

    sent_materials = []

    def invoke(stage, instruction, payload, schema, *, call_id=None):
        target = _STAGES.get(stage)
        if target is None or not isinstance(schema, dict):
            raise DomainError("mech_evidence_stage_invalid", "机制评价阶段不符合接入约定", 502)
        # Runtime accepts this optional fourth argument and dispatches the original
        # method-owned JSON Schema through its existing strict model adapter.
        materials = copy.deepcopy(payload.get("materials", []))
        keywords = {"call_id": call_id} if call_id is not None else {}
        output = call(target, instruction, payload, schema, **keywords)
        sent_materials.extend(materials)
        return output

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
    if isinstance(result, dict) and result.get("status") == "not_applicable":
        return {
            **result,
            **_state(
                packet, "not_applicable",
                version=getattr(method, "provider_version", version), code=result.get("error_code")
            ),
        }
    if not isinstance(result, dict) or not isinstance(result.get("overall"), dict):
        return _state(packet, "failed", version=version, code="mech_evidence_result_invalid")
    return {
        **result,
        **mechanism_passage_bindings(packet, sent_materials, assessment=result),
        "provider": "mech_evidence",
        "provider_version": getattr(method, "provider_version", version),
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


class _HTTPMethods:
    """Keep native methods remote while the adapter retains fixed material checks."""

    def __init__(self, execute, provider_version, context):
        self.execute = execute
        self.provider_version = provider_version
        self.context = copy.deepcopy(context or {})

    def _run(self, operation, arguments, call):
        response = self.execute(operation, arguments, call=call)
        self.provider_version = response["provider_version"]
        if response.get("status") == "not_applicable":
            return {
                key: copy.deepcopy(response[key])
                for key in ("status", "error_code", "reason", "context")
                if key in response
            }
        return response["result"]

    def evaluate(self, case, call):
        if hasattr(case, "model_dump"):
            case = case.model_dump(mode="json")
        return self._run(
            "evaluate",
            {"mode": "biomedical_research", "task": "mechanism", **self.context, "case": case},
            call,
        )

    def assess_independence(self, arguments, call):
        return self._run("assess_independence", copy.deepcopy(arguments), call)


def load_mech_evidence_http(execute, provider_version=None, *, context=None):
    """HTTP Case parsing happens in the service, with no dependency on its local checkout."""
    return None, _HTTPMethods(execute, provider_version, context), provider_version
