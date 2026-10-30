"""Known report failures retain drafts; unknown calls never turn into a retry or approval."""

import copy
import json
from types import SimpleNamespace

import pytest
from bioradar.research.investigations.analysis_service import (
    AnalysisService,
    InvestigationAnalysisRecord,
)
from sqlalchemy.orm import Session
from test_biocheck_coordination import (
    ControlledCalls,
    execute,
    model_error,
    packet,
)
from test_biocheck_coordination import current_biocheck as current_biocheck  # noqa: F401
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start

pytestmark = pytest.mark.ENGINE


@pytest.mark.parametrize("mode", ["run", "check"])
@pytest.mark.parametrize("code", ["model_output_invalid", "model_output_truncated"])
def test_known_failed_initial_verification_preserves_exact_draft(current_biocheck, mode, code):
    settings, loaded = current_biocheck
    failure = model_error(code, "biocheck_check", "failed")
    calls = ControlledCalls(fail_at="biocheck_check", error=failure)
    data = packet()
    original = copy.deepcopy(data)
    result = execute(mode, settings, data, calls, loaded)
    assert result["status"] == "failed"
    assert result["final_text"] == calls.draft
    assert result["verification"] is None
    assert result["initial_verification"] is None
    assert result["revision"] is None and result["error_code"] == code
    assert calls.stages == (
        ["biocheck_plan", "biocheck_write", "biocheck_check"] if mode == "run" else ["biocheck_check"]
    )
    assert data == original


@pytest.mark.parametrize("mode", ["run", "check"])
@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("unconfirmed", "model_output_invalid"),
        ("unconfirmed", "model_call_outcome_unconfirmed"),
        ("failed", "model_transport_error"),
    ],
)
def test_unknown_or_other_failures_propagate_without_resend(current_biocheck, mode, status, code):
    from bioradar.adapters.reasoning_model import ModelCallError

    settings, loaded = current_biocheck
    failure = model_error(code, "biocheck_check", status)
    calls = ControlledCalls(fail_at="biocheck_check", error=failure)
    with pytest.raises(ModelCallError) as raised:
        execute(mode, settings, packet(), calls, loaded)
    assert raised.value is failure
    assert calls.stages == (
        ["biocheck_plan", "biocheck_write", "biocheck_check"] if mode == "run" else ["biocheck_check"]
    )


@pytest.mark.parametrize("mode", ["run", "check"])
@pytest.mark.parametrize("code", ["method_input_invalid", "professional_outcome_unconfirmed"])
def test_initial_method_refusal_retains_draft_but_unknown_domain_still_propagates(
    current_biocheck, mode, code
):
    from bioradar.api.errors import DomainError

    settings, loaded = current_biocheck
    failure = DomainError(code, "SYNTHETIC explicit method refusal or unknown outcome", 502)
    calls = ControlledCalls(fail_at="biocheck_check", error=failure)
    if code == "method_input_invalid":
        result = execute(mode, settings, packet(), calls, loaded)
        assert result["status"] == "failed" and result["final_text"] == calls.draft
        assert result["verification"] is None and result["revision"] is None
        assert result["error_code"] == code
    else:
        with pytest.raises(DomainError) as raised:
            execute(mode, settings, packet(), calls, loaded)
        assert raised.value is failure
    assert calls.stages == (
        ["biocheck_plan", "biocheck_write", "biocheck_check"] if mode == "run" else ["biocheck_check"]
    )


@pytest.mark.parametrize("code", ["method_input_invalid", "professional_outcome_unconfirmed"])
def test_known_generation_refusal_has_no_professional_draft_and_unknown_is_not_retried(
    current_biocheck, code
):
    from bioradar.api.errors import DomainError

    settings, loaded = current_biocheck
    failure = DomainError(code, "SYNTHETIC generation refusal or unknown outcome", 502)
    calls = ControlledCalls(fail_at="biocheck_plan", error=failure)
    if code == "method_input_invalid":
        result = execute("run", settings, packet(), calls, loaded)
        assert result["status"] == "failed" and result["final_text"] is None
        assert result["verification"] is None and result["revision"] is None
        assert result["generation_error"] and result["error_code"] == code
    else:
        with pytest.raises(DomainError) as raised:
            execute("run", settings, packet(), calls, loaded)
        assert raised.value is failure
    assert calls.stages == ["biocheck_plan"]


@pytest.mark.parametrize("has_previous_report", [False, True])
def test_initial_report_checkpoint_never_overwrites_previous_checked_round(has_previous_report):
    class Boundary(Exception):
        pass

    structured = {
        "conditional_answer": {
            "answer": "SYNTHETIC retained structured draft",
            "conditions": [],
            "unknowns": [],
        },
        "claims": [],
        "explanations": [],
        "gaps": [],
        "limitations": [],
        "review": {"status": "passed"},
        "update": {"stance": "initial", "reasons": [], "changed_claims": []},
    }
    checked = SimpleNamespace(model_dump=lambda **kwargs: copy.deepcopy(structured))
    service = object.__new__(AnalysisService)
    service._guard = lambda *args, **kwargs: None
    observed = {}

    def checkpoint(*args, **kwargs):
        observed.update(kwargs)
        raise Boundary()

    service._checkpoint = checkpoint
    fixed = {"question": "SYNTHETIC retained report boundary", "model_parameters": {"max_calls": 24}}
    progress = {"calls": [], "report_rounds": []}
    if has_previous_report:
        progress["report_rounds"] = [{"result": {"report": {"final_text": "SYNTHETIC older checked text"}}}]
    with pytest.raises(Boundary):
        service._saved_final_report("unused", "unused", fixed, progress, checked, {})
    assert observed["fixed"] == fixed
    if has_previous_report:
        assert "result" not in observed
    else:
        assert observed["result"] == structured


def test_followup_excerpt_budget_retains_initial_reading_and_actual_search_receipt(
    runtime, database, monkeypatch
):
    import bioradar.research.investigations.analysis_service as module

    make, actor, view, source, model, _, _ = runtime
    service = make()
    accepted = start(runtime, auto_retrieve=True)
    adopt = service._adopt_original
    before = {}

    def exhaust_after_reading(identifier, token, fixed, progress, original, tag):
        before.setdefault("evidence", copy.deepcopy(progress["evidence"]))
        before.setdefault("reading", copy.deepcopy(progress["query_policy"]["reading"]))
        before.setdefault(
            "received",
            [
                copy.deepcopy(call)
                for call in progress["calls"]
                if call["stage"] == "read_plan" and call["status"] == "received"
            ],
        )
        used = sum(
            len(item["text"]) + len(json.dumps(item.get("facts", {}), ensure_ascii=False))
            for item in progress["evidence"]
        )
        # Exercise the actual excerpt-budget gate, with no direct injected DomainError.
        with monkeypatch.context() as patch:
            original_limits = module.reading_limits
            patch.setattr(module, "reading_limits", lambda value: {
                **original_limits(value), "max_characters": used,
            })
            return adopt(identifier, token, fixed, progress, original, tag)

    service._adopt_original = exhaust_after_reading
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state in {"completed", "needs_review"}, result.error_code
    assert before["received"] and len(before["received"]) == 1
    with Session(database) as session:
        stored = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert stored.progress["evidence"] == before["evidence"]
        assert stored.progress["query_policy"]["reading"] == before["reading"]
        assert [call for call in stored.progress["calls"] if call["stage"] == "read_plan"] == before[
            "received"
        ]
        retained = [
            query
            for query in stored.progress["queries"]
            if (query.get("usage") or {}).get("adoption_stop_reason") == "analysis_evidence_budget"
        ]
        assert retained and all(query["status"] == "read" for query in retained)
        assert all(query["records"] and query["returned"] > 0 for query in retained)
        assert all(query["evidence_keys"] == [] and query["new_versions"] == 0 for query in retained)
        assert all(query["cost"]["tool_calls"] > 0 for query in retained)
        assert any("未读" in message for message in stored.progress["limitations"])
    assert sum(request["task"] == "read_plan" for request in model.requests) == 1
    assert any(path == "/api/history/records" for path, _ in source.calls)


def test_missing_verification_returns_review_need_without_another_provider_request():
    from bioradar.adapters.biocheck import quality_feedback_biocheck

    def forbidden(*args, **kwargs):
        raise AssertionError("No verifier output exists to give the professional feedback service.")

    result = quality_feedback_biocheck(
        SimpleNamespace(), packet(), {},
        loaded=(SimpleNamespace(quality_feedback=forbidden), "fixed-test-version"),
    )
    assert result["error_code"] == "biocheck_verification_unavailable"
    assert result["review_required"][0]["kind"] == "verification_unavailable"
    assert result["evidence_gaps"] == result["repair_units"] == []
