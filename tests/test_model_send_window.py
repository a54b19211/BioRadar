"""Actual model intent gates preserve a useful send window and confirmed replay."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_models import AnalysisBudget
from bioradar.research.investigations.analysis_service import (
    InvestigationAnalysisRecord,
    minimum_send_seconds,
)
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start


def test_stage_window_uses_confirmed_same_stage_latency_and_original_timeout():
    fixed = {"model_parameters": {"timeout_seconds": 180, "min_send_seconds": 60}}
    progress = {
        "calls": [
            {"stage": "read_plan", "status": "received", "receipt": {"elapsed_ms": 179999}},
            {"stage": "biocheck_check", "status": "received", "receipt": {"elapsed_ms": 154001}},
            {"stage": "biocheck_recheck", "status": "unconfirmed", "receipt": {"elapsed_ms": 180000}},
        ]
    }
    assert minimum_send_seconds(fixed, progress, "biocheck_recheck") == 155
    assert minimum_send_seconds(fixed, progress, "biocheck_plan") == 60
    progress["calls"][1]["receipt"]["elapsed_ms"] = 200000
    assert minimum_send_seconds(fixed, progress, "biocheck_check") == 180
    assert minimum_send_seconds({"model_parameters": {"timeout_seconds": 45}}, {"calls": []}, "check") == 45
    assert minimum_send_seconds({"model_parameters": {"timeout_seconds": 180}}, {"calls": []}, "check") == 60


@pytest.mark.ENGINE
def test_twenty_five_seconds_remaining_is_durable_not_sent_and_restart_sends_nothing(runtime, database):
    make, actor, view, _, model, settings, _ = runtime
    settings.semantic_llm_timeout_seconds = 180
    settings.semantic_llm_max_output_tokens = 32768
    settings.analysis_min_call_seconds = 60
    accepted = start(runtime, budget=AnalysisBudget(max_seconds=300))
    service = make()

    def near_deadline(identifier, token, fixed, progress):
        if not progress["calls"]:
            progress["started_at"] = (datetime.now(UTC) - timedelta(seconds=275)).isoformat()
            service._checkpoint(identifier, token, progress)
        service._call(
            identifier,
            token,
            "shared_verify",
            {"type": "object"},
            fixed,
            progress,
            {"task": "shared_verify"},
            call_key="SYNTHETIC-window",
        )
        raise AssertionError("late model must not be sent")

    service._research_result = near_deadline
    service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        before = copy.deepcopy(row.progress["calls"])
        assert row.state == "blocked" and row.error_code == "analysis_time_budget"
        assert len(before) == 1 and before[0]["status"] == "failed"
        assert before[0]["receipt"]["status"] == "not_sent"
        assert before[0]["required_send_seconds"] == 60
        assert before[0]["model_parameters"]["max_output_tokens"] == 32768
        assert row.fixed_input["model_parameters"]["min_send_seconds"] == 60
        row.state, row.dispatch_pending = "queued", False
    assert model.requests == []
    service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.progress["calls"] == before
        assert row.error_code == "analysis_time_budget"
    assert model.requests == []


@pytest.mark.ENGINE
def test_sufficient_window_keeps_normal_output_and_received_replays_after_deadline(runtime, database):
    make, _, _, _, model, settings, _ = runtime
    settings.semantic_llm_timeout_seconds = 180
    settings.semantic_llm_max_output_tokens = 32768
    settings.analysis_min_call_seconds = 60
    accepted = start(runtime, budget=AnalysisBudget(max_seconds=300))
    service = make()

    def confirmed(identifier, token, fixed, progress):
        progress["started_at"] = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
        service._checkpoint(identifier, token, progress)
        args = (
            identifier,
            token,
            "shared_verify",
            {"type": "object"},
            fixed,
            progress,
            {"task": "shared_verify"},
        )
        first, _ = service._call(*args, call_key="SYNTHETIC-received")
        progress["started_at"] = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        service._checkpoint(identifier, token, progress)
        replay, _ = service._call(*args, call_key="SYNTHETIC-received")
        assert first == replay == {"status": "PASS"}
        raise DomainError("analysis_model_budget", "SYNTHETIC measured stop", 422)

    service._research_result = confirmed
    service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert len(row.progress["calls"]) == 1
        call = row.progress["calls"][0]
        assert call["status"] == "received"
        assert call["model_parameters"]["max_output_tokens"] == 32768
        assert call["required_send_seconds"] == 60
    assert len(model.requests) == 1
    assert model.wires[0]["max_tokens"] == 32768


@pytest.mark.ENGINE
def test_sent_checkpoint_that_consumes_the_minimum_window_saves_not_sent(runtime, database, monkeypatch):
    import bioradar.research.investigations.analysis_service as module

    class Clock(datetime):
        offset = timedelta()

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + cls.offset

    monkeypatch.setattr(module, "datetime", Clock)
    make, _, _, _, model, settings, _ = runtime
    settings.semantic_llm_timeout_seconds = 180
    settings.semantic_llm_max_output_tokens = 32768
    settings.analysis_min_call_seconds = 60
    accepted = start(runtime, budget=AnalysisBudget(max_seconds=300))
    service = make()
    checkpoint = service._checkpoint

    def slow_sent(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if progress["calls"] and progress["calls"][0]["status"] == "sent":
            Clock.offset = timedelta(seconds=20)

    service._checkpoint = slow_sent

    def seventy_seconds(identifier, token, fixed, progress):
        progress["started_at"] = (datetime.now(UTC) - timedelta(seconds=230)).isoformat()
        service._checkpoint(identifier, token, progress)
        service._call(
            identifier, token, "shared_verify", {"type": "object"}, fixed, progress, {"task": "shared_verify"}
        )
        raise AssertionError("checkpoint left only fifty seconds, no model must be sent")

    service._research_result = seventy_seconds
    service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        call = row.progress["calls"][0]
        assert row.error_code == "analysis_time_budget"
        assert call["status"] == "failed" and call["receipt"]["status"] == "not_sent"
        assert call["required_send_seconds"] == 60
        assert call["receipt"]["elapsed_ms"] >= 20000
    assert model.requests == []
