"""SYNTHETIC budget-stop disclosure; retained prose and received ledgers stay exact."""

import copy
import hashlib

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import freeze_passages
from bioradar.research.investigations.analysis_service import AnalysisService
from bioradar.research.investigations.reading_coverage import retained_reading_coverage
from test_retained_policy_reading import saved_reading

pytestmark = pytest.mark.ENGINE


def host_and_state():
    fixed, progress = saved_reading()
    fixed["execution_version"] = "query-step-v1"
    fixed["query_policy"]["final_reference_repair_version"] = "bioradar-synthesis-reference-repair-v1"
    progress["limitations"] = []
    checkpoints = []
    host = object.__new__(AnalysisService)
    host._checkpoint = lambda *args, **kwargs: checkpoints.append(copy.deepcopy(kwargs))
    return host, fixed, progress, checkpoints


def test_budget_stop_keeps_old_reading_but_exposes_later_evidence_and_professional_result():
    host, fixed, progress, checkpoints = host_and_state()
    old_reading, ledger = copy.deepcopy(progress["query_policy"]["reading"]), copy.deepcopy(progress["calls"])
    additional = copy.deepcopy(progress["evidence"][0])
    additional.update(
        key="E2",
        text="SYNTHETIC later evidence not present in the received reading request.",
        material_entry_id=None,
    )
    additional["record_ref"]["object_id"] = "SYNTHETIC-later-document"
    additional["record_ref"]["adopted_version"] = "SYNTHETIC-later-version"
    additional["end"] = len(additional["text"])
    additional["text_sha256"] = hashlib.sha256(additional["text"].encode()).hexdigest()
    progress["evidence"].append(freeze_passages(additional))
    artifact = {
        "id": "professional:SYNTHETIC",
        "version": "SYNTHETIC-fixed-result",
        "kind": "professional_assessment",
        "provider": "mech_evidence",
        "status": "review_required",
    }
    progress["kdd_artifacts"] = [artifact]
    progress["kdd_results"] = {artifact["id"]: {"status": artifact["status"]}}
    progress["queries"] = [
        {
            "index": 0,
            "decision_step": 3,
            "action_id": "SYNTHETIC-doc",
            "status": "read",
            "evidence_keys": ["E2"],
            "cost": {"tool_calls": 1},
        },
        {
            "index": 1,
            "decision_step": 4,
            "action_id": "SYNTHETIC-professional",
            "status": "read",
            "artifact_refs": [{"id": artifact["id"], "version": artifact["version"]}],
            "cost": {"tool_calls": 1},
        },
    ]
    result = host._retained_reading_draft(
        "SYNTHETIC-analysis",
        "SYNTHETIC-lease",
        fixed,
        progress,
        DomainError("analysis_token_budget", "SYNTHETIC exhausted after actual new results", 409),
    )
    basis = result["query_policy"]["synthesis_reading_basis"]
    assert result["conditional_answer"] == old_reading["conditional_answer"]
    assert progress["query_policy"]["reading"] == old_reading and progress["calls"] == ledger
    assert basis["coverage_version"] == "retained-reading-coverage-v1"
    assert basis["cannot_use_as_current_conclusion"] is True
    assert basis["call_key"] == ledger[0]["call_key"] and basis["call_index"] == 0
    assert basis["after_query_index"] is None and basis["after_decision_step"] is None
    assert [item["evidence_key"] for item in basis["input_evidence"]] == ["E1"]
    assert [item["evidence_key"] for item in basis["not_incorporated"]["evidence"]] == ["E2"]
    assert basis["not_incorporated"]["evidence"][0]["record_ref"] == additional["record_ref"]
    pending = basis["not_incorporated"]["artifacts"]
    assert pending == [{**artifact, "query_index": 1, "decision_step": 4, "receipt_status": "read"}]
    assert result["review"]["status"] == "needs_review" and result["report"] is None
    assert "不能作为当前结论" in basis["qualification"]
    assert checkpoints[-1]["result"] == result == progress["final_delivery_result"]
    assert "payload" not in basis and "output" not in basis


@pytest.mark.parametrize("version,unincorporated", [("original-result", False), ("later-result", True)])
def test_artifact_input_refs_come_only_from_received_payload_and_match_exact_version(version, unincorporated):
    _, _, progress, _ = host_and_state()
    original = progress["calls"][0]
    original["payload"]["analysis_results"] = [
        {
            "output": {
                "report_projection": {
                    "full_result_ref": {"id": "pattern:SYNTHETIC", "version": "original-result"}
                }
            }
        }
    ]
    original["payload"]["query_history"] = [
        {"index": 0, "action_id": "SYNTHETIC-old-action", "status": "read"}
    ]
    progress["queries"] = [{"index": 0, "decision_step": 2, "action_id": "SYNTHETIC-old-action"}]
    progress["kdd_artifacts"] = [{"id": "pattern:SYNTHETIC", "version": version, "kind": "pattern_result"}]
    before = copy.deepcopy(progress)
    basis = retained_reading_coverage(progress, original)
    assert basis["input_artifact_refs"] == [{"id": "pattern:SYNTHETIC", "version": "original-result"}]
    assert bool(basis["not_incorporated"]["artifacts"]) is unincorporated
    assert basis["after_query_index"] == 0 and basis["after_decision_step"] == 2
    assert progress == before


def test_unknown_synthesis_or_professional_outcome_is_not_replaced_by_retained_delivery():
    host, fixed, progress, checkpoints = host_and_state()
    before = copy.deepcopy(progress)
    failure = DomainError("professional_outcome_unconfirmed", "SYNTHETIC unknown outcome", 502)
    with pytest.raises(DomainError) as raised:
        host._retained_reading_draft("id", "lease", fixed, progress, failure)
    assert raised.value is failure and progress == before and not checkpoints
