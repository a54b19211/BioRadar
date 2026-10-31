"""Finite synthetic proposal layouts; no source/oracle/model requests or relaxed qualification."""

import copy
import hashlib
import json

import pytest
from bioradar.research.datasets import narrative_v2
from bioradar.research.datasets.narrative_proposal import (
    PROPOSAL_VERSION,
    proposal_payload,
    proposal_schema,
)
from bioradar.research.mining.models import canonical_hash
from jsonschema import Draft202012Validator
from test_opus_narrative import prose

pytestmark = pytest.mark.ENGINE


def span(text, quote, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {"text": quote, "start": start, "end": start + len(quote)}


def source(text):
    return prose("SYNTHETIC-proposal-contract-only", text)


def scalar():
    text = "合成云桐公司2025年H1净订单为14.5万元。"
    row = {
        "subject": span(text, "合成云桐公司"),
        "metric": span(text, "净订单"),
        "value": span(text, "14.5万元"),
        "unit": None,
        "period": span(text, "2025年H1"),
        "context": span(text, text),
    }
    return text, [row]


def columns(*, full_periods=False):
    text = (
        "合成云桐公司2021年H1/2022年H1净订单分别为7/11万元。"
        if full_periods
        else "合成云桐公司2021/2022年H1净订单分别为7/11万元。"
    )
    periods = [span(text, str(year) + ("年H1" if full_periods else "")) for year in (2021, 2022)]
    values = [span(text, value) for value in ("7", "11")]
    rows = []
    for index in range(2):
        rows.append(
            {
                "subject": span(text, "合成云桐公司"),
                "metric": span(text, "净订单"),
                "value": copy.deepcopy(values[index]),
                "unit": span(text, "万元"),
                "period": copy.deepcopy(periods[index]) if full_periods else span(text, "H1"),
                "period_year": None if full_periods else copy.deepcopy(periods[index]),
                "context": span(text, text),
                "ordered_pair": {"periods": copy.deepcopy(periods), "values": copy.deepcopy(values)},
            }
        )
    return text, rows


def validate(text, rows):
    return narrative_v2.validate({"observations": rows}, source(text))


def issue_codes(result):
    return {issue["reason"] for gap in result["gaps"] for issue in gap["issues"]}


def test_new_schema_makes_context_explicit_but_old_schema_and_payload_are_exactly_unchanged():
    text, rows = scalar()
    record = source(text)
    old_schema = narrative_v2.schema()
    old_payload = narrative_v2.selection_payload(record)
    before = canonical_hash([old_schema, old_payload, record.model_dump(mode="json")])
    new_schema = proposal_schema()
    new_payload = proposal_payload(record)
    Draft202012Validator.check_schema(new_schema)
    assert new_schema["title"] == "NarrativeProposalV1"
    assert new_schema["required"] == ["observations"]
    row = new_schema["$defs"]["NarrativeObservationV2"]
    assert row["required"] == ["subject", "metric", "value", "period", "context"]
    assert row["properties"]["context"]["$ref"] == "#/$defs/LiteralSpan"
    assert "anyOf" not in row["properties"]["context"]
    assert all(value.get("description") for value in row["properties"].values())
    assert new_payload["proposal_version"] == PROPOSAL_VERSION
    assert new_payload["qualification_contract_version"] == narrative_v2.VERSION
    assert new_payload["text"] == old_payload["text"]
    assert new_payload["record_ref"] == old_payload["record_ref"]
    assert new_payload["allowed_metric_roles"] == old_payload["allowed_metric_roles"]
    assert (
        canonical_hash(
            [narrative_v2.schema(), narrative_v2.selection_payload(record), record.model_dump(mode="json")]
        )
        == before
    )
    # Exact schema fingerprint from the frozen v2 contract, not a new permissive schema.
    encoded = json.dumps(old_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert (
        hashlib.sha256(encoded).hexdigest()
        == "cbf39ee669a89f9c985c0f3b51c52336f5a2ae98c4e14619c2fe373d53b3ff7a"
    )
    omitted = copy.deepcopy(rows)
    omitted[0].pop("context")
    assert Draft202012Validator(old_schema).is_valid({"observations": omitted})
    assert not Draft202012Validator(new_schema).is_valid({"observations": omitted})
    assert Draft202012Validator(new_schema).is_valid({"observations": []})


@pytest.mark.parametrize(
    "layout",
    [
        "attached_unit",
        "duplicate_unit",
        "bare_plus_unit",
        "year_columns",
        "missing_context",
        "missing_year",
        "whole_period_columns",
        "split_whole_period_column",
    ],
)
def test_eight_existing_shapes_use_the_same_hard_v2_qualification(layout):
    expected = 1
    if layout in {"attached_unit", "duplicate_unit", "bare_plus_unit"}:
        text, rows = scalar()
        if layout in {"duplicate_unit", "bare_plus_unit"}:
            rows[0]["unit"] = span(text, "万元")
        if layout == "bare_plus_unit":
            rows[0]["value"] = span(text, "14.5")
        expected = 0 if layout == "duplicate_unit" else 1
    else:
        full = layout in {"whole_period_columns", "split_whole_period_column"}
        text, rows = columns(full_periods=full)
        expected = 2
        if layout == "missing_context":
            for row in rows:
                row.pop("context")
            expected = 0
        elif layout == "missing_year":
            for row in rows:
                row.pop("period_year")
            expected = 0
        elif layout == "split_whole_period_column":
            for index, row in enumerate(rows):
                row.update(period=span(text, "H1", index), period_year=span(text, str(2021 + index)))
            expected = 0
    before = canonical_hash([text, rows])
    result = validate(text, rows)
    assert len(result["observations"]) == expected
    assert bool(result["gaps"]) == (expected == 0)
    assert canonical_hash([text, rows]) == before
    if layout == "duplicate_unit":
        assert "separate_unit_requires_single_numeric_token" in issue_codes(result)
    elif layout == "missing_context":
        assert "ordered_columns_outside_context" in issue_codes(result)
    elif layout == "split_whole_period_column":
        assert "selected_period_and_value_are_not_same_column" in issue_codes(result)


def test_two_fully_synthetic_examples_are_exactly_located_and_pass_the_actual_unchanged_validator():
    text, _ = scalar()
    data = proposal_payload(source(text))
    examples = data["synthetic_examples"]
    assert len(examples) == 2
    assert {example["layout"] for example in examples} == {
        "whole_period_columns",
        "year_columns_with_shared_half_year",
    }
    schema = Draft202012Validator(proposal_schema())
    for example in examples:
        assert example["is_synthetic_example"] is True
        schema.validate(example["output"])
        result = validate(example["text"], example["output"]["observations"])
        assert not result["gaps"] and len(result["observations"]) == 2
        for row in example["output"]["observations"]:
            for key, literal in narrative_v2._locator_fields(
                narrative_v2.NarrativeObservationV2.model_validate(row)
            ):
                assert example["text"][literal.start : literal.end] == literal.text
            pair = row["ordered_pair"]
            selected = row["period"] if row["period"] in pair["periods"] else row["period_year"]
            assert pair["periods"].index(selected) == pair["values"].index(row["value"])
            assert row["context"]["text"] == example["text"]


def test_incomplete_period_columns_unsupported_units_and_generic_subject_are_still_rejected():
    text, rows = columns()
    cases = []
    wrong_columns = copy.deepcopy(rows[:1])
    wrong_columns[0]["ordered_pair"]["periods"].reverse()
    cases.append((text, wrong_columns, "columns_are_not_original_ordered_lists"))
    text, rows = scalar()
    wrong_unit = copy.deepcopy(rows)
    wrong_unit[0].update(value=span(text, "14.5"), unit=span(text, "万"))
    cases.append((text, wrong_unit, "unit_or_currency_not_explicit"))
    generic_subject = copy.deepcopy(rows)
    generic_subject[0]["subject"] = span(text, "公司")
    cases.append((text, generic_subject, "generic_party_is_not_a_named_business_subject"))
    for text, rows, reason in cases:
        # This helper does not normalize, repair or promote model proposals.
        before = copy.deepcopy(rows)
        result = validate(text, rows)
        assert not result["observations"] and reason in issue_codes(result)
        assert rows == before


def test_context_must_cover_actual_named_subject_and_values_even_when_schema_shape_is_legal():
    text = "合成云桐公司\n报告期：2025年H1\n净订单为14.5万元。"
    row = {
        "subject": span(text, "合成云桐公司"),
        "metric": span(text, "净订单"),
        "value": span(text, "14.5万元"),
        "unit": None,
        "period": span(text, "2025年H1"),
        "context": span(text, "净订单为14.5万元。"),
    }
    Draft202012Validator(proposal_schema()).validate({"observations": [row]})
    invalid = validate(text, [row])
    assert not invalid["observations"]
    assert "subject_metric_value_not_in_local_context" in issue_codes(invalid)
    row["context"] = span(text, text)
    valid = validate(text, [row])
    assert len(valid["observations"]) == 1 and not valid["gaps"]


@pytest.mark.parametrize("unknown", [None, "future-unsupported", True])
def test_unknown_proposal_version_never_falls_back_to_legacy_or_generates_a_new_request(unknown):
    text, _ = scalar()
    with pytest.raises(ValueError, match="unsupported literal narrative proposal version"):
        proposal_schema(version=unknown)
    with pytest.raises(ValueError, match="unsupported literal narrative proposal version"):
        proposal_payload(source(text), version=unknown)
