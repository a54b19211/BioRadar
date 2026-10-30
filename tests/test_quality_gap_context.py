"""Structured professional gaps are text checks, without dropping the original obligations."""

import copy
import json

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import compact_query_context, semantic_reading_state
from bioradar.research.investigations.query_policy import build_coverage_checks


def state():
    feedback = {
        "evidence_gaps": [
            {
                "kind": "material_absence",
                "unit_id": "U1",
                "claim_index": 0,
                "span": "SYNTHETIC missing control",
                "reason": {"missing": "SYNTHETIC 反证、对照与适用条件", "material": "SYNTHETIC 固定原件E1"},
            },
            {
                "kind": "evidence_selection",
                "unit_id": "U2",
                "claim_index": 1,
                "span": "SYNTHETIC selected passage",
                "reason": "SYNTHETIC unchanged literal question",
            },
        ],
        "review_required": [],
        "repair_units": [],
    }
    return (
        {
            "question": "SYNTHETIC scoped research question",
            "query_policy": {"effective": "query_planner"},
            "quality_feedback": feedback,
        },
        {"evidence": [{"key": "E1"}], "query_policy": {"checks": []}},
    )


def test_structured_quality_gap_questions_roundtrip_without_mutating_feedback_or_saved_checks():
    fixed, progress = state()
    before = copy.deepcopy((fixed, progress))
    checks = build_coverage_checks(fixed, progress)
    quality = [check for check in checks if check["id"].startswith("quality:")]
    assert len(quality) == len(fixed["quality_feedback"]["evidence_gaps"])
    assert all(isinstance(check["question"], str) for check in quality)
    assert json.loads(quality[0]["question"]) == fixed["quality_feedback"]["evidence_gaps"][0]["reason"]
    assert "反证" in quality[0]["question"] and "\\u" not in quality[0]["question"]
    assert quality[1]["question"] == "SYNTHETIC unchanged literal question"
    assert (fixed, progress) == before
    legacy = copy.deepcopy(progress)
    legacy["query_policy"]["checks"] = copy.deepcopy(quality)
    legacy["query_policy"]["checks"][0]["question"] = copy.deepcopy(
        fixed["quality_feedback"]["evidence_gaps"][0]["reason"]
    )
    saved = copy.deepcopy(legacy)
    replay = [check for check in build_coverage_checks(fixed, legacy) if check["id"].startswith("quality:")]
    assert replay == quality and legacy == saved


def test_compactor_preserves_legacy_structured_question_and_all_raw_gap_details():
    fixed, _ = state()
    reason = fixed["quality_feedback"]["evidence_gaps"][0]["reason"]
    context = {
        "coverage_checks": [
            {
                "id": "quality:old",
                "question": reason,
                "status": "unknown",
                "required": True,
                "evidence_ids": [],
            }
        ],
        "quality_feedback": fixed["quality_feedback"],
        "query_history": [],
        "mechanism_evaluation": None,
    }
    before = copy.deepcopy(context)
    compact = compact_query_context(context)
    assert compact["coverage_checks"][0]["question"] == reason
    assert compact["quality_feedback"] == fixed["quality_feedback"]
    semantic = semantic_reading_state([], context)
    assert semantic["quality"]["evidence_gaps"] == fixed["quality_feedback"]["evidence_gaps"]
    assert context == before


@pytest.mark.parametrize("invalid", [float("nan"), {"missing": float("inf")}, {"not_json"}])
def test_invalid_saved_question_is_a_safe_domain_error_not_generic_execution_failure(invalid):
    fixed, progress = state()
    fixed.pop("quality_feedback")
    progress["query_policy"]["checks"] = [
        {"id": "quality:old", "question": invalid, "status": "unknown", "required": True, "evidence_ids": []}
    ]
    with pytest.raises(DomainError) as caught:
        build_coverage_checks(fixed, progress)
    assert caught.value.code == "analysis_policy_coverage_invalid"
