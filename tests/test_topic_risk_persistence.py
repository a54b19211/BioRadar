"""Normal topic admission -> real PG/RQ -> synthetic HTTP receipts -> scoped risk adoption.

All statements and method responses are SYNTHETIC controls. No result rows are seeded.
"""

import copy
import json
import os
import threading
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.reasoning_model import complete_json
from bioradar.api.app import create_app
from bioradar.api.errors import DomainError
from bioradar.research.investigations import analysis_service
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.mining.models import canonical_hash
from bioradar.topics.models import TopicCreate, TopicEvaluationRecord, TopicUpdate
from bioradar.topics.risk import TopicRiskReviewRequest
from bioradar.topics.service import TopicService
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session
from test_engine_persistence import CREDENTIAL
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_research_recovery_faults import actual_queue as actual_queue  # noqa: F401
from test_research_recovery_faults import expire_lease
from test_topic_risk import CONTROL, DECISION, SUPPORT, bound_analysis
from test_topics import NOW, dated, definition

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def risk_http(runtime):
    make, _, _, source, model, settings, _ = runtime
    state = SimpleNamespace(scope=None, stages=[], operations=[])
    settings.analysis_query_policy = "query_planner"
    settings.analysis_kdd_enabled = True
    settings.analysis_max_calls = 24
    settings.analysis_max_tokens = 524288
    settings.analysis_max_seconds = 1800
    settings.topic_hourly_investigations = 4
    settings.biocheck_transport = "http"
    settings.biocheck_service_token = SecretStr("SYNTHETIC-ONLY-TOKEN")
    settings.analysis_search_source_ids = []
    settings.query_planner_project_dir = Path(
        __import__("os").environ["BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR"]
    )
    source.records = [dated(1, "2026-09-01", "北江医院收治7例不明原因肺炎。\n" + SUPPORT + "\n" + CONTROL)]
    draft = "\n\n".join((DECISION, SUPPORT, CONTROL))

    def transform(payload, result):
        if payload["task"] not in {"read_plan", "synthesize"}:
            return
        evidence = next(
            item for item in payload["evidence"] if SUPPORT in "".join(p["text"] for p in item["passages"])
        )
        claims = []
        for identifier, statement in (("C0", DECISION), ("C1", SUPPORT), ("C2", CONTROL)):
            quote = SUPPORT if identifier == "C0" else statement
            passage = next(p for p in evidence["passages"] if quote in p["text"])
            claims.append(
                {
                    "id": identifier,
                    "text": statement,
                    "kind": "inference" if identifier == "C0" else "source_statement",
                    "scope": "SYNTHETIC District Z interval T",
                    "conditions": [],
                    "status": "supported",
                    "evidence_links": [
                        {
                            "evidence_key": evidence["evidence_key"],
                            "passage_id": passage["id"],
                            "role": "supports",
                            "reason": "SYNTHETIC fixed original",
                        }
                    ],
                }
            )
        result.update(
            claims=claims,
            gaps=[],
            explanations=[],
            conditional_answer={
                "answer": DECISION,
                "conditions": ["Only the SYNTHETIC declared district and interval."],
                "unknowns": [],
            },
        )
        if payload["task"] == "read_plan":
            result["next_queries"] = []
            result["coverage_checks"] = [
                {
                    "id": row["id"],
                    "status": "passed",
                    "evidence_ids": [evidence["evidence_key"]],
                    "reason": "SYNTHETIC control covers the declared test scope.",
                }
                for row in payload.get("query_context", {}).get("coverage_checks", [])
            ]
        else:
            result.update(
                warning_assessment=None,
                competing_hypotheses=[
                    {
                        "id": "SYNTHETIC-H1",
                        "explanation": "SYNTHETIC bounded surveillance explanation.",
                        "supporting_claim_ids": ["C1"],
                        "opposing_claim_ids": [],
                        "discriminating_evidence_needed": "SYNTHETIC independent repeated assay.",
                        "untested_conditions": ["SYNTHETIC later interval remains unknown."],
                    }
                ],
                current_judgment={
                    "least_refuted_hypothesis_id": "SYNTHETIC-H1",
                    "reason": "SYNTHETIC current adopted evidence supports the bounded explanation.",
                    "next_discriminating_evidence": "SYNTHETIC independent repeated assay.",
                    "qualification": "这是模型判断，未证实",
                },
            )
            result["topic_risk_reassessment"] = bound_analysis(state.scope)["result"][
                "topic_risk_reassessment"
            ]
            result["update"] = {
                "stance": "initial",
                "changed_claims": [],
                "reasons": ["SYNTHETIC test observation."],
                "decisive_evidence_keys": [evidence["evidence_key"]],
            }

    model.transform = transform

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(
                {
                    "schema_version": "bioradar.professional.v1",
                    "provider": "biocheck",
                    "provider_version": "SYNTHETIC-risk-native-v1",
                    "operations": ["generate", "verify", "audit_support", "quality_feedback"],
                }
            )

        def do_POST(self):
            wire = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(wire)
            if "messages" in body:
                state.stages.append(body["response_format"]["json_schema"]["name"])
                response = model.handler(httpx.Request("POST", "http://127.0.0.1/v1", content=wire))
                self.reply(response.json())
                return
            operation, args = body["operation"], body["arguments"]
            state.operations.append(operation)
            if operation == "generate":
                result = {"draft": draft}
            elif operation == "verify":
                assert args["draft"] == draft
                evidence = next(item for item in args["packet"]["evidence"] if SUPPORT in item["text"])
                units, offset = [], 0
                for index, statement in enumerate((DECISION, SUPPORT, CONTROL)):
                    units.append(
                        {
                            "unit_id": f"U{index}",
                            "text": statement,
                            "start": offset,
                            "end": offset + len(statement),
                            "verdict": "supported",
                            "issues": [],
                            "claims": [
                                {
                                    "kind": "fact",
                                    "span": statement,
                                    "verdict": "supported",
                                    "evidence": [
                                        {"evidence_id": evidence["id"], "version": evidence["version"]}
                                    ],
                                }
                            ],
                        }
                    )
                    offset += len(statement) + 2
                result = {"draft": draft, "status": "PASS", "units": units, "verification_errors": []}
            elif operation == "audit_support":
                result = {
                    "status": "PASS",
                    "original_status": args["review"]["status"],
                    "verification_errors": [],
                    "checks": [
                        {
                            "unit_id": unit["unit_id"],
                            "claim_index": index,
                            "verdict": "supported",
                            "evidence": claim["evidence"],
                        }
                        for unit in args["review"]["units"]
                        for index, claim in enumerate(unit["claims"])
                    ],
                }
            elif operation == "quality_feedback":
                result = {"evidence_gaps": [], "repair_units": [], "review_required": []}
            else:
                raise AssertionError(operation)
            self.reply(
                {
                    "schema_version": "bioradar.professional.v1",
                    "provider": "biocheck",
                    "provider_version": "SYNTHETIC-risk-native-v1",
                    "operation_id": body["operation_id"],
                    "status": "completed",
                    "result": result,
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.semantic_llm_base_url = f"http://127.0.0.1:{server.server_port}/v1"
    settings.biocheck_service_url = f"http://127.0.0.1:{server.server_port}"

    def topic():
        service = make()
        return TopicService(service.engine, service.reader)

    def analysis():
        service = make()
        service.model_call = complete_json
        return service

    try:
        yield topic, analysis, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("adoption", ["http_replay", "automatic_tick"])
def test_real_topic_rq_receipts_adopt_risk_once_after_lost_response_and_reopen_for_new_scope(
    runtime, database, risk_http, actual_queue, monkeypatch, adoption
):
    topic, analysis, meter = risk_http
    service = topic()
    actor = service.actor
    _, created = service.create(
        actor,
        TopicCreate(
            is_demo=True,
            definition=definition(
                domain="public_health", question="SYNTHETIC District Z interval T and surveillance controls"
            ),
        ),
        "risk-topic",
    )
    topic_id = created["id"]
    assert service.tick(now=NOW)["state"] == "checked"
    evaluation = service.evaluations(actor, topic_id).items[0]
    with Session(database) as session:
        stored = session.get(TopicEvaluationRecord, evaluation.id)
        analysis_id = stored.followup["analysis_id"]
        fixed = copy.deepcopy(session.get(InvestigationAnalysisRecord, analysis_id).fixed_input)
        meter.scope = fixed["origin"]["topic_risk_scope"]
    assert meter.scope["observation_revision"] == evaluation.observation_revision
    assert meter.scope["risk_basis_sha256"] == evaluation.tracking["risk"]["basis_sha256"]
    assert topic().reconcile_risk(evaluation.id)["status"] == "pending"
    original_evaluation = copy.deepcopy(evaluation.model_dump(mode="json"))
    _, queue, drain = actual_queue

    crash = [True]

    @wraps(analysis_service.execute_analysis)
    def execute(identifier, input_hash):
        database.dispose(close=False)
        service = analysis()
        checkpoint = service._checkpoint

        def exit_after_received(*args, **kwargs):
            checkpoint(*args, **kwargs)
            report = (kwargs.get("result") or {}).get("report") or {}
            if crash[0] and (report.get("support_audit") or {}).get("status") == "PASS":
                os._exit(77)  # A real child exit after both HTTP receipts committed.

        service._checkpoint = exit_after_received
        return service.execute(identifier, input_hash)

    monkeypatch.setattr(analysis_service, "execute_analysis", execute)
    # The queue job was accepted before monkeypatching; its importable name now resolves here.
    assert queue.count == 1
    drain()
    with Session(database) as session:
        interrupted = session.get(InvestigationAnalysisRecord, analysis_id)
        assert interrupted.state == "running"
        assert interrupted.result["report"]["support_audit"]["status"] == "PASS"
        fixed_before_recovery = copy.deepcopy(interrupted.fixed_input)
        saved_calls = copy.deepcopy(interrupted.progress["calls"])
    before_recovery = copy.deepcopy((meter.stages, meter.operations))
    assert topic().reconcile_risk(evaluation.id)["status"] == "pending"
    expire_lease(database, analysis_id)  # Test worker lease only, no report/result/decision edits.
    crash[0] = False
    analysis().recover()
    analysis().recover()
    assert queue.count == 1
    drain()
    assert meter.stages == before_recovery[0]  # Received model calls are never sent again.
    assert meter.operations == [*before_recovery[1], "quality_feedback"]  # The not-yet-run 0-model step.
    assert meter.operations.count("verify") == meter.operations.count("audit_support") == 1
    with Session(database) as session:
        restored = session.get(InvestigationAnalysisRecord, analysis_id)
        assert restored.fixed_input == fixed_before_recovery
        assert restored.progress["calls"] == saved_calls
    final = analysis().get(actor, stored.followup["investigation_id"], analysis_id)
    assert final.state in {"completed", "needs_review"}, final.error_code
    assert final.result.report["status"] == final.result.report["support_audit"]["status"] == "PASS"
    assert final.result.topic_risk_reassessment.decision == "resolved_within_scope"
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, analysis_id)
        before_analysis = canonical_hash([row.fixed_input, row.progress, row.result])
        assert row.progress["report_risk_packets"]["0"]
    posts = copy.deepcopy((meter.stages, meter.operations))
    assert meter.operations.count("verify") == meter.operations.count("audit_support") == 1
    view = topic().get(actor, topic_id)
    prior_control_revision = view.control_revision
    if adoption == "automatic_tick":
        assert topic().tick(now=NOW)["state"] == "checked"
        view = topic().get(actor, topic_id)
        assert view.tracking["risk"]["status"] == "resolved"
    body = TopicRiskReviewRequest(
        expected_revision=view.control_revision, evaluation_id=evaluation.id, analysis_id=analysis_id
    )
    # Exercise the normal HTTP endpoint and lose only its client response after PG commits.
    app = create_app(runtime[5])
    import bioradar.topics.routes as routes

    monkeypatch.setattr(
        routes, "history_reader", lambda settings: __import__("contextlib").nullcontext(topic().reader)
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        client.headers["Authorization"] = "Bearer " + CREDENTIAL
        response = client.post(
            f"/api/engine/topics/{topic_id}/risk-reassessments",
            json=body.model_dump(),
            headers={"Idempotency-Key": "SYNTHETIC-risk-adoption"},
        )
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["assessment"]["status"] == "accepted", first["assessment"]
        replay = client.post(
            f"/api/engine/topics/{topic_id}/risk-reassessments",
            json=body.model_dump(),
            headers={"Idempotency-Key": "SYNTHETIC-risk-adoption"},
        )
        assert replay.status_code == 200 and replay.json() == first
        fake = {**body.model_dump(), "decision": "resolved"}
        assert (
            client.post(
                f"/api/engine/topics/{topic_id}/risk-reassessments",
                json=fake,
                headers={"Idempotency-Key": "SYNTHETIC-no-client-truth"},
            ).status_code
            == 422
        )
    assert topic().reconcile_risk(evaluation.id) == first["assessment"]
    resolved = topic().get(actor, topic_id)
    assert resolved.tracking["risk"]["status"] == "resolved"
    assert resolved.control_revision == prior_control_revision + 1
    assert resolved.tracking["cadence_hours"] == resolved.definition.cadence_hours
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, analysis_id)
        assert canonical_hash([row.fixed_input, row.progress, row.result]) == before_analysis
        assert (
            session.get(TopicEvaluationRecord, evaluation.id).result["tracking"]["risk"]["status"]
            == "unresolved"
        )
    current_tracking = service.evaluation(actor, topic_id, evaluation.id).tracking
    comparison = current_tracking.pop("hypothesis_comparison")
    assert comparison["current_judgment"] == final.result.current_judgment.model_dump(mode="json")
    assert comparison["analysis_id"] == analysis_id
    assert comparison["selection"] == "model_judgment_unverified"
    assert current_tracking == original_evaluation["tracking"]
    assert resolved.tracking["hypothesis_comparison"] == comparison
    assert (meter.stages, meter.operations) == posts
    # A changed question cannot carry a scoped resolution to another event or population.
    changed = resolved.definition.model_copy(
        update={"question": "SYNTHETIC different district and later interval"}
    )
    _, revised = topic().update(
        actor,
        topic_id,
        TopicUpdate(is_demo=True, expected_revision=resolved.control_revision, definition=changed),
        "SYNTHETIC-new-risk-scope",
    )
    assert revised["tracking"]["risk"]["status"] == "unresolved"
    assert revised["tracking"]["risk"]["prior_resolution"]["status"] == "accepted"
    with pytest.raises(DomainError) as rejected:
        topic().review_risk(
            actor,
            topic_id,
            TopicRiskReviewRequest(
                expected_revision=revised["control_revision"],
                evaluation_id=evaluation.id,
                analysis_id=analysis_id,
            ),
            "SYNTHETIC-stale-scope",
        )
    assert rejected.value.code in {"not_found", "topic_risk_analysis_mismatch"}
    assert (meter.stages, meter.operations) == posts
