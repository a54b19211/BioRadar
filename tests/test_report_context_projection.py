"""The new report projection removes exact duplicates, never scientific qualifications."""

import copy
import json

from bioradar.research.investigations.kdd import compact_report_continuation_scope


def sample_scope():
    common = {
        "parameters": {"policy": {"criteria": ["fixed"] * 100}, "policy_hash": "kept"},
        "calculation_basis": {"unit": "records", "denominator": {"unknown": ["coverage"] * 50}},
        "limitations": ["not independent studies", "counterevidence remains"] * 30,
    }
    return {
        "analysis_results": [
            {
                "id": "calc:" + str(index),
                **copy.deepcopy(common),
                "output": {
                    "status": "unsupported" if index else "success",
                    "patterns": [{"negative": True}],
                    "reason": "population mismatch",
                },
                "input_refs": [{"dataset_ref": "fixed", "version": "v1"}],
            }
            for index in range(4)
        ],
        "mechanism": {"critical_gaps": ["missing control"], "conditions": ["human confirmation"]},
        "unread_versions": ["kept"],
    }


def test_shared_calculation_context_is_lossless_and_does_not_mutate_saved_scope():
    original = sample_scope()
    before = copy.deepcopy(original)
    projected, version = compact_report_continuation_scope(original)
    context = projected.pop("analysis_result_context")
    assert original == before
    assert version == context["projection_version"] == "shared-calculation-context-v1"
    assert context["applies_to_result_ids"] == [r["id"] for r in original["analysis_results"]]
    for row in projected["analysis_results"]:
        row.update(copy.deepcopy(context["fields"]))
    assert projected == original
    assert context["source_paths"]["parameters"] == [f"$.analysis_results[{i}].parameters" for i in range(4)]


def test_different_or_missing_values_are_retained_per_calculation():
    original = sample_scope()
    original["analysis_results"][1]["limitations"].append("a different failure")
    del original["analysis_results"][2]["calculation_basis"]
    projected, _ = compact_report_continuation_scope(original)
    assert set(projected["analysis_result_context"]["fields"]) == {"parameters"}
    for before, after in zip(original["analysis_results"], projected["analysis_results"], strict=True):
        assert before["limitations"] == after["limitations"]
        assert before.get("calculation_basis") == after.get("calculation_basis")
        assert before["output"] == after["output"]


def test_projection_is_smaller_idempotent_and_small_rows_stay_whole():
    scope = sample_scope()
    projected, version = compact_report_continuation_scope(scope)
    assert len(json.dumps(projected)) < len(json.dumps(scope))
    assert compact_report_continuation_scope(projected) == (projected, version)
    small = {"analysis_results": [{"id": "a", "parameters": {}}, {"id": "b", "parameters": {}}]}
    assert compact_report_continuation_scope(small)[0] == small
