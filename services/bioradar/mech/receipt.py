"""Is a 200 receipt of MechEvidence (mechevidence.evaluate.v2) a well-formed answer? Two passes:

1. Shape: the receipt (and each model call the radar logs) against the JSON Schema MechEvidence publishes at
   GET /v2/contract. The radar fetches it once per service URL and keeps it for the process; when it cannot be
   fetched or is not a v2 receipt schema, nothing is sent (ServiceError mechevidence contract_unavailable /
   contract_invalid). A JSON "integer" here is never a float such as 1.0.
2. Cross-field rules the schema cannot state (an archived answer agrees with itself, the archive covers every
   counted request with global contiguous numbers, rows reference what exists, `complete` agrees with the
   notes). They are MechEvidence's rules (its receipt.py), kept here as the radar reads them; the fixtures in
   tests/fixtures/mech-contract-v2 pin both sides to the same outcomes.

contract.adopt runs both before comparing the receipt with its request."""

import hashlib

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError

from bioradar.adapters import services
from bioradar.adapters.services import ServiceError

CONTRACT_V2 = "mechevidence.evaluate.v2"
# A raw model answer is archived up to this many bytes; a longer one is cut and marked truncated.
OUTPUT_ARCHIVE_LIMIT = 256 * 1024
INCOMPLETE_CODES = ("pending_budget", "review_failed")

_TYPES = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda _checker, value: isinstance(value, int) and not isinstance(value, bool)
)
_Validator = validators.extend(Draft202012Validator, type_checker=_TYPES)
_published: dict[str, dict] = {}  # service URL -> {"receipt": validator, "call": validator, "answer": ...}


def load(answer) -> dict:
    """The validators of a GET /v2/contract answer; ServiceError contract_invalid if it is not one."""
    schema = answer.get("receipt_schema") if isinstance(answer, dict) else None
    if answer is None or not isinstance(schema, dict) or answer.get("contract_version") != CONTRACT_V2:
        raise ServiceError("mechevidence", "contract_invalid")
    try:
        _Validator.check_schema(schema)
        call = {"$ref": "#/$defs/llm_call", "$defs": schema["$defs"]}
        _Validator.check_schema(call)
    except (SchemaError, KeyError, TypeError):
        raise ServiceError("mechevidence", "contract_invalid") from None
    return {"receipt": _Validator(schema), "call": _Validator(call), "answer": answer}


def published(settings) -> dict:
    """MechEvidence's receipt format (GET /v2/contract), fetched once per service URL."""
    url = settings.mechevidence_url or ""
    if url not in _published:
        try:
            answer = services.mechevidence_contract(settings)
        except ServiceError as exc:
            raise ServiceError("mechevidence", "contract_unavailable", exc.code) from exc
        _published[url] = load(answer)
    return _published[url]


def _problems(validator, value, root: str) -> list[str]:
    found = []
    for error in validator.iter_errors(value):
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
        found.append(f"{root}{path}: {error.message}"[:500])
    return found


def shape_problems(receipt, schema: dict) -> list[str]:
    """Empty = the receipt has the published shape."""
    return _problems(schema["receipt"], receipt, "receipt")


def call_shape_problems(call, schema: dict) -> list[str]:
    """Empty = this one model call (of a receipt or of an error answer) has the published shape."""
    return _problems(schema["call"], call, "call")


# ---- cross-field rules (run only on a receipt whose shape is right) ----


def _answer(where: str, out: dict) -> list[str]:
    problems = []
    status, content, digest, error = out["status"], out["content"], out["sha256"], out["error"]
    if out["delivered"] == "yes":
        if status is None or content is None or digest is None:
            return [f"{where} was answered but lacks status, text or sha256"]
        if error != (None if 200 <= status < 300 else f"http_{status}"):
            problems.append(f"{where}.error {error!r} does not agree with HTTP status {status}")
        if out["truncated"]:
            if len(content.encode("utf-8")) > OUTPUT_ARCHIVE_LIMIT:
                problems.append(f"{where}.content is longer than the archive limit")
        elif hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            problems.append(f"{where}.content does not hash to its sha256")
    else:
        if status is not None or content is not None or digest is not None or out["truncated"]:
            problems.append(f"{where} was not answered but carries a status, text, hash or truncation")
        if error is None:
            problems.append(f"{where} was not answered and names no error")
    return problems


def _archive(calls: list[dict]) -> list[str]:
    problems, expected_index, seen_ids = [], 1, set()
    for number, call in enumerate(calls):
        where, outputs = f"llm_calls[{number}]", call["outputs"]
        if len(outputs) != call["requests"]:
            problems.append(f"{where} counts {call['requests']} requests but archives {len(outputs)} outputs")
        for position, out in enumerate(outputs, 1):
            spot = f"{where}.outputs[{position - 1}]"
            if out["attempt"] != position:
                problems.append(
                    f"{spot}.attempt is {out['attempt']}, expected {position} (contiguous from 1)"
                )
            if out["index"] != expected_index:
                problems.append(
                    f"{spot}.index is {out['index']}, expected {expected_index} (global, contiguous from 1)"
                )
            expected_index += 1
            if not out["attempt_id"] or not out["decision_id"] or out["attempt_id"] in seen_ids:
                problems.append(f"{spot} lacks a unique attempt_id or a decision_id")
            seen_ids.add(out["attempt_id"])
            problems += _answer(spot, out)
        if call["error"] is None and not (
            outputs and outputs[-1]["delivered"] == "yes" and outputs[-1]["status"] == 200
        ):
            problems.append(f"{where} succeeded but its last request was not answered 200")
    return problems


def _ids(receipt: dict) -> tuple[dict, dict, list[str]]:
    problems, observations, targets = [], {}, {}
    for row in receipt["observations"]:
        if row["id"] in observations:
            problems.append(f"observation id {row['id']} is used twice")
        observations[row["id"]] = row
    for name in ("claims", "joins"):
        for row in receipt[name]:
            if row["id"] in targets:
                problems.append(f"{name} id {row['id']} is used twice")
            targets[row["id"]] = row
    return observations, targets, problems


def _references(receipt: dict, material_ids: list[str]) -> list[str]:
    observations, targets, problems = _ids(receipt)
    for row in receipt["joins"]:
        if row["from"] not in targets or row["to"] not in targets:
            problems.append(f"join {row['id']} links {row['from']} -> {row['to']}, which do not exist")
    for row in targets.values():
        for field in ("supporting", "opposing", "compatible"):
            problems += [
                f"{row['id']}.{field} cites observation {oid}, which does not exist"
                for oid in row[field]
                if oid not in observations
            ]
    for row in observations.values():
        if row["material"] not in material_ids:
            problems.append(
                f"observation {row['id']} cites material {row['material']}, which is not in the request"
            )
        if row["passage"].split(".")[0] != row["material"]:
            problems.append(f"observation {row['id']} cites passage {row['passage']} of another material")
        problems += [
            f"observation {row['id']} targets {target}, which does not exist"
            for target in row["targets"]
            if target not in targets
        ]
    problems += [
        f"a note cites material {note['material']}, which is not in the request"
        for note in receipt["material_notes"]
        if note["material"] is not None and note["material"] not in material_ids
    ]
    return problems


def consistency_problems(receipt: dict, scope: dict, material_ids: list[str]) -> list[str]:
    """Cross-field problems of a receipt whose shape is right. `scope` is the request's normalised scope,
    which the receipt must echo; `material_ids` are the request's material ids."""
    problems = []
    if receipt["scope"] != scope:
        problems.append("scope does not echo the request's scope")
    reasons = sorted({note["code"] for note in receipt["material_notes"] if note["code"] in INCOMPLETE_CODES})
    if receipt["not_complete_because"] != reasons or receipt["complete"] is not (not reasons):
        problems.append("complete / not_complete_because disagree with material_notes")
    for row in receipt["materials"]:
        if row["locators_returned"] != sum(len(item["locators"]) for item in row["locators"]):
            problems.append(f"material {row['id']}: locators_returned does not count its locators")
    return problems + _archive(receipt["llm_calls"]) + _references(receipt, material_ids)
