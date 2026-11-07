"""A request the radar sends is checked first by the rules MechEvidence's service applies: each of the 19
negative requests MechEvidence publishes is refused with its error code, and every request it publishes as
valid passes."""

import copy
import json
from pathlib import Path

import pytest

from bioradar.mech.request import RequestInvalid, check_request

FIXTURES = Path(__file__).parent / "fixtures" / "mech-contract-v2"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


NEGATIVE = load("negative_requests.json")


def test_there_are_nineteen_negative_requests():
    assert len(NEGATIVE) == 19 and len({case["name"] for case in NEGATIVE}) == 19


@pytest.mark.parametrize("case", NEGATIVE, ids=lambda case: case["name"])
def test_a_request_mechevidence_would_refuse_is_not_sent(case):
    with pytest.raises(RequestInvalid) as refused:
        check_request(case["request"])
    assert refused.value.code == case["error_code"]
    assert case.get("message_contains", "") in refused.value.message


def test_every_request_mechevidence_publishes_as_valid_passes():
    check_request(load("request.json"))
    check_request(load("receipt_outcomes.json")["request"])
    document = load("reuse_keys.json")
    for case in [document["base"], *document["cases"]]:
        check_request(case["request"])


def test_each_way_to_break_a_valid_request_is_caught():
    base = load("request.json")

    def broken(change):
        request = copy.deepcopy(base)
        change(request)
        with pytest.raises(RequestInvalid) as refused:
            check_request(request)
        return refused.value.code

    assert broken(lambda r: r["materials"][0].update(source_kind="secondary")) == "input_invalid"
    assert broken(lambda r: r["materials"][1].update(id=r["materials"][0]["id"])) == "input_invalid"
    assert broken(lambda r: r.update(authorization_ref="has a space")) == "input_invalid"
    assert broken(lambda r: r.update(expected_evaluator_id="abc")) == "input_invalid"
    assert broken(lambda r: r["scope"]["species"].update(extra=1)) == "field_not_in_contract"
    assert broken(lambda r: r["materials"][1]["identifiers"].update(isbn="1")) == "field_not_in_contract"
    assert (
        broken(lambda r: r["materials"][0].update(text=r["materials"][0]["text"] + " "))
        == "material_binding_mismatch"
    )
    assert broken(lambda r: r["materials"][0]["binding"].update(chars=1)) == "material_binding_mismatch"
    assert broken(lambda r: r["materials"][0]["binding"].update(chars=0)) == "material_binding_invalid"
