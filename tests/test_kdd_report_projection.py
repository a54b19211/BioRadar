"""Report projection preserves selection and diagnostic truth without resending every pattern."""

import copy
from collections import Counter

import pytest
from bioradar.research.investigations.kdd import analysis_results
from bioradar.research.mining.kdd import run_kdd
from bioradar.research.mining.models import KddPolicy, NormalizedSnapshot, ResourceLimits
from test_fabric_who import CUTOFF, FIXTURES
from test_kdd_mining import metric_record, policy
from test_mining import snapshot

pytestmark = pytest.mark.ENGINE


def test_report_details_keep_saved_selection_order_index_gaps_and_unchanged_full_result():
    records = [
        metric_record(f"company-{company}-q{period}", 100 + period * 50, period, subject=f"company-{company}")
        for company in range(5)
        for period in range(3)
    ]
    result = run_kdd(snapshot(records), policy("business", limits=ResourceLimits(review_capacity=6)))
    payload = result.model_dump(mode="json")
    selected = payload["candidates"]
    assert len(selected) > 3
    for method in payload["method_results"]:
        for index, candidate in enumerate(method["candidates"]):
            # An explicit synthetic oversized result exercises the projection boundary.
            candidate["metrics"]["synthetic_large_condition"] = "bounded-fixture-condition" * 200
            candidate["data_gaps"].append("critical-gap-" + str(index))
            candidate["next_data_requirements"].append("required-original-" + str(index))
    # Keep selected objects identical to their fixed method candidates.
    generated = {c["candidate_key"]: c for m in payload["method_results"] for c in m["candidates"]}
    payload["candidates"] = [copy.deepcopy(generated[c["candidate_key"]]) for c in selected]
    payload["pattern_evaluations"][0].pop("value_score", None)
    progress = {
        "kdd_source_artifact_id": "source-fixed",
        "kdd_artifacts": [
            {
                "id": "result-fixed",
                "kind": "pattern_result",
                "version": result.result_hash,
                "source_artifact_id": "source-fixed",
                "source_version": result.snapshot_hash,
            }
        ],
        "kdd_results": {"result-fixed": payload},
    }
    before = copy.deepcopy(progress)
    output = analysis_results(progress)[0]["output"]
    projection = output["report_projection"]
    index = projection["selected_index"]
    assert [item["candidate_key"] for item in index] == [item["candidate_key"] for item in selected]
    detailed_selected = [item for item in output["patterns"] if item["selected"]]
    assert [item["candidate_key"] for item in detailed_selected] == [
        item["candidate_key"] for item in selected[:2]
    ]
    decisions = {item["candidate_key"]: item["decision"] for item in payload["selection_decisions"]}
    assert all(
        n <= 1
        for n in Counter(
            decisions[p["candidate_key"]] for p in output["patterns"] if not p["selected"]
        ).values()
    )
    for row in index:
        fullref = row["full_result_ref"]
        assert fullref["id"] == "result-fixed" and fullref["version"] == result.result_hash
        _, _, method_index, _, candidate_index = fullref["output_path"].split("/")
        assert (
            payload["method_results"][int(method_index)]["candidates"][int(candidate_index)]["candidate_key"]
            == row["candidate_key"]
        )
        evaluation = next(
            e for e in payload["pattern_evaluations"] if e["candidate_key"] == row["candidate_key"]
        )
        assert ("value_score" in row) == ("value_score" in evaluation)
        if "value_score" in row:
            assert row["value_score"] == evaluation["value_score"]
    for row in output["patterns"]:
        assert row["metrics"] == generated[row["candidate_key"]]["metrics"]
    assert set(projection["all_pattern_gaps"]) == {g for c in generated.values() for g in c["data_gaps"]}
    assert {"required-original-" + str(i) for i in range(len(generated))} <= set(
        projection["all_required_evidence"]
    )
    assert projection["omitted_selected_detail_count"] == len(selected) - 2
    assert projection["generated_count"] == len(generated)
    assert "报告详情样例，不是重新筛选" in projection["qualification"]
    assert progress == before


def test_large_who_projection_keeps_each_method_basis_and_all_selected_locations():
    original = NormalizedSnapshot.model_validate_json(
        (FIXTURES / "adopted_eight_notice_projection.json").read_text()
    )
    result = run_kdd(original, KddPolicy(kdd_direction="public_health", as_of=CUTOFF))
    payload = result.model_dump(mode="json")
    for method_index, method in enumerate(payload["method_results"]):
        for index, candidate in enumerate(method["candidates"]):
            candidate["metrics"]["synthetic_large_condition"] = "SYNTHETIC exact source conditions " * 150
            candidate["data_gaps"].append(f"critical-who-gap-{method_index}-{index}")
            candidate["next_data_requirements"].append(f"who-original-required-{method_index}-{index}")
    candidates = {item["candidate_key"]: item for m in payload["method_results"] for item in m["candidates"]}
    payload["candidates"] = [copy.deepcopy(candidates[c["candidate_key"]]) for c in payload["candidates"]]
    progress = {
        "kdd_source_artifact_id": "who-source-fixed",
        "kdd_artifacts": [
            {
                "id": "who-result-fixed",
                "kind": "pattern_result",
                "version": result.result_hash,
                "source_artifact_id": "who-source-fixed",
                "source_version": result.snapshot_hash,
            }
        ],
        "kdd_results": {"who-result-fixed": payload},
    }
    before = copy.deepcopy(progress)
    projected = analysis_results(progress)
    for method_index, item in enumerate(projected):
        basis = item["calculation_basis"]
        assert basis["population"] == payload["data_preparation"]["coverage"]
        assert basis["denominator"] == payload["data_preparation"]["denominator"]
        assert basis["analysis_unit"] == payload["data_preparation"]["unit_contract"]
        assert basis["period"] == {
            "as_of": payload["data_preparation"]["as_of"],
            "time_mode": payload["data_preparation"]["time_mode"],
        }
        assert item["parameters"]["policy_hash"] == payload["policy_hash"]
        assert "policy" not in item["parameters"]
        assert item["method_version"] == payload["method_results"][method_index]["method_version"]
        output = item["output"]
        projection = output["report_projection"]
        assert projection["version"] == "who_shared_result_identity_v4"
        common = projection["full_result_ref"]
        assert common == {"id": "who-result-fixed", "version": result.result_hash}
        expected_keys = [
            c["candidate_key"] for c in payload["candidates"] if c["method_id"] == output["method"]
        ]
        assert [c["candidate_key"] for c in projection["selected_index"]] == expected_keys
        for selected in projection["selected_index"]:
            assert set(selected["full_result_ref"]) == {"output_path"}
            _, _, method, _, candidate = selected["full_result_ref"]["output_path"].split("/")
            assert (
                payload["method_results"][int(method)]["candidates"][int(candidate)]["candidate_key"]
                == selected["candidate_key"]
            )
        full_candidates = payload["method_results"][method_index]["candidates"]
        assert set(projection["all_pattern_gaps"]) == {gap for c in full_candidates for gap in c["data_gaps"]}
        assert {req for c in full_candidates for req in c["next_data_requirements"]} <= set(
            projection["all_required_evidence"]
        )
        for candidate in output["patterns"]:
            assert candidate["metrics"] == candidates[candidate["candidate_key"]]["metrics"]
        assert sum(c["selected"] for c in output["patterns"]) <= 2
    assert progress == before
