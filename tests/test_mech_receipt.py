"""MechEvidence's formats (GET /v2/contract) are fetched, never assumed, and checked with the generic
jsonschema library."""

import json
from pathlib import Path

import pytest

from bioradar.adapters import services
from bioradar.adapters.services import ServiceError
from bioradar.mech import receipt as receipt_format

FIXTURES = Path(__file__).parent / "fixtures" / "mech-contract-v2"


def load(name):
    return json.loads((FIXTURES / name).read_text("utf-8"))


def test_the_formats_come_from_mechevidence_and_their_absence_stops_the_send(monkeypatch):
    settings = type("S", (), {"mechevidence_url": "http://mech.test", "mechevidence_token": "t"})()
    monkeypatch.setattr(receipt_format, "_published", {})
    asked = []
    monkeypatch.setattr(services, "mechevidence_contract", lambda s: asked.append(1) or load("contract.json"))
    first = receipt_format.published(settings)
    assert receipt_format.published(settings) is first and asked == [1]  # fetched once

    monkeypatch.setattr(receipt_format, "_published", {})

    def down(s):
        raise ServiceError("mechevidence", "unreachable")

    monkeypatch.setattr(services, "mechevidence_contract", down)
    with pytest.raises(ServiceError) as caught:
        receipt_format.published(settings)
    assert caught.value.code == "contract_unavailable"
    contract = load("contract.json")
    for answer in (
        None,
        {},
        dict(contract, contract_version="mechevidence.evaluate.v1"),
        dict(contract, receipt_schema={"type": 7}),
        {k: v for k, v in contract.items() if k != "reuse_key_schema"},
    ):
        with pytest.raises(ServiceError) as caught:
            receipt_format.load(answer)
        assert caught.value.code == "contract_invalid"


def test_a_receipt_and_one_model_call_are_checked_against_the_published_format():
    schema = receipt_format.load(load("contract.json"))
    receipt = load("receipt.json")
    assert receipt_format.shape_problems(receipt, schema) == []
    assert receipt_format.shape_problems(dict(receipt, reuse_key=7), schema)
    call = receipt["llm_calls"][0]
    assert receipt_format.call_shape_problems(call, schema) == []
    broken = dict(call, requests="1", outputs=[dict(call["outputs"][0], attempt=1.0)])
    del broken["stage"]
    assert len(receipt_format.call_shape_problems(broken, schema)) == 3
