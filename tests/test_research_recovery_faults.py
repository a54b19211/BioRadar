"""Durable PostgreSQL/RQ recovery with a local, synthetic, metered model HTTP server."""

import copy
import json
import multiprocessing
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from bioradar.adapters.reasoning_model import complete_json
from bioradar.api.errors import DomainError
from bioradar.research.investigations import analysis_service
from bioradar.research.investigations.analysis_models import AnalysisBudget
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.service import InvestigationEntryRecord
from pydantic import SecretStr
from redis import Redis
from rq import Queue, Worker
from rq.job import Job
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start
from test_shared_research_adapters import installed_biocheck as installed_biocheck  # noqa: F401

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def model_http(runtime, installed_biocheck):
    make, _, _, _, model, settings, _ = runtime
    state = SimpleNamespace(
        stages=[], fault=None, audit_enabled=False, audit_status="PASS", release=threading.Event()
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            wire = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(wire)
            stage = body["response_format"]["json_schema"]["name"]
            payload = json.loads(body["messages"][1]["content"])
            state.stages.append(stage)
            if state.fault == (stage, "timeout"):
                state.release.wait(8)
            if stage.startswith("biocheck_"):
                if stage == "biocheck_support_audit":
                    output = {
                        "status": state.audit_status,
                        "original_status": payload["review"]["status"],
                        "scope": "SYNTHETIC selected support only; not the whole report",
                        "checks": [{"unit_id": "U1", "claim_index": 0, "status": state.audit_status}],
                        "verification_errors": [],
                    }
                elif stage == "biocheck_plan":
                    output = {"points": ["SYNTHETIC bounded report plan"]}
                elif stage in {"biocheck_write", "biocheck_revise"}:
                    output = {
                        "draft": "SYNTHETIC repaired report [e1]."
                        if stage == "biocheck_revise"
                        else "SYNTHETIC initial report needing correction [e1]."
                    }
                else:
                    passed = stage == "biocheck_recheck"
                    output = {
                        "status": "PASS" if passed else "FAIL",
                        "draft": payload["draft"],
                        "problem_units": [] if passed else ["U1"],
                        "units": [{"unit_id": "U1", "claims": [{"kind": "fact", "verdict": "supported"}]}]
                        if state.audit_enabled
                        else [],
                        "coverage": {"verdict": "complete"},
                    }
                result = {
                    "id": "synthetic-local-completion",
                    "model": "synthetic-model",
                    "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                }
            else:
                response = model.handler(httpx.Request("POST", "http://127.0.0.1/v1", content=wire))
                result = response.json()
            if state.fault == (stage, "empty"):
                result["choices"][0]["message"]["content"] = ""
            encoded = json.dumps(result).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.semantic_llm_base_url = f"http://127.0.0.1:{server.server_port}/v1"
    settings.semantic_llm_timeout_seconds = 5
    settings.biocheck_project_dir = installed_biocheck.biocheck_project_dir

    def factory():
        service = make()
        service.model_call = complete_json
        return service

    try:
        yield factory, state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def actual_queue(runtime):
    url = os.environ.get("BIORADAR_ENGINE_TEST_REDIS_URL")
    if not url:
        pytest.skip("BIORADAR_ENGINE_TEST_REDIS_URL is required for actual RQ recovery checks")
    connection = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    assert connection.ping()
    settings = runtime[5]
    settings.redis_url = SecretStr(url)
    settings.queue_prefix = "bioradar:recovery-test:" + uuid4().hex
    queue = Queue(settings.queue_prefix + ":routine", connection=connection)

    def work():
        child_connection = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        try:
            Worker(
                [queue.name],
                connection=child_connection,
                name="recovery-test-" + uuid4().hex,
                job_monitoring_interval=1,
            ).work(burst=True, logging_level="CRITICAL")
        finally:
            child_connection.close()

    def drain():
        process = multiprocessing.get_context("fork").Process(target=work)
        process.start()
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
            pytest.fail("Isolated RQ worker did not finish")
        assert process.exitcode == 0

    try:
        yield connection, queue, drain
    finally:
        registries = [queue.started_job_registry, queue.finished_job_registry, queue.failed_job_registry]
        identifiers = set(queue.get_job_ids())
        for registry in registries:
            identifiers.update(registry.get_job_ids())
        for identifier in identifiers:
            Job.fetch(identifier, connection=connection).delete()
        queue.delete(delete_jobs=True)
        connection.close()


def expire_lease(database, identifier, *, expire_budget=False):
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, identifier)
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        if expire_budget:
            progress = copy.deepcopy(row.progress)
            progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            row.progress = progress


def test_received_five_stage_report_survives_worker_exit_deadline_and_duplicate_rq(
    runtime, database, audit_model_http, actual_queue, monkeypatch
):
    make, actor, view, *_ = runtime
    factory, meter = audit_model_http
    connection, queue, drain = actual_queue
    crash = [True]

    @wraps(analysis_service.execute_analysis)
    def execute(identifier, input_hash):
        database.dispose(close=False)
        service = factory()
        checkpoint = service._checkpoint

        def exit_after_received(*args, **kwargs):
            checkpoint(*args, **kwargs)
            progress = args[2]
            if crash[0] and any(
                c["stage"] == "biocheck_recheck" and c["status"] == "received" for c in progress["calls"]
            ):
                with Session(database) as session, session.begin():
                    row = session.get(InvestigationAnalysisRecord, identifier)
                    saved = copy.deepcopy(row.progress)
                    saved.pop("final_report_plan", None)
                    row.progress = saved
                os._exit(77)

        service._checkpoint = exit_after_received
        return service.execute(identifier, input_hash)

    monkeypatch.setattr(analysis_service, "execute_analysis", execute)
    accepted = start(runtime, budget=AnalysisBudget(max_calls=7))
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        progress = copy.deepcopy(row.progress)
        progress["final_report_plan"] = {
            "repair": True,
            "reserve_support_audit": False,
            "basis": "legacy_fixture",
        }
        row.progress = progress
    assert queue.count == 1
    drain()
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.state == "running" and row.result is None
        assert Job.fetch(row.rq_job_id, connection=connection).is_failed
        assert "final_report" not in row.progress
        saved_calls = copy.deepcopy(row.progress["calls"])
        assert len(saved_calls) == 7 and all(c["status"] == "received" for c in saved_calls)
        assert [c["stage"] for c in saved_calls[2:]] == [
            "biocheck_plan",
            "biocheck_write",
            "biocheck_check",
            "biocheck_revise",
            "biocheck_recheck",
        ]
    saved_posts = list(meter.stages)
    assert len(saved_posts) == 7
    expire_lease(database, accepted["id"], expire_budget=True)
    crash[0] = False
    factory().recover()
    factory().recover()  # A known failed RQ job receives a fresh dispatch id, preserving the analysis.
    assert queue.count == 1
    queue.enqueue(execute, accepted["id"], accepted["input_hash"], job_id="duplicate-" + uuid4().hex)
    drain()
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review", result.error_code
    assert result.result.report["status"] == "PASS"
    assert result.result.report["support_audit"]["status"] == "NOT_RUN"
    assert result.result.report["support_audit"]["error_code"] == "analysis_time_budget"
    assert result.result.report["final_text"] == "SYNTHETIC repaired report [e1]."
    assert result.result.report["final_text"] == result.result.report["verification"]["draft"]
    assert result.result.report["initial_verification"]["status"] == "FAIL"
    assert meter.stages == saved_posts
    assert result.budget["usage"]["calls"] == 7
    assert result.budget["usage"]["total_tokens"] == 150 * len(meter.stages)
    assert result.note_entry_id
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
        assert session.get(InvestigationAnalysisRecord, accepted["id"]).progress["calls"] == saved_calls
        assert (
            session.scalar(
                select(func.count())
                .select_from(InvestigationEntryRecord)
                .where(
                    InvestigationEntryRecord.kind == "note",
                    InvestigationEntryRecord.payload["machine_analysis_id"].astext == accepted["id"],
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    ("budget", "error_code", "posts"),
    [
        (AnalysisBudget(max_calls=1), "analysis_model_budget", 1),
        (AnalysisBudget(max_tokens=128), "analysis_token_budget", 0),
    ],
)
def test_exhausted_budget_stops_before_another_actual_http_post(
    runtime, model_http, budget, error_code, posts
):
    make, actor, view, *_ = runtime
    factory, meter = model_http
    accepted = start(runtime, budget=budget)
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == error_code
    assert len(meter.stages) == posts and result.note_entry_id is None
    assert result.budget["usage"]["total_tokens"] == 150 * posts
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    assert len(meter.stages) == posts


def test_elapsed_time_saves_received_response_but_stops_the_next_request(runtime, model_http):
    make, actor, view, *_ = runtime
    factory, meter = model_http
    accepted = start(runtime)
    service = factory()
    checkpoint = service._checkpoint

    def elapsed_after_response(identifier, token, progress, **kwargs):
        if any(c["stage"] == "read_plan" and c["status"] == "received" for c in progress["calls"]):
            progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        checkpoint(identifier, token, progress, **kwargs)

    service._checkpoint = elapsed_after_response
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_time_budget"
    assert meter.stages == ["read_plan"]
    assert result.calls[0]["status"] == "received" and result.note_entry_id is None


@pytest.mark.parametrize(("stage", "fault"), [("biocheck_revise", "timeout"), ("biocheck_recheck", "empty")])
def test_upstream_fault_does_not_turn_an_unverified_revision_into_success(runtime, model_http, stage, fault):
    make, actor, view, *_ = runtime
    factory, meter = model_http
    meter.fault = stage, fault
    accepted = start(runtime)
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.note_entry_id is None and result.result is None
    last = result.calls[-1]
    assert last["stage"] == stage
    assert last["status"] == ("unconfirmed" if fault == "timeout" else "failed")
    assert last["receipt"]["status"] == last["status"]
    posts = list(meter.stages)
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    assert meter.stages == posts


def test_failed_received_receipt_save_remains_unknown_and_is_not_resent(runtime, database, model_http):
    make, actor, view, *_ = runtime
    factory, meter = model_http
    accepted = start(runtime)
    service = factory()
    checkpoint = service._checkpoint

    def fail_before_receipt(identifier, token, progress, **kwargs):
        if any(c["stage"] == "biocheck_recheck" and c["status"] == "received" for c in progress["calls"]):
            raise SQLAlchemyError("SYNTHETIC response was received but its database commit failed")
        checkpoint(identifier, token, progress, **kwargs)

    service._checkpoint = fail_before_receipt
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.state == "running" and row.result is None
        assert row.progress["calls"][-1]["status"] == "sent"
    posts = list(meter.stages)
    expire_lease(database, accepted["id"])
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "model_call_outcome_unconfirmed"
    assert result.note_entry_id is None and result.result is None and meter.stages == posts


def test_completed_receipts_do_not_bypass_current_source_revocation(runtime, database, model_http):
    _, actor, view, source, *_ = runtime
    factory, meter = model_http
    accepted = start(runtime)
    service = factory()
    checkpoint = service._checkpoint

    def interrupted_after_check(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if any(c["stage"] == "biocheck_recheck" and c["status"] == "received" for c in progress["calls"]):
            raise SQLAlchemyError("SYNTHETIC exit before saving the complete report")

    service._checkpoint = interrupted_after_check
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    posts = list(meter.stages)
    expire_lease(database, accepted["id"], expire_budget=True)
    source.enabled.clear()
    factory().recover()
    with pytest.raises(DomainError) as failure:
        factory().execute(accepted["id"], accepted["input_hash"])
    assert failure.value.code == "history_source_revoked"
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.state == "queued" and row.result is None and row.note_entry_id is None
    assert meter.stages == posts


@pytest.mark.parametrize("last_received", ["biocheck_check", "biocheck_revise"])
def test_optional_repair_deadline_preserves_checked_original_and_never_sends_next_step(
    runtime, model_http, last_received
):
    make, actor, view, *_ = runtime
    factory, meter = model_http
    accepted = start(runtime)
    service = factory()
    checkpoint = service._checkpoint

    def expire_after_received(identifier, token, progress, **kwargs):
        if any(c["stage"] == last_received and c["status"] == "received" for c in progress["calls"]):
            progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            service.reader.deadline = time.monotonic() - 1
        checkpoint(identifier, token, progress, **kwargs)

    service._checkpoint = expire_after_received
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review" and result.note_entry_id is not None
    report = result.result.report
    assert report["status"] == "FAIL" and report["revision"] is None
    assert report["final_text"] == report["verification"]["draft"] == report["initial_verification"]["draft"]
    assert report["final_text"] == "SYNTHETIC initial report needing correction [e1]."
    assert report["repair_error_code"] == "analysis_time_budget"
    expected_next = "biocheck_revise" if last_received == "biocheck_check" else "biocheck_recheck"
    assert report["repair_stopped_at"] == expected_next
    assert expected_next not in meter.stages and meter.stages[-1] == last_received
    assert all(c["status"] == "received" for c in result.calls)
    assert result.budget["usage"]["total_tokens"] == 150 * len(meter.stages)
    if last_received == "biocheck_revise":
        assert report["unverified_revision"]["draft"] == "SYNTHETIC repaired report [e1]."


@pytest.fixture
def send_clock(monkeypatch):
    class Clock(datetime):
        offset = timedelta()

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + cls.offset

    monkeypatch.setattr(analysis_service, "datetime", Clock)
    return Clock


def test_model_intent_commit_crossing_deadline_is_durably_not_sent(runtime, model_http, send_clock):
    make, actor, view, *_ = runtime
    factory, meter = model_http
    accepted = start(runtime, budget=AnalysisBudget(max_seconds=5))
    service = factory()
    checkpoint, stalled = service._checkpoint, [False]

    def slow_intent(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if not stalled[0] and any(c["status"] == "sent" for c in progress["calls"]):
            stalled[0] = True
            send_clock.offset += timedelta(seconds=6)

    service._checkpoint = slow_intent
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_time_budget"
    assert not meter.stages and result.note_entry_id is None
    assert result.calls[0]["status"] == "failed"
    assert result.calls[0]["receipt"]["status"] == "not_sent"
    assert result.calls[0]["receipt"]["error_code"] == "analysis_time_budget"
    assert result.budget["usage"]["calls"] == result.budget["usage"]["total_tokens"] == 0
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    assert not meter.stages


@pytest.mark.parametrize("stage", ["biocheck_revise", "biocheck_recheck"])
def test_unsent_optional_step_resumes_its_saved_budget_stop_without_losing_checked_text(
    runtime, database, model_http, send_clock, stage
):
    make, actor, view, *_ = runtime
    factory, meter = model_http
    accepted = start(runtime, budget=AnalysisBudget(max_seconds=5))
    service = factory()
    checkpoint, stalled, interrupted = service._checkpoint, [False], [False]

    def stop_after_unsent_receipt(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        call = next((c for c in progress["calls"] if c["stage"] == stage), None)
        if call is None:
            return
        if not stalled[0] and call["status"] == "sent":
            stalled[0] = True
            send_clock.offset += timedelta(seconds=6)
        if not interrupted[0] and call.get("receipt", {}).get("status") == "not_sent":
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC worker exit after the explicit unsent receipt committed")

    service._checkpoint = stop_after_unsent_receipt
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    assert interrupted[0] and stage not in meter.stages
    posts = list(meter.stages)
    expire_lease(database, accepted["id"])
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review", result.error_code
    report = result.result.report
    assert report["status"] == "FAIL"
    assert report["final_text"] == report["verification"]["draft"] == report["initial_verification"]["draft"]
    assert report["final_text"] == "SYNTHETIC initial report needing correction [e1]."
    assert report["repair_error_code"] == "analysis_time_budget" and report["repair_stopped_at"] == stage
    assert meter.stages == posts and result.budget["usage"]["calls"] == len(posts)
    assert result.budget["usage"]["total_tokens"] == 150 * len(posts)


def test_weknora_intent_commit_crossing_deadline_is_not_sent_or_retried(
    runtime, database, model_http, send_clock, monkeypatch
):
    import bioradar.adapters.weknora as weknora

    make, actor, view, *_ = runtime
    factory, meter = model_http
    posts = []
    monkeypatch.setattr(weknora, "provider_status", lambda settings: {"available": True})

    def unexpected_post(*args, **kwargs):
        posts.append(kwargs)
        raise AssertionError("Expired search must not be invoked")

    monkeypatch.setattr(weknora, "search_candidates", unexpected_post)
    accepted = start(runtime, budget=AnalysisBudget(max_seconds=5))
    service = factory()
    checkpoint, stalled, interrupted = service._checkpoint, [False], [False]

    def slow_intent(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        query = next((q for q in progress["queries"] if q.get("provider") == "weknora"), None)
        if query is None:
            return
        if not stalled[0] and query["status"] == "sent":
            stalled[0] = True
            send_clock.offset += timedelta(seconds=6)
        if not interrupted[0] and (query.get("usage") or {}).get("request_status") == "not_sent":
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC exit after the unsent search receipt committed")

    service._checkpoint = slow_intent
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    assert interrupted[0] and not posts and meter.stages == ["read_plan"]
    expire_lease(database, accepted["id"])
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_time_budget"
    query = next(q for q in result.queries if q.provider == "weknora")
    assert query.status == "failed" and query.usage["request_status"] == "not_sent"
    assert not posts and meter.stages == ["read_plan"]


def test_late_transport_cap_keeps_the_committed_request_hash_and_parameters(runtime, model_http, send_clock):
    make, actor, view, *_ = runtime
    factory, _ = model_http
    runtime[5].semantic_llm_timeout_seconds = 10
    accepted = start(runtime, budget=AnalysisBudget(max_seconds=10))
    service = factory()
    checkpoint, intent = service._checkpoint, []

    def slow_intent(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        first = progress["calls"][0] if progress["calls"] else None
        if not intent and first and first["status"] == "sent":
            intent.append(copy.deepcopy(first))
            send_clock.offset += timedelta(seconds=2)

    service._checkpoint = slow_intent
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review", result.error_code
    call = result.calls[0]
    assert call["input_hash"] == call["receipt"]["input_hash"] == intent[0]["input_hash"]
    assert call["model_parameters"] == intent[0]["model_parameters"]
    assert 1 <= call["transport_timeout_seconds"] < call["model_parameters"]["timeout_seconds"]


@pytest.fixture
def audit_model_http(model_http, runtime):
    factory, meter = model_http
    core = runtime[5].biocheck_project_dir / "app/core.py"
    core.write_text(
        core.read_text() + "\ndef audit_support(packet, review, call, targets=None):\n"
        "    return call('support_audit', INSTRUCTION, dict(packet, review=review, targets=targets))\n"
    )
    meter.audit_enabled = True
    return factory, meter


def test_received_support_audit_replays_after_full_budget_and_deadline_without_more_posts(
    runtime, database, audit_model_http, send_clock
):
    make, actor, view, *_ = runtime
    factory, meter = audit_model_http
    accepted = start(runtime, budget=AnalysisBudget(max_calls=8, max_seconds=5))
    service = factory()
    checkpoint, interrupted = service._checkpoint, [False]

    def stop_after_audit(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        audit = next((c for c in progress["calls"] if c["stage"] == "biocheck_support_audit"), None)
        if not interrupted[0] and audit and audit["status"] == "received":
            assert progress["support_audit_plan"]["targets"] == [["U1", 0]]
            assert progress["final_report_plan"]["repair"] is True
            interrupted[0] = True
            send_clock.offset += timedelta(seconds=6)
            raise SQLAlchemyError(
                "SYNTHETIC exit after audit response committed but before audit result saved"
            )

    service._checkpoint = stop_after_audit
    with pytest.raises(RuntimeError, match="unconfirmed"):
        service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.result["report"]["status"] == "PASS" and "support_audit" not in row.result["report"]
        calls = copy.deepcopy(row.progress["calls"])
        decision = copy.deepcopy(row.progress["final_report_plan"])
        targets = copy.deepcopy(row.progress["support_audit_plan"])
    posts = list(meter.stages)
    assert len(posts) == 8 and posts[-1] == "biocheck_support_audit"
    expire_lease(database, accepted["id"])
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert result.result.report["status"] == result.result.report["support_audit"]["status"] == "PASS"
    assert result.result.report["final_text"] == result.result.report["verification"]["draft"]
    assert meter.stages == posts and result.budget["usage"]["calls"] == 8
    with Session(database) as session:
        progress = session.get(InvestigationAnalysisRecord, accepted["id"]).progress
        assert progress["calls"] == calls and progress["final_report_plan"] == decision
        assert progress["support_audit_plan"] == targets


@pytest.mark.parametrize("fault", ["review", "empty", "timeout", "receipt_save_failure"])
def test_support_audit_fault_preserves_original_checked_report_and_never_reposts(
    runtime, database, audit_model_http, fault
):
    make, actor, view, *_ = runtime
    factory, meter = audit_model_http
    if fault in {"empty", "timeout"}:
        meter.fault = "biocheck_support_audit", fault
    elif fault == "review":
        meter.audit_status = "REVIEW"
    accepted = start(runtime, budget=AnalysisBudget(max_calls=8))
    service = factory()
    checkpoint, interrupted = service._checkpoint, [False]

    def audit_save_failure(identifier, token, progress, **kwargs):
        audit = next((c for c in progress["calls"] if c["stage"] == "biocheck_support_audit"), None)
        if fault == "receipt_save_failure" and audit and audit["status"] == "received":
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC audit response receipt did not commit")
        checkpoint(identifier, token, progress, **kwargs)
        if fault == "empty" and not interrupted[0] and audit and audit["status"] == "failed":
            interrupted[0] = True
            raise SQLAlchemyError("SYNTHETIC exit after the known malformed audit response committed")

    service._checkpoint = audit_save_failure
    if fault in {"receipt_save_failure", "empty"}:
        with pytest.raises(RuntimeError, match="unconfirmed"):
            service.execute(accepted["id"], accepted["input_hash"])
        expire_lease(database, accepted["id"])
    else:
        service.execute(accepted["id"], accepted["input_hash"])
    posts = list(meter.stages)
    assert len(posts) == 8
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.result.report["status"] == "PASS"
    assert result.result.report["final_text"] == result.result.report["verification"]["draft"]
    assert result.result.report["final_text"] == "SYNTHETIC repaired report [e1]."
    assert meter.stages == posts
    if fault in {"review", "empty"}:
        assert result.state == "needs_review"
        assert result.result.report["support_audit"]["status"] == "REVIEW"
        if fault == "empty":
            assert result.result.report["support_audit"]["error_code"] == "model_output_invalid"
    else:
        assert result.state == "blocked" and "support_audit" not in result.result.report
        assert result.error_code in {"model_transport_unconfirmed", "model_call_outcome_unconfirmed"}
        assert result.note_entry_id is None


def test_new_budget_prioritizes_support_audit_over_optional_repair(runtime, database, audit_model_http):
    make, actor, view, *_ = runtime
    factory, meter = audit_model_http
    accepted = start(runtime, budget=AnalysisBudget(max_calls=7))
    factory().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review" and result.result.report["status"] == "FAIL"
    assert result.result.report["support_audit"]["status"] == "PASS"
    assert make().list(actor, view["id"]).limits["support_audit_calls"] == 1
    assert make().options(actor, is_demo=True)["limits"]["support_audit_calls"] == 1
    assert meter.stages == [
        "read_plan",
        "synthesize",
        "biocheck_plan",
        "biocheck_write",
        "biocheck_check",
        "biocheck_support_audit",
    ]
    with Session(database) as session:
        plan = session.get(InvestigationAnalysisRecord, accepted["id"]).progress["final_report_plan"]
        assert plan["repair"] is False and plan["reserve_support_audit"] is True
