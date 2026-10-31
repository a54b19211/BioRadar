"""SYNTHETIC fixed-source updates; no model, network or semantic relevance guesses."""

import copy

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.material_impact import (
    MATERIAL_IMPACT_VERSION,
    evaluate_material_impact,
)
from bioradar.research.mining.models import EngineRecord, canonical_hash
from test_mining import record

pytestmark = pytest.mark.ENGINE


def changed(old, *, text=None, raw_changed=True, **updates):
    value = old.model_dump(mode="json")
    old_ref = value["record_ref"]
    new_ref = {
        **old_ref,
        "adopted_version": old_ref["adopted_version"] + "-next",
        "content_hash": canonical_hash(["SYNTHETIC next raw", text, updates])
        if raw_changed
        else old_ref["content_hash"],
    }
    value.update(
        record_ref=new_ref,
        evidence_refs=[new_ref if ref == old_ref else ref for ref in value["evidence_refs"]],
        document_hash=canonical_hash(["SYNTHETIC next document", text, updates])
        if raw_changed
        else value["document_hash"],
        **updates,
    )
    if text is not None:
        value.update(normalized_text=text, normalized_text_hash=None)
    return EngineRecord.model_validate(value)


def context(old, *, role="supports"):
    ref = old.record_ref.model_dump(mode="json")
    fixed = {
        "question": "SYNTHETIC evaluate orders and the stated technical condition",
        "query_policy": {"material_impact_version": MATERIAL_IMPACT_VERSION, "direction": "business"},
        "evidence_time": {"mode": "current", "cutoff": "2026-10-31T00:00:00+00:00"},
        "evidence": [{"key": "E1", "record_ref": ref}],
    }
    result = {"claims": [{"id": "C1", "evidence_links": [{"evidence_key": "E1", "role": role}]}]}
    return fixed, {}, result


def assess(old, new, *, role="supports", progress=None, result=None):
    fixed, state, output = context(old, role=role)
    state.update(progress or {})
    if result is not None:
        output = result
    update = {
        "entry_id": "SYNTHETIC-update",
        "revision": 8,
        "new_ref": new.record_ref.model_dump(mode="json"),
    }
    return evaluate_material_impact(fixed, state, output, [update], [old], [new])


@pytest.mark.parametrize("role", ["supports", "refutes", "limits"])
def test_actual_dependency_rechecks_full_source_not_only_unchanged_quote_or_domain(role):
    old = record("SYNTHETIC-tech", "SYNTHETIC technical condition supports the order assumption.")
    new = changed(old, text=old.normalized_text + " The source now reports a negative safety qualification.")
    report = assess(old, new, role=role)
    assert report["status"] == "affects_dependency"
    assert report["decisions"][0]["affected_claim_ids"] == ["C1"]
    assert report["report_currentness"] == "prior_fixed_materials"


@pytest.mark.parametrize(
    "updates",
    [
        {"record_flags": ["retracted"]},
        {"signal_context": {"material_type": "review"}},
        {"signal_context": {"document_evidence": {"statement_status": "unverified_report"}}},
    ],
)
def test_source_qualification_and_context_changes_are_not_input_equivalence(updates):
    old = record("SYNTHETIC-source", "SYNTHETIC unchanged quote.")
    assert assess(old, changed(old, **updates))["status"] == "affects_dependency"


def test_raw_hash_change_and_missing_full_content_are_not_declared_equivalent():
    old = record("SYNTHETIC-source", "SYNTHETIC unchanged quote.")
    new = changed(old)
    assert assess(old, new, role="context")["status"] == "impact_pending"
    new = changed(old, text_status="unavailable", normalized_text=None, normalized_text_hash=None)
    assert assess(old, new)["decisions"][0]["reason"] == "complete_source_inputs_unavailable"


def test_equivalence_requires_same_complete_raw_qualified_inputs_and_does_not_resolve_risk():
    old = record("SYNTHETIC-source", "SYNTHETIC unchanged full content.", record_flags=("retracted",))
    new = changed(old, raw_changed=False)
    fixed, state, result = context(old)
    before = copy.deepcopy((fixed, state, result, old.model_dump(), new.model_dump()))
    output = assess(old, new)
    assert output["status"] == "equivalent_inputs"
    assert "resolved" not in str(output)
    assert before == (fixed, state, result, old.model_dump(), new.model_dump())


def test_real_policy_reading_refutation_counts_even_if_final_claim_has_context_role():
    old = record("SYNTHETIC-tech", "SYNTHETIC original technical assumption.")
    new = changed(old, text=old.normalized_text + " SYNTHETIC counterevidence.")
    progress = {
        "query_policy": {
            "reading": {
                "claims": [{"id": "R1", "evidence_links": [{"evidence_key": "E1", "role": "refutes"}]}]
            }
        }
    }
    output = assess(old, new, role="context", progress=progress)
    assert output["decisions"][0]["affected_claim_ids"] == ["R1"]


@pytest.mark.parametrize(
    "kind,method,execution,expected",
    [
        ("company_metric_period", "business_metric_change", "success", "affects_dependency"),
        ("company_metric_period", "business_metric_change", "unsupported", "impact_pending"),
        ("source_statement", "public_health_source_claims", "success", "impact_pending"),
        ("source_information_event", "public_health_source_claims", "success", "impact_pending"),
        ("graph_relation_hint", "pipeline_graph_hints", "success", "impact_pending"),
    ],
)
def test_only_actual_computation_units_add_calculation_dependency(kind, method, execution, expected):
    old = record("SYNTHETIC-calc", "SYNTHETIC orders during the stated period.")
    new = changed(old, text="SYNTHETIC corrected order amount and reporting period.")
    state = {
        "kdd_artifacts": [{"id": "pattern:SYNTHETIC", "kind": "pattern_result"}],
        "kdd_results": {
            "pattern:SYNTHETIC": {
                "method_results": [{"method_id": method, "execution_status": execution}],
                "data_preparation": {
                    "units": [
                        {
                            "unit_kind": kind,
                            "members": [{"record_ref": old.record_ref.model_dump(mode="json")}],
                        }
                    ]
                },
            }
        },
    }
    output = assess(old, new, role="context", progress=state)
    assert output["status"] == expected
    assert output["decisions"][0]["affected_claim_ids"] == (
        ["calculation"] if expected == "affects_dependency" else []
    )


def test_missing_or_cross_instance_fixed_mapping_remains_pending_and_unknown_version_rejects():
    old = record("SYNTHETIC-source", "SYNTHETIC original.")
    new = changed(old, text="SYNTHETIC later.", source_instance="SYNTHETIC-other-instance")
    assert assess(old, new)["decisions"][0]["reason"] == "previous_fixed_version_unavailable"
    fixed, state, result = context(old)
    fixed["query_policy"]["material_impact_version"] = "unsupported"
    with pytest.raises(DomainError) as error:
        evaluate_material_impact(fixed, state, result, [], [old], [new])
    assert error.value.code == "analysis_material_impact_version"
