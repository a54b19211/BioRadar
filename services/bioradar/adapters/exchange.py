import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from bioradar.api.errors import DomainError


class ExchangeValidator:
    """Validate the frozen provider contract AND correlate a response to its call.

    No network retries here: an unknown submission outcome needs reconciliation,
    not a second submission. The full JSON Schema remains the single authority.
    """

    def __init__(self, contract_dir: Path):
        schema = json.loads((contract_dir / "provider-exchange.schema.json").read_text())
        self.validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def validate(
        self, document: dict[str, Any], *, call_id: str, capability: str, input_hash: str
    ) -> dict[str, Any]:
        errors = list(self.validator.iter_errors(document))
        if errors:
            # Do not log jsonschema.message: it can interpolate sensitive input.
            raise DomainError("provider_protocol_error", "供应方结果不符合交换契约", 502)
        if (document["call_id"], document["capability"], document["request"]["input_hash"]) != (
            call_id,
            capability,
            input_hash,
        ):
            raise DomainError("provider_call_mismatch", "供应方结果与本次调用不匹配", 502)
        return copy.deepcopy(document)
