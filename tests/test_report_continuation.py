"""SYNTHETIC same-analysis report continuation; no paid model or business data."""

import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bioradar.adapters.reasoning_model import ModelCallError
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import analysis_budget
from bioradar.research.investigations.analysis_models import AnalysisCreate, AnalysisResult
from bioradar.research.investigations.analysis_service import AnalysisService, InvestigationAnalysisRecord
from bioradar.research.mining.models import canonical_hash
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start
from test_research_recovery_faults import actual_queue as actual_queue  # noqa: F401

pytestmark = pytest.mark.ENGINE
T0 = datetime(2026, 1, 1, tzinfo=UTC)
NEW_PROVIDER = "b" * 64


def resume_fields(**changes):
    return {
        "expected_input_hash": "a" * 64,
        "stopped_call_id": "c" * 64,
        "reason": "SYNTHETIC explicit report-tail continuation",
        "max_input_bytes": 262144,
        "extra_seconds": 600,
        "biocheck_provider_version": NEW_PROVIDER,
        **changes,
    }


def time_fixture():
    fixed = {
        "budget": {"max_calls": 32, "max_tokens": 1048576, "max_seconds": 3600, "max_queries": 16},
        "model_parameters": {"max_calls": 32, "timeout_seconds": 180, "max_input_bytes": 131072},
    }
    progress = {
        "started_at": T0.isoformat(),
        "finished_at": (T0 + timedelta(seconds=100)).isoformat(),
        "calls": [{"status": "received", "receipt": {"status": "completed", "usage": {"total_tokens": 150}}}],
        "report_continuation": {
            "id": "SYNTHETIC-continuation",
            "actor_id": "SYNTHETIC-operator",
            "reason": "SYNTHETIC explicitly approved extra window",
            "authorized_at": (T0 + timedelta(seconds=4000)).isoformat(),
            "original_started_at": T0.isoformat(),
            "original_deadline": (T0 + timedelta(seconds=3600)).isoformat(),
            "extra_seconds": 600,
            "new_deadline": (T0 + timedelta(seconds=4600)).isoformat(),
            "automatic_wait_seconds_at_authorization": 0,
        },
    }
    return fixed, progress


def test_resume_report_schema_is_explicit_and_old_none_wire_is_unchanged():
    assert "resume_report" not in AnalysisCreate(expected_revision=1).model_dump(mode="json")
    body = AnalysisCreate.model_validate(
        {
            "expected_revision": 1,
            "retry_of_analysis_id": "SYNTHETIC-analysis",
            "resume_report": resume_fields(),
        }
    )
    assert body.resume_report.max_input_bytes == 262144
    assert body.resume_report.biocheck_provider_version == NEW_PROVIDER


def test_report_continuation_deadline_preserves_spent_and_names_standby_gap():
    fixed, progress = time_fixture()
    before = copy.deepcopy((fixed, progress))
    budget = analysis_budget(fixed, progress, T0 + timedelta(seconds=4010))
    assert budget["usage"]["remaining_seconds"] == 590
    assert budget["usage"]["total_tokens"] == budget["usage"]["budgeted_tokens"] == 150
    assert budget["usage"]["original_elapsed_seconds"] == 100
    assert budget["usage"]["elapsed_seconds"] == 4010
    assert budget["usage"]["report_continuation_standby_seconds"] == 400
    assert budget["original_limits"]["max_seconds"] == 3600
    assert (fixed, progress) == before


def test_report_continuation_forged_deadline_does_not_extend_budget():
    fixed, progress = time_fixture()
    progress["report_continuation"]["new_deadline"] = (T0 + timedelta(days=1)).isoformat()
    with pytest.raises(DomainError) as caught:
        analysis_budget(fixed, progress, T0 + timedelta(seconds=4010))
    assert caught.value.code == "analysis_report_continuation_invalid"


def test_without_report_continuation_budget_stays_on_original_clock():
    fixed, progress = time_fixture()
    progress.pop("report_continuation")
    budget = analysis_budget(fixed, progress, T0 + timedelta(seconds=4010))
    assert budget["usage"]["remaining_seconds"] == -410
    assert budget["usage"]["elapsed_seconds"] == 100
    assert "report_continuation" not in budget and "original_limits" not in budget


@pytest.fixture
def report_stop(runtime, database, monkeypatch):
    """Build only SYNTHETIC receipts, with a genuine adapter not_sent byte failure."""
    make, actor, view, _, model, settings, _ = runtime
    settings.analysis_max_calls = 32
    settings.analysis_max_tokens = 1048576
    settings.analysis_max_seconds = 3600
    settings.semantic_llm_max_input_bytes = 131072
    original = AnalysisService._final_report

    def stop_at_plan(self, identifier, token, fixed, progress, checked, raw):
        fixed["query_policy"].update(schema_version="query-planner.external.v2", effective="query_planner")
        fixed["query_context_version"] = "semantic-v2"
        fixed["providers"]["biocheck"] = {
            "available": True,
            "transport": "http",
            "service_endpoint": "http://synthetic-old.invalid",
            "version": "old-method-v1",
        }
        progress["provider_versions"] = {"biocheck": "old-method-v1"}
        progress["professional_capabilities"] = {"biocheck": {"operations": ["generate"]}}
        progress["report_workflow"] = {
            "version": "biocheck-quality-feedback-v1",
            "phase": "generation",
            "base_checked": checked.model_dump(mode="json"),
            "base_synthesis": copy.deepcopy(raw),
            "evidence_feedback_used": False,
            "repair_used": False,
        }
        progress["report_rounds"] = [
            {
                "index": 0,
                "kind": "generation",
                "status": "running",
                "activated": True,
                "analysis": checked.model_dump(mode="json"),
                "synthesis": copy.deepcopy(raw),
                "evidence": copy.deepcopy(progress["evidence"]),
            }
        ]
        progress["active_report_round"] = 0
        progress["report_research_scopes"] = {
            "0": {
                "mode": "current",
                "analysis_results": [
                    {"id": "synthetic-result-1", "parameters": {"scope": "SYNTHETIC " * 300}},
                    {"id": "synthetic-result-2", "parameters": {"scope": "SYNTHETIC " * 300}},
                ],
            }
        }
        progress["final_report_plan"] = {"repair": False, "reserve_support_audit": True}
        progress["final_candidate"] = "SYNTHETIC saved structured draft; not a report."
        progress["professional_operations"] = {
            "synthetic-old-operation": {
                "status": "blocked",
                "provider_version": "old-method-v1",
                "exchanges": [
                    {"status": "received", "attempts": 1, "response": {"status": "model_required"}}
                ],
            }
        }
        self._checkpoint(identifier, token, progress, fixed=fixed, result=checked.model_dump(mode="json"))
        self._call(
            identifier,
            token,
            "biocheck_plan",
            {"type": "object"},
            fixed,
            progress,
            {"synthetic_oversized": "X" * 139000},
            call_key="biocheck:old-operation:old-plan",
            system_prompt="SYNTHETIC instruction",
        )
        pytest.fail("SYNTHETIC oversized request must be stopped before model transport")

    monkeypatch.setattr(AnalysisService, "_final_report", stop_at_plan)
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    monkeypatch.setattr(AnalysisService, "_final_report", original)
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.error_code == "model_input_budget"
        progress = copy.deepcopy(row.progress)
        # Explicit synthetic saved history with nine confirmed outputs and two
        # not_sent failures. No extra network requests are made to seed this history.
        received = [c for c in progress["calls"] if c["status"] == "received"]
        for index in range(9 - len(received)):
            c = copy.deepcopy(received[0])
            c.update(
                call_id=canonical_hash([row.id, "synthetic-saved", index]),
                call_key="synthetic-saved:" + str(index),
            )
            c["receipt"]["call_id"] = c["call_id"]
            progress["calls"].insert(0, c)
        stopped = next(c for c in progress["calls"] if c["stage"] == "biocheck_plan")
        prior_read = copy.deepcopy(stopped)
        prior_read.update(
            stage="read_plan",
            call_key="optional-reading-not-sent",
            call_id=canonical_hash([row.id, "prior-read-not-sent"]),
        )
        prior_read["receipt"].update(stage="read_plan", call_id=prior_read["call_id"])
        progress["calls"].insert(-1, prior_read)
        row.progress = progress
        before = {
            "fixed": copy.deepcopy(row.fixed_input),
            "progress": copy.deepcopy(progress),
            "result": copy.deepcopy(row.result),
            "input_hash": row.input_hash,
        }
    settings.biocheck_transport = "http"
    settings.biocheck_service_url = "http://synthetic-new.invalid"
    settings.biocheck_service_token = SecretStr("SYNTHETIC-ONLY-SERVICE-TOKEN")
    body = AnalysisCreate(
        expected_revision=before["progress"]["expected_revision"],
        retry_of_analysis_id=accepted["id"],
        resume_report=resume_fields(
            expected_input_hash=before["input_hash"], stopped_call_id=stopped["call_id"]
        ),
    )
    assert len(model.requests) == 3
    return make, actor, view, settings, accepted, before, body


def test_native_resume_admission_retains_every_prior_record_and_is_idempotent(
    report_stop, database, monkeypatch
):
    make, actor, view, _, accepted, before, body = report_stop
    dispatches = []
    monkeypatch.setattr(AnalysisService, "dispatch", lambda self, identifier: dispatches.append(identifier))
    first = make().start(actor, view["id"], body, "synthetic-resume")
    repeated = make().start(actor, view["id"], body, "synthetic-resume")
    second_key = make().start(actor, view["id"], body, "synthetic-resume-same-body")
    assert first[0] == 202 and repeated == first and second_key[0] == 200
    assert dispatches == [accepted["id"]]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.fixed_input == before["fixed"] and row.input_hash == before["input_hash"]
        assert row.result == before["result"] and row.progress["calls"] == before["progress"]["calls"]
        assert row.progress["professional_operations"] == before["progress"]["professional_operations"]
        assert row.progress["report_rounds"][0] == before["progress"]["report_rounds"][0]
        assert (
            row.progress["report_research_scopes"]["0"] == before["progress"]["report_research_scopes"]["0"]
        )
        assert (
            row.progress["report_rounds"][1]["synthesis"]
            == before["progress"]["report_workflow"]["base_synthesis"]
        )
        assert (
            row.progress["report_rounds"][1]["analysis"]
            == before["progress"]["report_workflow"]["base_checked"]
        )
        scope = row.progress["report_research_scopes"]["1"]
        assert "analysis_result_context" in scope
        frozen = make()._report_context(row.fixed_input, row.progress, row.result)
        assert frozen["analysis_result_context"] == scope["analysis_result_context"]
        assert "analysis_result_context" not in frozen["research_scope"]
    public = first[1]["progress"]["report_continuation"]
    assert public["base_input_hash"] == before["input_hash"]
    assert public["provider_pin"]["version"] == NEW_PROVIDER
    assert "final_candidate" not in json.dumps(public)


@pytest.mark.parametrize(
    "invalid",
    [
        "model_unknown",
        "query_unknown",
        "rpc_unknown",
        "old_bio_received",
        "stale_input",
        "changed_revision",
        "target_read_plan",
        "cap_not_increased",
    ],
)
def test_native_resume_rejects_unknown_or_changed_basis(report_stop, database, invalid):
    make, actor, view, _, accepted, _, body = report_stop
    if invalid in {"stale_input", "target_read_plan", "cap_not_increased"}:
        changes = {
            "stale_input": {"expected_input_hash": "f" * 64},
            "target_read_plan": {"stopped_call_id": canonical_hash([accepted["id"], "prior-read-not-sent"])},
            "cap_not_increased": {"max_input_bytes": 131072},
        }[invalid]
        body = body.model_copy(update={"resume_report": body.resume_report.model_copy(update=changes)})
    elif invalid == "changed_revision":
        body = body.model_copy(update={"expected_revision": body.expected_revision + 1})
    else:
        with Session(database) as session, session.begin():
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            progress = copy.deepcopy(row.progress)
            if invalid == "model_unknown":
                progress["calls"][0]["status"] = "unconfirmed"
            elif invalid == "query_unknown":
                progress["queries"][0]["status"] = "sent"
            elif invalid == "rpc_unknown":
                progress["professional_operations"]["synthetic-old-operation"]["exchanges"][0]["status"] = (
                    "unknown"
                )
            else:
                progress["calls"][-1]["receipt"]["status"] = "completed"
            row.progress = progress
    with pytest.raises(DomainError):
        make().start(actor, view["id"], body, "synthetic-refused-resume")
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.state == "blocked" and "report_continuation" not in row.progress


def begin_report_round(report_stop, database):
    make, actor, view, _, accepted, _, body = report_stop
    make().start(actor, view["id"], body, "synthetic-resume")
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        row.state, row.execution_token = "running", "synthetic-lease"
        row.lease_until = datetime.now(UTC) + timedelta(seconds=600)
        fixed, progress = copy.deepcopy(row.fixed_input), copy.deepcopy(row.progress)
    return make(), accepted["id"], fixed, progress


def test_new_attempt_profiles_actual_bytes_and_received_replay_precedes_all_new_work(report_stop, database):
    service, identifier, fixed, progress = begin_report_round(report_stop, database)
    before_calls = copy.deepcopy(progress["calls"])
    requests = []
    from bioradar.adapters.reasoning_model import complete_json

    def handler(request):
        requests.append(request.content)
        return httpx.Response(
            200,
            json={
                "id": "synthetic-tail",
                "model": fixed["model"],
                "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        payload = {"SYNTHETIC-new-packet": "X" * 140000}
        output, progress = service._call(
            identifier,
            "synthetic-lease",
            "biocheck_plan",
            {"type": "object"},
            fixed,
            progress,
            payload,
            call_key="biocheck:new-op:new-plan",
            system_prompt="SYNTHETIC instruction",
        )
        assert output == {"ok": True} and len(requests) == 1
        assert progress["calls"][:-1] == before_calls
        call = progress["calls"][-1]
        assert call["model_parameters"]["max_input_bytes"] == 262144
        assert call["call_key"].startswith("report-round:1:")
        assert call["continues_call_id"] == before_calls[-1]["call_id"]
        assert progress["report_continuation"]["profiles"][0]["request_bytes"] == len(requests[0])
        assert fixed["model_parameters"]["max_input_bytes"] == 131072
        # Re-enter from the actual DB checkpoint, as after a process exit.
        with Session(database) as session:
            saved = copy.deepcopy(session.get(InvestigationAnalysisRecord, identifier).progress)
        service.model_call = lambda *args, **kwargs: pytest.fail("Received tail must not resend")
        assert (
            service._call(
                identifier,
                "synthetic-lease",
                "biocheck_plan",
                {"type": "object"},
                fixed,
                saved,
                payload,
                call_key="biocheck:new-op:new-plan",
                system_prompt="SYNTHETIC instruction",
            )[0]
            == output
        )
        assert len(requests) == 1


@pytest.mark.parametrize("size,raises,expected_cap", [(1000, False, 131072), (270000, True, 262144)])
def test_explicit_cap_is_measured_bounded_and_never_applied_to_legacy(
    report_stop, database, size, raises, expected_cap
):
    service, identifier, fixed, progress = begin_report_round(report_stop, database)
    sent = []
    from bioradar.adapters.reasoning_model import complete_json

    def handler(request):
        sent.append(request.content)
        return httpx.Response(
            200,
            json={
                "id": "synthetic-tail",
                "model": fixed["model"],
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        if raises:
            with pytest.raises(ModelCallError) as caught:
                service._call(
                    identifier,
                    "synthetic-lease",
                    "biocheck_write",
                    {"type": "object"},
                    fixed,
                    progress,
                    {"synthetic": "X" * size},
                )
            assert caught.value.receipt.status == "not_sent" and sent == []
        else:
            service._call(
                identifier,
                "synthetic-lease",
                "biocheck_write",
                {"type": "object"},
                fixed,
                progress,
                {"synthetic": "X" * size},
            )
            assert len(sent) == 1
    assert progress["calls"][-1]["model_parameters"]["max_input_bytes"] == expected_cap
    assert (
        service.settings.semantic_llm_max_input_bytes
        == fixed["model_parameters"]["max_input_bytes"]
        == 131072
    )


def test_new_provider_is_pinned_independently_and_old_rpc_is_never_reused(report_stop, database, monkeypatch):
    from bioradar.adapters.biocheck import _callback
    from bioradar.research.investigations import professional
    from test_professional_rpc import completed, required

    service, identifier, fixed, progress = begin_report_round(report_stop, database)
    old_operation = copy.deepcopy(progress["professional_operations"]["synthetic-old-operation"])
    old_versions = copy.deepcopy(progress["provider_versions"])
    old_caps = copy.deepcopy(progress["professional_capabilities"])
    http_requests, model_requests = [], []
    original_client = httpx.Client

    def handler(request):
        http_requests.append((request.method, request.url.host))
        assert request.url.host == "synthetic-new.invalid"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "schema_version": "bioradar.professional.v1",
                    "provider": "biocheck",
                    "provider_version": NEW_PROVIDER,
                    "operations": ["generate"],
                },
            )
        body = json.loads(request.content)
        if body["model_receipts"]:
            return httpx.Response(
                200, json=completed(body, {"synthetic": "new provider result"}, version=NEW_PROVIDER)
            )
        return httpx.Response(200, json=required(body, stage="plan_bound", version=NEW_PROVIDER))

    def model_call(config, **kwargs):
        from bioradar.adapters.reasoning_model import ModelCallReceipt, ModelCompletion, ModelUsage

        model_requests.append(kwargs["call_id"])
        now = datetime.now(UTC)
        return ModelCompletion(
            content='{"synthetic":"confirmed output"}',
            parsed={"synthetic": "confirmed output"},
            receipt=ModelCallReceipt(
                call_id=kwargs["call_id"],
                stage=kwargs["stage"],
                input_hash=kwargs["input_hash"],
                prompt_version=kwargs["prompt_version"],
                requested_model=fixed["model"],
                configured_model_revision=fixed["model_revision"],
                status="completed",
                started_at=now,
                completed_at=now,
                elapsed_ms=1,
                max_output_tokens=config.semantic_llm_max_output_tokens,
                usage=ModelUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            ),
        )

    service.model_call = model_call
    monkeypatch.setattr(
        professional.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    loaded = service._load_professional(identifier, "synthetic-lease", fixed, progress, "biocheck")
    result = loaded[0].generate(
        {"synthetic": "packet"},
        _callback(service._shared_callback(identifier, "synthetic-lease", fixed, progress)),
    )
    assert result == {"synthetic": "new provider result"}
    assert len(model_requests) == 1 and [m for m, _ in http_requests] == ["GET", "POST", "POST"]
    assert progress["provider_versions"] == old_versions and progress["professional_capabilities"] == old_caps
    assert progress["professional_operations"]["synthetic-old-operation"] == old_operation
    assert progress["report_continuation"]["loaded_provider_version"] == NEW_PROVIDER
    assert progress["report_continuation_capabilities"]["provider_version"] == NEW_PROVIDER
    assert model_requests[0] != progress["report_continuation"]["from_call_id"]
    before = copy.deepcopy(progress)
    loaded = service._load_professional(identifier, "synthetic-lease", fixed, progress, "biocheck")
    assert (
        loaded[0].generate(
            {"synthetic": "packet"},
            _callback(service._shared_callback(identifier, "synthetic-lease", fixed, progress)),
        )
        == result
    )
    assert len(model_requests) == 1 and len(http_requests) == 3
    assert progress["professional_operations"] == before["professional_operations"]
    # The existing single author-repair child remains in the same explicit grant,
    # while a different generation round cannot inherit it.
    progress["report_rounds"].append({"index": 2, "kind": "repair", "parent_index": 1})
    progress["active_report_round"] = 2
    progress["report_workflow"]["phase"] = "repair_active"
    loaded = service._load_professional(identifier, "synthetic-lease", fixed, progress, "biocheck")
    assert (
        loaded[0].generate(
            {"synthetic": "repair child packet"},
            _callback(service._shared_callback(identifier, "synthetic-lease", fixed, progress)),
        )
        == result
    )
    assert len(model_requests) == 2 and len(http_requests) == 5  # no second capabilities GET
    assert progress["calls"][-1]["report_continuation_id"] == progress["report_continuation"]["id"]
    assert progress["calls"][-1]["call_key"].startswith("report-round:2:")
    progress["report_rounds"].append({"index": 3, "kind": "generation"})
    progress["active_report_round"] = 3
    with pytest.raises(DomainError, match="固定专业服务"):
        service._load_professional(identifier, "synthetic-lease", fixed, progress, "biocheck")
    assert len(model_requests) == 2 and len(http_requests) == 5


def test_new_provider_mismatch_stops_before_operation_or_model(report_stop, database, monkeypatch):
    from bioradar.research.investigations import professional

    service, identifier, fixed, progress = begin_report_round(report_stop, database)
    original_client, methods = httpx.Client, []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "schema_version": "bioradar.professional.v1",
                "provider": "biocheck",
                "provider_version": "different-method",
                "operations": ["generate"],
            },
        )

    monkeypatch.setattr(
        professional.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(DomainError, match="续稿专业服务"):
        service._load_professional(identifier, "synthetic-lease", fixed, progress, "biocheck")
    assert methods == ["GET"]
    assert "loaded_provider_version" not in progress["report_continuation"]


def test_new_model_unknown_is_retained_and_does_not_gain_a_second_attempt(report_stop, database):
    from bioradar.adapters.reasoning_model import complete_json

    service, identifier, fixed, progress = begin_report_round(report_stop, database)
    requests = []

    def handler(request):
        requests.append(request.content)
        raise httpx.ReadTimeout("SYNTHETIC unknown model result", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        with pytest.raises(ModelCallError) as caught:
            service._call(
                identifier,
                "synthetic-lease",
                "biocheck_write",
                {"type": "object"},
                fixed,
                progress,
                {"synthetic": "packet"},
            )
        assert caught.value.receipt.status == "unconfirmed"
        with pytest.raises(DomainError) as caught:
            service._call(
                identifier,
                "synthetic-lease",
                "biocheck_write",
                {"type": "object"},
                fixed,
                progress,
                {"synthetic": "packet"},
            )
        assert caught.value.code == "model_call_outcome_unconfirmed" and len(requests) == 1


def test_native_http_endpoint_reuses_analysis_and_exports_continuation_metadata(
    report_stop, database, monkeypatch
):
    import bioradar.research.history.service as history_module
    from bioradar.api.app import create_app
    from fastapi.testclient import TestClient

    make, _, view, settings, accepted, before, body = report_stop
    from contextlib import contextmanager

    from test_engine_persistence import CREDENTIAL

    @contextmanager
    def reader(*args, **kwargs):
        yield make().reader

    monkeypatch.setattr(history_module, "history_reader", reader)
    monkeypatch.setattr(AnalysisService, "dispatch", lambda *args: False)
    settings.data_fabric_url = "http://127.0.0.1"
    settings.data_fabric_history_read_token = SecretStr("SYNTHETIC-ONLY-READ-TOKEN")
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        try:
            path = f"/api/engine/investigations/{view['id']}/analyses"
            headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "synthetic-http-resume"}
            response = client.post(path, headers=headers, json=body.model_dump(mode="json"))
            assert response.status_code == 202, response.text
            public = response.json()
            assert public["id"] == accepted["id"] and len(public["calls"]) == 11
            assert public["progress"]["report_continuation"]["base_input_hash"] == before["input_hash"]
            export = client.get(path + "/" + accepted["id"] + "/export?format=json", headers=headers)
            assert export.status_code == 200
            assert (
                export.json()["analysis"]["progress"]["report_continuation"]
                == public["progress"]["report_continuation"]
            )
        finally:
            app.state.database = None


def test_legacy_retry_still_creates_a_separate_analysis(report_stop, database, monkeypatch):
    make, actor, view, _, accepted, before, _ = report_stop
    monkeypatch.setattr(AnalysisService, "dispatch", lambda *args: False)
    # This legacy test isolates the unchanged fresh-analysis path from provider status HTTP.
    monkeypatch.setattr(
        AnalysisService, "_provider_status", lambda *args, **kwargs: before["fixed"]["providers"]
    )
    code, retried = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=before["progress"]["expected_revision"], retry_of_analysis_id=accepted["id"]
        ),
        "synthetic-old-retry",
    )
    assert code == 202 and retried["id"] != accepted["id"] and retried["calls"] == []
    assert "report_continuation" not in retried["progress"]


def test_actual_rq_exit_after_received_tail_recovers_same_id_without_resending(
    report_stop, database, actual_queue, monkeypatch
):
    import os
    from functools import wraps

    import bioradar.research.investigations.analysis_service as service_module
    from bioradar.adapters.reasoning_model import ModelCallReceipt, ModelCompletion, ModelUsage

    make, actor, view, _, accepted, before, body = report_stop
    _, queue, drain = actual_queue

    def synthetic_model(config, **kwargs):
        now = datetime.now(UTC)
        return ModelCompletion(
            content='{"synthetic":true}',
            parsed={"synthetic": True},
            receipt=ModelCallReceipt(
                call_id=kwargs["call_id"],
                stage=kwargs["stage"],
                input_hash=kwargs["input_hash"],
                prompt_version=kwargs["prompt_version"],
                requested_model="synthetic-model",
                configured_model_revision="synthetic-v1",
                status="completed",
                started_at=now,
                completed_at=now,
                elapsed_ms=1,
                max_output_tokens=config.semantic_llm_max_output_tokens,
                usage=ModelUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            ),
        )

    def report_tail(self, identifier, token, fixed, progress):
        output, progress = self._call(
            identifier,
            token,
            "biocheck_plan",
            {"type": "object"},
            fixed,
            progress,
            {"SYNTHETIC-tail": True},
            call_key="biocheck:new-op:plan",
        )
        assert output == {"synthetic": True}
        if not progress["report_continuation"].get("synthetic_exit_recorded"):
            progress["report_continuation"]["synthetic_exit_recorded"] = True
            self._checkpoint(identifier, token, progress)
            os._exit(77)
        checked = copy.deepcopy(progress["report_workflow"]["base_checked"])
        checked["review"]["status"] = "needs_review"
        checked["report"] = {
            "status": "REVIEW",
            "final_text": "SYNTHETIC report-tail recovery test; not a professional result.",
            "support_audit": {"status": "NOT_RUN"},
        }
        return checked, progress

    @wraps(service_module.execute_analysis)
    def execute(identifier, input_hash):
        service = make()
        service.model_call = synthetic_model
        return service.execute(identifier, input_hash)

    monkeypatch.setattr(AnalysisService, "_research_result", report_tail)
    monkeypatch.setattr(service_module, "execute_analysis", execute)
    code, public = make().start(actor, view["id"], body, "synthetic-rq-resume")
    assert code == 202 and public["id"] == accepted["id"] and queue.count == 1
    queued = queue.fetch_job(queue.get_job_ids()[0])
    assert queued.timeout > before["fixed"]["model_parameters"]["job_timeout_seconds"]
    drain()
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.state == "running" and len(row.progress["calls"]) == 12
        assert row.progress["calls"][-1]["status"] == "received"
        saved_tail = copy.deepcopy(row.progress["calls"][-1])
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    make().recover()
    make().recover()
    assert queue.count == 1
    drain()
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review" and result.id == accepted["id"]
    assert result.calls[:-1] == [
        {k: v for k, v in c.items() if k not in {"payload", "output"}} for c in before["progress"]["calls"]
    ]
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.fixed_input == before["fixed"] and row.input_hash == before["input_hash"]
        assert row.progress["calls"][-1] == saved_tail
        assert len(row.progress["calls"]) == 12
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
    assert result.budget["usage"]["total_tokens"] == 9 * 150 + 120


def test_known_optional_revision_failure_preserves_checked_text_and_finishes_needs_review(
    report_stop, database, monkeypatch
):
    from types import SimpleNamespace

    import bioradar.adapters.biocheck as biocheck

    make, actor, view, _, accepted, _, body = report_stop
    old_text = "SYNTHETIC already checked text; this test is not professional approval."
    verification = {"status": "PASS", "draft": old_text, "verification_method": "cited_claims"}
    monkeypatch.setattr(
        biocheck,
        "revise_biocheck",
        lambda *args, **kwargs: {
            "status": "failed",
            "needs_verification": False,
            "error_code": "biocheck_revision_invalid",
            "draft": old_text,
            "changed_units": [],
        },
    )

    def final_tail(self, identifier, token, fixed, progress):
        preserved = copy.deepcopy(progress["report_workflow"]["base_checked"])
        preserved["report"] = {
            "status": "PASS",
            "final_text": old_text,
            "verification": verification,
            "support_audit": {"status": "PASS"},
        }
        progress["report_rounds"][0]["result"] = copy.deepcopy(preserved)
        progress["report_rounds"][1].update(kind="repair", parent_index=0)
        progress["report_workflow"].update(phase="repair_active", repair_used=True)
        result, progress = self._quality_reports(
            identifier,
            token,
            fixed,
            progress,
            AnalysisResult.model_validate(preserved),
            progress["report_workflow"]["base_synthesis"],
            (SimpleNamespace(), NEW_PROVIDER),
        )
        return result.model_dump(mode="json"), progress

    monkeypatch.setattr(AnalysisService, "_research_result", final_tail)
    make().start(actor, view["id"], body, "synthetic-known-revision-stop")
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review"
    assert result.result.report["final_text"] == old_text
    assert result.result.report["verification"] == verification
    assert result.result.report["rounds"][1]["status"] == "failed"
    assert result.progress["report_revision_error"]["code"] == "biocheck_revision_invalid"
    with Session(database) as session:
        assert len(session.get(InvestigationAnalysisRecord, accepted["id"]).progress["calls"]) == 11


def test_continuation_rq_and_reader_window_follow_shared_deadline_without_changing_fixed_timeout():
    from bioradar.research.investigations.analysis_service import execution_seconds

    fixed, progress = time_fixture()
    fixed["model_parameters"]["job_timeout_seconds"] = 3660
    progress["report_continuation"].update(
        authorized_at=(T0 + timedelta(seconds=10)).isoformat(),
        extra_seconds=1800,
        new_deadline=(T0 + timedelta(seconds=5400)).isoformat(),
    )
    now = T0 + timedelta(seconds=20)
    before = copy.deepcopy(fixed)
    assert execution_seconds(fixed) == 3660
    assert execution_seconds(fixed, progress, now=now) > 5380
    assert fixed == before


def test_report_continuation_quality_gap_cannot_reopen_queries_or_resynthesize(
    report_stop, database, monkeypatch
):
    from types import SimpleNamespace

    import bioradar.adapters.biocheck as biocheck

    service, identifier, fixed, progress = begin_report_round(report_stop, database)
    before_calls = copy.deepcopy(progress["calls"])
    before_queries = copy.deepcopy(progress["queries"])
    checked = AnalysisResult.model_validate(progress["report_workflow"]["base_checked"])
    checked.report = {
        "status": "REVIEW",
        "final_text": "SYNTHETIC checked report with an unresolved material gap.",
        "verification": {"status": "REVIEW"},
        "support_audit": {"status": "REVIEW"},
    }
    monkeypatch.setattr(service, "_saved_final_report", lambda *args: (checked, progress))
    gap = {"id": "synthetic-unsearched", "question": "SYNTHETIC unresolved source question"}
    monkeypatch.setattr(
        biocheck,
        "quality_feedback_biocheck",
        lambda *args, **kwargs: {
            "evidence_gaps": [gap],
            "review_required": [],
            "repair_units": [],
        },
    )
    monkeypatch.setattr(
        service, "_policy_research", lambda *args: pytest.fail("Report-only grant cannot query")
    )
    monkeypatch.setattr(
        service, "_quality_synthesis", lambda *args: pytest.fail("Saved base synthesis must replay")
    )
    result, progress = service._quality_reports(
        identifier,
        "synthetic-lease",
        fixed,
        progress,
        checked,
        progress["report_workflow"]["base_synthesis"],
        (SimpleNamespace(), NEW_PROVIDER),
    )
    current = progress["report_rounds"][1]
    assert current["unsearched_gaps"] == [gap]
    assert current["feedback_stop_reason"] == "report_continuation_fixed_scope"
    assert progress["report_workflow"]["phase"] == "complete"
    assert progress["queries"] == before_queries and progress["calls"] == before_calls
    assert result.report["status"] == "REVIEW"


def test_continuation_absolute_deadline_does_not_grow_during_new_automatic_budget_wait():
    fixed, progress = time_fixture()
    fixed["execution_budget"] = {"version": "automatic-buckets-v1", "pool": "monitor"}
    progress["budget_wait_intervals"] = [
        {
            "reason": "analysis_automatic_budget_wait",
            "started_at": (T0 + timedelta(seconds=4005)).isoformat(),
            "ended_at": (T0 + timedelta(seconds=4009)).isoformat(),
        }
    ]
    now = T0 + timedelta(seconds=4010)
    before = copy.deepcopy(progress)
    assert analysis_budget(fixed, progress, now)["usage"]["remaining_seconds"] == 590
    assert progress == before
    progress.pop("report_continuation")
    # The old automatic-clock policy still pauses for the same four real seconds.
    assert analysis_budget(fixed, progress, now)["usage"]["remaining_seconds"] == -406
