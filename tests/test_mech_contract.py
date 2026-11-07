"""The shared MechEvidence v2 contract (extraction_contract.mech_v2) as the radar uses it, against everything
MechEvidence publishes about it: its fixtures (request, receipt, evaluator, 11 reuse-key cases, 51 receipt
outcomes, service errors) and its literature identity contract. A change of the shared package that the
fixtures do not follow shows here first."""

import hashlib
import json
from pathlib import Path

import pytest
from extraction_contract.mech_v2 import IdentityError, source_identity

from bioradar.mech import contract
from bioradar.mech.contract import UnsupportedRequest, adopt, evaluator_id_of, material_echo, reuse_key

FIXTURES = Path(__file__).parent / "fixtures" / "mech-contract-v2"
# MechEvidence r2/mech-v2 at 5bb6383: examples/contract-v2/*.json and examples/identity_contract.json
PINNED = {
    "evaluator.json": "6d7918e8ba0ac0380f90c1f1e4a295274abb07e63e3c1999a3f24fcb6071322c",
    "negative_requests.json": "64e59dc086f34e079ddd7b0700b82eae02e81d1eecc7064fb6b6e5260fd6f939",
    "receipt.json": "751abdf4d069303c36c72065136000653069dd878a919f58f804129d953a9a48",
    "receipt_outcomes.json": "ebdef7304b5f75c5d9f7a534a532a28e50f55021bc0d56770e977d8f44d9d1fe",
    "request.json": "469a5af0d57f4554c0caf800ba6e1efe798e249a2fcdb8996bb62c65c98b1d61",
    "reuse_keys.json": "cf8831bb9c5391566a25550b4627830a94f81ee6112134207030bf206a539f37",
    "service_errors.json": "12a8bba536ce5826aee5ce0065f3702f2b4a058e1fb4452f81e0ebf21cf22f54",
    "identity_contract.json": "55a6965de32ba09d89642441392624dd9556249523d223a3d5e095d6920bd864",
}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_the_fixtures_and_the_receipt_rules_are_the_ones_mechevidence_published():
    assert {p.name for p in FIXTURES.glob("*.json")} == set(PINNED)
    for name, sha in PINNED.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha, name


def test_the_evaluator_id_is_the_hash_of_the_fields_it_covers():
    evaluator = load("evaluator.json")["evaluator"]
    assert evaluator_id_of(evaluator) == evaluator["evaluator_id"]
    audit_only = dict(evaluator, code_version="another build")
    assert evaluator_id_of(audit_only) == evaluator["evaluator_id"]  # code_version is not in the id
    assert evaluator_id_of(dict(evaluator, batch_size=4)) != evaluator["evaluator_id"]


def test_the_committed_request_has_the_key_and_the_echo_of_its_receipt():
    request, receipt = load("request.json"), load("receipt.json")
    assert reuse_key(request, receipt["evaluator"]) == receipt["reuse_key"]
    assert material_echo(request) == receipt["materials"]


def test_the_reuse_keys_of_the_published_cases():
    document = load("reuse_keys.json")
    evaluator = load("evaluator.json")["evaluator"]
    cases = [("base", document["base"])] + [(case["name"], case) for case in document["cases"]]
    assert len(cases) == 12
    for name, case in cases:
        got = reuse_key(case["request"], dict(evaluator, evaluator_id=case["evaluator_id"]))
        assert got == case["reuse_key"], name
    keys = {name: case["reuse_key"] for name, case in cases}
    for case in document["cases"]:
        assert (keys[case["name"]] != keys["base"]) is case["differs_from_base"], case["name"]


def test_the_radar_asks_for_no_decomposition_of_its_own():
    request = dict(load("request.json"), claims=[{"text": "x", "kind": "causal"}])
    with pytest.raises(UnsupportedRequest):
        reuse_key(request, load("evaluator.json")["evaluator"])


INVALID = ("failed", "receipt_invalid")
EXPECTED = {
    "exact": ("completed", None),
    "reuse_key_differs": ("failed", "receipt_mismatch"),
    "binding_echo_swapped": ("failed", "receipt_mismatch"),
    "binding_echo_other_text": ("failed", "receipt_mismatch"),
    "evaluator_id_differs": ("failed", "receipt_mismatch"),
    "evaluator_fields_edited_id_kept": ("failed", "receipt_mismatch"),
    "not_complete_budget_cut": ("completed", None),
    "audit_incomplete": ("failed", "audit_incomplete"),
    "truncated_answer": ("failed", "audit_incomplete"),
    "outputs_missing": INVALID,
    "output_text_edited_hash_kept": INVALID,
    "output_hash_not_hex": INVALID,
    "requests_more_than_outputs": INVALID,
    "outputs_more_than_requests": INVALID,
    "attempt_numbers_not_contiguous": INVALID,
    "unanswered_attempt_carries_an_answer": INVALID,
    "successful_call_not_answered_200": INVALID,
    "llm_calls_not_a_list": INVALID,
    "invalid_overall_verdict": INVALID,
    "invalid_claim_verdict": INVALID,
    "invalid_join_verdict": INVALID,
    "invalid_observation_relation": INVALID,
    "complete_false_without_a_reason": INVALID,
    "complete_true_with_a_budget_note": INVALID,
    "scope_echo_changed": INVALID,
    "required_field_missing": INVALID,
    "assessability_status_invalid": INVALID,
    "stage_is_a_list": INVALID,
    "delivered_is_a_list": INVALID,
    "attempt_is_a_bool": INVALID,
    "attempt_is_a_float": INVALID,
    "claim_verdict_is_a_list": INVALID,
    "observation_relation_is_an_object": INVALID,
    "observation_cites_a_material_outside_the_request": INVALID,
    "observation_passage_of_another_material": INVALID,
    "observation_targets_a_missing_claim": INVALID,
    "claim_cites_a_missing_observation": INVALID,
    "echo_has_an_extra_non_object_element": INVALID,
    "echo_has_an_extra_object_element": ("failed", "receipt_mismatch"),
    "echo_locators_changed": ("failed", "receipt_mismatch"),
    "success_with_an_unanswered_output": INVALID,
    "answered_200_but_error_set": INVALID,
    "answered_without_text": INVALID,
    "global_index_not_contiguous": INVALID,
    "attempt_id_reused": INVALID,
    "decision_id_empty": INVALID,
    "attempt_id_missing": INVALID,
    "reuse_key_is_a_number": INVALID,
    "receipt_is_a_list": INVALID,
    "receipt_is_a_string": INVALID,
    "receipt_is_null": INVALID,
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_what_the_radar_does_with_each_receipt(name):
    """A receipt that is not for its request is failed/receipt_mismatch, one of the wrong shape or with
    contradicting fields failed/receipt_invalid, an incomplete audit failed too; the raw receipt is kept for
    audit and no conclusion is usable. complete=false is adoptable for this analysis but never cacheable."""
    document = load("receipt_outcomes.json")
    case = next(item for item in document["cases"] if item["name"] == name)
    outcome = adopt(document["request"], case["receipt"], document["expected_evaluator_id"])
    assert json.loads(json.dumps(outcome)) == case["outcome"]
    status, reason = EXPECTED[name]
    assert outcome["result"]["status"] == status and outcome["result"].get("reason") == reason
    if status == "failed":
        assert outcome["rejected"]["response"] == case["receipt"] and "response" not in outcome["result"]
        if reason == "receipt_mismatch":
            assert outcome["rejected"]["mismatch"] == outcome["result"]["mismatch"] != []
        if reason == "receipt_invalid":
            assert outcome["rejected"]["invalid"] == outcome["result"]["invalid"] != []
    else:
        assert outcome["rejected"] is None
        assert outcome["result"]["cacheable"] is (name == "exact")


def test_every_published_outcome_is_asserted():
    assert len(EXPECTED) == 51
    assert {case["name"] for case in load("receipt_outcomes.json")["cases"]} == set(EXPECTED)


def test_a_receipt_of_no_known_shape_never_raises():
    request = load("request.json")
    for receipt in (None, 7, "x", [], {}, {"llm_calls": 3}, {"evaluator": []}):
        outcome = adopt(request, receipt, "0" * 64)
        assert outcome["result"]["status"] == "failed" and outcome["rejected"]["response"] == receipt


# ---- the literature identity the key depends on ----

IDENTITY = load("identity_contract.json")


@pytest.mark.parametrize("row", IDENTITY["accepted"], ids=lambda row: row["name"])
def test_accepted_identifiers(row):
    assert source_identity(row["fields"]) == row["identity"]


@pytest.mark.parametrize("row", IDENTITY["unrecognized"], ids=lambda row: row["name"])
def test_pseudo_urls_and_free_text_are_not_identifiers(row):
    identity = source_identity(row["fields"])
    if row["identity_prefix"] == "text:":
        assert identity is None  # MechEvidence then groups the material by its own text
    else:
        assert identity.startswith(row["identity_prefix"])


@pytest.mark.parametrize("row", IDENTITY["rejected"], ids=lambda row: row["name"])
def test_conflicting_or_invalid_identifiers_are_an_error(row):
    with pytest.raises(IdentityError) as caught:
        source_identity(row["fields"])
    assert caught.value.code == row["error_code"] and caught.value.kind == row["identifier_type"]


def test_cross_source_pairs_and_a_boolean_pmid():
    for row in IDENTITY["pairs"]:
        assert (source_identity(row["left"]) == source_identity(row["right"])) == row["same_group"], row[
            "name"
        ]
    with pytest.raises(IdentityError):
        source_identity({"pmid": True})


def test_an_identity_error_is_part_of_the_key_not_a_crash():
    request = load("request.json")
    evaluator = load("evaluator.json")["evaluator"]
    broken = json.loads(json.dumps(request))
    broken["materials"][1]["identifiers"] = {"pmid": "111", "url": "https://pubmed.ncbi.nlm.nih.gov/222/"}
    key = reuse_key(broken, evaluator)
    assert key != reuse_key(request, evaluator)
    assert contract.material_inputs(broken)[1]["identity"] == "error:material_identity_conflict"
