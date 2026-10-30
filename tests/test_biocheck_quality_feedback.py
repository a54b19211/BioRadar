"""BioCheck feedback uses saved findings; revised text stays unverified until Radar checks it."""

import copy
from types import SimpleNamespace

import pytest
from bioradar.adapters.biocheck import quality_feedback_biocheck, revise_biocheck
from bioradar.api.errors import DomainError

pytestmark = pytest.mark.ENGINE


def packet():
    return {
        "question": "SYNTHETIC: does the source support this sentence?",
        "evidence": [{"id": "E1", "version": "fixed-v1", "text": "SYNTHETIC source says A activates B."}],
    }


def review():
    draft = "SYNTHETIC A inhibits B [E1]."
    return {
        "status": "REVIEW",
        "draft": draft,
        "problem_units": ["U1"],
        "verification_errors": [],
        "coverage": {"verdict": "complete"},
        "units": [
            {
                "unit_id": "U1",
                "text": draft,
                "start": 0,
                "end": len(draft),
                "claims": [
                    {
                        "span": "A inhibits B",
                        "kind": "fact",
                        "verdict": "contradicted",
                        "evidence": [{"evidence_id": "E1", "version": "fixed-v1"}],
                    }
                ],
                "issues": [],
            }
        ],
    }


def feedback():
    return {
        "machine_status": "REVIEW",
        "audit_status": "not_requested",
        "evidence_gaps": [],
        "repair_units": ["U1"],
        "review_required": [],
    }


class FixedMethods:
    """Return explicit synthetic method receipts; the adapter is the unit under test."""

    def __init__(self, result=None):
        self.result = result if result is not None else feedback()
        self.inputs = []
        self.revisions = 0

    def quality_feedback(self, saved, audit=None, *, source_errors=()):
        self.inputs.append((saved, audit, source_errors))
        return copy.deepcopy(self.result)

    def revise(self, data, saved, invoke, *, audit=None, source_errors=()):
        self.revisions += 1
        return invoke("revise", "SYNTHETIC revision instruction", {"draft": saved["draft"]})


def settings():
    return SimpleNamespace(biocheck_project_dir=None)


def test_feedback_is_zero_call_pure_and_retains_the_actual_machine_status():
    data, saved, methods = packet(), review(), FixedMethods()
    before = copy.deepcopy((data, saved))
    result = quality_feedback_biocheck(settings(), data, saved, loaded=(methods, "method-sha"))
    assert (data, saved) == before
    assert methods.revisions == 0 and len(methods.inputs) == 1
    assert result["machine_status"] == "REVIEW"
    assert result["repair_units"] == ["U1"] and result["review_required"] == []
    assert result["provider_version"] == "method-sha"
    assert result["evidence_versions"]["E1"]["version"] == "fixed-v1"
    assert len(result["text_sha256"]) == 64


@pytest.mark.parametrize("kind", ["source_failure", "verification_error", "audit_uncertain"])
def test_waiting_feedback_never_sends_a_revision(kind):
    waiting = feedback()
    waiting["review_required"] = [{"kind": kind, "reason": "SYNTHETIC unresolved error"}]
    methods = FixedMethods(waiting)
    calls = []
    result = revise_biocheck(
        settings(),
        packet(),
        review(),
        lambda *args: calls.append(args),
        source_errors=["SYNTHETIC source error"] if kind == "source_failure" else (),
        loaded=(methods, "method-sha"),
    )
    assert result["status"] == "blocked"
    assert result["changed_units"] == [] and result["needs_verification"] is False
    assert not calls and methods.revisions == 0
    if kind == "source_failure":
        assert methods.inputs[0][2] == ("SYNTHETIC source error",)


def test_pure_evidence_gap_goes_to_retrieval_without_auto_revision():
    missing = feedback()
    missing.update(
        repair_units=[],
        machine_status="PASS",
        evidence_gaps=[
            {
                "kind": "evidence_selection",
                "unit_id": "U1",
                "claim_index": 0,
                "span": "SYNTHETIC claim",
                "reason": "Selected passage does not support it.",
                "action": "reselect_or_retrieve",
            }
        ],
    )
    methods = FixedMethods(missing)
    result = revise_biocheck(
        settings(),
        packet(),
        review(),
        lambda *args: pytest.fail("Unexpected revision"),
        loaded=(methods, "candidate-method"),
    )
    assert result["status"] == "not_changed" and result["changed_units"] == []
    assert result["feedback"]["machine_status"] == "PASS"
    assert result["feedback"]["evidence_gaps"][0]["action"] == "reselect_or_retrieve"


def test_source_version_or_selected_audit_mismatch_blocks_revision():
    methods = FixedMethods()
    data = packet()
    data["evidence"][0]["version"] = "new-version"
    result = quality_feedback_biocheck(settings(), data, review(), loaded=(methods, "method-sha"))
    assert result["repair_units"] == []
    assert any(item["kind"] == "source_mismatch" for item in result["review_required"])
    audit = {"status": "PASS", "text_sha256": "a previous report", "checks": [], "verification_errors": []}
    result = quality_feedback_biocheck(
        settings(), packet(), review(), audit=audit, loaded=(methods, "method-sha")
    )
    assert result["repair_units"] == []
    assert any(item["kind"] == "audit_mismatch" for item in result["review_required"])


def test_changed_draft_requires_new_whole_report_verification_and_has_no_inherited_audit():
    saved, methods, calls = review(), FixedMethods(), []
    corrected = "SYNTHETIC A activates B [E1]."

    def invoke(stage, instruction, payload):
        calls.append((stage, payload["draft"]))
        return {
            "draft": corrected,
            "changed_units": [{"unit_id": "U1", "before": saved["draft"], "after": corrected}],
        }

    result = revise_biocheck(settings(), packet(), saved, invoke, loaded=(methods, "method-sha"))
    assert calls == [("biocheck_revise", saved["draft"])]
    assert result["status"] == "changed" and result["needs_verification"] is True
    assert result["draft"] == corrected and len(result["changed_units"]) == 1
    assert result["verification"] is None and result["support_audit"] is None
    assert saved["draft"] != corrected


@pytest.mark.parametrize("declared_noop", [True, False])
def test_unchanged_text_is_not_a_success_even_when_method_reports_a_changed_unit(declared_noop):
    saved, methods = review(), FixedMethods()

    def invoke(*args):
        changes = (
            [{"unit_id": "U1", "before": saved["draft"], "after": saved["draft"]}] if declared_noop else []
        )
        return {"draft": saved["draft"], "changed_units": changes}

    result = revise_biocheck(settings(), packet(), saved, invoke, loaded=(methods, "method-sha"))
    assert result["status"] == "not_changed" and result["changed_units"] == []
    assert result["needs_verification"] is False


@pytest.mark.parametrize("defect", ["unapproved_unit", "undeclared_body", "invalid_before"])
def test_revision_receipt_cannot_change_unapproved_text(defect):
    saved, methods = review(), FixedMethods()
    corrected = "SYNTHETIC corrected claim [E1]."
    changes = [{"unit_id": "U1", "before": saved["draft"], "after": corrected}]
    if defect == "unapproved_unit":
        changes[0]["unit_id"] = "U2"
    elif defect == "invalid_before":
        changes[0]["before"] = "A different report"
    candidate = corrected + " Extra undeclared claim." if defect == "undeclared_body" else corrected
    result = revise_biocheck(
        settings(),
        packet(),
        saved,
        lambda *args: {"draft": candidate, "changed_units": changes},
        loaded=(methods, "method-sha"),
    )
    assert result["status"] == "failed"
    assert result["draft"] == saved["draft"] and result["changed_units"] == []
    assert result["error_code"] == "biocheck_revision_invalid"


@pytest.mark.parametrize(
    "failure",
    [
        DomainError("model_call_outcome_unconfirmed", "SYNTHETIC unknown model outcome", 409),
        ValueError("SYNTHETIC callback failure"),
    ],
)
def test_revision_callback_failures_propagate_without_claiming_success(failure):
    def invoke(*args):
        raise failure

    with pytest.raises(type(failure), match="SYNTHETIC"):
        revise_biocheck(settings(), packet(), review(), invoke, loaded=(FixedMethods(), "method-sha"))


def test_unavailable_feedback_method_is_explicit_and_cannot_reuse_legacy_repair():
    methods = SimpleNamespace(revise=lambda *args: pytest.fail("Unexpected legacy repair"))
    result = revise_biocheck(
        settings(),
        packet(),
        review(),
        lambda *args: pytest.fail("Unexpected callback"),
        loaded=(methods, "legacy-method"),
    )
    assert result["status"] == "blocked"
    assert result["error_code"] == "biocheck_feedback_unavailable"
    assert result["feedback"]["review_required"]
