"""SYNTHETIC graph discovery on real PG/RQ; all graph HTTP is MockTransport."""

import copy
import json
import os
from functools import wraps

import httpx
import pytest
from bioradar.adapters.fixed_graph import question_relationship_need
from bioradar.adapters.query_graph import build_graph_candidates
from bioradar.api.errors import DomainError
from bioradar.research.investigations import analysis_service
from bioradar.research.investigations.analysis_models import AnalysisCreate, InitialQuery
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationCreate
from bioradar.research.investigations.query_policy import build_coverage_checks
from bioradar.research.investigations.service import InvestigationService
from pydantic import SecretStr
from rq.job import Job
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph_package_discovery import setup_graph
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_query_policy_runtime import external
from test_research_recovery_faults import actual_queue as actual_queue  # noqa: F401
from test_research_recovery_faults import expire_lease

pytestmark = pytest.mark.ENGINE


def test_explicit_single_object_graph_need_is_discovered_and_received_actions_replay_on_rq(
    runtime, database, actual_queue, monkeypatch, tmp_path
):
    make, actor, _, source, model, settings, _ = runtime
    connection, queue, drain = actual_queue
    external(runtime)
    settings.analysis_kdd_enabled = True
    settings.analysis_graph_source_ids = ["reviewed_graph"]
    settings.data_fabric_graph_query_token = SecretStr("SYNTHETIC-graph-query-only")
    settings.engine_source_id_allowlist["fixture"] = ["fixture-source", "reviewed_graph"]
    source.enabled.add("reviewed_graph")
    question = "What is the mechanism of SYNTHETIC B?"
    service = make()
    _, view = InvestigationService(service.engine).create(
        actor,
        InvestigationCreate(title="SYNTHETIC relationship probe", question=question, is_demo=True),
        "SYNTHETIC-relationship-case",
    )
    _, _, _, reader, _, _ = setup_graph()
    original_transport = reader.client._transport
    meter = tmp_path / "synthetic-graph-requests.jsonl"

    def graph(request):
        with meter.open("a") as stream:
            stream.write(json.dumps({"method": request.method, "path": request.url.path}) + "\n")
        return original_transport.handle_request(request)

    reader.client = httpx.Client(base_url="http://fabric.test", transport=httpx.MockTransport(graph))
    interrupt = [True]

    def factory():
        current = make()
        current.reader = reader

        def research(identifier, token, fixed, progress):
            assert fixed["query_policy"]["discovery_version"] == "bounded-required-gap-discovery-v1"
            assert fixed["graph_scope"] == ["reviewed_graph"]
            state = progress["query_policy"]
            start = question.index("SYNTHETIC B")
            objects = [
                {
                    "id": "b",
                    "name": "SYNTHETIC B",
                    "question_span": {"start": start, "end": start + len("SYNTHETIC B")},
                }
            ]
            state.update(phase="followup", named_objects=objects)
            need = question_relationship_need(fixed, objects)
            checks = build_coverage_checks(fixed, progress)
            assert need and any(c["id"] == need["id"] and c["status"] == "unknown" for c in checks)
            for step, operation in enumerate(("catalog", "adopt", "resolve", "neighbors")):
                if step < len(state["decisions"]):
                    action = state["decisions"][step]["action"]
                else:
                    action = next(
                        c
                        for c in build_graph_candidates(settings, fixed, progress, None, checks=checks)
                        if c["arguments"]["operation"] == operation
                    )
                    assert need["id"] in action["covers"]
                    state["decisions"].append({"step": step, "decision": "continue", "action": action})
                    state["pending_action"] = action
                    current._checkpoint(identifier, token, progress)
                receipt = current._execute_policy_action(identifier, token, fixed, progress, action, step)
                assert receipt["status"] == "read"
                state["decisions"][step]["receipt"] = {"action_id": action["id"], "status": "read"}
                state["pending_action"] = None
                current._checkpoint(identifier, token, progress)
            assert not progress["evidence"] and not progress["calls"]
            assert all(c["status"] == "unknown" for c in checks if c["id"] == need["id"])
            raise DomainError("synthetic_graph_probe_finished", "SYNTHETIC controlled graph stop", 422)

        current._research_result = research
        checkpoint = current._checkpoint

        def crash_after_received_adopt(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            if (
                interrupt[0]
                and progress.get("graph_packages")
                and any(
                    q["status"] == "read" and q["plan"].get("operation") == "adopt"
                    for q in progress["queries"]
                )
            ):
                os._exit(77)

        current._checkpoint = crash_after_received_adopt
        return current

    @wraps(analysis_service.execute_analysis)
    def execute(identifier, input_hash):
        database.dispose(close=False)
        return factory().execute(identifier, input_hash)

    monkeypatch.setattr(analysis_service, "execute_analysis", execute)
    try:
        accepted = service.start(
            actor,
            view["id"],
            AnalysisCreate(
                expected_revision=view["revision"],
                auto_retrieve=True,
                initial_queries=[InitialQuery(source_id="fixture-source", terms=["SYNTHETIC"])],
                query_policy="query_planner",
            ),
            "SYNTHETIC-relationship-analysis",
        )[1]
        assert queue.count == 1
        drain()
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            assert row.state == "running" and Job.fetch(row.rq_job_id, connection=connection).is_failed
            assert len(row.progress["graph_packages"]) == 1
            assert [q["status"] for q in row.progress["queries"]] == ["read", "read"]
            old_calls = copy.deepcopy(row.progress["calls"])
        before = [json.loads(line) for line in meter.read_text().splitlines()]
        assert len(before) == 2
        expire_lease(database, accepted["id"])
        interrupt[0] = False
        factory().recover()
        factory().recover()
        assert queue.count == 1
        drain()
        after = [json.loads(line) for line in meter.read_text().splitlines()]
        assert len(after) == 6 and after[:2] == before
        assert sum(x["path"] == "/api/history/records" for x in after) == 1
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            assert row.error_code == "synthetic_graph_probe_finished"
            assert row.progress["calls"] == old_calls == []
            assert [q["status"] for q in row.progress["queries"]] == ["read"] * 4
            assert sum(q["cost"]["tool_calls"] for q in row.progress["queries"]) == 6
            assert not row.progress["evidence"]
            neighborhood = next(
                c for c in row.progress["graph_context"] if c.get("operation") == "fixed_neighbors"
            )
            assert "不自动确认" in neighborhood["qualification"]
            assert any(d["source_id"] == "reviewed_graph" for d in row.fixed_input["dependency_sources"])
        assert not model.requests
    finally:
        reader.client.close()
