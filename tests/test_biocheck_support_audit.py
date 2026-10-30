"""Exercise actual BioCheck support review with saved receipts; no model requests."""

import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bioradar.adapters.biocheck import (
    audit_biocheck_support,
    build_packet,
    check_biocheck,
    load_biocheck,
    run_biocheck,
)
from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def provider():
    directory = Path(
        os.environ.get("BIORADAR_TEST_BIOCHECK_DIR", Path(__file__).resolve().parents[2] / "BioCheck")
    )
    if not (directory / "app/core.py").is_file():
        pytest.skip("Current BioCheck checkout required")
    settings = Settings(biocheck_project_dir=directory)
    loaded = load_biocheck(settings)
    assert callable(getattr(loaded[0], "audit_support", None))
    return settings, loaded, directory


def data():
    return build_packet(
        "SYNTHETIC effect",
        [
            {
                "key": "E1",
                "text": "A activates B.",
                "record_ref": {"adopted_version": "fixed-v1", "object_id": "synthetic-original"},
            }
        ],
    )


def verified(core, packet, *, invalid_segment=False):
    draft = "A activates B [E1]."
    raw = {
        "reviews": [
            {
                "unit_id": "U1",
                "verdict": "supported",
                "issues": [],
                "claims": [
                    {
                        "kind": "fact",
                        "span": "A activates B",
                        "verdict": "supported",
                        "evidence": [
                            {
                                "evidence_id": "E1",
                                "segment_id": "S999" if invalid_segment else "S1",
                            }
                        ],
                    }
                ],
            }
        ],
        "coverage": {"verdict": "complete"},
    }
    return core.verify(packet, draft, lambda *_: copy.deepcopy(raw))


def supported(payload):
    return {
        "checks": [
            {
                "unit_id": item["unit_id"],
                "claim_index": item["claim_index"],
                "verdict": "supported",
                "reason": "SYNTHETIC exact selected passage",
                "support": [
                    {
                        "evidence_id": passage["evidence_id"],
                        "segment_id": passage["segment_id"],
                        "quote": passage["quote"],
                    }
                    for passage in item["evidence"]
                ],
            }
            for item in payload["claims"]
        ]
    }


def error(code, status, *, http_status=None):
    return ModelCallError(
        code,
        "SYNTHETIC model error",
        ModelCallReceipt(
            call_id="support-audit-call",
            stage="biocheck_support_audit",
            input_hash="a" * 64,
            prompt_version="synthetic",
            requested_model="synthetic",
            configured_model_revision=None,
            started_at=datetime.now(UTC),
            max_output_tokens=1024,
            status=status,
            http_status=http_status,
            error_code=code,
        ),
    )


def test_support_audit_is_independent_and_bound_to_exact_text_materials_and_targets(provider):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)
    before = copy.deepcopy((packet, review))
    calls = []

    def call(stage, instruction, payload):
        calls.append((stage, payload))
        return supported(payload)

    result = audit_biocheck_support(settings, packet, review, call, loaded=loaded)
    assert result["status"] == "PASS" and result["original_status"] == review["status"] == "PASS"
    assert result["targets"] == [["U1", 0]]
    assert result["text_sha256"] == hashlib.sha256(review["draft"].encode()).hexdigest()
    assert result["provider_version"] == loaded[1]
    assert result["evidence_map"] == packet["evidence_map"]
    assert result["evidence_versions"]["E1"] == {
        "version": "fixed-v1",
        "selected_text_sha256": hashlib.sha256(b"A activates B.").hexdigest(),
    }
    assert len(calls) == 1 and calls[0][0] == "biocheck_support_audit"
    assert result["checks"][0]["evidence"][0]["version"] == "fixed-v1"
    assert (packet, review) == before


def test_multiple_selected_passages_can_jointly_support_a_claim(provider):
    settings, loaded, _ = provider
    packet = build_packet(
        "SYNTHETIC union",
        [
            {"key": "E1", "text": "A binds B.", "record_ref": {"adopted_version": "v1"}},
            {"key": "E2", "text": "Binding activates C.", "record_ref": {"adopted_version": "v2"}},
        ],
    )
    draft = "A binds B and activates C [E1][E2]."
    response = {
        "reviews": [
            {
                "unit_id": "U1",
                "verdict": "supported",
                "issues": [],
                "claims": [
                    {
                        "kind": "fact",
                        "span": "A binds B and activates C",
                        "verdict": "supported",
                        "evidence": [
                            {"evidence_id": "E1", "segment_id": "S1"},
                            {"evidence_id": "E2", "segment_id": "S1"},
                        ],
                    }
                ],
            }
        ],
        "coverage": {"verdict": "complete"},
    }
    review = loaded[0].verify(packet, draft, lambda *_: copy.deepcopy(response))
    assert review["status"] == "PASS"
    result = audit_biocheck_support(
        settings, packet, review, lambda stage, instruction, payload: supported(payload), loaded=loaded
    )
    assert result["status"] == "PASS"
    assert [(e["evidence_id"], e["version"]) for e in result["checks"][0]["evidence"]] == [
        ("E1", "v1"),
        ("E2", "v2"),
    ]
    assert review["draft"] == draft


@pytest.mark.parametrize(
    "case_name,expected", [("original_selection", "REVIEW"), ("manual_s2_control", "PASS")]
)
def test_replay_two_saved_real_receipts_without_new_model_calls(provider, case_name, expected):
    settings, loaded, directory = provider
    sample = json.loads((directory / "evaluation/selected_support_case.json").read_text())
    results = json.loads((directory / "evaluation/selected_support_results.json").read_text())
    case = next(c for c in results["cases"] if c["name"] == case_name)
    packet = copy.deepcopy(sample["packet"])
    review = loaded[0].verify(packet, sample["draft"], lambda *_: copy.deepcopy(sample["raw_output"]))
    assert review["status"] == "PASS"
    if case_name == "manual_s2_control":
        # The upstream comparison is explicitly manual reselection, never automatic repair.
        unit = next(u for u in review["units"] if u["unit_id"] == "U1")
        chosen = next(c for c in case["audit"]["checks"] if c["claim_index"] == 0)
        unit["claims"][0]["evidence"] = copy.deepcopy(chosen["evidence"])
    before = copy.deepcopy(review)
    calls = []

    def replay(stage, instruction, payload):
        calls.append((stage, payload))
        return copy.deepcopy(case["raw_output"])

    audit = audit_biocheck_support(settings, packet, review, replay, targets=case["targets"], loaded=loaded)
    assert audit["status"] == expected
    assert {key: audit[key] for key in case["audit"]} == case["audit"]
    assert audit["text_sha256"] == hashlib.sha256(sample["draft"].encode()).hexdigest()
    assert audit["targets"] == case["targets"]
    assert calls[0][0] == "biocheck_support_audit" and len(calls) == 1
    assert review == before and review["status"] == "PASS"
    if case_name == "original_selection":
        assert audit["checks"][0]["verdict"] == "not_supported"
        assert audit["checks"][0]["evidence"][0]["segment_id"] == "S1"
        assert audit["checks"][1]["verdict"] == "supported"


@pytest.mark.parametrize("code", ["analysis_model_budget", "analysis_token_budget", "analysis_time_budget"])
@pytest.mark.parametrize("model_receipt", [False, True])
def test_presend_budget_stop_is_explicit_not_run_and_keeps_old_checked_text(provider, code, model_receipt):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)
    before = copy.deepcopy(review)

    def stop(*_):
        raise error(code, "not_sent") if model_receipt else DomainError(code, "SYNTHETIC budget", 422)

    audit = audit_biocheck_support(settings, packet, review, stop, loaded=loaded)
    assert audit["status"] == "NOT_RUN" and audit["error_code"] == code
    assert audit["targets"] == [["U1", 0]] and audit["not_run_reason"]
    assert audit["model_receipt_status"] == "not_sent"
    assert review == before


def test_presend_model_input_budget_is_not_run(provider):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)

    def stop(*_):
        raise error("model_input_budget", "not_sent")

    assert audit_biocheck_support(settings, packet, review, stop, loaded=loaded)["status"] == "NOT_RUN"


@pytest.mark.parametrize(
    "value", [None, [], {}, {"checks": []}, {"checks": [{"unit_id": "wrong", "claim_index": 0}]}]
)
def test_malformed_response_remains_review_without_altering_original_pass(provider, value):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)
    audit = audit_biocheck_support(settings, packet, review, lambda *_: copy.deepcopy(value), loaded=loaded)
    assert audit["status"] == "REVIEW" and review["status"] == "PASS"
    assert review["draft"] == "A activates B [E1]."


@pytest.mark.parametrize("code", ["model_output_invalid", "model_protocol_error"])
def test_known_http200_malformed_response_becomes_independent_review(provider, code):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)

    def fail(*_):
        raise error(code, "failed", http_status=200)

    audit = audit_biocheck_support(settings, packet, review, fail, loaded=loaded)
    assert audit["status"] == "REVIEW" and audit["error_code"] == code
    assert audit["model_receipt_status"] == "failed" and review["status"] == "PASS"


@pytest.mark.parametrize(
    "failure",
    [
        error("model_output_invalid", "unconfirmed", http_status=200),
        error("model_transport_unconfirmed", "unconfirmed"),
        error("analysis_time_budget", "failed"),
        error("model_protocol_error", "failed", http_status=503),
        DomainError("model_call_outcome_unconfirmed", "SYNTHETIC unknown", 409),
        DomainError("history_source_revoked", "SYNTHETIC revoked", 403),
        RuntimeError("SYNTHETIC receipt persistence failed"),
        ValueError("SYNTHETIC receipt persistence failed"),
    ],
)
def test_unknown_permission_and_callback_persistence_failures_still_block(provider, failure):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)

    def stop(*_):
        raise failure

    with pytest.raises(type(failure)) as raised:
        audit_biocheck_support(settings, packet, review, stop, loaded=loaded)
    assert raised.value is failure


def test_verification_errors_are_not_run_and_cannot_be_misrepresented_as_audit_pass(provider):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet, invalid_segment=True)
    assert review["status"] == "REVIEW" and review["verification_errors"]
    result = audit_biocheck_support(
        settings, packet, review, lambda *_: pytest.fail("No model request is permitted"), loaded=loaded
    )
    assert result["status"] == "NOT_RUN" and result["error_code"] == "biocheck_verification_errors"
    assert result["verification_errors"] == review["verification_errors"]


def test_no_supported_facts_is_not_run_without_a_model_request(provider):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)
    review["units"][0]["claims"][0]["kind"] = "absence"
    result = audit_biocheck_support(
        settings, packet, review, lambda *_: pytest.fail("No supported fact"), loaded=loaded
    )
    assert result["status"] == "NOT_RUN" and result["error_code"] == "biocheck_support_no_targets"


def test_wrong_saved_support_version_is_review_before_any_model_request(provider):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)
    review["units"][0]["claims"][0]["evidence"][0]["version"] = "different-v2"
    result = audit_biocheck_support(
        settings, packet, review, lambda *_: pytest.fail("Mismatched immutable support"), loaded=loaded
    )
    assert result["status"] == "REVIEW" and result["verification_errors"]


def test_received_callback_result_can_reconstruct_audit_without_another_post(provider):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)
    received, posts = {}, []

    def durable(stage, instruction, payload):
        signature = (stage, instruction, json.dumps(payload, sort_keys=True))
        if signature not in received:
            posts.append(stage)
            received[signature] = supported(payload)
        return copy.deepcopy(received[signature])

    first = audit_biocheck_support(settings, packet, review, durable, loaded=loaded)
    recovered = audit_biocheck_support(settings, packet, review, durable, loaded=loaded)
    assert first == recovered and posts == ["biocheck_support_audit"]


def test_old_method_without_audit_is_explicit_not_run(provider):
    settings, loaded, _ = provider
    packet = data()
    review = verified(loaded[0], packet)
    result = audit_biocheck_support(
        settings, packet, review, lambda *_: pytest.fail("No audit method"), loaded=(object(), loaded[1])
    )
    assert result["status"] == "NOT_RUN" and result["error_code"] == "biocheck_support_unavailable"


@pytest.mark.parametrize("with_scope", [False, True])
def test_actual_method_keeps_frozen_scope_through_generation_each_check_and_audit(provider, with_scope):
    settings, loaded, _ = provider
    packet = data()
    scope = {
        "cutoff": "2026-10-30T00:00:00+00:00",
        "source_ids": ["synthetic-source"],
        "dimensions": [
            {"dimension": "support", "status": "checked", "coverage_status": "unknown"},
            {"dimension": "counterevidence", "status": "queried_no_results"},
            {"dimension": "competition", "status": "source_failure"},
            {"dimension": "patents", "status": "not_checked"},
        ],
        "queries": [
            {"status": "empty", "source_id": "synthetic-source", "terms": ["counterexample"]},
            {"status": "failed", "error_code": "history_source_unavailable"},
            {"status": "unconfirmed", "cost": {"tool_calls": None}},
        ],
        "qualification": "SYNTHETIC actual process only; checked is not complete coverage.",
    }
    if with_scope:
        packet["assessment"] = {"research_scope": copy.deepcopy(scope)}
    before = copy.deepcopy(packet)
    calls = []

    def call(stage, instruction, payload):
        calls.append((stage, instruction, copy.deepcopy(payload)))
        if with_scope:
            assert payload["assessment"]["research_scope"] == scope
        else:
            assert "assessment" not in payload
        if stage == "biocheck_plan":
            return {
                "points": [
                    {
                        "intent": "SYNTHETIC effect",
                        "evidence_ids": ["E1"],
                        "binding": {},
                        "support": [{"evidence_id": "E1", "segment_id": "S1"}],
                    }
                ]
            }
        if stage == "biocheck_write":
            return {"draft": "A activates B [E1]."}
        if stage == "biocheck_support_audit":
            return supported(payload)
        assert stage == "biocheck_check"
        return {
            "reviews": [
                {
                    "unit_id": "U1",
                    "verdict": "supported",
                    "issues": [],
                    "claims": [
                        {
                            "kind": "fact",
                            "span": "A activates B",
                            "verdict": "supported",
                            "evidence": [{"evidence_id": "E1", "segment_id": "S1"}],
                        }
                    ],
                }
            ],
            "coverage": {"verdict": "complete", "reason": "SYNTHETIC single fact only."},
        }

    generated = run_biocheck(settings, packet, call, repair=False, loaded=loaded)
    assert generated["status"] == "PASS"
    for round_index in range(2):
        result = (
            generated
            if round_index == 0
            else check_biocheck(settings, packet, generated["final_text"], call, repair=False, loaded=loaded)
        )
        assert result["status"] == "PASS"
        audit = audit_biocheck_support(settings, packet, result["verification"], call, loaded=loaded)
        assert audit["status"] == "PASS"
    assert [stage for stage, _, _ in calls] == [
        "biocheck_plan",
        "biocheck_write",
        "biocheck_check",
        "biocheck_support_audit",
        "biocheck_check",
        "biocheck_support_audit",
    ]
    # Compare the adapter handoff with the actual upstream audit request, including the prompt.
    original = []
    loaded[0].audit_support(
        copy.deepcopy(packet),
        copy.deepcopy(generated["verification"]),
        lambda stage, instruction, payload: original.append((instruction, payload)) or supported(payload),
    )
    expected_payload = copy.deepcopy(original[0][1])
    if with_scope:
        expected_payload["assessment"] = {"research_scope": scope}
    assert calls[-1][1:] == (original[0][0], expected_payload)
    assert packet == before
    if with_scope:
        # Content PASS must not upgrade failed/unsearched/unknown research scope.
        assert packet["assessment"]["research_scope"] == scope


def test_scoped_audit_unknown_receipt_still_blocks_without_another_call(provider):
    settings, loaded, _ = provider
    packet = data()
    packet["assessment"] = {"research_scope": [{"dimension": "patents", "status": "not_checked"}]}
    review = verified(loaded[0], packet)
    before = copy.deepcopy((packet, review))
    failure = error("model_transport_unconfirmed", "unconfirmed")
    calls = []

    def stop(stage, instruction, payload):
        calls.append(copy.deepcopy(payload))
        assert payload["assessment"]["research_scope"] == packet["assessment"]["research_scope"]
        raise failure

    with pytest.raises(ModelCallError) as raised:
        audit_biocheck_support(settings, packet, review, stop, loaded=loaded)
    assert raised.value is failure
    assert len(calls) == 1 and (packet, review) == before
