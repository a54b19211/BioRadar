"""SEC unit/row projection is source-bound; fixtures are authored, with no HTTP or models."""
import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest
from bioradar.adapters.fabric_history import validate_record
from bioradar.api.errors import DomainError

pytestmark = pytest.mark.ENGINE


def record():
    original = {
        "label": "SYNTHETIC source rows",
        "units": {
            "USD": [
                {"val": 1, "start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-01"},
                {"val": 2, "start": "2025-01-01", "end": "2025-06-30", "form": "10-Q"},
            ],
            "USD/shares~basic": [{"val": 0.25, "end": "2025-03-31", "custom/key~": "fixed"}],
        },
    }
    observations = []
    for unit, rows in original["units"].items():
        escaped = unit.replace("~", "~0").replace("/", "~1")
        for index, row in enumerate(rows):
            path = f"/units/{escaped}/{index}"
            observations.append({
                **copy.deepcopy(row),
                "unit": unit,
                "unit_locator": {"json_pointer": "/units", "object_key": unit},
                "source_row_pointer": path,
                "field_locators": {
                    key: {"json_pointer": path + "/" + key.replace("~", "~0").replace("/", "~1")}
                    for key in row
                },
            })
    raw = json.dumps(original, ensure_ascii=False)
    return {
        "schema_version": "datafabric.history.record.v1", "source_id": "sec_companyfacts",
        "is_demo": True, "source_record_id": "SYNTHETIC:metric", "version_id": "a" * 64,
        "record_kind": "document", "raw_content": raw, "raw_media_type": "application/json",
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(), "clean_text": "SYNTHETIC fixture",
        "parser_version": "SYNTHETIC projection fixture", "field_locators": {},
        "facts": {"financial_observations": observations},
        "first_seen_at": "2025-06-01T00:00:00Z", "received_at": "2025-06-01T00:00:00Z",
    }


def validate(value):
    return validate_record(value, value["source_id"], True, "current", datetime(2026, 1, 1, tzinfo=UTC))


def test_native_sec_financial_projection_binds_every_row_unit_and_escaped_pointer():
    value = record()
    before = copy.deepcopy(value)
    assert validate(value) is value and value == before
    observations = value["facts"]["financial_observations"]
    assert len(observations) == 3
    assert observations[-1]["source_row_pointer"] == "/units/USD~1shares~0basic/0"
    assert observations[-1]["field_locators"]["custom/key~"]["json_pointer"].endswith("/custom~1key~0")


def test_native_row_order_can_change_without_changing_bindings():
    value = record()
    value["facts"]["financial_observations"].reverse()
    assert validate(value) is value


@pytest.mark.parametrize(
    "mutation",
    [
        "value", "number_type", "unit", "unit_object_key", "row_pointer", "field_pointer",
        "omitted_row", "duplicate_row", "extra_field", "omitted_field", "omitted_locator",
        "foreign_source", "native_rows_not_list", "duplicate_original_key",
    ],
)
def test_sec_projection_rejects_partial_or_forged_native_bindings(mutation):
    value = record()
    observations = value["facts"]["financial_observations"]
    first = observations[0]
    if mutation == "value":
        first["val"] = 999
    elif mutation == "number_type":
        first["val"] = True
    elif mutation == "unit":
        first["unit"] = "EUR"
    elif mutation == "unit_object_key":
        first["unit_locator"]["object_key"] = "EUR"
    elif mutation == "row_pointer":
        first["source_row_pointer"] = "/units/USD/1"
    elif mutation == "field_pointer":
        first["field_locators"]["val"]["json_pointer"] = "/units/USD/1/val"
    elif mutation == "omitted_row":
        observations.pop()
    elif mutation == "duplicate_row":
        observations.append(copy.deepcopy(first))
    elif mutation == "extra_field":
        first["derived_population"] = "invented"
    elif mutation == "omitted_field":
        first.pop("filed")
    elif mutation == "omitted_locator":
        first["field_locators"].pop("val")
    elif mutation == "foreign_source":
        value["source_id"] = "ordinary_json_source"
    elif mutation == "native_rows_not_list":
        native = json.loads(value["raw_content"])
        native["units"]["USD"] = {"0": native["units"]["USD"][0]}
        value["raw_content"] = json.dumps(native)
        value["raw_sha256"] = hashlib.sha256(value["raw_content"].encode()).hexdigest()
    else:
        value["raw_content"] = '{"units":{},"units":{}}'
        value["raw_sha256"] = hashlib.sha256(value["raw_content"].encode()).hexdigest()
    with pytest.raises(DomainError) as error:
        validate(value)
    assert error.value.code == "history_source_invalid"
