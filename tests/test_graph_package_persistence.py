"""New graph discovery through the real host checkpoint; isolated PG and synthetic HTTP."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from bioradar.research.investigations.analysis_models import AnalysisCreate, InitialQuery
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationCreate
from bioradar.research.investigations.service import InvestigationService
from pydantic import SecretStr
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph_package_discovery import candidates, setup_graph
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_query_policy_runtime import external

pytestmark = pytest.mark.ENGINE


def test_graph_catalog_and_adopt_are_durable_and_replay_without_new_reads(runtime, database):
    make, actor, _, source, model, settings, _ = runtime
    external(runtime)
    settings.analysis_manual_per_hour = 20
    settings.analysis_graph_source_ids = ["reviewed_graph"]
    settings.data_fabric_graph_query_token = SecretStr("SYNTHETIC-query-only")
    settings.engine_source_id_allowlist["fixture"] = ["fixture-source", "reviewed_graph"]
    source.enabled.add("reviewed_graph")
    service = make()
    _, view = InvestigationService(service.engine).create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC graph discovery", question="How does SYNTHETIC B relate?", is_demo=True
        ),
        "SYNTHETIC-catalog-case",
    )
    accepted = service.start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=view["revision"],
            auto_retrieve=True,
            initial_queries=[InitialQuery(source_id="fixture-source", terms=["SYNTHETIC"])],
            query_policy="query_planner",
        ),
        "SYNTHETIC-catalog-analysis",
    )[1]
    _, _, _, reader, fixed_calls, catalogs = setup_graph()
    service.reader = reader
    token = "SYNTHETIC-owned-lease"
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        row.state, row.execution_token = "running", token
        row.lease_until = datetime.now(UTC) + timedelta(seconds=60)
        fixed, progress = copy.deepcopy(row.fixed_input), copy.deepcopy(row.progress)
        fixed["is_demo"] = row.is_demo
        progress.setdefault("expected_revision", row.based_on_revision)
        progress["started_at"] = datetime.now(UTC).isoformat()
        progress["query_policy"]["phase"] = "followup"
        progress["query_policy"]["named_objects"] = [
            {
                "id": "b",
                "name": "SYNTHETIC B",
                "question_span": {"start": 9, "end": 20},
            }
        ]
        row.progress = copy.deepcopy(progress)
    chosen = []
    for step, operation in enumerate(("catalog", "adopt")):
        action = next(
            c for c in candidates(settings, fixed, progress) if c["arguments"]["operation"] == operation
        )
        chosen.append(action)
        progress["query_policy"].setdefault("decisions", []).append(
            {"step": step, "decision": "continue", "action": action}
        )
        service._checkpoint(accepted["id"], token, progress)
        receipt = service._execute_policy_action(accepted["id"], token, fixed, progress, action, step)
        assert receipt["status"] == "read", receipt
        assert receipt["cost"]["tool_calls"] == 1
    with Session(database) as session:
        saved = session.get(InvestigationAnalysisRecord, accepted["id"])
        fixed, restored = copy.deepcopy(saved.fixed_input), copy.deepcopy(saved.progress)
        fixed["is_demo"] = saved.is_demo
    assert len(restored["graph_packages"]) == 1
    assert any(c["arguments"]["operation"] == "resolve" for c in candidates(settings, fixed, restored))
    before = len(catalogs), len(fixed_calls)
    resumed = make()
    resumed.reader = reader
    for step, action in enumerate(chosen):
        receipt = resumed._execute_policy_action(accepted["id"], token, fixed, restored, action, step)
        assert receipt["status"] == "read"
    assert (len(catalogs), len(fixed_calls)) == before == (1, 1)
    assert model.requests == []
    reader.client.close()
