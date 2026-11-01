"""Current synthesis claim IDs, future-only wire contracts and no automatic ID repair."""

import copy
import hashlib

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import (
    freeze_passages,
    readable_result,
    report_context,
    validate_synthesis,
    validated_result,
)
from bioradar.research.investigations.analysis_models import (
    AnalysisEvidence,
    AnalysisQueryReceipt,
    FinalResearchSynthesis,
    ModelSynthesis,
    QueryPagination,
)
from bioradar.research.investigations.research_assessment import (
    FINAL_ASSESSMENT_VERSION,
    assessment_binding,
    assessment_binding_note,
    assessment_result_view,
    final_assessment_context,
    synthesis_schema,
)


def evidence():
    text = (
        "SYNTHETIC bounded source: a signal and its scope require review; no scientific outcome is claimed."
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return [
        freeze_passages(
            AnalysisEvidence(
                key="E1",
                material_entry_id=None,
                snapshot_id="SYNTHETIC-snapshot",
                record_ref={
                    "provider": "SYNTHETIC",
                    "object_type": "document",
                    "object_id": "source-1",
                    "adopted_version": "fixed-v1",
                    "version_source": "provider",
                    "content_hash": digest,
                    "authorization_ref": "SYNTHETIC-grant",
                },
                source_id="SYNTHETIC-source",
                title="SYNTHETIC source",
                text=text,
                start=0,
                end=len(text),
                text_sha256=digest,
            ).model_dump(mode="json")
        )
    ]


def synthesis():
    claims = [
        {
            "id": identifier,
            "text": text,
            "kind": "inference",
            "scope": "SYNTHETIC fixed scope",
            "conditions": [],
            "status": "supported",
            "evidence_links": [
                {
                    "evidence_key": "E1",
                    "passage_id": "E1:p001",
                    "role": "supports",
                    "reason": "SYNTHETIC source",
                }
            ],
        }
        for identifier, text in [
            ("C1_signal", "SYNTHETIC signal requires verification."),
            ("C2_scope", "SYNTHETIC declared scope was inspected."),
            ("C3_decision", "SYNTHETIC decision within the declared scope."),
        ]
    ]
    return {
        "claims": claims,
        "explanations": [],
        "gaps": [],
        "conditional_answer": {
            "answer": "SYNTHETIC overall watch and verify the named allegation immediately.",
            "conditions": [],
            "unknowns": ["SYNTHETIC truth unresolved"],
        },
        "update": {
            "stance": "initial",
            "changed_claims": [],
            "reasons": ["SYNTHETIC initial draft"],
            "decisive_evidence_keys": [],
        },
    }


def enhanced():
    value = synthesis()
    value.update(
        warning_assessment={
            "decision": "watch",
            "supporting_claim_ids": ["C1_signal"],
            "opposing_claim_ids": ["C2_scope"],
            "critical_unknowns": ["SYNTHETIC truth unresolved"],
            "next_actions": ["SYNTHETIC immediately verify the specific allegation"],
            "reason": "SYNTHETIC overall observation with targeted verification",
        },
        competing_hypotheses=[
            {
                "id": "H1",
                "explanation": "SYNTHETIC competing explanation",
                "supporting_claim_ids": ["C1_signal"],
                "opposing_claim_ids": ["C2_scope"],
                "discriminating_evidence_needed": "SYNTHETIC independent source",
                "untested_conditions": [],
            }
        ],
    )
    return value


def frozen():
    return {
        "query_policy": {
            "effective": "query_planner",
            "schema_version": "query-planner.external.v2",
            "research_contract_version": "bioradar-research-input-v1",
            "final_assessment_version": FINAL_ASSESSMENT_VERSION,
        }
    }


def review(value):
    return {
        "status": "passed",
        "claims": [
            {"claim_id": claim["id"], "verdict": "grounded", "issue": "", "evidence_keys": ["E1"]}
            for claim in value["claims"]
        ],
        "overall_limitations": [],
        "findings": [],
    }


def test_baseline_schema_stays_original_and_current_method_has_one_schema():
    value = synthesis()
    for fixed in [
        {"query_policy": {"effective": "baseline"}},
    ]:
        assert synthesis_schema(fixed) is ModelSynthesis
        assert final_assessment_context(fixed) == {}
        parsed, _ = validate_synthesis(value, evidence(), parent=None)
        assert parsed.model_dump(mode="json") == value
    assert "warning_assessment" not in ModelSynthesis.model_json_schema()["properties"]
    current = frozen()
    assert synthesis_schema(current) is FinalResearchSynthesis
    assert final_assessment_context(current)["version"] == FINAL_ASSESSMENT_VERSION
    assert {"warning_assessment", "competing_hypotheses"}.issubset(
        FinalResearchSynthesis.model_json_schema()["required"]
    )
    current["query_policy"]["final_assessment_version"] = "SYNTHETIC-unknown"
    assert synthesis_schema(current) is FinalResearchSynthesis


def test_future_manual_context_accepts_explicit_null_origin_without_fabricating_topic_scope():
    fixed = frozen()
    fixed["origin"] = None
    original = copy.deepcopy(fixed)
    context = final_assessment_context(fixed)
    assert context["version"] == FINAL_ASSESSMENT_VERSION
    assert "topic_risk_scope" not in context and "topic_risk_instruction" not in context
    assert fixed == original


def test_same_synthesis_carries_bound_assessment_without_equating_general_and_targeted_actions():
    value = enhanced()
    original = copy.deepcopy(value)
    result = validated_result(
        value, review(value), evidence(), parent=None, limitations=[], schema=FinalResearchSynthesis
    )
    assert result.warning_assessment.decision == "watch"
    assert result.warning_assessment.next_actions == value["warning_assessment"]["next_actions"]
    assert result.conditional_answer.answer == value["conditional_answer"]["answer"]
    assert result.competing_hypotheses[0].supporting_claim_ids == ["C1_signal"]
    assert assessment_binding(result)["status"] == "bound"
    assert "topic_risk_reassessment" not in result.model_dump(mode="json")
    assert value == original


def test_professional_context_uses_exact_final_findings_and_marks_legacy_unbound_without_a_guess():
    value = enhanced()
    result = validated_result(
        value, review(value), evidence(), parent=None, limitations=[], schema=FinalResearchSynthesis
    )
    packet = report_context(result.model_dump(mode="json"), None)["radar_analysis"]
    assert packet["warning_assessment"] == value["warning_assessment"]
    assert packet["competing_hypotheses"] == value["competing_hypotheses"]
    assert "warning_assessment" not in report_context(synthesis(), None)["radar_analysis"]
    legacy = result.model_dump(mode="json")
    legacy["warning_assessment"]["supporting_claim_ids"] = ["C1"]
    original = copy.deepcopy(legacy)
    packet = report_context(legacy, None)["radar_analysis"]
    assert packet["assessment_binding"]["status"] == "unbound"
    assert "warning_assessment" not in packet and "competing_hypotheses" not in packet
    assert legacy == original




def test_missing_enhanced_fields_cannot_silently_fall_back_to_reading_sidecar():
    with pytest.raises(DomainError) as exc:
        validate_synthesis(synthesis(), evidence(), parent=None, schema=FinalResearchSynthesis)
    assert exc.value.code == "analysis_output_invalid"




@pytest.mark.parametrize("fault", ["duplicate_hypothesis", "opposing_support", "alert_without_actions"])
def test_shared_assessment_semantic_guards_remain_strict(fault):
    value = enhanced()
    if fault == "duplicate_hypothesis":
        value["competing_hypotheses"].append(copy.deepcopy(value["competing_hypotheses"][0]))
    elif fault == "opposing_support":
        value["warning_assessment"]["opposing_claim_ids"] = ["C1_signal"]
    else:
        value["warning_assessment"].update(decision="alert", critical_unknowns=[], next_actions=[])
    with pytest.raises(DomainError) as exc:
        validate_synthesis(value, evidence(), parent=None, schema=FinalResearchSynthesis)
    assert exc.value.code == "analysis_assessment_invalid"


def risk_scope():
    return {
        "topic_id": "SYNTHETIC-topic",
        "scope_revision": 2,
        "observation_revision": 3,
        "definition_sha256": "a" * 64,
        "risk_basis_sha256": "b" * 64,
    }


def with_risk():
    value = enhanced()
    value["topic_risk_reassessment"] = {
        "scope": risk_scope(),
        "decision": "resolved_within_scope",
        "decision_claim_id": "C3_decision",
        "decision_statement": value["claims"][2]["text"],
        "supporting_claim_ids": ["C1_signal"],
        "scope_control_claim_ids": ["C2_scope"],
        "opposing_claim_ids": [],
        "scope_coverage": "full_declared_scope",
        "remaining_critical_gaps": [],
        "conditions": [],
        "reason": "SYNTHETIC structural test, not a validated scientific resolution",
    }
    return value


def test_topic_candidate_is_carried_only_with_exact_current_claim_text_and_host_frozen_scope():
    value = with_risk()
    result = validated_result(
        value,
        review(value),
        evidence(),
        parent=None,
        limitations=[],
        schema=FinalResearchSynthesis,
        topic_risk_scope=risk_scope(),
    )
    assert result.topic_risk_reassessment.decision_statement == result.claims[2].text
    assert result.topic_risk_reassessment.scope.model_dump(mode="json") == risk_scope()
    # Professional verification/body checks are a separate adoption gate, not asserted by this validator.
    assert result.professional_status == "not_reviewed"


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "role_reuse",
        "own_basis",
        "statement_space",
        "stamp",
        "partial",
        "critical_gap",
        "missing_host_stamp",
    ],
)
def test_topic_candidate_cannot_escape_claim_or_frozen_scope_binding(fault):
    value = with_risk()
    risk = value["topic_risk_reassessment"]
    scope = risk_scope()
    if fault == "unknown":
        risk["supporting_claim_ids"] = ["C1"]
    elif fault == "role_reuse":
        risk["scope_control_claim_ids"] = ["C1_signal"]
    elif fault == "own_basis":
        risk["supporting_claim_ids"] = ["C3_decision"]
    elif fault == "statement_space":
        risk["decision_statement"] += " "
    elif fault == "stamp":
        risk["scope"]["observation_revision"] += 1
    elif fault == "partial":
        risk["scope_coverage"] = "partial"
    elif fault == "critical_gap":
        risk["remaining_critical_gaps"] = ["SYNTHETIC unresolved coverage"]
    else:
        scope = None
    original = copy.deepcopy(value)
    with pytest.raises(DomainError) as exc:
        validate_synthesis(
            value, evidence(), parent=None, schema=FinalResearchSynthesis, topic_risk_scope=scope
        )
    assert exc.value.code == "analysis_assessment_invalid"
    assert value == original




def test_optional_pagination_roundtrip_does_not_change_legacy_receipt_wire():
    receipt = AnalysisQueryReceipt(index=0, plan={"terms": ["SYNTHETIC"]}, status="empty")
    legacy = receipt.model_dump(mode="json")
    assert "pagination" not in legacy
    page = {"has_more": True, "next_cursor": "SYNTHETIC-next", "max_seq": 12, "page_number": 2}
    newer = receipt.model_copy(update={"pagination": QueryPagination.model_validate(page)}).model_dump(
        mode="json"
    )
    assert newer.pop("pagination") == page
    assert newer == legacy


def test_future_topic_instruction_copies_only_the_frozen_scope_without_modifying_it():
    fixed = frozen()
    definition = {"question": "SYNTHETIC declared scope", "source_ids": ["SYNTHETIC-source"]}
    fixed["origin"] = {"topic_risk_scope": risk_scope(), "topic_risk_definition": definition}
    original = copy.deepcopy(fixed)
    context = final_assessment_context(fixed)
    assert context["topic_risk_scope"] == risk_scope()
    assert context["topic_risk_definition"] == definition
    assert "逐字" in context["topic_risk_instruction"]
    context["topic_risk_scope"]["scope_revision"] += 1
    assert fixed == original


def test_legacy_result_view_and_summary_flag_unbound_without_changing_saved_body():
    raw = enhanced()
    raw["warning_assessment"]["supporting_claim_ids"] = ["C1"]
    raw["report"] = {"final_text": "SYNTHETIC saved report 🧬 unchanged", "status": "PASS"}
    original = copy.deepcopy(raw)
    view = assessment_result_view(raw)
    assert view["assessment_binding"]["status"] == "unbound"
    assert "assessment_binding" not in raw
    assert view["report"] == raw["report"]
    assert readable_result(raw).endswith(raw["report"]["final_text"])
    assert readable_result(raw).startswith(assessment_binding_note(raw))
    assert raw == original
    assert "assessment_binding" not in assessment_result_view(synthesis())


def test_shared_synthetic_model_adds_nullable_fields_only_for_enhanced_response_schema():
    import json

    import httpx
    from test_investigation_analysis import Model

    outputs = []
    for schema in [ModelSynthesis, FinalResearchSynthesis]:
        wire = {
            "messages": [
                {"content": "SYNTHETIC system"},
                {
                    "content": json.dumps(
                        {
                            "task": "synthesize",
                            "evidence": [{"evidence_key": "E1", "passages": [{"id": "E1:p001"}]}],
                        }
                    )
                },
            ],
            "response_format": {"json_schema": {"schema": schema.model_json_schema()}},
        }
        response = Model().handler(httpx.Request("POST", "http://synthetic.invalid", json=wire))
        outputs.append(json.loads(response.json()["choices"][0]["message"]["content"]))
    assert "warning_assessment" not in outputs[0] and "competing_hypotheses" not in outputs[0]
    assert outputs[1].pop("warning_assessment") is None
    assert outputs[1].pop("competing_hypotheses") == []
    assert outputs[1] == outputs[0]


def reference_repair_pair():
    original = enhanced()
    original["warning_assessment"]["supporting_claim_ids"] = ["C1"]
    original["competing_hypotheses"][0]["opposing_claim_ids"] = ["C2"]
    repaired = copy.deepcopy(original)
    repaired["warning_assessment"]["supporting_claim_ids"] = ["C1_signal"]
    repaired["competing_hypotheses"][0]["opposing_claim_ids"] = ["C2_scope"]
    return original, repaired










def test_compound_claim_can_support_and_limit_a_hypothesis():
    from bioradar.research.investigations.research_assessment import claim_assessment_errors

    value = {
        "claims": [{"id": "C1", "text": "去重减少两条记录，但仍有十二名住院患者。"}],
        "competing_hypotheses": [{
            "id": "H1", "explanation": "重复计数解释部分增幅，不能解释全部聚集。",
            "supporting_claim_ids": ["C1"], "opposing_claim_ids": ["C1"],
        }],
    }
    assert claim_assessment_errors(value) == []
    value["competing_hypotheses"][0]["opposing_claim_ids"] = ["missing"]
    assert claim_assessment_errors(value)[0]["reason"] == "unknown_claim"
