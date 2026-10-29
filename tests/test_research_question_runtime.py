"""Question intake, actual PostgreSQL persistence and bounded synthetic HTTP calls."""

import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bioradar.adapters.reasoning_model import ModelUsage
from bioradar.api.app import create_app
from bioradar.research.investigations.analysis_models import AnalysisBudget, AnalysisCreate
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationCreate, InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_engine_persistence import CREDENTIAL
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start
from test_shared_research_adapters import installed_biocheck as installed_biocheck  # noqa: F401

pytestmark = pytest.mark.ENGINE


def empty_question(runtime):
    make, actor, *_ = runtime
    _, view = InvestigationService(make().engine).create(
        actor,
        InvestigationCreate(title="SYNTHETIC question", question="SYNTHETIC question", is_demo=True),
        "synthetic-empty-question",
    )
    return view


def test_question_without_material_plans_reads_and_separates_followup_queries(runtime, database):
    make, actor, _, _, model, *_ = runtime
    inquiry = empty_question(runtime)
    _, accepted = make().start(
        actor, inquiry["id"], AnalysisCreate(expected_revision=1, auto_retrieve=True), "ask"
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [r["task"] for r in model.requests] == ["question_plan", "read_plan", "synthesize", "check"]
    assert "AND" in model.requests[0]["instruction"] and "对象词放第一项" in model.requests[0]["instruction"]
    assert [(q.phase, q.index, q.new_versions) for q in result.queries] == [
        ("initial", 0, 1),
        ("followup", 0, 0),
    ]
    assert result.result.report["final_text"] and result.result.report["status"] == "unavailable"
    assert result.budget["usage"]["total_tokens"] == 600
    assert result.evidence[0].history_binding and result.evidence[0].material_entry_id
    original = result.model_dump(mode="json")
    current = InvestigationService(make().engine).get(actor, inquiry["id"])
    _, followup = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=current.revision,
            question="SYNTHETIC follow-up question",
            parent_analysis_id=result.id,
            auto_retrieve=True,
        ),
        "followup",
    )
    assert {item["record_ref"]["adopted_version"] for item in followup["evidence"]} == {
        item.record_ref.adopted_version for item in result.evidence
    }
    make().execute(followup["id"], followup["input_hash"])
    assert model.requests[4]["task"] == "read_plan"
    assert (
        "AND" in model.requests[4]["query_instruction"]
        and "对象词放第一项" in model.requests[4]["query_instruction"]
    )
    updated = make().get(actor, inquiry["id"], followup["id"])
    assert updated.state == "completed", updated.error_code
    assert updated.question == "SYNTHETIC follow-up question" and updated.parent_analysis_id == result.id
    old = make().get(actor, inquiry["id"], result.id).model_dump(mode="json")
    assert old["result"] == original["result"] and old["question"] == original["question"]
    adopted = runtime[-1]
    current = InvestigationService(make().engine).get(actor, inquiry["id"])
    InvestigationService(make().engine).append(
        actor,
        inquiry["id"],
        InvestigationEntryCreate.model_validate(
            {
                "kind": "material",
                "expected_revision": current.revision,
                "snapshot_id": adopted["id"],
                "record_ref": runtime[0]()
                .engine.get_snapshot(actor, adopted["id"])
                .snapshot.records[0]
                .record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC new externally selected material after follow-up",
            }
        ),
        "followup-material",
    )
    children = make().list(actor, inquiry["id"]).items
    assert children[0].question == updated.question and children[0].id != updated.id
    with Session(database) as session:
        assert session.scalar(
            select(InvestigationAnalysisRecord).where(InvestigationAnalysisRecord.id == result.id)
        )


def test_call_limit_and_token_limit_stop_before_the_next_http_call(runtime):
    make, actor, view, _, model, *_ = runtime
    _, accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=view["revision"], budget=AnalysisBudget(max_calls=1)),
        "one-model-call",
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.error_code == "analysis_model_budget" and len(model.requests) == 1
    assert result.budget["usage"]["total_tokens"] == 150
    current = InvestigationService(make().engine).get(actor, view["id"])
    _, small = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=current.revision, budget=AnalysisBudget(max_tokens=128)),
        "tiny-token-budget",
    )
    make().execute(small["id"], small["input_hash"])
    assert make().get(actor, view["id"], small["id"]).error_code == "analysis_token_budget"
    assert len(model.requests) == 1


def test_missing_usage_uses_labelled_upper_bound_and_continues(runtime):
    make, actor, view, _, model, *_ = runtime
    accepted = start(runtime)
    service = make()
    invoke = service.model_call

    def no_usage(*args, **kwargs):
        response = invoke(*args, **kwargs)
        response.receipt.usage = ModelUsage()
        return response

    service.model_call = no_usage
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    usage = result.budget["usage"]
    assert result.state == "completed" and len(model.requests) == 3
    assert usage["total_tokens"] is None and usage["unknown_usage_calls"] == 3
    assert usage["estimated_tokens"] == usage["budgeted_tokens"] > 0
    assert usage["monetary_cost"] is None


def test_expired_total_deadline_does_not_send_a_model_request(runtime, database):
    make, actor, view, _, model, *_ = runtime
    accepted = start(runtime)
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        progress = copy.deepcopy(row.progress)
        progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        row.progress = progress
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.error_code == "analysis_time_budget" and not model.requests


def test_final_shared_verification_replaces_generic_check_and_preserves_text(runtime, monkeypatch):
    import bioradar.adapters.biocheck as biocheck

    make, actor, view, _, model, *_ = runtime
    drafts = []
    monkeypatch.setattr(
        biocheck, "provider_status", lambda settings: {"available": True, "provider": "biocheck"}
    )

    def generate(settings, packet, call, *, repair=True, loaded=None):
        call("biocheck_plan", "SYNTHETIC constrained plan", {"task": "shared_verify"})
        call("biocheck_write", "SYNTHETIC constrained report", {"task": "shared_verify"})
        draft = "SYNTHETIC generated report with its conditions and current update."
        drafts.append(draft)
        checked = call(
            "biocheck_check", "SYNTHETIC whole-report verification", {"task": "shared_verify", "draft": draft}
        )
        return {
            "provider": "biocheck",
            "status": checked["status"],
            "final_text": draft,
            "verification": {"status": checked["status"], "draft": draft},
            "evidence_map": packet["evidence_map"],
        }

    monkeypatch.setattr(biocheck, "run_biocheck", generate)
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [c["stage"] for c in result.calls] == [
        "read_plan",
        "synthesize",
        "biocheck_plan",
        "biocheck_write",
        "biocheck_check",
    ]
    assert result.result.report["status"] == "PASS"
    assert result.result.report["final_text"] == result.result.report["verification"]["draft"] == drafts[0]
    assert result.result.professional_status == "not_reviewed"
    assert result.budget["usage"]["total_tokens"] == 750


def test_ask_http_idempotency_options_preserved_failure_and_markdown(runtime, database, monkeypatch):
    from bioradar.research.history import service as history_module

    make, _, _, _, model, settings, _ = runtime
    settings.data_fabric_url = "http://127.0.0.1"
    settings.data_fabric_history_read_token = SecretStr("SYNTHETIC")

    @contextmanager
    def reader(*args, **kwargs):
        yield make().reader

    monkeypatch.setattr(history_module, "history_reader", reader)
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "http-question"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        options = client.get("/api/engine/investigations/research-options?is_demo=true", headers=headers)
        assert options.status_code == 200 and options.json()["limits"]["max_calls"] == 16
        payload = {"question": "SYNTHETIC HTTP question", "is_demo": True}
        response = client.post("/api/engine/investigations/ask", headers=headers, json=payload)
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["analysis"]["evidence"] == []
        make().execute(accepted["analysis_id"], accepted["analysis"]["input_hash"])
        repeated = client.post("/api/engine/investigations/ask", headers=headers, json=payload)
        assert repeated.status_code in {200, 202}, repeated.text
        assert repeated.json()["analysis_id"] == accepted["analysis_id"]
        assert len(model.requests) == 4
        base = f"/api/engine/investigations/{accepted['investigation_id']}/analyses/{accepted['analysis_id']}"
        analysis = client.get(base, headers=headers).json()
        markdown = client.get(base + "/export?format=markdown", headers=headers)
        assert markdown.status_code == 200
        assert markdown.text == analysis["result"]["report"]["final_text"]
        bad = client.post(
            "/api/engine/investigations/ask",
            headers={**headers, "Idempotency-Key": "invalid-budget"},
            json={**payload, "budget": {"max_calls": 17}},
        )
        assert bad.status_code == 422
        saved = bad.json()["error"]["details"]["investigation_id"]
        assert client.get("/api/engine/investigations/" + saved, headers=headers).status_code == 200
        app.state.database = None


def test_empty_initial_lookup_is_explicit_and_does_not_synthesize(runtime):
    make, actor, _, source, model, *_ = runtime
    inquiry = empty_question(runtime)
    source.records = []
    _, accepted = make().start(
        actor, inquiry["id"], AnalysisCreate(expected_revision=1, auto_retrieve=True), "empty"
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_no_material"
    assert [p["task"] for p in model.requests] == ["question_plan"]
    assert len(result.queries) == 1 and result.queries[0].status == "empty"
    assert result.result is None and result.evidence == []


def test_wrong_comparison_is_caught_after_current_draft_review(runtime, database):
    make, actor, view, _, model, *_ = runtime
    parent = start(runtime)
    make().execute(parent["id"], parent["input_hash"])
    old = make().get(actor, view["id"], parent["id"]).result.model_dump(mode="json")

    def inject(payload, result):
        if payload["task"] == "compare":
            result["reasons"] = ["SYNTHETIC unsupported event occurred since the prior version."]
        if payload["task"] == "final_check":
            assert "unsupported event" in payload["draft_to_check"]["update"]["reasons"][0]
            result["findings"] = [
                {
                    "section": "update",
                    "issue": "SYNTHETIC evidence does not establish this new event.",
                    "evidence_keys": [],
                    "claim_id": None,
                }
            ]

    model.transform = inject
    current = InvestigationService(make().engine).get(actor, view["id"])
    _, accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=current.revision,
            parent_analysis_id=parent["id"],
            question="SYNTHETIC changed question",
        ),
        "changed",
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review" and result.result.review.status == "needs_review"
    assert result.calls[-1]["stage"] == "final_check"
    assert result.result.review.findings[0].section == "update"
    assert make().get(actor, view["id"], parent["id"]).result.model_dump(mode="json") == old


def test_shared_callback_rejects_different_payload_under_one_saved_key(runtime, monkeypatch):
    import bioradar.adapters.biocheck as biocheck

    make, actor, view, _, model, *_ = runtime
    monkeypatch.setattr(biocheck, "provider_status", lambda settings: {"available": True})

    def duplicate(settings, packet, call, *, repair=True, loaded=None):
        call("biocheck_plan", "SYNTHETIC plan", {"task": "shared_verify", "revision": 1})
        call("biocheck_plan", "SYNTHETIC plan", {"task": "shared_verify", "revision": 2})
        raise AssertionError("A changed saved request must not reach a second model POST")

    monkeypatch.setattr(biocheck, "run_biocheck", duplicate)
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.error_code == "analysis_call_input_changed"
    assert [c["stage"] for c in result.calls] == ["read_plan", "synthesize", "biocheck_plan"]
    assert len(model.requests) == 3 and result.calls[-1]["status"] == "received"


def test_actual_model_timeout_is_clipped_by_total_time_budget(runtime):
    make, actor, view, _, _, *_ = runtime
    _, accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=view["revision"], budget=AnalysisBudget(max_seconds=5)),
        "five-seconds",
    )
    service = make()
    invoke, timeouts = service.model_call, []

    def timed(settings, **kwargs):
        timeouts.append(settings.semantic_llm_timeout_seconds)
        return invoke(settings, **kwargs)

    service.model_call = timed
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert len(timeouts) == 3 and all(1 <= value < 5 for value in timeouts)


def test_optional_mechanism_cannot_use_the_final_generation_slots(runtime, monkeypatch):
    import bioradar.adapters.biocheck as biocheck
    import bioradar.adapters.mech_evidence as mechanism

    make, actor, view, _, model, *_ = runtime
    monkeypatch.setattr(biocheck, "provider_status", lambda settings: {"available": True})

    monkeypatch.setattr(mechanism, "provider_status", lambda settings: {"available": True})

    def propose(payload, result):
        if payload["task"] == "read_plan":
            result["mechanism_candidate"] = "SYNTHETIC explicitly unproved mechanism candidate."

    model.transform = propose

    def generated(settings, packet, call, *, repair=True, loaded=None):
        for stage in ("biocheck_plan", "biocheck_write", "biocheck_check"):
            call(stage, "SYNTHETIC report method", {"task": "shared_verify"})
        text = "SYNTHETIC bounded generated and verified report."
        return {
            "provider": "biocheck",
            "status": "PASS",
            "final_text": text,
            "verification": {"status": "PASS", "draft": text},
        }

    monkeypatch.setattr(biocheck, "run_biocheck", generated)
    _, accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=view["revision"], budget=AnalysisBudget(max_calls=5)),
        "five-calls",
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert result.result.mechanism_evaluation["status"] == "skipped_budget"
    assert len(model.requests) == 5 and result.calls[-1]["stage"] == "biocheck_check"
    assert result.result.report["status"] == "PASS"


def test_generation_is_not_sent_without_budget_for_its_verification(runtime, monkeypatch):
    import bioradar.adapters.biocheck as biocheck

    make, actor, view, _, model, *_ = runtime
    monkeypatch.setattr(biocheck, "provider_status", lambda settings: {"available": True})

    def generated(settings, packet, call, *, repair=True, loaded=None):
        call("biocheck_plan", "SYNTHETIC report plan", {"task": "shared_verify"})
        call(
            "biocheck_write", "SYNTHETIC report generation", {"task": "shared_verify", "context": "x" * 40000}
        )
        raise AssertionError("No final generation POST should be sent without verification budget")

    monkeypatch.setattr(biocheck, "run_biocheck", generated)
    _, accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=view["revision"], budget=AnalysisBudget(max_tokens=50000)),
        "small-final-budget",
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.error_code == "analysis_token_budget" and result.result is None
    assert [c["stage"] for c in result.calls] == ["read_plan", "synthesize", "biocheck_plan"]
    assert len(model.requests) == 3


def test_provider_observation_changes_do_not_interrupt_the_first_actual_use(runtime, monkeypatch):
    import bioradar.adapters.biocheck as biocheck

    make, actor, view, _, model, *_ = runtime
    version = ["observed-v1"]
    monkeypatch.setattr(
        biocheck,
        "provider_status",
        lambda settings: {
            "available": True,
            "configured": True,
            "version": version[0],
        },
    )
    monkeypatch.setattr(biocheck, "load_biocheck", lambda settings: (object(), version[0]))

    def change_before_use(payload, result):
        if payload["task"] == "read_plan":
            version[0] = "first-used-v2"

    model.transform = change_before_use

    def generate(settings, packet, call, *, repair=True, loaded=None):
        assert loaded[1] == "first-used-v2"
        call("biocheck_plan", "SYNTHETIC pinned plan", {"task": "shared_verify"})
        version[0] = "disk-now-v3"
        call("biocheck_write", "SYNTHETIC pinned write", {"task": "shared_verify"})
        call("biocheck_check", "SYNTHETIC pinned verify", {"task": "shared_verify"})
        return {
            "provider": "biocheck",
            "provider_version": loaded[1],
            "status": "PASS",
            "final_text": "SYNTHETIC report from one loaded method version.",
        }

    monkeypatch.setattr(biocheck, "run_biocheck", generate)
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert result.result.report["provider_version"] == "first-used-v2"
    assert result.providers["biocheck"]["version"] == "first-used-v2"
    assert len(model.requests) == 5


def test_resume_with_used_provider_version_change_keeps_receipts_without_new_post(
    runtime, database, monkeypatch
):
    import bioradar.adapters.biocheck as biocheck
    from sqlalchemy.exc import SQLAlchemyError

    make, actor, view, _, model, *_ = runtime
    version = ["used-v1"]
    monkeypatch.setattr(
        biocheck,
        "provider_status",
        lambda settings: {
            "available": True,
            "configured": True,
            "version": version[0],
        },
    )
    monkeypatch.setattr(biocheck, "load_biocheck", lambda settings: (object(), version[0]))

    def generate(settings, packet, call, *, repair=True, loaded=None):
        call("biocheck_plan", "SYNTHETIC pinned plan", {"task": "shared_verify"})
        call("biocheck_write", "SYNTHETIC pinned write", {"task": "shared_verify"})
        raise AssertionError("The interrupted invocation cannot reach generation")

    monkeypatch.setattr(biocheck, "run_biocheck", generate)
    accepted = start(runtime)
    service = make()
    checkpoint = service._checkpoint
    interrupted = [False]

    def stop_after_plan(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if not interrupted[0] and any(
            c["stage"] == "biocheck_plan" and c["status"] == "received" for c in progress["calls"]
        ):
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC checkpoint interruption")

    service._checkpoint = stop_after_plan
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    assert interrupted[0] and len(model.requests) == 3
    version[0] = "new-method-v2"
    with Session(database) as session, session.begin():
        session.get(InvestigationAnalysisRecord, accepted["id"]).lease_until = datetime.now(UTC) - timedelta(
            seconds=1
        )
    make().recover()
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_provider_changed"
    assert result.calls[-1]["status"] == "received" and len(model.requests) == 3
    assert result.providers["biocheck"]["version"] == "used-v1"


def test_initial_plan_limitation_is_labelled_as_an_earlier_stage(runtime):
    make, actor, _, _, model, *_ = runtime
    inquiry = empty_question(runtime)

    def limit(payload, result):
        if payload["task"] == "question_plan":
            result["limitations"] = ["当前未读取任何材料，本计划不输出事实。"]

    model.transform = limit
    _, accepted = make().start(
        actor, inquiry["id"], AnalysisCreate(expected_revision=1, auto_retrieve=True), "stage-limit"
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.evidence and result.result
    assert "初始查找阶段：当前未读取任何材料，本计划不输出事实。" in result.limitations
    assert "当前未读取任何材料，本计划不输出事实。" not in result.limitations


def test_report_context_keeps_mechanism_limits_without_duplicate_quotations():
    from bioradar.research.investigations.analysis import report_context

    source = {
        "claims": [
            {
                "id": "C1",
                "text": "SYNTHETIC qualified claim",
                "evidence_links": [
                    {
                        "evidence_key": "E1",
                        "role": "refutes",
                        "quote": "DO_NOT_REPEAT_ORIGINAL",
                        "start": 0,
                        "end": 22,
                    }
                ],
            }
        ],
        "conditional_answer": {"answer": "SYNTHETIC conditional answer"},
        "update": {"reasons": ["SYNTHETIC prior-version difference"]},
    }
    mechanism = {
        "status": "completed",
        "scope": {"disease": "SYNTHETIC limited setting"},
        "overall": {"verdict": "insufficient"},
        "conflicts": [{"id": "X1"}],
        "critical_gaps": [{"reason": "SYNTHETIC missing causal experiment"}],
        "material_type_mapping": [
            {"evidence_id": "E1", "source_kind": "primary", "basis": "registry_not_experiment"}
        ],
        "coverage": {"status": "partial", "untyped_evidence_ids": ["E2"]},
        "evidence": [
            {
                "id": "O1",
                "relation": "opposes",
                "limitations": ["SYNTHETIC selected passage only"],
                "references": [
                    {"id": "P1", "material_id": "E1", "version": "v1", "text": "DO_NOT_REPEAT_ORIGINAL"}
                ],
            }
        ],
        "local_review": {"text": "DO_NOT_REPEAT_ORIGINAL"},
        "mechanism_review": {"text": "DO_NOT_REPEAT_ORIGINAL"},
        "evidence_map": {"text": "DO_NOT_REPEAT_ORIGINAL"},
    }
    original = copy.deepcopy((source, mechanism))
    context = report_context(source, mechanism)
    assert "DO_NOT_REPEAT_ORIGINAL" not in str(context)
    summary = context["mechanism_evaluation"]
    for key in ("scope", "overall", "conflicts", "critical_gaps", "material_type_mapping", "coverage"):
        assert summary[key] == mechanism[key]
    assert summary["evidence"][0]["relation"] == "opposes"
    assert summary["evidence"][0]["limitations"] == mechanism["evidence"][0]["limitations"]
    assert summary["evidence"][0]["references"] == [{"id": "P1", "material_id": "E1", "version": "v1"}]
    assert context["radar_analysis"]["claims"][0]["evidence_links"] == [
        {"evidence_key": "E1", "role": "refutes"}
    ]
    assert context["radar_analysis"]["update"] == source["update"]
    assert (source, mechanism) == original


def test_dynamic_generation_cap_and_received_write_resume_before_verification(runtime, database, monkeypatch):
    import bioradar.adapters.biocheck as biocheck
    from sqlalchemy.exc import SQLAlchemyError

    make, actor, view, _, model, settings, _ = runtime
    settings.semantic_llm_max_output_tokens = 32768
    monkeypatch.setattr(biocheck, "provider_status", lambda settings: {"available": True})

    def generated(settings, packet, call, *, repair=True, loaded=None):
        call("biocheck_plan", "SYNTHETIC plan", {"task": "shared_verify"})
        call("biocheck_write", "SYNTHETIC write", {"task": "shared_verify", "context": "x" * 3000})
        call("biocheck_check", "SYNTHETIC verify", {"task": "shared_verify"})
        text = "SYNTHETIC generated and verified report after recovery."
        return {
            "provider": "biocheck",
            "status": "PASS",
            "final_text": text,
            "verification": {"status": "PASS", "draft": text},
        }

    monkeypatch.setattr(biocheck, "run_biocheck", generated)
    _, accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=view["revision"], budget=AnalysisBudget(max_tokens=74000)),
        "bounded-write-recovery",
    )
    service = make()
    invoke, checkpoint = service.model_call, service._checkpoint
    interrupted = [False]

    def no_write_usage(config, **kwargs):
        response = invoke(config, **kwargs)
        if kwargs["stage"] == "biocheck_write":
            response.receipt.usage = ModelUsage()
        return response

    def stop_after_write(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if not interrupted[0] and any(
            c["stage"] == "biocheck_write" and c["status"] == "received" for c in progress["calls"]
        ):
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC stop after persisted write, before verification")

    service.model_call, service._checkpoint = no_write_usage, stop_after_write
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    assert interrupted[0] and len(model.requests) == 4
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        write = copy.deepcopy(row.progress["calls"][-1])
        assert (
            write["stage"] == "biocheck_write"
            and 128 <= write["model_parameters"]["max_output_tokens"] < 32768
        )
        assert write["receipt"]["max_output_tokens"] == write["model_parameters"]["max_output_tokens"]
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    make().recover()
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert len(model.requests) == 5 and result.calls[-1]["stage"] == "biocheck_check"
    assert result.calls[-2]["receipt"]["max_output_tokens"] == write["receipt"]["max_output_tokens"]
    assert result.result.report["verification"]["draft"] == result.result.report["final_text"]
    assert result.budget["usage"]["budgeted_tokens"] <= 74000


def test_all_downstream_payloads_share_compact_mechanism_and_keep_original_receipts(runtime, database):
    from bioradar.research.investigations.analysis import compact_mechanism, freeze_payload
    from bioradar.research.investigations.analysis_models import VerifiedClaim

    make, actor, view, *_ = runtime
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        fixed, progress = copy.deepcopy(row.fixed_input), copy.deepcopy(row.progress)
        synthesis = next(c["output"] for c in progress["calls"] if c["stage"] == "synthesize")
        review = next(c["output"] for c in progress["calls"] if c["stage"] == "check")
        claims = [VerifiedClaim.model_validate(c) for c in row.result["claims"]]
    repeated = "SYNTHETIC_ORIGINAL_MUST_NOT_REPEAT" * 3000
    mechanism = {
        "status": "completed",
        "overall": {"verdict": "insufficient"},
        "scope": {"disease": "SYNTHETIC limited scope"},
        "critical_gaps": [{"reason": "SYNTHETIC no completed causal experiment"}],
        "evidence": [
            {
                "id": "O1",
                "relation": "opposes",
                "limitations": ["SYNTHETIC registered design only"],
                "references": [{"id": "P1", "material_id": "E1", "version": "v1", "text": repeated}],
            }
        ],
        "local_review": {"raw": repeated},
        "mechanism_review": {"raw": repeated},
        "evidence_map": {"raw": repeated},
    }
    fixed["mechanism_evaluation"] = mechanism
    original = copy.deepcopy(fixed)
    for stage in ("read_plan", "synthesize", "check", "revise", "recheck", "compare", "final_check"):
        payload = freeze_payload(
            stage,
            fixed,
            progress["evidence"],
            queries=progress["queries"],
            synthesis=None if stage in {"read_plan", "synthesize"} else synthesis,
            review=review,
            verified_claims=claims,
        )
        assert repeated not in str(payload), stage
        if stage == "read_plan":
            assert "mechanism_evaluation" not in payload
        else:
            assert payload["mechanism_evaluation"] == compact_mechanism(mechanism), stage
            assert payload["mechanism_evaluation"]["evidence"][0]["relation"] == "opposes"
            assert payload["mechanism_evaluation"]["evidence"][0]["limitations"] == [
                "SYNTHETIC registered design only"
            ]
    assert fixed == original and fixed["mechanism_evaluation"]["local_review"]["raw"] == repeated


def test_verifier_allowance_uses_this_packet_and_draft_instead_of_global_input_ceiling():
    from bioradar.research.investigations.analysis import verification_input_allowance

    payload = {
        "question": "SYNTHETIC",
        "evidence": [{"id": "E1", "text": "x" * 6000}],
        "assessment": {"overall": "SYNTHETIC limited finding"},
        "plan": {"raw": "x" * 30000},
    }
    draft = "SYNTHETIC source draft. " * 100
    small = verification_input_allowance(payload, draft, 65536)
    large = verification_input_allowance(payload, draft, 196608)
    assert small == large < 65536
    assert verification_input_allowance({**payload, "draft": draft * 2}, draft, 196608) > large
    assert verification_input_allowance(payload, draft * 100, 65536) == 65536 + 1024


def test_model_ledger_upper_bound_covers_the_exact_encoded_request(runtime, database):
    import hashlib

    from bioradar.adapters.reasoning_model import encode_request

    make, _, _, _, _, settings, _ = runtime
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        calls = copy.deepcopy(session.get(InvestigationAnalysisRecord, accepted["id"]).progress["calls"])
    for call in calls:
        actual = settings.model_copy(
            update={"semantic_llm_max_output_tokens": call["model_parameters"]["max_output_tokens"]}
        )
        wire = encode_request(
            actual,
            stage=call["stage"],
            system_prompt=call["system_prompt"],
            payload=call["payload"],
            response_schema=call["response_schema"],
        )
        assert call["receipt"]["request_sha256"] == hashlib.sha256(wire).hexdigest()
        assert call["token_upper_bound"] >= len(wire) + call["model_parameters"]["max_output_tokens"]


@contextmanager
def literal_and_source(service, source):
    import httpx

    previous = service.reader.client

    def handler(request):
        response = source.handler(request)
        if request.url.path == "/api/history/records" and response.is_success:
            page = response.json()
            terms = [term.casefold() for term in request.url.params.get_list("terms")]
            page["items"] = [
                item
                for item in page["items"]
                if all(
                    term
                    in (item["title"] + " " + item["clean_text"] + " " + item["source_record_id"]).casefold()
                    for term in terms
                )
            ]
            return httpx.Response(200, json=page)
        return response

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        service.reader.client = client
        try:
            yield
        finally:
            service.reader.client = previous


def searched_terms(source):
    return [
        tuple(value for key, value in params if key == "terms")
        for path, params in source.calls
        if path == "/api/history/records"
    ]


def test_empty_and_query_relaxes_once_in_the_same_source_and_keeps_original_query(runtime):
    make, actor, _, source, model, *_ = runtime
    inquiry = empty_question(runtime)
    _, accepted = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=1,
            auto_retrieve=True,
            initial_queries=[
                {
                    "source_id": "fixture-source",
                    "terms": ["ABSENT", "SYNTHETIC"],
                    "reason": "SYNTHETIC narrow plan",
                }
            ],
        ),
        "literal-empty-relaxation",
    )
    service = make()
    with literal_and_source(service, source):
        service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    initial = [q for q in result.queries if q.phase == "initial"]
    assert [(q.plan.terms, q.status, q.new_versions) for q in initial] == [
        (["ABSENT", "SYNTHETIC"], "empty", 0),
        (["SYNTHETIC"], "read", 1),
    ]
    assert initial[1].relaxed_from.terms == initial[0].plan.terms
    assert initial[1].plan.source_id == initial[0].plan.source_id == "fixture-source"
    assert "同时满足" in initial[1].relaxation_reason and "ABSENT" in initial[1].plan.reason
    assert len(result.evidence) == 1 and result.evidence[0].history_binding
    assert searched_terms(source)[:2] == [("ABSENT", "SYNTHETIC"), ("SYNTHETIC",)]
    assert len(initial) == 2 and result.budget["usage"]["queries"] <= 6
    assert [p["task"] for p in model.requests] == ["read_plan", "synthesize", "check"]


@pytest.mark.parametrize("phase", ["initial", "followup"])
def test_empty_query_does_not_append_relaxation_when_query_budget_is_used(runtime, phase):
    make, actor, view, source, model, *_ = runtime
    inquiry = empty_question(runtime) if phase == "initial" else view
    if phase == "followup":

        def narrow(payload, output):
            if payload["task"] == "read_plan":
                output["next_queries"][0]["terms"] = ["ABSENT", "SYNTHETIC"]

        model.transform = narrow
    _, accepted = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=inquiry["revision"],
            auto_retrieve=True,
            budget=AnalysisBudget(max_queries=1),
            initial_queries=[{"source_id": "fixture-source", "terms": ["ABSENT", "SYNTHETIC"]}]
            if phase == "initial"
            else [],
        ),
        "literal-no-relaxation-budget",
    )
    service = make()
    with literal_and_source(service, source):
        service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == ("blocked" if phase == "initial" else "completed"), result.error_code
    if phase == "initial":
        assert result.error_code == "analysis_no_material" and not model.requests
    assert len(result.queries) == 1 and result.queries[0].status == "empty"
    assert searched_terms(source) == [("ABSENT", "SYNTHETIC")]
    assert result.budget["usage"]["queries"] == 1
    assert any("未追加减词补查" in message for message in result.limitations)


@pytest.mark.parametrize("phase", ["initial", "followup"])
def test_relaxed_query_receipt_resumes_without_repeating_search_or_adoption(runtime, database, phase):
    from sqlalchemy.exc import SQLAlchemyError

    make, actor, view, source, model, *_ = runtime
    inquiry = empty_question(runtime) if phase == "initial" else view
    if phase == "followup":

        def narrow(payload, output):
            if payload["task"] == "read_plan":
                output["next_queries"][0]["terms"] = ["ABSENT", "SYNTHETIC"]

        model.transform = narrow
    _, accepted = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=inquiry["revision"],
            auto_retrieve=True,
            budget=AnalysisBudget(max_queries=2),
            initial_queries=[{"source_id": "fixture-source", "terms": ["ABSENT", "SYNTHETIC"]}]
            if phase == "initial"
            else [],
        ),
        "literal-relaxation-recovery",
    )
    service = make()
    checkpoint, interrupted = service._checkpoint, [False]

    def stop_after_relaxed_read(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if not interrupted[0] and any(
            q.get("relaxed_from") and q["status"] == "read" for q in progress["queries"]
        ):
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC interruption after saved relaxed search")

    service._checkpoint = stop_after_relaxed_read
    with literal_and_source(service, source):
        with pytest.raises(RuntimeError, match="unconfirmed"):
            service.execute(accepted["id"], accepted["input_hash"])
    assert interrupted[0] and searched_terms(source) == [("ABSENT", "SYNTHETIC"), ("SYNTHETIC",)]
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.progress["query_relaxation"]["phase"] == phase
        assert row.progress["query_relaxation"]["original_plan"]["terms"] == ["ABSENT", "SYNTHETIC"]
        assert row.progress["queries"][1]["records"] and row.progress["queries"][1]["new_versions"] == 0
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    resumed = make()
    resumed.recover()
    with literal_and_source(resumed, source):
        resumed.execute(accepted["id"], accepted["input_hash"])
        resumed.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert searched_terms(source) == [("ABSENT", "SYNTHETIC"), ("SYNTHETIC",)]
    assert len(result.evidence) == (1 if phase == "initial" else 2) and result.queries[1].new_versions == 1
    assert result.budget["usage"]["queries"] == 2


def test_auto_adopted_first_question_materials_are_reused_and_followup_can_find_another_original(
    runtime, database
):
    from test_graph import original_record

    make, actor, _, source, model, *_ = runtime
    inquiry = empty_question(runtime)
    _, first = make().start(
        actor, inquiry["id"], AnalysisCreate(expected_revision=1, auto_retrieve=True), "auto-first"
    )
    make().execute(first["id"], first["input_hash"])
    parent = make().get(actor, inquiry["id"], first["id"])
    assert parent.state == "completed" and len(parent.evidence) == 1
    original_result = parent.result.model_dump(mode="json")
    view = InvestigationService(make().engine).get(actor, inquiry["id"])
    assert all(
        entry.payload.machine_analysis_id == parent.id for entry in view.entries if entry.kind == "material"
    )
    source.records.append(
        original_record(
            "synthetic-new-paper", {"body": "SYNTHETIC newly adopted paper with a limited cohort."}
        )
    )

    def narrow_followup(payload, output):
        if payload["task"] == "read_plan":
            output["next_queries"][0]["terms"] = ["ABSENT", "SYNTHETIC"]

    model.transform = narrow_followup
    _, child = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=view.revision,
            parent_analysis_id=parent.id,
            question="SYNTHETIC follow-up: do these materials cover a different population?",
            auto_retrieve=True,
        ),
        "auto-followup",
    )
    assert [item["record_ref"] for item in child["evidence"]] == [
        item.record_ref.model_dump(mode="json") for item in parent.evidence
    ]
    assert child["evidence"][0]["material_entry_id"] == parent.evidence[0].material_entry_id
    requests_before, source_before = len(model.requests), len(source.calls)
    service = make()
    with literal_and_source(service, source):
        service.execute(child["id"], child["input_hash"])
    result = make().get(actor, inquiry["id"], child["id"])
    assert result.state == "completed", result.error_code
    assert [payload["task"] for payload in model.requests[requests_before:]] == [
        "read_plan",
        "synthesize",
        "check",
        "compare",
        "final_check",
    ]
    assert all(q.phase == "followup" for q in result.queries)
    assert result.queries[0].status == "empty" and result.queries[1].plan.terms == ["SYNTHETIC"]
    assert result.queries[1].relaxed_from.terms == ["ABSENT", "SYNTHETIC"]
    assert result.queries[1].new_versions == 1 and len(result.evidence) == 2
    inherited = next(item for item in result.evidence if item.record_ref == parent.evidence[0].record_ref)
    assert inherited.history_binding == parent.evidence[0].history_binding
    assert any(
        path.endswith(parent.evidence[0].history_binding["version_id"])
        for path, _ in source.calls[source_before:]
    )
    assert make().get(actor, inquiry["id"], parent.id).result.model_dump(mode="json") == original_result
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, result.id)
        assert row.progress["query_relaxation"]["phase"] == "followup"
        assert all(call["stage"] != "question_plan" for call in row.progress["calls"])


def test_initial_relaxation_is_the_only_relaxation_even_if_followup_is_empty(runtime):
    make, actor, _, source, model, *_ = runtime
    inquiry = empty_question(runtime)

    def narrow_followup(payload, output):
        if payload["task"] == "read_plan":
            output["next_queries"][0]["terms"] = ["ABSENT", "SYNTHETIC"]

    model.transform = narrow_followup
    _, accepted = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=1,
            auto_retrieve=True,
            initial_queries=[{"source_id": "fixture-source", "terms": ["ABSENT", "SYNTHETIC"]}],
        ),
        "one-relaxation-only",
    )
    service = make()
    with literal_and_source(service, source):
        service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    relaxed = [q for q in result.queries if q.relaxed_from is not None]
    assert len(relaxed) == 1 and relaxed[0].phase == "initial"
    assert searched_terms(source) == [("ABSENT", "SYNTHETIC"), ("SYNTHETIC",), ("ABSENT", "SYNTHETIC")]


def test_relaxation_does_not_repeat_an_equivalent_query_already_read(runtime):
    make, actor, _, source, model, *_ = runtime
    inquiry = empty_question(runtime)

    def no_followup(payload, output):
        if payload["task"] == "read_plan":
            output["next_queries"] = []

    model.transform = no_followup
    _, accepted = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=1,
            auto_retrieve=True,
            initial_queries=[
                {"source_id": "fixture-source", "terms": [" SYNTHETIC "]},
                {"source_id": "fixture-source", "terms": ["ABSENT", "SYNTHETIC"]},
            ],
        ),
        "deduplicate-relaxed-search",
    )
    service = make()
    with literal_and_source(service, source):
        service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert searched_terms(source) == [("SYNTHETIC",), ("ABSENT", "SYNTHETIC")]
    assert len(result.queries) == 2 and all(q.relaxed_from is None for q in result.queries)


@pytest.mark.parametrize("max_calls", [16, 8])
def test_final_report_can_repair_after_generic_revision_within_its_own_budget(
    runtime, database, installed_biocheck, max_calls
):
    from bioradar.adapters.reasoning_model import complete_json
    from bioradar.api.errors import DomainError

    make, actor, view, _, model, settings, _ = runtime
    settings.analysis_manual_per_hour = 6
    parent = start(runtime)
    make().execute(parent["id"], parent["input_hash"])
    parent_before = make().get(actor, view["id"], parent["id"]).model_dump(mode="json")
    settings.biocheck_project_dir = installed_biocheck.biocheck_project_dir

    def generic_problem(payload, output):
        if payload["task"] in {"check", "recheck"}:
            output["findings"] = [
                {
                    "section": "conditional_answer",
                    "claim_id": None,
                    "issue": "SYNTHETIC source draft still needs qualification.",
                    "evidence_keys": [payload["evidence"][0]["evidence_key"]],
                }
            ]

    model.transform = generic_problem
    current = InvestigationService(make().engine).get(actor, view["id"])
    _, child = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=current.revision,
            parent_analysis_id=parent["id"],
            question="SYNTHETIC follow-up needs a separately checked final report",
            budget=AnalysisBudget(max_calls=max_calls),
        ),
        "generic-then-provider-repair",
    )
    provider_posts = []

    def provider_response(request):
        wire = request.read()
        body = json.loads(wire)
        payload = json.loads(body["messages"][1]["content"])
        stage = body["response_format"]["json_schema"]["name"]
        provider_posts.append(stage)
        if stage == "biocheck_plan":
            output = {"points": ["SYNTHETIC bound plan"]}
        elif stage in {"biocheck_write", "biocheck_revise"}:
            output = {
                "draft": "SYNTHETIC repaired final report."
                if stage == "biocheck_revise"
                else "SYNTHETIC final report with a remaining problem."
            }
        else:
            output = {
                "status": "PASS" if stage == "biocheck_recheck" else "FAIL",
                "draft": payload["draft"],
                "problem_units": [] if stage == "biocheck_recheck" else ["U1"],
                "units": [],
                "coverage": {"verdict": "complete"},
            }
        return httpx.Response(
            200,
            json={
                "id": "synthetic-final-report-completion",
                "model": "synthetic-model",
                "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    service = make()
    generic_call = service.model_call
    with httpx.Client(transport=httpx.MockTransport(provider_response)) as provider_client:

        def invoke(configuration, **kwargs):
            if kwargs["stage"].startswith("biocheck_"):
                return complete_json(configuration, client=provider_client, **kwargs)
            return generic_call(configuration, **kwargs)

        service.model_call = invoke
        service.execute(child["id"], child["input_hash"])
        service.execute(child["id"], child["input_hash"])
    result = make().get(actor, view["id"], child["id"])
    stages = [call["stage"] for call in result.calls]
    assert stages[:5] == ["read_plan", "synthesize", "check", "revise", "recheck"]
    expected_provider = ["biocheck_plan", "biocheck_write", "biocheck_check"]
    if max_calls == 16:
        expected_provider += ["biocheck_revise", "biocheck_recheck"]
        assert result.state == "completed"
        assert result.result.report["status"] == "PASS"
        assert result.result.report["final_text"] == "SYNTHETIC repaired final report."
        assert result.result.report["initial_verification"]["status"] == "FAIL"
    else:
        assert result.state == "needs_review"
        assert result.result.report["status"] == "FAIL"
        assert result.result.report["final_text"] == "SYNTHETIC final report with a remaining problem."
        assert result.result.report["revision"] is None
    assert stages[5:] == provider_posts == expected_provider
    assert len(stages) <= max_calls
    assert result.budget["usage"]["calls"] == len(stages)
    assert result.budget["usage"]["total_tokens"] == 150 * len(stages)
    assert result.result.report["final_text"] == result.result.report["verification"]["draft"]
    assert make().get(actor, view["id"], parent["id"]).model_dump(mode="json") == parent_before

    saved_result = result.model_dump(mode="json")
    current = InvestigationService(make().engine).get(actor, view["id"])
    retry_body = AnalysisCreate(expected_revision=current.revision, retry_of_analysis_id=child["id"])
    posts_before = len(model.requests), list(provider_posts)
    if result.state == "completed":
        with pytest.raises(DomainError) as failure:
            make().start(actor, view["id"], retry_body, "completed-report-is-not-a-retry")
        assert failure.value.code == "analysis_retry_invalid"
    else:
        code, retried = make().start(actor, view["id"], retry_body, "explicit-needs-review-retry")
        assert code == 202 and retried["id"] != child["id"]
        assert retried["question"] == result.question
        assert retried["parent_analysis_id"] == parent["id"]
        with Session(database) as session:
            stored = session.get(InvestigationAnalysisRecord, retried["id"])
            assert stored.fixed_input["retry_of_analysis_id"] == child["id"]
            assert stored.fixed_input["question"] == result.question
        code, duplicate = make().start(actor, view["id"], retry_body, "explicit-needs-review-retry-again")
        assert code == 200 and duplicate["id"] == retried["id"]
        assert make().get(actor, view["id"], child["id"]).model_dump(mode="json") == saved_result
    assert (len(model.requests), provider_posts) == posts_before


def test_retry_without_parent_keeps_none_and_uses_the_original_question(runtime):
    from bioradar.api.errors import DomainError

    make, actor, view, _, model, settings, _ = runtime
    settings.analysis_manual_per_hour = 6

    def unresolved(payload, output):
        if payload["task"] in {"check", "recheck"}:
            output["findings"] = [
                {
                    "section": "conditional_answer",
                    "claim_id": None,
                    "issue": "SYNTHETIC unresolved qualification",
                    "evidence_keys": [],
                }
            ]

    model.transform = unresolved
    original = start(runtime, question="SYNTHETIC original question differs from the investigation title")
    make().execute(original["id"], original["input_hash"])
    previous = make().get(actor, view["id"], original["id"])
    assert previous.state == "needs_review" and previous.parent_analysis_id is None
    old = previous.model_dump(mode="json")
    latest = InvestigationService(make().engine).get(actor, view["id"])
    body = AnalysisCreate(expected_revision=latest.revision, retry_of_analysis_id=previous.id)
    before = len(model.requests)
    code, attempt = make().start(actor, view["id"], body, "explicit-retry-without-parent")
    assert code == 202 and attempt["id"] != previous.id
    assert attempt["parent_analysis_id"] is None
    assert attempt["question"] == previous.question and attempt["question"] != view["question"]
    invalid_parent = body.model_copy(update={"parent_analysis_id": attempt["id"]})
    with pytest.raises(DomainError) as failure:
        make().start(actor, view["id"], invalid_parent, "retry-cannot-override-parent-validation")
    assert failure.value.code == "analysis_parent_invalid"
    assert make().get(actor, view["id"], previous.id).model_dump(mode="json") == old
    assert len(model.requests) == before


def test_final_repair_resumes_after_saved_initial_check_without_reserving_finished_calls(
    runtime, database, monkeypatch
):
    import bioradar.adapters.biocheck as biocheck
    from sqlalchemy.exc import SQLAlchemyError

    make, actor, view, _, model, *_ = runtime
    monkeypatch.setattr(biocheck, "provider_status", lambda settings: {"available": True})

    def generate(settings, packet, call, *, repair=True, loaded=None):
        for stage in ("biocheck_plan", "biocheck_write", "biocheck_check"):
            call(stage, "SYNTHETIC report method", {"task": "shared_verify", "phase": stage})
        text, status = "SYNTHETIC initially failed final report", "FAIL"
        if repair:
            for stage in ("biocheck_revise", "biocheck_recheck"):
                call(stage, "SYNTHETIC report method", {"task": "shared_verify", "phase": stage})
            text, status = "SYNTHETIC repaired final report", "PASS"
        return {
            "provider": "biocheck",
            "status": status,
            "final_text": text,
            "verification": {"status": status, "draft": text},
        }

    monkeypatch.setattr(biocheck, "run_biocheck", generate)
    accepted = start(runtime, budget=AnalysisBudget(max_calls=7))
    service = make()
    checkpoint, interrupted = service._checkpoint, [False]

    def stop_after_saved_check(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if not interrupted[0] and any(
            c["stage"] == "biocheck_check" and c["status"] == "received" for c in progress["calls"]
        ):
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC interruption after checked final report")

    service._checkpoint = stop_after_saved_check
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    assert interrupted[0] and len(model.requests) == 5
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        saved_calls = copy.deepcopy(row.progress["calls"])
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    resumed = make()
    resumed.recover()
    resumed.execute(accepted["id"], accepted["input_hash"])
    resumed.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [c["stage"] for c in result.calls] == [
        "read_plan", "synthesize", "biocheck_plan", "biocheck_write", "biocheck_check",
        "biocheck_revise", "biocheck_recheck",
    ]
    assert len(model.requests) == result.budget["usage"]["calls"] == 7
    assert result.budget["usage"]["total_tokens"] == 1050
    assert result.result.report["status"] == "PASS"
    assert result.result.report["final_text"] == result.result.report["verification"]["draft"]
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.progress["calls"][:5] == saved_calls
