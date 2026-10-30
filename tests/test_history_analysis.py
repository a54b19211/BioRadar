"""Historical semantic admission uses actual PostgreSQL; no model request is sent."""

import copy
import importlib.util
import json
import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.api.errors import DomainError
from bioradar.research.history.analysis import start_history_analysis
from bioradar.research.history.models import HistoryAnalysisCreate
from bioradar.research.history.service import HistoricalStudyRecord
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_history import history_source, reader, request
from test_history import runtime as runtime  # noqa: F401
from test_shared_research_adapters import installed_biocheck as installed_biocheck  # noqa: F401

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def semantic_history(runtime):
    service, actor = runtime
    for name in ("0006_monitor.py", "0007_topics.py", "0008_investigation_analyses.py"):
        path = Path(__file__).resolve().parents[1] / "db/migrations/versions" / name
        spec = importlib.util.spec_from_file_location("semantic_history_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with (
            service.engine.database.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.upgrade()
    service.reader = history_source()
    saved = service.create(actor, request(), "saved-history")
    settings = service.engine.settings
    settings.semantic_llm_enabled = True
    settings.semantic_llm_base_url = "http://127.0.0.1:19999/v1"
    from pydantic import SecretStr

    settings.semantic_llm_api_key = SecretStr("SYNTHETIC-NOT-A-REAL-KEY")
    settings.semantic_llm_model = "synthetic-history-model"
    settings.analysis_search_source_ids = ["history-fixture"]
    settings.redis_url = None
    history_reader = reader(
        lambda req: httpx.Response(
            200, json={"items": [{"source_id": "history-fixture", "read_enabled": True}]}
        )
    )
    service.reader = history_reader
    yield service, actor, saved
    history_reader.client.close()


def test_history_starts_real_analysis_with_only_features_and_stable_intent(semantic_history):
    service, actor, saved = semantic_history
    body = HistoryAnalysisCreate(query_policy="baseline")
    result = start_history_analysis(service, actor, saved.id, body, "same-request")
    again = start_history_analysis(service, actor, saved.id, body, "same-request")
    assert result.id == again.id and result.state == "queued"
    with Session(service.engine.database) as session:
        row = session.get(InvestigationAnalysisRecord, result.id)
        study = session.get(HistoricalStudyRecord, saved.id)
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
        assert row.trigger == "history" and row.parent_analysis_id is None
        assert row.fixed_input["evidence_time"] == {
            "mode": saved.request.mode,
            "cutoff": saved.cutoff.isoformat(),
            "study_id": saved.id,
            "run_id": saved.request.run_id,
        }
        features = {
            item["snapshot_id"] for item in study.payload["snapshots"] if item["purpose"] == "features"
        }
        assert row.fixed_input["evidence"]
        assert {item["snapshot_id"] for item in row.fixed_input["evidence"]} <= features
        assert all(
            "positive-new" not in str(item) and "positive-old" not in str(item)
            for item in row.fixed_input["evidence"]
        )
        assert row.progress["calls"] == []
        assert study.prediction_hash == saved.prediction_hash
        assert study.payload["predictions"] == [p.model_dump(mode="json") for p in saved.predictions]
    assert service.get(actor, saved.id).semantic_analyses[0]["id"] == result.id


def test_history_recovers_creation_before_link_checkpoint_without_duplicate(semantic_history, monkeypatch):
    service, actor, saved = semantic_history
    from bioradar.research.history import analysis

    original = analysis._remember

    def interrupt_link(history, current_actor, study_id, key, field, value):
        if field == "analysis_id":
            raise RuntimeError("SYNTHETIC exit after analysis was committed")
        return original(history, current_actor, study_id, key, field, value)

    monkeypatch.setattr(analysis, "_remember", interrupt_link)
    with pytest.raises(RuntimeError):
        start_history_analysis(service, actor, saved.id, HistoryAnalysisCreate(), "recover")
    with Session(service.engine.database) as session:
        committed_id = session.scalar(select(InvestigationAnalysisRecord.id))
    monkeypatch.setattr(analysis, "_remember", original)
    restored = start_history_analysis(service, actor, saved.id, HistoryAnalysisCreate(), "recover")
    assert restored.id == committed_id and restored.calls == []
    with Session(service.engine.database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1


def test_history_rejects_changed_request_and_unrelated_material_basis(semantic_history):
    service, actor, saved = semantic_history
    first = HistoryAnalysisCreate(query_policy="baseline")
    start_history_analysis(service, actor, saved.id, first, "fixed")
    with pytest.raises(DomainError, match="同一请求编号"):
        start_history_analysis(
            service, actor, saved.id, HistoryAnalysisCreate(query_policy="query_planner"), "fixed"
        )
    # Mark the recorded snapshots as outcomes. An empty explicit selection must not
    # silently mean "all investigation materials" at the shared executor boundary.
    with Session(service.engine.database) as session, session.begin():
        row = session.get(HistoricalStudyRecord, saved.id, with_for_update=True)
        payload = copy.deepcopy(row.payload)
        for item in payload["snapshots"]:
            item["purpose"] = "outcomes"
        row.payload = payload
    with pytest.raises(DomainError) as error:
        start_history_analysis(service, actor, saved.id, first, "different")
    assert error.value.code == "history_analysis_basis_invalid"


def test_history_closed_investigation_is_not_reopened(semantic_history):
    service, actor, saved = semantic_history
    investigations = InvestigationService(service.engine)
    view = investigations.get(actor, saved.investigation_id)
    investigations.append(
        actor,
        view.id,
        InvestigationEntryCreate(
            kind="status", status="closed", expected_revision=view.revision, text="SYNTHETIC user closes it"
        ),
        "close",
    )
    with pytest.raises(DomainError) as error:
        start_history_analysis(service, actor, saved.id, HistoryAnalysisCreate(), "closed")
    assert error.value.code == "investigation_closed"
    with Session(service.engine.database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 0


def test_history_external_policy_runs_through_real_rq_without_later_labels_in_model_input(
    semantic_history, database, installed_biocheck, monkeypatch
):
    import test_research_recovery_faults as recovery
    from bioradar.api.app import create_app
    from bioradar.research.history.compute import score_predictions
    from bioradar.research.history.models import HistoryEvaluationCreate
    from bioradar.research.investigations import analysis_service
    from bioradar.research.investigations.analysis_models import AnalysisView
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from test_history import event, instant, observation
    from test_investigation_analysis import Model

    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.skip("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR supplies the installed pure policy")
    service, actor, saved = semantic_history
    settings = service.engine.settings
    settings.query_planner_project_dir = Path(directory)
    settings.semantic_llm_model = "synthetic-model"
    assert settings.engine_source_id_allowlist["fixture"] == ["history-fixture"]
    with database.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    assert schema.startswith("engine_test_")
    fixed_prediction_hash = saved.prediction_hash
    fixed_predictions = [item.model_dump(mode="json") for item in saved.predictions]
    service.reader = history_source()
    early = service.evaluate(
        actor, saved.id, HistoryEvaluationCreate(cutoff=instant(2018, 7)), "before-maturity"
    )
    later = service.evaluate(
        actor, saved.id, HistoryEvaluationCreate(cutoff=instant(2019)), "mature-labels-only"
    )
    assert early["metrics"]["brier_score"] is None and early["metrics"]["censored"] == 2
    assert later["metrics"]["brier_score"] == 0.25
    with Session(database) as session:
        study = session.get(HistoricalStudyRecord, saved.id)
        records = copy.deepcopy(study.payload["evidence"])
        feature_snapshot_ids = {
            item["snapshot_id"] for item in study.payload["snapshots"] if item["purpose"] == "features"
        }
    assert any(item["unit_id"] == "new-a" and item["status"] == "event" for item in later["outcomes"])

    def source_http(request):
        if request.url.path == "/api/history/sources":
            return httpx.Response(
                200, json={"items": [{"source_id": "history-fixture", "read_enabled": True}]}
            )
        if request.url.path.startswith("/api/history/versions/"):
            record = records[request.url.path.rsplit("/", 1)[-1]]
            assert record["record_kind"] != "outcome"
            assert request.url.params["mode"] == saved.request.mode
            assert request.url.params["cutoff"] == saved.cutoff.isoformat()
            return httpx.Response(200, json={"record": record})
        raise AssertionError("Unexpected source HTTP request in the bounded history acceptance")

    history_reader = reader(source_http)
    service.reader = history_reader

    @contextmanager
    def connected_history(*args):
        yield history_reader

    monkeypatch.setattr("bioradar.research.history.routes.history_reader", connected_history)
    monkeypatch.setattr(
        "bioradar.adapters.fabric_history.current_history_sources", lambda settings: history_reader.sources()
    )
    model = Model()

    def bounded_reading(payload, output):
        if payload["task"] == "read_plan":
            output["gaps"], output["next_queries"] = [], []
            output["coverage_checks"] = [
                {
                    "id": check["id"],
                    "status": "passed",
                    "evidence_ids": [payload["evidence"][0]["evidence_key"]],
                    "reason": "SYNTHETIC bounded feature-only coverage.",
                }
                for check in payload["coverage_checks"]
            ]

    model.transform = bounded_reading

    def make():
        return analysis_service.AnalysisService(service.engine, history_reader)

    bridge = (make, actor, None, None, model, settings, None)
    try:
        with contextmanager(recovery.model_http.__wrapped__)(bridge, installed_biocheck) as (factory, meter):
            settings.biocheck_project_dir = None
            settings.mech_evidence_project_dir = None
            with contextmanager(recovery.actual_queue.__wrapped__)(bridge) as (connection, queue, drain):

                @wraps(analysis_service.execute_analysis)
                def execute(identifier, input_hash):
                    database.dispose(close=False)
                    return factory().execute(identifier, input_hash)

                monkeypatch.setattr(analysis_service, "execute_analysis", execute)
                app = create_app(settings)
                headers = {
                    "Authorization": "Bearer history-test-credential-long-enough",
                    "Idempotency-Key": "history-rq-external",
                }
                with TestClient(app, base_url="http://localhost") as client:
                    app.state.database = database
                    post_path = "/api/engine/history/studies/" + saved.id + "/analyses"
                    response = client.post(post_path, json={"query_policy": "query_planner"}, headers=headers)
                    assert response.status_code == 202, response.text
                    accepted = AnalysisView.model_validate(response.json())
                    assert accepted.state == "queued" and queue.count == 1
                    drain()
                    get_path = (
                        "/api/engine/investigations/" + saved.investigation_id + "/analyses/" + accepted.id
                    )
                    response = client.get(get_path, headers=headers)
                    assert response.status_code == 200, response.text
                    result = AnalysisView.model_validate(response.json())
                assert result.state == "completed", result.error_code
                assert result.query_policy["effective"] == "query_planner"
                assert result.query_policy["decisions"][-1]["decision"] == "complete"
                assert (
                    result.query_policy["decisions"][-1]["policy"]["training_status"] == "heuristic_untrained"
                )
                assert meter.stages == ["read_plan", "synthesize", "check"]
                with Session(database) as session:
                    row = session.get(InvestigationAnalysisRecord, accepted.id)
                    assert row.fixed_input["evidence_time"]["cutoff"] == saved.cutoff.isoformat()
                    assert row.fixed_input["evidence_time"]["mode"] == saved.request.mode
                    assert {
                        item["snapshot_id"] for item in row.fixed_input["evidence"]
                    } <= feature_snapshot_ids
                    assert all(call["status"] == "received" for call in row.progress["calls"])
                    job = recovery.Job.fetch(row.rq_job_id, connection=connection)
                    assert job.is_finished
                    actual_policy_version = row.fixed_input["query_policy"]["version"]
                prompts = json.dumps(model.requests, ensure_ascii=False)
                for forbidden in (
                    "positive-new",
                    "positive-old",
                    "negative-old",
                    "prediction_hash",
                    "event_observed",
                    "observation_end",
                ):
                    assert forbidden not in prompts
                assert all(payload["as_of"] == saved.cutoff.isoformat() for payload in model.requests)
                unchanged = service.get(actor, saved.id)
                assert unchanged.prediction_hash == fixed_prediction_hash
                assert [item.model_dump(mode="json") for item in unchanged.predictions] == fixed_predictions
                labels = (
                    event(
                        "new-a", observed=False, public=instant(2019), start=instant(2018), end=instant(2019)
                    ),
                    event("new-b", public=instant(2018, 6)).model_copy(update={"event_at": instant(2018, 6)}),
                )
                _, immature = score_predictions(
                    saved.predictions, observation(instant(2018, 7), events=(labels[1],)), 1
                )
                outcomes, mature = score_predictions(
                    saved.predictions, observation(instant(2019), events=labels), 1
                )
                assert immature.censored == 2 and immature.brier_score is None
                assert {item.status for item in outcomes if item.unit_id.startswith("new-")} == {
                    "event",
                    "no_event",
                }
                assert mature.false_positive_count == 1 and mature.missed_observed_events == 1
                assert mature.precision_at_budget == 0 and mature.recall_at_budget == 0
                artifact = os.environ.get("BIORADAR_HISTORY_QUERY_ACCEPTANCE_ARTIFACT")
                if artifact:
                    Path(artifact).write_text(
                        json.dumps(
                            {
                                "schema": schema,
                                "http_post_path": post_path,
                                "http_post_status": 202,
                                "http_get_path": get_path,
                                "http_get_status": 200,
                                "analysis_id": accepted.id,
                                "rq_job_id": row.rq_job_id,
                                "rq_job_finished": True,
                                "policy_version": actual_policy_version,
                                "policy": result.query_policy,
                                "cutoff": saved.cutoff.isoformat(),
                                "model_stages": meter.stages,
                                "synthetic_http_requests": len(model.requests),
                                "labels_excluded_from_all_prompts": True,
                                "predictions_unchanged": True,
                                "immature": immature.model_dump(mode="json"),
                                "mature_counterexample": mature.model_dump(mode="json"),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
    finally:
        history_reader.client.close()
