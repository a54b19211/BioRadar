"""The formats MechEvidence (mechevidence.evaluate.v2) publishes at GET /v2/contract, as JSON Schemas
the radar checks answers against with the generic jsonschema library: a receipt, one model call, and the
POST /v2/reuse-key answer. The radar keeps no copy of MechEvidence's rules: the evaluator id, the reuse key,
the literature identity and the receipt's cross-field rules are MechEvidence's alone (it refuses to return a
receipt that breaks them).

The formats are fetched once per service URL and kept for the process; when they cannot be fetched or are not
v2 formats, nothing is sent (ServiceError mechevidence contract_unavailable / contract_invalid). A JSON
"integer" here is never a float such as 1.0."""

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError

from bioradar.adapters import services
from bioradar.adapters.services import ServiceError

CONTRACT_V2 = "mechevidence.evaluate.v2"

_TYPES = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda _checker, value: isinstance(value, int) and not isinstance(value, bool)
)
_Validator = validators.extend(Draft202012Validator, type_checker=_TYPES)
_published: dict[str, dict] = {}  # service URL -> {"receipt", "call", "reuse_key": validators, "answer"}


def load(answer) -> dict:
    """The validators of a GET /v2/contract answer; ServiceError contract_invalid if it is not one."""
    ok = isinstance(answer, dict) and answer.get("contract_version") == CONTRACT_V2
    schema = answer.get("receipt_schema") if ok else None
    keyed = answer.get("reuse_key_schema") if ok else None
    if not isinstance(schema, dict) or not isinstance(keyed, dict):
        raise ServiceError("mechevidence", "contract_invalid")
    try:
        call = {"$ref": "#/$defs/llm_call", "$defs": schema["$defs"]}
        for each in (schema, call, keyed):
            _Validator.check_schema(each)
    except (SchemaError, KeyError, TypeError):
        raise ServiceError("mechevidence", "contract_invalid") from None
    return {
        "receipt": _Validator(schema),
        "call": _Validator(call),
        "reuse_key": _Validator(keyed),
        "answer": answer,
    }


def published(settings) -> dict:
    """MechEvidence's formats (GET /v2/contract), fetched once per service URL."""
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


def reuse_key_problems(answer, schema: dict) -> list[str]:
    """Empty = a POST /v2/reuse-key answer has the published shape."""
    return _problems(schema["reuse_key"], answer, "reuse_key_answer")
