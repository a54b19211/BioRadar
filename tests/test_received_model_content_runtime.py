"""SYNTHETIC responses only: native PG/RQ response retention, replay and accounting.

Every database write uses the existing engine_test UUID schema fixture. The real
RQ fixture uses an isolated queue. All method/model HTTP uses MockTransport.
"""

import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from functools import wraps
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.reasoning_model import complete_json
from bioradar.api.errors import DomainError
from bioradar.research.investigations import analysis_service, kdd, professional
from bioradar.research.investigations.analysis import analysis_budget
from bioradar.research.investigations.analysis_models import AnalysisBudget, AnalysisCreate
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.execution_budget import WAIT_CODE, reserve_automatic_call
from bioradar.research.mining.models import canonical_hash
from pydantic import SecretStr
from rq.job import Job
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start
from test_query_policy_runtime import external
from test_research_recovery_faults import actual_queue as actual_queue  # noqa: F401
from test_research_recovery_faults import expire_lease

pytestmark = pytest.mark.ENGINE
PRIVATE = "SYNTHETIC_PRIVATE_ASSISTANT_CONTENT"
INSTRUCTION = "SYNTHETIC_PRIVATE_METHOD_INSTRUCTION"
USAGE = {"prompt_tokens": 137, "completion_tokens": 23, "total_tokens": 160}


class SyntheticWorkerExit(BaseException):
    """Leave the same durable running state as an abrupt host stop."""


def model_response(content, usage):
    envelope = {
        "id": "SYNTHETIC-response-id",
        "model": "synthetic-model",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }
    if usage is not None:
        envelope["usage"] = usage
    # Preserve the test's NUL/unpaired surrogate on the provider wire as escapes.
    return httpx.Response(
        200,
        content=json.dumps(envelope, ensure_ascii=True).encode("ascii"),
        headers={"content-type": "application/json"},
    )


def call_research(service, expected=None):
    def research(identifier, token, fixed, progress):
        output, _ = service._call(
            identifier,
            token,
            "shared_verify",
            {"type": "object"},
            fixed,
            progress,
            {"task": "shared_verify"},
            system_prompt=INSTRUCTION,
            call_key="SYNTHETIC-retained-content",
        )
        assert output == expected
        raise DomainError("synthetic_received_probe_finished", "SYNTHETIC controlled test stop", 422)

    service._research_result = research
    return service


def interrupt_raw_checkpoint(service):
    checkpoint = service._checkpoint

    def stop(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if any(c["status"] == "response_received" for c in progress["calls"]):
            raise SyntheticWorkerExit()

    service._checkpoint = stop


def assert_private_snapshot(call, content):
    saved = call["private_response"]
    encoded = json.dumps(content, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    assert saved["format"] == "assistant-content-json-v1"
    assert saved["content_json"] == encoded and encoded.isascii()
    assert json.loads(encoded) == content
    assert saved["content_sha256"] == hashlib.sha256(encoded.encode("ascii")).hexdigest()
    assert saved["content_present"] is True
    assert saved["refused"] is False and saved["tool_calls_present"] is False
    assert saved["receipt"]["call_id"] == call["call_id"]
    assert saved["receipt"]["input_hash"] == call["input_hash"]
    return saved


def assert_public_has_no_private_content(view):
    public = json.dumps(view.model_dump(mode="json"), ensure_ascii=True)
    for forbidden in (PRIVATE, INSTRUCTION, "private_response", "content_json", "system_prompt"):
        assert forbidden not in public
    return public


def test_professional_rpc_raw_checkpoint_worker_exit_recovers_locally_without_any_new_post(
    runtime, database, actual_queue, monkeypatch, tmp_path
):
    make, actor, view, _, _, settings, _ = runtime
    connection, queue, drain = actual_queue
    external(runtime)
    settings.mech_evidence_transport = "http"
    settings.mech_evidence_service_url = "http://synthetic-method.invalid"
    settings.mech_evidence_service_token = SecretStr("SYNTHETIC-method-test-token")
    meter = tmp_path / "synthetic-post-meter.jsonl"
    version = "SYNTHETIC-mech-method-v1"
    # Duplicate members are deliberately outside the Mech syntax/root retry policy.
    content = '{"marker":"' + PRIVATE + '","marker":"SYNTHETIC duplicate"}'
    crash = [True]
    original_client = httpx.Client

    def record(kind):
        with meter.open("a") as stream:
            stream.write(json.dumps({"kind": kind}) + "\n")

    def model(request):
        assert request.method == "POST"
        record("model_POST")
        return model_response(content, USAGE)

    def method(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "schema_version": "bioradar.professional.v1",
                    "provider": "mech_evidence",
                    "provider_version": version,
                    "operations": ["evaluate"],
                },
            )
        assert request.method == "POST"
        record("method_POST")
        body = json.loads(request.content)
        assert body["operation"] == "evaluate" and not body["model_receipts"]
        return httpx.Response(
            200,
            json={
                "schema_version": "bioradar.professional.v1",
                "provider": "mech_evidence",
                "provider_version": version,
                "operation_id": body["operation_id"],
                "status": "model_required",
                "model_call": {
                    "call_id": body["operation_id"] + ":SYNTHETIC-step",
                    "request_hash": "SYNTHETIC-native-opaque-request-hash",
                    "stage": "local_review",
                    "instruction": INSTRUCTION,
                    "payload": {"input": "SYNTHETIC fixed material"},
                    "schema": {"type": "object"},
                },
            },
        )

    model_client = original_client(transport=httpx.MockTransport(model))
    monkeypatch.setattr(
        professional.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(method), **kwargs),
    )

    def factory():
        service = make()
        service.model_call = lambda config, **kwargs: complete_json(config, client=model_client, **kwargs)
        service._saved_policy_reading = lambda _: SimpleNamespace(
            mechanism_candidate="SYNTHETIC A acts through M."
        )

        def research(identifier, token, fixed, progress):
            service._prepare(identifier, token, fixed, progress)
            # The original fixture is explicitly synthetic; classify only its test fragment.
            progress["evidence"][0].setdefault("facts", {})["source_kind"] = "primary"
            kdd.ensure_artifacts(service, identifier, token, fixed, progress)
            source = next(a for a in progress["kdd_artifacts"] if a["kind"] == "source_documents")
            action = {
                "id": "SYNTHETIC-durable-professional-action",
                "kind": "professional_assess",
                "arguments": {
                    "scope": copy.deepcopy(source["scope"]),
                    "input_refs": [{"id": source["id"], "version": source["version"]}],
                    "capability_id": "mech_evidence.evaluate",
                    "capability_version": version,
                },
            }
            state = progress["query_policy"]
            state["phase"] = "followup"
            if not state["decisions"]:
                state["decisions"].append({"step": 0, "decision": "continue", "action": action})
                state["pending_action"] = action
            service._checkpoint(identifier, token, progress, fixed=fixed)
            service._execute_policy_action(identifier, token, fixed, progress, action, 0)
            raise AssertionError("Duplicate assistant content must not become a professional result")

        service._research_result = research
        checkpoint = service._checkpoint

        def stop_after_raw(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            if crash[0] and any(c["status"] == "response_received" for c in progress["calls"]):
                os._exit(77)

        service._checkpoint = stop_after_raw
        return service

    @wraps(analysis_service.execute_analysis)
    def execute(identifier, input_hash):
        database.dispose(close=False)
        return factory().execute(identifier, input_hash)

    monkeypatch.setattr(analysis_service, "execute_analysis", execute)
    try:
        accepted = start(runtime, budget=AnalysisBudget(max_calls=8), query_policy="query_planner")
        assert queue.count == 1
        drain()
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            assert row.state == "running" and Job.fetch(row.rq_job_id, connection=connection).is_failed
            assert len(row.progress["calls"]) == 1
            call = copy.deepcopy(row.progress["calls"][0])
            assert call["status"] == "response_received" and "output" not in call
            before = assert_private_snapshot(call, content)
            assert before["receipt"]["usage"] == USAGE
            assert row.progress["queries"][0]["kind"] == "professional_assess"
            assert row.progress["queries"][0]["status"] == "prepared"
            operation = next(
                o
                for o in row.progress["professional_operations"].values()
                if (o.get("binding") or {}).get("operation") == "evaluate"
            )
            assert operation["exchanges"][-1]["response"]["status"] == "model_required"
            assert not operation["model_receipts"]
        original_posts = meter.read_text().splitlines()
        assert [json.loads(line)["kind"] for line in original_posts] == ["method_POST", "model_POST"]
        expire_lease(database, accepted["id"], expire_budget=True)
        crash[0] = False
        factory().recover()
        factory().recover()  # Existing dispatcher rotates a known failed local RQ id.
        assert queue.count == 1
        drain()
        public = make().get(actor, view["id"], accepted["id"])
        assert public.state == "blocked" and public.error_code == "model_output_invalid"
        assert_public_has_no_private_content(public)
        assert public.budget["usage"]["calls"] == 1
        assert public.budget["usage"]["total_tokens"] == USAGE["total_tokens"]
        assert public.budget["usage"]["unknown_usage_calls"] == 0
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            call = row.progress["calls"][0]
            assert call["status"] == "failed" and call["private_response"] == before
            assert call["receipt"]["usage"] == USAGE
            assert call["receipt"]["output_diagnostic"]["category"] == "duplicate_member"
            assert row.progress.get("mech_format_retry") is None
        assert meter.read_text().splitlines() == original_posts
    finally:
        model_client.close()


def test_response_received_decodes_after_time_and_call_budget_exhaustion_without_post(runtime, database):
    make, actor, view, _, _, _, _ = runtime
    expected = {"status": "PASS", "private_marker": PRIVATE}
    content = json.dumps(expected)
    posts = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: posts.append(request.method) or model_response(content, USAGE)
        )
    ) as client:

        def factory():
            service = make()
            service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
            return call_research(service, expected)

        accepted = start(runtime, budget=AnalysisBudget(max_calls=1, max_seconds=300))
        first = factory()
        interrupt_raw_checkpoint(first)
        with pytest.raises(SyntheticWorkerExit):
            first.execute(accepted["id"], accepted["input_hash"])
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            saved = assert_private_snapshot(row.progress["calls"][0], content)
            assert row.progress["calls"][0]["status"] == "response_received"
        expire_lease(database, accepted["id"], expire_budget=True)
        factory().recover()
        factory().execute(accepted["id"], accepted["input_hash"])
        public = make().get(actor, view["id"], accepted["id"])
        assert public.error_code == "synthetic_received_probe_finished"
        assert public.calls[0]["status"] == "received"
        assert public.budget["usage"]["remaining_seconds"] < 0
        assert public.budget["usage"]["calls"] == 1
        assert public.budget["usage"]["total_tokens"] == USAGE["total_tokens"]
        assert_public_has_no_private_content(public)
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            assert row.progress["calls"][0]["private_response"] == saved
        assert posts == ["POST"]


@pytest.mark.parametrize("usage", [USAGE, None], ids=["known-usage", "unknown-usage"])
def test_invalid_content_is_private_and_real_usage_survives_public_failure(runtime, database, usage):
    make, actor, view, _, _, _, _ = runtime
    content = PRIVATE + "\x00\ud800{"  # Deliberate syntax error plus PostgreSQL-unsafe raw characters.
    posts = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: posts.append(request.method) or model_response(content, usage)
        )
    ) as client:
        service = make()
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        call_research(service)
        accepted = start(runtime)
        service.execute(accepted["id"], accepted["input_hash"])
        public = make().get(actor, view["id"], accepted["id"])
        assert public.state == "blocked" and public.error_code == "model_output_invalid"
        assert_public_has_no_private_content(public)
        diagnostic = public.calls[0]["receipt"]["output_diagnostic"]
        assert diagnostic["layer"] == "assistant_content" and diagnostic["category"] == "json_syntax"
        assert diagnostic["exception_type"] == "JSONDecodeError"
        assert diagnostic["position"] == 0
        assert set(diagnostic) <= {
            "layer",
            "category",
            "content_type",
            "root_type",
            "exception_type",
            "position",
            "line",
            "column",
            "content_sha256",
        }
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            call = row.progress["calls"][0]
            assert call["status"] == "failed"
            saved = assert_private_snapshot(call, content)
            assert diagnostic["content_sha256"] == saved["content_sha256"]
            if usage is not None:
                assert call["receipt"]["usage"] == usage
        budget = public.budget["usage"]
        assert budget["calls"] == 1
        if usage is None:
            assert budget["total_tokens"] is None and budget["unknown_usage_calls"] == 1
            assert budget["budgeted_tokens"] > 0 and budget["estimated_tokens"] > 0
        else:
            assert budget["total_tokens"] == usage["total_tokens"]
            assert budget["known_total_tokens"] == usage["total_tokens"]
            assert budget["unknown_usage_calls"] == 0 and budget["estimated_tokens"] == 0
        service.execute(accepted["id"], accepted["input_hash"])
        assert posts == ["POST"]


@pytest.mark.parametrize("status", ["sent", "unconfirmed"])
def test_unknown_without_raw_never_reposts_and_remains_unknown_usage(runtime, database, status):
    make, actor, view, _, _, _, _ = runtime
    accepted = start(runtime)
    now = datetime.now(UTC)
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        progress = copy.deepcopy(row.progress)
        progress["started_at"] = now.isoformat()
        progress["calls"] = [
            {
                "stage": "shared_verify",
                "call_key": "SYNTHETIC-retained-content",
                "call_id": canonical_hash([row.id, "SYNTHETIC-retained-content"]),
                "status": status,
                "token_upper_bound": 900,
                "receipt": {"status": "unconfirmed", "usage": {}},
            }
        ]
        row.progress = progress
        row.state, row.execution_token = "running", "SYNTHETIC-lost-worker"
        row.lease_until = now - timedelta(seconds=1)
    service = make()
    service.model_call = lambda *_args, **_kwargs: pytest.fail("Unknown model outcome must never POST")
    service.recover()
    service.execute(accepted["id"], accepted["input_hash"])
    public = service.get(actor, view["id"], accepted["id"])
    assert public.state == "blocked" and public.error_code == "model_call_outcome_unconfirmed"
    assert public.calls[0]["status"] == status
    assert public.budget["usage"]["total_tokens"] is None
    assert public.budget["usage"]["unknown_usage_calls"] == 1
    assert public.budget["usage"]["budgeted_tokens"] == 900


@pytest.mark.parametrize("known", [True, False], ids=["measured-raw", "unknown-raw"])
def test_daily_reservation_counts_raw_and_failed_receipts_once_and_unknown_as_upper_bound(
    runtime, database, known
):
    make, actor, view, _, _, settings, _ = runtime
    settings.automatic_daily_model_tokens = settings.topic_daily_model_tokens = 1000
    now = datetime.now(UTC)
    jobs = []
    for index in range(2):
        accepted = make().start(
            actor,
            view["id"],
            AnalysisCreate(expected_revision=view["revision"], question=f"SYNTHETIC daily ledger {index}"),
            f"SYNTHETIC-ledger-{index}",
        )[1]
        token = f"SYNTHETIC-ledger-token-{index}"
        with Session(database) as session, session.begin():
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            fixed, progress = copy.deepcopy(row.fixed_input), copy.deepcopy(row.progress)
            fixed["execution_budget"] = {
                "version": "automatic-buckets-v1",
                "pool": "monitor",
                "topic_id": "SYNTHETIC-topic",
                "automatic_daily_tokens": 1000,
                "topic_daily_tokens": 1000,
            }
            progress["started_at"] = now.isoformat()
            if index == 0:
                # Explicit ledger fixtures exercise the actual cross-analysis PG query;
                # the professional/RQ test above exercises authentic response transitions.
                progress["calls"] = [
                    {
                        "call_id": "SYNTHETIC-raw",
                        "stage": "mech_local_review",
                        "status": "response_received",
                        "token_upper_bound": 400,
                        "receipt": {
                            "status": "completed",
                            "usage": {"prompt_tokens": 77, "completion_tokens": 23, "total_tokens": 100}
                            if known
                            else {},
                        },
                    },
                    {
                        "call_id": "SYNTHETIC-failed",
                        "stage": "shared_verify",
                        "status": "failed",
                        "token_upper_bound": 999,
                        "receipt": {"status": "failed", "usage": {"total_tokens": 50}},
                    },
                    {
                        "call_id": "SYNTHETIC-unknown",
                        "stage": "shared_verify",
                        "status": "unconfirmed",
                        "token_upper_bound": 200,
                        "receipt": {"status": "unconfirmed", "usage": {}},
                    },
                ]
                for call in progress["calls"]:
                    call["automatic_reservation"] = {
                        "day": now.date().isoformat(),
                        "topic_id": "SYNTHETIC-topic",
                    }
            else:
                progress["calls"] = [
                    {"call_id": "SYNTHETIC-next", "status": "prepared", "token_upper_bound": 650}
                ]
            row.fixed_input, row.progress = fixed, copy.deepcopy(progress)
            row.state, row.execution_token, row.lease_until = "running", token, now + timedelta(minutes=5)
        jobs.append((accepted["id"], token, fixed, progress))
    identifier, token, fixed, progress = jobs[1]
    if known:
        reserve_automatic_call(make(), identifier, token, fixed, progress, progress["calls"][0], now)
    else:
        with pytest.raises(DomainError) as stopped:
            reserve_automatic_call(make(), identifier, token, fixed, progress, progress["calls"][0], now)
        assert stopped.value.code == WAIT_CODE
    with Session(database) as session:
        saved = session.get(InvestigationAnalysisRecord, identifier).progress
        expected = 350 if known else 650
        assert saved["automatic_budget"]["global_used_before_this_call"] == expected
        assert saved["automatic_budget"]["topic_used_before_this_call"] == expected
        assert bool(saved.get("budget_wait")) is (not known)
    source_fixed, source_progress = jobs[0][2:]
    summary = analysis_budget(source_fixed, source_progress, now)["usage"]
    assert summary["calls"] == 3 and summary["budgeted_tokens"] == (350 if known else 650)
    assert summary["total_tokens"] is None
    assert summary["known_total_tokens"] == (150 if known else 50)
    assert summary["unknown_usage_calls"] == (1 if known else 2)
