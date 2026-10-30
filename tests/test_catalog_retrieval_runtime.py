"""Real PG executor and installed selector with explicitly synthetic source/model replies."""

import copy
import os
from pathlib import Path

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.research.investigations.analysis_models import AnalysisCreate, HistoricalAnalysisScope
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationCreate
from bioradar.research.investigations.service import InvestigationService
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_engine_persistence import GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph import original_record
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_query_policy_runtime import expire

pytestmark = pytest.mark.ENGINE


@pytest.mark.parametrize("path", ["language", "catalog", "catalog_received_recovery"])
def test_new_v2_empty_keyword_alternatives_reach_fixed_originals(runtime, database, path):
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.skip("Needs installed query-planner pure implementation with catalog discovery")
    make, actor, _, source, model, settings, _ = runtime
    settings.query_planner_project_dir = Path(directory)
    settings.analysis_query_policy = "query_planner"
    settings.analysis_kdd_enabled = True
    settings.analysis_manual_per_hour = 10
    if path == "language":
        source.records = [
            original_record(
                "synthetic-enzyme-material",
                {"body": "SYNTHETIC fixed source describes enzyme variation and its limits."},
            )
        ]
    source.records[0]["availability_proof"] = {
        "available_at": "2026-10-10T00:00:00Z",
        "verified_by": "SYNTHETIC test-only proof",
        "verified_at": "2026-10-11T00:00:00Z",
        "uri": "https://example.invalid/synthetic-fixed-record",
        "sha256": source.records[0]["raw_sha256"],
    }
    cutoff = "2026-10-20T00:00:00+00:00"
    investigation = InvestigationService(make().engine)
    _, view = investigation.create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC 跨语言原件检索",
            question="SYNTHETIC 已授权原件中是否有酶变化的材料？",
            is_demo=True,
        ),
        "synthetic-v2-language-" + path,
    )

    def reply(payload, result):
        if payload["task"] == "question_plan":
            assert payload["retrieval_strategy_version"] == "literal-language-alternatives-v1"
            result["queries"] = [
                {"source_id": "fixture-source", "terms": ["酶变化"], "reason": "问题原词"},
                {"source_id": "fixture-source", "terms": ["enzyme"], "reason": "等义英语待检索"},
            ]
        elif payload["task"] == "read_plan":
            result["next_queries"] = []
            result["gaps"] = []

    model.transform = reply
    observed = []

    def records(request):
        response = source.handler(request)
        if request.url.path != "/api/history/records":
            return response
        observed.append(list(request.url.params.multi_items()))
        terms = request.url.params.get_list("terms")
        page = response.json()
        if not terms:
            page["next_cursor"] = "SYNTHETIC-unread-next-page"
        return httpx.Response(200, json=page)

    accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=view["revision"],
            auto_retrieve=True,
            historical_scope=HistoricalAnalysisScope(mode="public_history_reconstruction", cutoff=cutoff),
        ),
        "synthetic-v2-initial-" + path,
    )[1]
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(records)) as client:
        service = make()
        service.reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90)
        if path == "catalog_received_recovery":
            checkpoint = service._checkpoint

            def exit_after_catalog_received(identifier, token, progress, **kwargs):
                checkpoint(identifier, token, progress, **kwargs)
                if any(
                    (q.get("usage") or {}).get("catalog_discovery")
                    and q["status"] == "read"
                    and not q["evidence_keys"]
                    for q in progress["queries"]
                ):
                    raise SQLAlchemyError("SYNTHETIC worker exit after durable catalog originals")

            service._checkpoint = exit_after_catalog_received
            with pytest.raises(RuntimeError):
                service.execute(accepted["id"], accepted["input_hash"])
            assert len(observed) == 3
            with Session(database) as session:
                saved = session.get(InvestigationAnalysisRecord, accepted["id"])
                assert saved.progress["queries"][-1]["status"] == "read"
                assert saved.progress["queries"][-1]["records"] == source.records
            calls_before = len([p for p in model.requests if p["task"] == "question_plan"])
            expire(database, accepted["id"])
            service = make()
            service.reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90)
            service.recover()
            service.execute(accepted["id"], accepted["input_hash"])
            assert len([p for p in model.requests if p["task"] == "question_plan"]) == calls_before
        else:
            service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state in {"completed", "needs_review"}, result.model_dump(mode="json")
    assert result.evidence and result.result
    assert all(e.record_ref.adopted_version == source.records[0]["version_id"] for e in result.evidence)
    assert len(observed) == (2 if path == "language" else 3)
    assert [[value for key, value in params if key == "terms"] for params in observed[:2]] == [
        ["酶变化"],
        ["enzyme"],
    ]
    for params in observed:
        value = dict(params)
        assert value["source_id"] == "fixture-source" and value["is_demo"] == "true"
        assert value["mode"] == "public_history_reconstruction"
        assert value["cutoff"] == cutoff and "cursor" not in value
        assert 1 <= int(value["limit"]) <= 20
    catalog = [q for q in result.queries if (q.usage or {}).get("catalog_discovery")]
    if path == "language":
        assert catalog == []
    else:
        assert len(catalog) == 1 and catalog[0].has_more
        assert catalog[0].cost["tool_calls"] == 1
        assert catalog[0].scope == "fabric_bounded_original_catalog_not_keyword_match"
        assert catalog[0].usage["coverage"]["complete"] is False
        assert catalog[0].usage["coverage"]["search_scope"] == "bounded_original_catalog_not_keyword_match"
    assert not any(q.provider == "weknora" for q in result.queries)
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert (
            row.fixed_input["query_policy"]["initial_retrieval_version"] == "literal-language-alternatives-v1"
        )
        before = copy.deepcopy(row.progress)
    make().execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        assert session.get(InvestigationAnalysisRecord, accepted["id"]).progress == before
    assert len(observed) == (2 if path == "language" else 3)
