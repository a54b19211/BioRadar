"""SYNTHETIC partial-evidence discovery on real PG/RQ; Fabric HTTP is MockTransport."""

import copy
import json
import os
from functools import wraps
from uuid import uuid4

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.query_planner import decide_query
from bioradar.api.errors import DomainError
from bioradar.research.investigations import analysis_service
from bioradar.research.investigations.analysis_models import (
    AnalysisCreate,
    HistoricalAnalysisScope,
)
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.kdd import extend_context
from bioradar.research.investigations.models import InvestigationCreate
from bioradar.research.investigations.query_policy import build_policy_context
from bioradar.research.investigations.service import InvestigationService
from rq.job import Job
from sqlalchemy.orm import Session
from test_engine_persistence import GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph import original_record
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_query_policy_runtime import external
from test_required_gap_discovery import GAP, setup_case
from test_research_recovery_faults import actual_queue as actual_queue  # noqa: F401
from test_research_recovery_faults import expire_lease

pytestmark = pytest.mark.ENGINE


def test_partial_catalog_revalidates_prepared_on_rq_and_replays_received_without_another_get(
    runtime, database, actual_queue, monkeypatch, tmp_path
):
    make, actor, _, source, model, settings, _ = runtime
    connection, queue, drain = actual_queue
    external(runtime)
    settings.analysis_max_queries = 8
    _, template, plans = setup_case(source_id="fixture-source")
    cutoff = "2026-10-20T00:00:00+00:00"
    old = original_record(
        "SYNTHETIC-old", {"body": "SYNTHETIC membrane filtration was assessed within stated limits."}
    )
    new = original_record(
        "SYNTHETIC-catalog-only", {"body": "SYNTHETIC additional fixed material preserves its own limits."}
    )
    new["published_seq"] = 2
    for item in (old, new):
        item["availability_proof"] = {
            "available_at": "2026-10-10T00:00:00Z",
            "verified_by": "SYNTHETIC test-only proof",
            "verified_at": "2026-10-11T00:00:00Z",
            "uri": "https://example.invalid/synthetic-original",
            "sha256": item["raw_sha256"],
        }
    source.records = [old, new]
    source.include_collection_sequence = True
    meter = tmp_path / "synthetic-partial-catalog-requests.jsonl"
    fault = ["prepared"]

    def records(request):
        with meter.open("a") as stream:
            stream.write(
                json.dumps({"path": request.url.path, "params": list(request.url.params.multi_items())})
                + "\n"
            )
        return source.handler(request)

    def factory():
        current = make()
        current.reader = FabricHistoryReader(
            httpx.Client(base_url="http://fabric.test", transport=httpx.MockTransport(records)),
            authorization_ref=GRANT,
            max_seconds=90,
        )

        def research(identifier, token, fixed, progress):
            current._prepare(identifier, token, fixed, progress)
            assert fixed["query_policy"]["discovery_version"] == "bounded-required-gap-discovery-v1"
            assert fixed["evidence_time"]["mode"] == "public_history_reconstruction"
            assert fixed["evidence_time"]["cutoff"] == cutoff
            state = progress["query_policy"]
            state.setdefault("initial_plans", copy.deepcopy(plans))
            if not state.get("checks"):
                state["checks"] = copy.deepcopy(template["query_policy"]["checks"])
            for index in range(len(plans)):
                if index < len(state["decisions"]):
                    continue
                request = build_policy_context(fixed, progress, phase="initial", plans=plans)
                action = next(
                    item
                    for item in request["candidates"]
                    if item["kind"] == "document_search"
                    and not item["arguments"].get("catalog_discovery")
                    and item["arguments"]["index"] == index
                )
                step = len(state["decisions"])
                state["decisions"].append(
                    {
                        "step": step,
                        "decision": "continue",
                        "action": action,
                        "policy_request": request,
                    }
                )
                state["pending_action"] = action
                current._checkpoint(identifier, token, progress)
                receipt = current._execute_policy_action(identifier, token, fixed, progress, action, step)
                state["decisions"][step]["receipt"] = {
                    "query_index": progress["queries"].index(receipt),
                    "status": receipt["status"],
                }
                state["pending_action"] = None
                for check in state["checks"]:
                    if check["id"] in {"source_materials", "answer_scope"}:
                        check.update(status="passed", evidence_ids=[progress["evidence"][0]["key"]])
                current._checkpoint(identifier, token, progress)
            assert [q["new_versions"] for q in progress["queries"][:3]] == [1, 0, 0]
            assert [q["status"] for q in progress["queries"][:3]] == ["read", "read", "empty"]
            assert len(progress["evidence"]) == (2 if progress.get("catalog_probe_done") else 1)
            if len(state["decisions"]) == len(plans):
                request = build_policy_context(fixed, progress, phase="initial", plans=plans)
                request = extend_context(fixed, progress, request)
                selected = decide_query(settings, request)
                assert selected["decision"] == "continue", selected
                action = selected["action"]
                assert action["arguments"].get("catalog_discovery") is True
                assert action["discovery_basis"]["query_index"] == 1 and GAP in action["covers"]
                state["decisions"].append(
                    {
                        "step": len(plans),
                        "decision": "continue",
                        "action": action,
                        "policy_request": request,
                    }
                )
                state["pending_action"] = action
                current._checkpoint(identifier, token, progress)
            action = state["decisions"][-1]["action"]
            receipt = current._execute_policy_action(identifier, token, fixed, progress, action, len(plans))
            assert receipt["status"] == "read" and receipt["new_versions"] == 1
            assert receipt["cost"]["tool_calls"] == 1
            assert len(progress["evidence"]) == 2 and not progress["calls"]
            state["decisions"][-1]["receipt"] = {
                "query_index": progress["queries"].index(receipt),
                "status": "read",
            }
            state["pending_action"] = None
            progress["catalog_probe_done"] = True
            current._checkpoint(identifier, token, progress)
            raise DomainError("synthetic_catalog_probe_finished", "SYNTHETIC controlled discovery stop", 422)

        current._research_result = research
        checkpoint = current._checkpoint

        def exit_at_catalog_boundary(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            catalog = next((q for q in progress["queries"] if q["plan"].get("terms") == []), None)
            if catalog and (
                fault[0] == "prepared"
                and catalog["status"] == "prepared"
                or fault[0] == "received"
                and catalog["status"] == "read"
                and not catalog["evidence_keys"]
            ):
                os._exit(77)

        current._checkpoint = exit_at_catalog_boundary
        return current

    @wraps(analysis_service.execute_analysis)
    def execute(identifier, input_hash):
        database.dispose(close=False)
        return factory().execute(identifier, input_hash)

    monkeypatch.setattr(analysis_service, "execute_analysis", execute)
    service = make()
    _, investigation = InvestigationService(service.engine).create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC partial catalog probe",
            question="SYNTHETIC membrane filtration validation measurements",
            is_demo=True,
        ),
        "SYNTHETIC-partial-catalog-case",
    )
    accepted = service.start(
        actor,
        investigation["id"],
        AnalysisCreate(
            expected_revision=investigation["revision"],
            auto_retrieve=True,
            historical_scope=HistoricalAnalysisScope(mode="public_history_reconstruction", cutoff=cutoff),
        ),
        "SYNTHETIC-partial-catalog-analysis",
    )[1]
    assert queue.count == 1
    drain()
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.state == "running" and Job.fetch(row.rq_job_id, connection=connection).is_failed, (
            row.error_code,
            row.progress.get("execution_diagnostic"),
            [(q["status"], q.get("error_code")) for q in row.progress["queries"]],
        )
        assert row.progress["queries"][-1]["status"] == "prepared"
        action_id = row.progress["query_policy"]["pending_action"]["id"]
        assert len(row.progress["evidence"]) == 1 and row.progress["calls"] == []

    def searches():
        return [
            json.loads(line)
            for line in meter.read_text().splitlines()
            if json.loads(line)["path"] == "/api/history/records"
        ]

    assert len(searches()) == 3
    expire_lease(database, accepted["id"])
    fault[0] = "received"
    factory().recover()
    factory().recover()
    assert queue.count == 1
    drain()
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.progress["query_policy"]["pending_action"]["id"] == action_id
        assert row.progress["queries"][-1]["status"] == "read"
        assert row.progress["queries"][-1]["records"] == [old, new]
        assert len(row.progress["evidence"]) == 1
    assert len(searches()) == 4
    expire_lease(database, accepted["id"])
    fault[0] = None
    factory().recover()
    factory().recover()
    assert queue.count == 1
    drain()
    public = make().get(actor, investigation["id"], accepted["id"]).model_dump(mode="json")
    assert public["error_code"] == "synthetic_catalog_probe_finished"
    assert len(public["evidence"]) == 2 and public["calls"] == []
    expected = [
        {key: row[key] for key in ("source_id", "source_record_id", "version_id")} for row in (old, new)
    ]
    assert public["queries"][-1]["returned_versions"] == expected
    assert public["queries"][1]["returned_versions"] == expected[:1]
    assert public["queries"][-1]["action_id"] == action_id
    assert public["queries"][-1]["cost"]["tool_calls"] == 1
    with Session(database) as session:
        saved = copy.deepcopy(session.get(InvestigationAnalysisRecord, accepted["id"]).progress)
    queue.enqueue(
        analysis_service.execute_analysis, accepted["id"], accepted["input_hash"], job_id=uuid4().hex
    )
    drain()
    with Session(database) as session:
        assert session.get(InvestigationAnalysisRecord, accepted["id"]).progress == saved
    assert len(searches()) == 4
    catalog = dict(searches()[-1]["params"])
    assert catalog["source_id"] == "fixture-source" and catalog["mode"] == "public_history_reconstruction"
    assert catalog["cutoff"] == cutoff and catalog["is_demo"] == "true"
    assert "terms" not in catalog and "cursor" not in catalog and int(catalog["limit"]) == 8
    assert not model.requests
