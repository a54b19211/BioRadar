"""Actual PG daily reservations plus bounded, source-matched semantic extraction."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_models import AnalysisCreate
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.execution_budget import (
    WAIT_CODE,
    finish_budget_wait,
    reserve_automatic_call,
    waiting_seconds,
)
from bioradar.research.investigations.narrative_execution import prepare_narrative_snapshot
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_mining import record
from test_mining import snapshot as mining_snapshot

pytestmark = pytest.mark.ENGINE


def test_concurrent_automatic_calls_share_durable_daily_reservation(runtime, database):
    make, actor, view, _, _, settings, _ = runtime
    settings.automatic_daily_model_tokens = 100
    settings.topic_daily_model_tokens = 100
    now = datetime.now(UTC)
    jobs = []
    for index in range(2):
        result = make().start(
            actor,
            view["id"],
            AnalysisCreate(expected_revision=view["revision"], question=f"budget {index}"),
            f"daily-budget-{index}",
        )[1]
        token = f"execution-{index}"
        with Session(database) as session, session.begin():
            row = session.get(InvestigationAnalysisRecord, result["id"])
            fixed = copy.deepcopy(row.fixed_input)
            fixed["execution_budget"] = {
                "version": "automatic-buckets-v1",
                "pool": "monitor",
                "topic_id": None,
                "automatic_daily_tokens": 100,
                "topic_daily_tokens": 100,
            }
            progress = copy.deepcopy(row.progress)
            call = {"call_id": f"call-{index}", "status": "prepared", "token_upper_bound": 60}
            progress["calls"] = [call]
            progress["started_at"] = now.isoformat()
            row.fixed_input, row.progress = fixed, copy.deepcopy(progress)
            row.state, row.execution_token, row.lease_until = "running", token, now + timedelta(minutes=5)
        jobs.append((result["id"], token, fixed, progress, call))

    def reserve(args):
        try:
            reserve_automatic_call(make(), *args, now)
        except DomainError as exc:
            return exc.code
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, jobs))
    assert sorted(outcomes) == sorted(["reserved", WAIT_CODE])
    with Session(database) as session:
        rows = [session.get(InvestigationAnalysisRecord, job[0]) for job in jobs]
        waiting = next(row for row in rows if row.progress.get("budget_wait"))
        received = next(row for row in rows if not row.progress.get("budget_wait"))
        assert waiting.progress["automatic_budget"]["global_used_before_this_call"] == 60
        assert "automatic_reservation" not in waiting.progress["calls"][0]
        assert received.progress["calls"][0]["status"] == "prepared"
        paused = copy.deepcopy(waiting.progress)
        tomorrow = datetime.fromisoformat(paused["budget_wait"]["next_budget_at"])
        assert finish_budget_wait(paused, tomorrow)
        assert waiting_seconds(waiting.fixed_input, paused, tomorrow) > 0
        assert not finish_budget_wait(paused, tomorrow)
    # This test never opens a model client; a reservation is not reported usage.


def test_literal_semantic_selection_is_cached_and_does_not_mutate_original():
    text = "净订单为12万元。主体为南川设备公司，统计期间为2026年第一季度。"
    row = record("narrative-host", text, source_id="external_archive")
    original = mining_snapshot([row])
    fixed = {
        "query_policy": {"research_contract_version": "bioradar-research-input-v1", "direction": "business"}
    }
    progress = {"calls": []}
    calls = []

    def span(value):
        start = text.index(value)
        return {"text": value, "start": start, "end": start + len(value)}

    def invoke(*args, **kwargs):
        calls.append(kwargs["call_key"])
        return {
            "observations": [
                {
                    "subject": span("南川设备公司"),
                    "metric": span("净订单"),
                    "value": span("12万元"),
                    "period": span("2026年第一季度"),
                }
            ]
        }, progress

    service = SimpleNamespace(
        _call=invoke, _checkpoint=lambda *a, **k: None, _final_token_allowance=lambda *a: 1024
    )
    derived = prepare_narrative_snapshot(
        service, "analysis", "lease", fixed, progress, original, allow_model=True
    )
    assert len(calls) == 1
    assert derived.records[0].metric_observations[0].value_lower == 120000
    assert not original.records[0].metric_observations
    restored = prepare_narrative_snapshot(
        service, "analysis", "lease", fixed, progress, original, allow_model=False
    )
    assert restored == derived and len(calls) == 1


def test_uncertain_semantic_call_is_not_relabelled_as_empty_success():
    text = "净订单为12万元。主体为南川设备公司，统计期间为2026年第一季度。"
    fixed = {
        "query_policy": {"research_contract_version": "bioradar-research-input-v1", "direction": "business"}
    }
    original = mining_snapshot([record("unknown-narrative", text)])

    def invoke(*a, **k):
        raise DomainError("model_call_outcome_unconfirmed", "synthetic unknown", 409)

    service = SimpleNamespace(
        _call=invoke, _checkpoint=lambda *a, **k: None, _final_token_allowance=lambda *a: 1024
    )
    with pytest.raises(DomainError, match="synthetic unknown"):
        prepare_narrative_snapshot(service, "analysis", "lease", fixed, {}, original, allow_model=True)


def test_arbitrary_historical_question_uses_public_http_and_original_proof(runtime, database, monkeypatch):
    from contextlib import contextmanager

    from bioradar.api.app import create_app
    from bioradar.research.history import service as history_module
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from test_engine_persistence import CREDENTIAL
    from test_graph import original_record

    make, actor, _, source, model, settings, _ = runtime
    settings.data_fabric_url = "http://127.0.0.1"
    settings.data_fabric_history_read_token = SecretStr("SYNTHETIC")
    value = original_record("historical-health", {"body": "SYNTHETIC unknown respiratory cluster."})
    value["availability_proof"] = {
        "available_at": "2019-12-30T12:00:00Z",
        "verified_by": "synthetic-proof-reviewer",
        "verified_at": "2026-10-31T00:00:00Z",
        "uri": "https://example.org/synthetic-archive",
        "sha256": "a" * 64,
    }
    source.records = [value]
    original_received = value["received_at"]

    @contextmanager
    def reader(*args, **kwargs):
        yield make().reader

    monkeypatch.setattr(history_module, "history_reader", reader)
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "historical-public-ask"}
    payload = {
        "question": "SYNTHETIC what was known about this respiratory cluster?",
        "is_demo": True,
        "kdd_direction": "public_health",
        "historical_scope": {
            "mode": "public_history_reconstruction",
            "cutoff": "2019-12-31T23:59:59Z",
        },
    }
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        response = client.post("/api/engine/investigations/ask", headers=headers, json=payload)
        assert response.status_code == 202, response.text
        accepted = response.json()
        make().execute(accepted["analysis_id"], accepted["analysis"]["input_hash"])
        result = make().get(actor, accepted["investigation_id"], accepted["analysis_id"])
        assert result.state == "completed", result.error_code
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, result.id)
            fixed = row.fixed_input
            assert fixed["evidence_time"]["mode"] == "public_history_reconstruction"
            assert fixed["evidence_time"]["study_id"] is None
            assert fixed["evidence_time"]["history_request_id"]
            assert fixed["graph_scope"] == []
            assert fixed["providers"]["weknora"]["available"] is False
            assert row.trigger == "history"
        requested = [dict(params) for path, params in source.calls if path == "/api/history/records"]
        assert requested and all(p["mode"] == "public_history_reconstruction" for p in requested)
        assert all(p["cutoff"] == "2019-12-31T23:59:59+00:00" for p in requested)
        assert source.records[0]["received_at"] == original_received
        assert len(model.requests) == 4
        app.state.database = None


def test_historical_reader_rejects_later_material_before_reading_model(runtime):
    from bioradar.research.investigations.analysis_models import HistoricalAnalysisScope
    from bioradar.research.investigations.models import InvestigationCreate
    from bioradar.research.investigations.service import InvestigationService
    from test_graph import original_record

    make, actor, _, source, model, *_ = runtime
    value = original_record("later-health", {"body": "SYNTHETIC FUTURE OUTCOME must not enter a prompt."})
    value["availability_proof"] = {
        "available_at": "2020-01-20T00:00:00Z",
        "verified_by": "synthetic-proof-reviewer",
        "verified_at": "2026-10-31T00:00:00Z",
        "uri": "https://example.org/synthetic-future",
        "sha256": "b" * 64,
    }
    source.records = [value]
    _, inquiry = InvestigationService(make().engine).create(
        actor,
        InvestigationCreate(title="SYNTHETIC history", question="SYNTHETIC cluster?", is_demo=True),
        "history-future-inquiry",
    )
    _, accepted = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=1,
            auto_retrieve=True,
            historical_scope=HistoricalAnalysisScope(
                mode="public_history_reconstruction",
                cutoff=datetime(2019, 12, 31, tzinfo=UTC),
            ),
        ),
        "history-future-analysis",
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, inquiry["id"], accepted["id"])
    assert result.state == "blocked" and result.result is None
    assert result.evidence == []
    assert [item["task"] for item in model.requests] == ["question_plan"]
    assert all("FUTURE OUTCOME" not in str(item) for item in model.requests)


def test_accelerated_topic_clock_http_is_test_only_and_demo_only(runtime, database, monkeypatch):
    from contextlib import contextmanager

    from bioradar.api.app import create_app
    from bioradar.topics import routes
    from fastapi.testclient import TestClient
    from test_engine_persistence import CREDENTIAL

    make, _, _, _, model, settings, _ = runtime

    @contextmanager
    def reader(*args, **kwargs):
        yield make().reader

    monkeypatch.setattr(routes, "history_reader", reader)
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + CREDENTIAL}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        response = client.post(
            "/api/engine/topics/simulation/tick",
            headers=headers,
            json={"as_of": "2026-11-10T00:00:00Z"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["clock_mode"] == "accelerated_demo_clock"
        assert response.json()["result"]["state"] == "no_topics"
        assert not model.requests
        settings.environment = "development"
        denied = client.post(
            "/api/engine/topics/simulation/tick",
            headers=headers,
            json={"as_of": "2026-11-11T00:00:00Z"},
        )
        assert denied.status_code == 404
        app.state.database = None


def test_historical_followup_keeps_explicit_narrowed_source_scope(runtime, database):
    from bioradar.research.investigations.analysis_models import HistoricalAnalysisScope
    from bioradar.research.investigations.models import InvestigationCreate
    from bioradar.research.investigations.service import InvestigationService

    make, actor, _, source, model, settings, _ = runtime
    settings.analysis_search_source_ids = ["fixture-source", "fixture-z"]
    settings.engine_source_id_allowlist["fixture"].append("fixture-z")
    source.enabled.add("fixture-z")
    _, inquiry = InvestigationService(make().engine).create(
        actor,
        InvestigationCreate(title="SYNTHETIC narrow", question="SYNTHETIC question", is_demo=True),
        "history-narrow-inquiry",
    )
    _, parent = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=1,
            auto_retrieve=True,
            historical_scope=HistoricalAnalysisScope(mode="operational_replay", cutoff=datetime.now(UTC)),
        ),
        "history-narrow-parent",
    )
    make().execute(parent["id"], parent["input_hash"])
    completed = make().get(actor, inquiry["id"], parent["id"])
    assert completed.state == "completed", completed.error_code
    current = InvestigationService(make().engine).get(actor, inquiry["id"])
    _, child = make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(
            expected_revision=current.revision,
            question="SYNTHETIC narrowed followup",
            parent_analysis_id=parent["id"],
            auto_retrieve=True,
            source_ids=["fixture-source"],
        ),
        "history-narrow-child",
    )
    with Session(database) as session:
        frozen = session.get(InvestigationAnalysisRecord, child["id"]).fixed_input
        previous = session.get(InvestigationAnalysisRecord, parent["id"]).fixed_input
        assert previous["search_scope"] == ["fixture-source", "fixture-z"]
        assert frozen["search_scope"] == ["fixture-source"]
        assert frozen["evidence_time"] == previous["evidence_time"]
        assert frozen["graph_scope"] == []
    assert len(model.requests) == 4
