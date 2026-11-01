"""Offline generic ID-only final repair; no real cases, transport or database."""

import copy
import hashlib
import json

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import freeze_passages, validate_synthesis
from bioradar.research.investigations.analysis_models import FinalResearchSynthesis
from bioradar.research.investigations.research_assessment import (
    FINAL_REFERENCE_REPAIR_VERSION,
    final_reference_errors,
    final_reference_repair_payload,
    validate_final_reference_repair,
)
from test_final_assessment_binding import enhanced, evidence, risk_scope


def inputs():
    first = evidence()[0]
    second = copy.deepcopy(first)
    second["key"] = "E2"
    second["record_ref"]["object_id"] = "source-2"
    second = freeze_passages(second)
    rows = [first, second]
    request = {
        "task": "synthesize",
        "instruction": "SYNTHETIC fixed original request",
        "evidence": [
            {"evidence_key": item["key"], "passages": copy.deepcopy(item["passages"])} for item in rows
        ],
    }
    return rows, request


def broken(kind="passage"):
    value = enhanced()
    link = value["claims"][0]["evidence_links"][0]
    if kind == "passage":
        link["passage_id"] = "E1:p999"
    elif kind == "owner":
        link["evidence_key"] = "MISSING"
    elif kind == "both":
        link.update(evidence_key="MISSING", passage_id="MISSING:p999")
    elif kind == "foreign":
        link["passage_id"] = "E2:p001"
    elif kind == "claim":
        value["warning_assessment"]["supporting_claim_ids"] = ["C1"]
    else:
        raise AssertionError(kind)
    return value


@pytest.mark.parametrize("kind", ["passage", "owner", "both", "foreign", "claim"])
def test_known_invalid_scalar_ids_only_are_repairable_then_full_synthesis_is_still_required(kind):
    rows, request = inputs()
    value = broken(kind)
    original, original_request = copy.deepcopy(value), copy.deepcopy(request)
    errors = final_reference_errors(value, request)
    assert errors and all(
        item["reason"] in {"unknown_claim", "unknown_evidence", "unknown_passage"} for item in errors
    )
    payload = final_reference_repair_payload(request, value, validation_error="SYNTHETIC-invalid-reference")
    assert payload["repair_version"] == FINAL_REFERENCE_REPAIR_VERSION
    assert payload["maximum_attempts"] == 1
    assert payload["reference_errors"] == errors
    assert payload["invalid_output"] == value and payload["original_request"] == request
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert payload["original_output_sha256"] == hashlib.sha256(encoded.encode()).hexdigest()
    repaired = enhanced()
    validate_final_reference_repair(value, repaired, request)
    validate_synthesis(repaired, rows, parent=None, schema=FinalResearchSynthesis)
    assert value == original and request == original_request


def test_mixed_unknown_claim_owner_and_passage_share_one_payload_and_invalid_field_allowlist():
    _, request = inputs()
    value = broken("both")
    value["warning_assessment"]["supporting_claim_ids"] = ["C1"]
    value["competing_hypotheses"][0]["opposing_claim_ids"] = ["C2"]
    payload = final_reference_repair_payload(request, value, validation_error="SYNTHETIC")
    paths = {tuple(item["path"]) for item in payload["reference_errors"]}
    assert paths == {
        ("claims", 0, "evidence_links", 0, "evidence_key"),
        ("claims", 0, "evidence_links", 0, "passage_id"),
        ("warning_assessment", "supporting_claim_ids", 0),
        ("competing_hypotheses", 0, "opposing_claim_ids", 0),
    }
    assert payload["maximum_attempts"] == 1
    validate_final_reference_repair(value, enhanced(), request)


def test_valid_owner_cannot_change_to_another_real_owner_to_launder_bad_passage():
    _, request = inputs()
    value = broken()
    changed = enhanced()
    changed["claims"][0]["evidence_links"][0].update(evidence_key="E2", passage_id="E2:p001")
    with pytest.raises(DomainError) as raised:
        validate_final_reference_repair(value, changed, request)
    assert raised.value.code == "analysis_reference_repair_invalid"


def test_unknown_owner_is_constrained_by_an_existing_unchanged_valid_passage():
    _, request = inputs()
    value = broken("owner")
    errors = final_reference_errors(value, request)
    assert len(errors) == 1 and errors[0]["allowed_id_values"] == ["E1"]
    changed = enhanced()
    changed["claims"][0]["evidence_links"][0].update(evidence_key="E2", passage_id="E2:p001")
    with pytest.raises(DomainError):
        validate_final_reference_repair(value, changed, request)


def test_two_unknown_ids_must_form_an_actual_pair_after_repair():
    _, request = inputs()
    value = broken("both")
    changed = enhanced()
    changed["claims"][0]["evidence_links"][0]["passage_id"] = "E2:p001"
    with pytest.raises(DomainError):
        validate_final_reference_repair(value, changed, request)


@pytest.mark.parametrize(
    "change",
    [
        "claim_text",
        "claim_id",
        "role",
        "reason",
        "conditions",
        "answer",
        "unknowns",
        "warning",
        "opposition",
        "hypothesis",
        "delete_link",
        "reorder_claims",
    ],
)
def test_reference_repair_cannot_change_any_body_condition_counterevidence_or_other_json(change):
    _, request = inputs()
    value, changed = broken(), enhanced()
    if change == "claim_text":
        changed["claims"][0]["text"] += " SYNTHETIC 改写 🧬"
    elif change == "claim_id":
        changed["claims"][0]["id"] = "DIFFERENT"
    elif change in {"role", "reason"}:
        changed["claims"][0]["evidence_links"][0][change] = "refutes" if change == "role" else "changed"
    elif change == "conditions":
        changed["claims"][0]["conditions"] = ["SYNTHETIC new condition"]
    elif change == "answer":
        changed["conditional_answer"]["answer"] += " changed"
    elif change == "unknowns":
        changed["conditional_answer"]["unknowns"] = []
    elif change == "warning":
        changed["warning_assessment"]["decision"] = "verify_now"
    elif change == "opposition":
        changed["warning_assessment"]["opposing_claim_ids"] = []
    elif change == "hypothesis":
        changed["competing_hypotheses"][0]["untested_conditions"] = ["SYNTHETIC changed"]
    elif change == "delete_link":
        changed["claims"][0]["evidence_links"] = []
    elif change == "reorder_claims":
        changed["claims"].reverse()
    with pytest.raises(DomainError):
        validate_final_reference_repair(value, changed, request)


def test_json_scalar_type_must_not_change_even_when_typed_model_coerces_true_to_one():
    _, request = inputs()
    value = broken()
    scope = risk_scope()
    scope["scope_revision"] = 1
    value["topic_risk_reassessment"] = {
        "scope": copy.deepcopy(scope),
        "decision": "unresolved",
        "decision_claim_id": "C3_decision",
        "decision_statement": value["claims"][2]["text"],
        "supporting_claim_ids": ["C1_signal"],
        "scope_control_claim_ids": ["C2_scope"],
        "opposing_claim_ids": [],
        "scope_coverage": "partial",
        "remaining_critical_gaps": ["SYNTHETIC unresolved scope"],
        "conditions": [],
        "reason": "SYNTHETIC limited reassessment",
    }
    changed = copy.deepcopy(value)
    changed["claims"][0]["evidence_links"][0]["passage_id"] = "E1:p001"
    changed["topic_risk_reassessment"]["scope"]["scope_revision"] = True
    assert {item["reason"] for item in final_reference_errors(value, request, topic_risk_scope=scope)} == {
        "unknown_passage"
    }
    with pytest.raises(DomainError):
        validate_final_reference_repair(value, changed, request, topic_risk_scope=scope)


@pytest.mark.parametrize("fault", ["overlap", "structure", "no_error", "unavailable_ids"])
def test_valid_negative_or_semantic_and_structural_failures_do_not_enter_reference_repair(fault):
    _, request = inputs()
    value = broken()
    if fault == "overlap":
        value["warning_assessment"]["opposing_claim_ids"] = ["C1_signal"]
    elif fault == "structure":
        value["claims"][0].pop("text")
    elif fault == "no_error":
        value = enhanced()
    else:
        request["evidence"] = []
    with pytest.raises(DomainError) as raised:
        final_reference_repair_payload(request, value, validation_error="SYNTHETIC")
    assert raised.value.code == "analysis_reference_repair_invalid"


@pytest.mark.parametrize("fault", ["duplicate_evidence", "duplicate_passage", "missing_passages"])
def test_original_input_identity_ambiguity_is_not_guessed(fault):
    _, request = inputs()
    if fault == "duplicate_evidence":
        request["evidence"].append(copy.deepcopy(request["evidence"][0]))
    elif fault == "duplicate_passage":
        request["evidence"][1]["passages"][0]["id"] = "E1:p001"
    else:
        request["evidence"][0]["passages"] = []
    with pytest.raises(DomainError) as raised:
        final_reference_errors(broken(), request)
    assert raised.value.code == "analysis_reference_repair_context_invalid"




def test_id_only_replacement_still_cannot_bypass_complete_synthesis_semantics():
    rows, request = inputs()
    value = broken("claim")
    changed = enhanced()
    changed["warning_assessment"]["supporting_claim_ids"] = ["C2_scope"]
    validate_final_reference_repair(value, changed, request)
    with pytest.raises(DomainError) as raised:
        validate_synthesis(changed, rows, parent=None, schema=FinalResearchSynthesis)
    assert raised.value.code == "analysis_assessment_invalid"
