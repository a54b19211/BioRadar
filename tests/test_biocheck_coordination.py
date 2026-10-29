"""Current BioCheck methods, fixed SYNTHETIC callbacks, and no model/client or upstream writes."""

import copy
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bioradar.adapters.biocheck import build_packet, check_biocheck, load_biocheck, run_biocheck
from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def current_biocheck():
    directory = Path(
        os.environ.get("BIORADAR_TEST_BIOCHECK_DIR", Path(__file__).resolve().parents[2] / "BioCheck")
    )
    if not (directory / "app/core.py").is_file():
        pytest.skip("Current BioCheck checkout required; set BIORADAR_TEST_BIOCHECK_DIR")
    settings = Settings(biocheck_project_dir=directory)
    loaded = load_biocheck(settings)
    assert loaded[0] is not None, "Configured current BioCheck methods must load successfully"
    return settings, loaded


def packet():
    return build_packet(
        "SYNTHETIC: does A activate B?",
        [{"key": "E1", "text": "A activates B.", "record_ref": {"adopted_version": "fixed-v1"}}],
    )


class ControlledCalls:
    def __init__(self, *, invalid_segment=False, contradicted=False, fail_at=None, error=None):
        self.stages = []
        self.checks = 0
        self.invalid_segment = invalid_segment
        self.contradicted = contradicted
        self.fail_at, self.error = fail_at, error
        self.draft = "A inhibits B [E1]." if contradicted else "A activates B [E1]."
        self.revised = "A activates B [E1]."

    def __call__(self, stage, instruction, payload):
        self.stages.append(stage)
        if stage == self.fail_at:
            raise self.error
        if stage in {"plan_bound", "biocheck_plan"}:
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
        if stage in {"write_bound", "biocheck_write"}:
            return {"draft": self.draft}
        if stage in {"revise", "biocheck_revise"}:
            return {"replacements": [{"unit_id": "U1", "text": self.revised}]}
        assert stage in {"verify", "biocheck_check", "biocheck_recheck"}, stage
        self.checks += 1
        first = self.checks == 1
        verdict = "contradicted" if first and self.contradicted else "supported"
        segment = "S999" if first and self.invalid_segment else "S1"
        span = payload["draft"].split(" [E1]")[0]
        return {
            "reviews": [
                {
                    "unit_id": "U1",
                    "verdict": verdict,
                    "issues": [],
                    "claims": [
                        {
                            "span": span,
                            "kind": "fact",
                            "verdict": verdict,
                            "evidence": [{"evidence_id": "E1", "segment_id": segment}],
                        }
                    ],
                }
            ],
            "coverage": {"verdict": "complete"},
        }


def execute(mode, settings, data, calls, loaded):
    if mode == "run":
        return run_biocheck(settings, data, calls, loaded=loaded)
    return check_biocheck(settings, data, calls.draft, calls, loaded=loaded)


def model_error(code, stage, status):
    return ModelCallError(
        code,
        "SYNTHETIC budget boundary",
        ModelCallReceipt(
            call_id="synthetic-coordination-call",
            stage=stage,
            input_hash="synthetic-input",
            prompt_version="synthetic-prompt",
            requested_model="synthetic-model",
            configured_model_revision=None,
            started_at=datetime.now(UTC),
            status=status,
            max_output_tokens=128,
        ),
        409,
    )


@pytest.mark.parametrize("mode", ["run", "check"])
def test_invalid_support_locator_retains_upstream_review_without_revision(current_biocheck, mode):
    settings, loaded = current_biocheck
    data = packet()
    upstream_calls = ControlledCalls(invalid_segment=True)
    upstream = loaded[0].run(copy.deepcopy(data), upstream_calls)
    assert upstream["status"] == "REVIEW" and upstream["revision"] is None
    calls = ControlledCalls(invalid_segment=True)
    result = execute(mode, settings, data, calls, loaded)
    assert result["status"] == "REVIEW"
    assert result["verification"] == upstream["final_review"] == result["initial_verification"]
    assert result["verification"]["verification_errors"]
    assert result["verification"]["problem_units"] == ["U1"]
    assert result["revision"] is None and result["final_text"] == calls.draft
    assert result["repair_error_code"] == "biocheck_verification_errors"
    assert calls.stages == (
        ["biocheck_plan", "biocheck_write", "biocheck_check"] if mode == "run" else ["biocheck_check"]
    )


@pytest.mark.parametrize("mode", ["run", "check"])
def test_valid_negative_review_can_still_repair_once_and_check_exact_text(current_biocheck, mode):
    settings, loaded = current_biocheck
    calls = ControlledCalls(contradicted=True)
    result = execute(mode, settings, packet(), calls, loaded)
    assert result["initial_verification"]["status"] == "FAIL"
    assert result["initial_verification"]["verification_errors"] == []
    assert result["status"] == "PASS"
    assert result["final_text"] == result["verification"]["draft"] == calls.revised
    assert calls.stages[-2:] == ["biocheck_revise", "biocheck_recheck"]


@pytest.mark.parametrize("mode", ["run", "check"])
@pytest.mark.parametrize("stage", ["biocheck_revise", "biocheck_recheck"])
@pytest.mark.parametrize(
    "code", ["analysis_model_budget", "analysis_token_budget", "analysis_time_budget", "model_input_budget"]
)
def test_unsent_optional_budget_stop_preserves_checked_original(current_biocheck, mode, stage, code):
    settings, loaded = current_biocheck
    error = (
        model_error(code, stage, "not_sent")
        if code == "model_input_budget"
        else DomainError(code, "SYNTHETIC pre-dispatch exhaustion", 409)
    )
    calls = ControlledCalls(contradicted=True, fail_at=stage, error=error)
    result = execute(mode, settings, packet(), calls, loaded)
    assert result["status"] == "FAIL"
    assert result["final_text"] == result["verification"]["draft"] == calls.draft
    assert result["verification"] == result["initial_verification"]
    assert result["revision"] is None
    assert result["repair_error_code"] == code
    assert result["repair_stopped_at"] == stage
    assert "发送前" in result["repair_stop_reason"]
    if stage == "biocheck_recheck":
        assert result["unverified_revision"]["draft"] == calls.revised
    else:
        assert result["unverified_revision"] is None
    assert calls.stages[-1] == stage


@pytest.mark.parametrize("status", ["failed", "unconfirmed", "completed"])
@pytest.mark.parametrize(
    "code", ["analysis_model_budget", "analysis_token_budget", "analysis_time_budget", "model_input_budget"]
)
def test_sent_or_unknown_model_receipts_are_not_optional_budget_fallback(current_biocheck, status, code):
    settings, loaded = current_biocheck
    error = model_error(code, "biocheck_recheck", status)
    calls = ControlledCalls(contradicted=True, fail_at="biocheck_recheck", error=error)
    with pytest.raises(ModelCallError) as failure:
        check_biocheck(settings, packet(), calls.draft, calls, loaded=loaded)
    assert failure.value is error


@pytest.mark.parametrize("code", ["model_call_outcome_unconfirmed", "model_timeout", "analysis_paused"])
def test_nonbudget_failures_keep_their_blocking_meaning(current_biocheck, code):
    settings, loaded = current_biocheck
    error = DomainError(code, "SYNTHETIC unresolved call or permission boundary", 409)
    calls = ControlledCalls(contradicted=True, fail_at="biocheck_recheck", error=error)
    with pytest.raises(DomainError) as failure:
        check_biocheck(settings, packet(), calls.draft, calls, loaded=loaded)
    assert failure.value is error


@pytest.mark.parametrize("mode", ["run", "check"])
def test_budget_failure_before_initial_verification_cannot_claim_a_checked_draft(current_biocheck, mode):
    settings, loaded = current_biocheck
    error = DomainError("analysis_time_budget", "SYNTHETIC no completed verification", 409)
    calls = ControlledCalls(fail_at="biocheck_check", error=error)
    with pytest.raises(DomainError) as failure:
        execute(mode, settings, packet(), calls, loaded)
    assert failure.value is error


def test_selected_support_audit_is_opt_in_and_remains_separate(current_biocheck):
    settings, loaded = current_biocheck
    data, calls = packet(), ControlledCalls()
    result = run_biocheck(settings, data, calls, loaded=loaded)
    assert result["status"] == "PASS"
    assert calls.stages == ["biocheck_plan", "biocheck_write", "biocheck_check"]
    before = copy.deepcopy(result)
    audit_stages = []

    def audit(stage, instruction, payload):
        audit_stages.append(stage)
        return {
            "checks": [
                {
                    "unit_id": "U1",
                    "claim_index": 0,
                    "verdict": "supported",
                    "reason": "SYNTHETIC selected source explicitly states activation",
                    "support": [{"evidence_id": "E1", "segment_id": "S1", "quote": "A activates B."}],
                }
            ]
        }

    selected = loaded[0].audit_support(data, result["verification"], audit, targets=[("U1", 0)])
    assert audit_stages == ["support_audit"]
    assert selected["status"] == "PASS" and len(selected["checks"]) == 1
    assert "不代表整稿通过" in selected["scope"]
    assert result == before


def test_packet_preserves_sourced_publication_ids_without_guessing_or_mutation():
    data = [
        {
            "key": "paper1",
            "text": "SYNTHETIC selected source",
            "record_ref": {"adopted_version": "fixed-v1"},
            "pmid": "context-value-is-not-the-source",
            "doi": "context-doi",
            "facts": {"pmid": "37924427", "doi": "10.1007/S40265-023-01954-W"},
        }
    ]
    before = copy.deepcopy(data)
    result = build_packet("SYNTHETIC identity", data)
    assert result["evidence"][0]["pmid"] == "37924427"
    assert result["evidence"][0]["doi"] == "10.1007/S40265-023-01954-W"
    assert data == before
    data[0]["facts"] = {"pmid": None, "doi": None}
    unknown = build_packet("SYNTHETIC explicit unknown", data)["evidence"][0]
    assert unknown["pmid"] is None and unknown["doi"] is None
    for name in ("facts", "pmid", "doi"):
        data[0].pop(name, None)
    data[0]["source_id"] = "pubmed"
    data[0]["source_record_id"] = "37924427"
    not_guessed = build_packet("SYNTHETIC record identity stays separate", data)["evidence"][0]
    assert "pmid" not in not_guessed and "doi" not in not_guessed
