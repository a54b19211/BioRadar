"""The v2 request as the radar builds it, checked before it is sent.

check_request applies the rules MechEvidence's service applies to a request (unknown fields, binding against
text, scope states, the contract version, the authorization reference, duplicates) with the same error codes,
so a request MechEvidence would answer 422 is never sent: the radar fails it first, with the code. The
negative requests MechEvidence publishes are the tests (tests/test_mech_request.py)."""

import hashlib
import re

from bioradar.mech.contract import CONTRACT_V2, binding_hashes

TOP = {
    "contract_version",
    "question",
    "mechanism",
    "scope",
    "materials",
    "claims",
    "joins",
    "expected_evaluator_id",
    "authorization_ref",
    "max_llm_calls",
}
MATERIAL = {"id", "title", "text", "source_kind", "document_type", "text_level", "identifiers", "binding"}
IDENTIFIERS = {"pmid", "doi", "url", "source_id", "record_id", "source_group"}
BINDING = {"sha256", "chars", "segments"}
SEGMENT = {"literal", "part", "start", "end", "sha256", "locators", "tables"}
PART = {
    "kind",
    "source",
    "record_id",
    "version_id",
    "raw_sha256",
    "is_demo",
    "text_sha256",
    "processor_version",
    "parser_version",
    "renderer",
    "from_version_id",
}
DIMENSIONS = ("species", "system", "disease")
SOURCE_KINDS = {"primary", "review", "graph"}
TEXT_LEVELS = {"full", "abstract", "excerpt"}
DOCUMENT_TYPES = {
    "research_article",
    "conference_abstract",
    "trial_results",
    "trial_registration",
    "study_protocol",
    "review",
    "press_release",
    "company_disclosure",
    "news",
    "social_post",
    "graph",
}
UNKNOWN_WORDS = {"", "unknown", "unspecified", "not reported", "not stated", "n/a", "未知", "未说明"}
HEX64 = re.compile(r"[0-9a-f]{64}")


class RequestInvalid(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extra(value, allowed: set, where: str) -> list[str]:
    """Paths of the keys of an object that the contract does not name there."""
    if not isinstance(value, dict):
        return []
    return [f"{where}.{k}" if where else k for k in value if k not in allowed]


def _binding_extras(binding, where: str) -> list[str]:
    found = _extra(binding, BINDING, where)
    segments = binding.get("segments") if isinstance(binding, dict) else None
    for j, segment in enumerate(segments if isinstance(segments, list) else []):
        found += _extra(segment, SEGMENT, f"{where}.segments.{j}")
        if isinstance(segment, dict):
            found += _extra(segment.get("part"), PART, f"{where}.segments.{j}.part")
    return found


def _unknown_fields(raw: dict) -> list[str]:
    """Paths of every field the contract does not name, anywhere in the request."""
    found = _extra(raw, TOP, "")
    scope = raw.get("scope")
    found += _extra(scope, set(DIMENSIONS), "scope")
    for name in DIMENSIONS:
        dim = scope.get(name) if isinstance(scope, dict) else None
        found += _extra(dim, {"state", "value"}, f"scope.{name}")
    materials = raw.get("materials") if isinstance(raw.get("materials"), list) else []
    for i, material in enumerate(materials):
        found += _extra(material, MATERIAL, f"materials.{i}")
        if isinstance(material, dict):
            found += _extra(material.get("identifiers"), IDENTIFIERS, f"materials.{i}.identifiers")
            found += _binding_extras(material.get("binding"), f"materials.{i}.binding")
    return found


def _scope_problems(scope) -> list[tuple[int, str, str]]:
    """(class, code, message): class 0 is a rule of the value, 2 is a missing or mistyped field."""
    if not isinstance(scope, dict):
        return [(2, "scope_state_invalid", "scope must be an object")]
    out = []
    for name in DIMENSIONS:
        dim = scope.get(name)
        if not isinstance(dim, dict) or dim.get("state") not in {"stated", "unknown", "not_required"}:
            out.append((2, "scope_state_invalid", f"scope.{name} needs a state"))
            continue
        value = dim.get("value")
        if dim["state"] == "stated":
            if not isinstance(value, str) or value.strip().casefold() in UNKNOWN_WORDS:
                out.append((0, "scope_state_invalid", f"scope.{name}: state=stated needs a non-empty value"))
        elif value is not None:
            out.append((0, "scope_state_invalid", f"scope.{name}: {dim['state']} must not carry a value"))
    return out


def _part_problems(part, where: str) -> list[tuple[int, str, str]]:
    if not isinstance(part, dict):
        return [(2, "material_binding_invalid", f"{where}: part must be an object")]
    bad = [
        name
        for name in ("kind", "source", "record_id")
        if not isinstance(part.get(name), str) or not 0 < len(part[name]) <= 512
    ]
    bad += [name for name in ("version_id", "raw_sha256", "text_sha256") if not _is_hex(part.get(name))]
    if part.get("from_version_id") is not None and not _is_hex(part["from_version_id"]):
        bad.append("from_version_id")
    if not any(part.get(name) for name in ("processor_version", "parser_version", "renderer")):
        bad.append("processor_version/parser_version/renderer")
    return [(2, "material_binding_invalid", f"{where}: invalid {', '.join(bad)}")] if bad else []


def _is_hex(value) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _segment_problems(segment, where: str) -> list[tuple[int, str, str]]:
    if not isinstance(segment, dict):
        return [(2, "material_binding_invalid", f"{where}: a segment must be an object")]
    if segment.get("literal") is not None:
        others = [k for k in SEGMENT - {"literal"} if segment.get(k) is not None]
        if not isinstance(segment["literal"], str) or segment["literal"] == "" or others:
            return [(2, "material_binding_invalid", f"{where}: a literal segment is {{literal}} only")]
        return []
    start, end = segment.get("start"), segment.get("end")
    ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end))
    if segment.get("part") is None or not ints or not _is_hex(segment.get("sha256")) or not 0 <= start < end:
        return [(2, "material_binding_invalid", f"{where}: a part segment needs part, start<end, sha256")]
    return _part_problems(segment["part"], f"{where}.part")


def _binding_problems(material: dict, where: str) -> list[tuple[int, str, str]]:
    binding = material.get("binding")
    if binding is None:
        return [(1, "material_binding_missing", f"{where}: a material without a binding names no version")]
    segments = binding.get("segments") if isinstance(binding, dict) else None
    if not isinstance(segments, list) or not segments or not _is_hex(binding.get("sha256")):
        return [(2, "material_binding_invalid", f"{where}.binding: sha256 and segments needed")]
    chars = binding.get("chars")
    if not isinstance(chars, int) or isinstance(chars, bool) or chars < 1:
        return [(2, "material_binding_invalid", f"{where}.binding.chars must be a positive integer")]
    problems = []
    for j, s in enumerate(segments):
        problems += _segment_problems(s, f"{where}.binding.segments.{j}")
    if problems:
        return problems
    if not any(s.get("part") is not None for s in segments):
        return [(0, "material_binding_missing", f"{where}: the binding has no part segment")]
    return _binding_text_problems(material, where)


def _binding_text_problems(material: dict, where: str) -> list[tuple[int, str, str]]:
    text, binding, cursor = material["text"], material["binding"], 0
    for index, segment in enumerate(binding["segments"]):
        if segment.get("literal") is not None:
            piece = text[cursor : cursor + len(segment["literal"])]
            ok = piece == segment["literal"]
        else:
            piece = text[cursor : cursor + segment["end"] - segment["start"]]
            ok = len(piece) == segment["end"] - segment["start"] and _sha(piece) == segment["sha256"]
        if not ok:
            return [(0, "material_binding_mismatch", f"{where}: segment {index} does not match the text")]
        cursor += len(piece)
    if cursor != len(text) or binding["chars"] != len(text) or _sha(text) != binding["sha256"]:
        return [(0, "material_binding_mismatch", f"{where}: the binding does not describe the whole text")]
    return []


def _material_problems(material, i: int) -> list[tuple[int, str, str]]:
    where = f"materials.{i}"
    if not isinstance(material, dict):
        return [(2, "input_invalid", f"{where} must be an object")]
    shape = (
        isinstance(material.get("id"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]+", material["id"]) is not None
        and isinstance(material.get("text"), str)
        and material["text"].strip() != ""
        and material.get("source_kind") in SOURCE_KINDS
        and material.get("text_level") in TEXT_LEVELS
        and (material.get("document_type") is None or material["document_type"] in DOCUMENT_TYPES)
    )
    if not shape:
        return [(2, "input_invalid", f"{where}: id, text, source_kind, document_type or text_level invalid")]
    return _binding_problems(material, where)


def _problems(raw: dict) -> list[tuple[int, str, str]]:
    out = []
    for field in ("question", "mechanism"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            out.append((2, "input_invalid", f"{field} must be a non-empty text"))
    out += _scope_problems(raw.get("scope"))
    materials = raw.get("materials")
    if not isinstance(materials, list):
        out.append((2, "input_invalid", "materials must be a list"))
    else:
        for i, material in enumerate(materials):
            out += _material_problems(material, i)
    ref = raw.get("authorization_ref")
    if ref is None:
        out.append((1, "authorization_required", "authorization_ref is required"))
    elif not isinstance(ref, str) or re.fullmatch(r"[\x21-\x7e]{1,512}", ref) is None:
        out.append((2, "input_invalid", "authorization_ref is not an opaque printable string"))
    expected = raw.get("expected_evaluator_id")
    if expected is not None and not _is_hex(expected):
        out.append((2, "input_invalid", "expected_evaluator_id is not a sha256"))
    return out


def check_request(raw) -> None:
    """Raises RequestInvalid(code, message) with the code MechEvidence's service would answer 422 with;
    returns when the request is one it accepts."""
    if not isinstance(raw, dict):
        raise RequestInvalid("input_invalid", "the request body must be a JSON object")
    if raw.get("contract_version") != CONTRACT_V2:
        raise RequestInvalid("contract_version_required", f'contract_version must be "{CONTRACT_V2}"')
    extras = _unknown_fields(raw)
    if extras:
        raise RequestInvalid("field_not_in_contract", "not in contract: " + ", ".join(extras[:10]))
    problems = _problems(raw)
    if problems:
        _, code, message = min(problems, key=lambda p: p[0])  # first of the most specific class
        raise RequestInvalid(code, message)
    seen: dict[str, str] = {}
    ids = [m["id"] for m in raw["materials"]]
    if len(ids) != len(set(ids)):
        raise RequestInvalid("input_invalid", "duplicate material ids")
    for material, value in zip(raw["materials"], binding_hashes(raw)):
        if value in seen:
            message = f"materials {seen[value]} and {material['id']} carry one binding"
            raise RequestInvalid("duplicate_material_binding", message)
        seen[value] = material["id"]
