"""Section 16 regressions use synthetic statements and no external model calls."""

import copy

import httpx
import pytest
from bioradar.adapters.reasoning_model import complete_json
from bioradar.api.config import Settings
from bioradar.research.investigations import analysis_models, execution_budget
from bioradar.research.investigations.analysis import (
    validate_policy_reading,
    validate_reading,
    verification_input_allowance,
)
from test_final_assessment_binding import enhanced, evidence

pytestmark = pytest.mark.ENGINE


def raw_reading():
    raw = enhanced()
    for name in ("update", "warning_assessment", "competing_hypotheses"):
        raw.pop(name)
    raw["next_queries"] = []
    return raw


def test_verification_reserves_one_draft_and_paragraph_copy_not_four_drafts():
    packet = {"question": "SYNTHETIC", "evidence": [{"text": "SYNTHETIC fixed material."}]}
    shorter = "SYNTHETIC paragraph.\n\n" * 100
    longer = shorter * 2
    small = verification_input_allowance(packet, shorter, 1_000_000)
    large = verification_input_allowance(packet, longer, 1_000_000)
    assert large - small == 2 * (len(longer.encode()) - len(shorter.encode()))


def test_baseline_discards_invalid_link_without_changing_words_and_marks_unbound_claim_unknown():
    raw = raw_reading()
    raw["claims"][0]["evidence_links"][0]["passage_id"] = "E1:nonexistent"
    before = copy.deepcopy(raw)
    parsed = validate_reading(raw, evidence(), [])
    assert parsed.claims[0].text == before["claims"][0]["text"]
    assert parsed.claims[0].status == "insufficient" and parsed.claims[0].evidence_links == []
    assert parsed.conditional_answer.model_dump(mode="json") == before["conditional_answer"]
    assert raw == before


def test_calibrated_input_uses_stage_measurement_and_twenty_percent_margin():
    fixed = {
        "model_parameters": {
            "model": "SYNTHETIC",
            "token_calibration": {
                "version": "SYNTHETIC-calibration-only",
                "model": "SYNTHETIC",
                "ratios": {"read_plan": 0.25},
                "margin": 1.2,
            },
        }
    }
    estimate = execution_budget.request_estimate(fixed, "read_plan", 1000)
    assert estimate["estimated_input_tokens"] == 300
    assert estimate["basis"] == "measured_stage_ratio" and estimate["request_bytes"] == 1000
    assert execution_budget.estimate_input_tokens(fixed, "read_plan", 1000) == 300
    unknown = execution_budget.request_estimate(fixed, "unmeasured_stage", 1000)
    assert unknown["basis"] == "uncalibrated_byte_upper_bound" and unknown["estimated_input_tokens"] >= 1000


def test_model_receipt_records_exact_encoded_body_bytes_without_secret_or_legacy_null():
    settings = Settings(
        environment="test",
        semantic_llm_enabled=True,
        semantic_llm_base_url="http://127.0.0.1:19999/v1",
        semantic_llm_api_key="SYNTHETIC-ONLY",
        semantic_llm_model="SYNTHETIC",
    )
    bodies = []

    def handler(request):
        bodies.append(request.content)
        return httpx.Response(
            200,
            json={
                "id": "SYNTHETIC",
                "model": "SYNTHETIC",
                "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = complete_json(
            settings,
            stage="read_plan",
            system_prompt="SYNTHETIC",
            payload={"task": "SYNTHETIC"},
            response_schema={"type": "object"},
            call_id="SYNTHETIC",
            input_hash="SYNTHETIC",
            prompt_version="SYNTHETIC",
            client=client,
        )
    assert result.receipt.request_bytes == len(bodies[0])
    old = result.receipt.model_dump(mode="json")
    old.pop("request_bytes")
    assert "request_bytes" not in type(result.receipt).model_validate(old).model_dump(mode="json")


def test_new_question_plan_has_named_unicode_spans_while_baseline_schema_stays_unchanged():
    data = {
        "queries": [],
        "limitations": [],
        "named_objects": [{"id": "entity-1", "name": "甲公司", "question_span": {"start": 10, "end": 13}}],
    }
    model = getattr(analysis_models, "ResearchQuestionPlan", analysis_models.QuestionPlan)
    parsed = model.model_validate(data)
    assert parsed.named_objects[0].question_span.start == 10
    assert "named_objects" not in analysis_models.QuestionPlan.model_json_schema()["properties"]


def test_hypothesis_judgment_is_optional_explicit_model_judgment_and_source_id_bound():
    raw = enhanced()
    raw.pop("update")
    raw["next_queries"] = []
    raw["current_judgment"] = {
        "least_refuted_hypothesis_id": "H1",
        "reason": "SYNTHETIC qualified model interpretation.",
        "next_discriminating_evidence": "SYNTHETIC independent record with a matched scope.",
    }
    parsed = validate_policy_reading(raw, evidence(), [])
    assert parsed.current_judgment.least_refuted_hypothesis_id == "H1"
    assert parsed.current_judgment.qualification == "这是模型判断，未证实"
    assert "current_judgment" not in analysis_models.ModelReading.model_json_schema()["properties"]


def test_baseline_missing_links_cannot_leave_a_passed_machine_review():
    from bioradar.research.investigations.analysis import validated_result
    from test_final_assessment_binding import review, synthesis

    raw = synthesis()
    raw["claims"][0]["evidence_links"] = []
    before = copy.deepcopy(raw)
    result = validated_result(raw, review(raw), evidence(), parent=None, limitations=[])
    assert result.claims[0].status == "insufficient"
    assert result.review.status == "needs_review"
    assert result.claims[0].text == raw["claims"][0]["text"] and raw == before


def test_legacy_null_provider_usage_remains_unknown_and_conservatively_reserved():
    from datetime import UTC, datetime

    from bioradar.research.investigations.analysis import analysis_budget

    fixed = {
        "model_parameters": {"timeout_seconds": 60},
        "budget": {
            "max_calls": 6,
            "max_tokens": 10000,
            "max_seconds": 1800,
            "max_queries": 2,
        },
    }
    saved = {
        "calls": [
            {
                "status": "received",
                "token_upper_bound": 1234,
                "receipt": {"status": "completed", "usage": None},
            }
        ]
    }
    usage = analysis_budget(fixed, saved, datetime.now(UTC))["usage"]
    assert usage["total_tokens"] is None and usage["unknown_usage_calls"] == 1
    assert usage["budgeted_tokens"] == 1234 and usage["known_total_tokens"] == 0


def test_calibration_binds_real_start_model_identity_outside_transport_parameters():
    from bioradar.api.errors import DomainError

    fixed = {
        "model": "SYNTHETIC-real-shape",
        "model_revision": None,
        "model_parameters": {
            "max_output_tokens": 32768,
            "token_calibration": {
                "version": "SYNTHETIC-calibration-only",
                "model": "SYNTHETIC-real-shape",
                "model_revision": None,
                "ratios": {"read_plan": 0.25},
                "margin": 1.2,
            },
        },
    }
    assert execution_budget.request_estimate(fixed, "read_plan", 1000)["estimated_input_tokens"] == 300
    fixed["model_revision"] = "SYNTHETIC-different-revision"
    with pytest.raises(DomainError) as failure:
        execution_budget.request_estimate(fixed, "read_plan", 1000)
    assert failure.value.code == "analysis_token_calibration_invalid"
