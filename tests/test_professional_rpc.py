"""Professional RPC boundaries use synthetic callbacks; no private model client or provider imports."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.biocheck import (
    audit_biocheck_support,
    build_packet,
    load_biocheck_http,
    run_biocheck,
)
from bioradar.adapters.mech_evidence import evaluate_mechanism, load_mech_evidence_http
from bioradar.adapters.professional_rpc import (
    SCHEMA_VERSION,
    execute_professional,
    get_capabilities,
)
from bioradar.api.errors import DomainError

pytestmark = pytest.mark.ENGINE


def required(body, *, version="method-v1", provider="biocheck", stage="verify", schema=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": body["operation_id"],
        "provider": provider,
        "provider_version": version,
        "status": "model_required",
        "model_call": {
            "call_id": body["operation_id"] + ":step-1",
            "request_hash": "opaque-native-request-hash",
            "stage": stage,
            "instruction": "SYNTHETIC method instruction",
            "payload": {"input": "SYNTHETIC fixed source"},
            "schema": schema,
        },
    }


def completed(body, result=None, *, version="method-v1", provider="biocheck"):
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": body["operation_id"],
        "provider": provider,
        "provider_version": version,
        "status": "completed",
        "result": result if result is not None else {"answer": "SYNTHETIC"},
    }


def run(client, state, call, *, arguments=None, checkpoint=lambda: None, **kwargs):
    return execute_professional(
        client,
        provider="biocheck",
        operation_id="synthetic-operation",
        operation="verify",
        arguments=arguments if arguments is not None else {"fixed": "SYNTHETIC"},
        call=call,
        state=state,
        checkpoint=checkpoint,
        **kwargs,
    )


def one_step_service(request):
    body = json.loads(request.content)
    return httpx.Response(200, json=completed(body) if body["model_receipts"] else required(body))


def test_rpc_pins_version_preserves_opaque_hash_and_recovers_completed_without_http():
    requests, saved, calls = [], [], []
    state = {}

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer SYNTHETIC-PRIVATE-TOKEN"
        return one_step_service(request)

    def call(stage, instruction, payload, schema, *, call_id):
        calls.append((stage, instruction, payload, schema, call_id))
        return {"received": True}

    with httpx.Client(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer SYNTHETIC-PRIVATE-TOKEN"},
    ) as client:
        first = run(client, state, call, checkpoint=lambda: saved.append(copy.deepcopy(state)))
        before = copy.deepcopy(state)
        second = run(client, state, call)
    assert first == second and state == before
    assert len(requests) == 2 and len(calls) == 1
    assert requests[0]["provider_version"] is None
    assert requests[1]["provider_version"] == state["provider_version"] == "method-v1"
    assert requests[1]["model_receipts"] == [
        {
            "call_id": "synthetic-operation:step-1",
            "request_hash": "opaque-native-request-hash",
            "output": {"received": True},
        }
    ]
    assert calls[0][-1] == "synthetic-operation:step-1" and calls[0][-2] is None
    assert all(exchange["status"] == "received" for exchange in state["exchanges"])
    assert any(s.get("exchanges", [{}])[-1].get("status") == "sent" for s in saved if s.get("exchanges"))
    assert "SYNTHETIC-PRIVATE-TOKEN" not in json.dumps(state)


def test_http_unknown_replays_only_the_identical_pure_envelope_once():
    requests, model_calls, state = [], [], {}

    def handler(request):
        requests.append(request.content)
        if len(requests) == 1:
            raise httpx.ReadTimeout("SYNTHETIC response lost", request=request)
        return one_step_service(request)

    def call(*args, **kwargs):
        model_calls.append(kwargs["call_id"])
        return {"received": True}

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        assert run(client, state, call)["status"] == "completed"
    assert requests[0] == requests[1] and len(requests) == 3
    assert model_calls == ["synthetic-operation:step-1"]
    assert state["exchanges"][0]["attempts"] == 2


def test_exhausted_http_replay_remains_explicitly_blocked_without_hidden_retry():
    requests, state = [], {}

    def handler(request):
        requests.append(request.content)
        raise httpx.ReadTimeout("SYNTHETIC unavailable", request=request)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(DomainError) as error:
                run(client, state, lambda *a, **k: pytest.fail("No model step received"))
            assert error.value.code == "professional_rpc_outcome_unconfirmed"
    assert len(requests) == 2
    assert state["status"] == "blocked" and state["exchanges"][0]["status"] == "unknown"


def test_received_model_receipt_is_replayed_after_host_interrupt_without_another_model_send():
    requests, ledger, sends, state = [], {}, [], {}
    interrupted = False

    def handler(request):
        requests.append(json.loads(request.content))
        return one_step_service(request)

    def call(stage, instruction, payload, schema, *, call_id):
        nonlocal interrupted
        identity = (stage, instruction, json.dumps(payload, sort_keys=True), json.dumps(schema))
        if call_id not in ledger:
            sends.append(call_id)
            ledger[call_id] = (identity, {"received": True})
        assert ledger[call_id][0] == identity
        if not interrupted:
            interrupted = True
            raise RuntimeError("SYNTHETIC stop after host received before RPC receipt checkpoint")
        return copy.deepcopy(ledger[call_id][1])

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="after host received"):
            run(client, state, call)
        assert state["model_receipts"] == []
        assert run(client, state, call)["status"] == "completed"
    assert len(sends) == 1 and len(requests) == 2


def test_unknown_model_outcome_never_advances_or_resends_the_model():
    requests, ledger, sends, state = [], {}, [], {}

    def handler(request):
        requests.append(request.content)
        return one_step_service(request)

    def call(stage, instruction, payload, schema, *, call_id):
        if call_id not in ledger:
            sends.append(call_id)
            ledger[call_id] = "unconfirmed"
        raise DomainError("model_call_outcome_unconfirmed", "SYNTHETIC host unknown", 409)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(DomainError) as error:
                run(client, state, call)
            assert error.value.code == "model_call_outcome_unconfirmed"
    assert len(requests) == len(sends) == 1 and state["model_receipts"] == []


def test_changed_provider_version_and_changed_arguments_cannot_resume_a_prior_operation():
    requests, calls, state = [], [], {}

    def handler(request):
        requests.append(request.content)
        body = json.loads(request.content)
        return httpx.Response(
            200, json=required(body) if len(requests) == 1 else completed(body, version="method-v2")
        )

    def call(*args, **kwargs):
        calls.append(kwargs["call_id"])
        return {}

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as error:
            run(client, state, call)
        assert error.value.code == "analysis_provider_changed"
        before = copy.deepcopy(state)
        with pytest.raises(DomainError) as error:
            run(client, state, call, arguments={"fixed": "different material"})
        assert error.value.code == "professional_rpc_input_changed"
    assert state == before and len(calls) == 1 and len(requests) == 2


@pytest.mark.parametrize("defect", ["wrong_provider", "wrong_operation", "nonobject_payload", "missing_hash"])
def test_protocol_identity_or_model_shape_failure_never_reaches_host_callback(defect):
    state = {}

    def handler(request):
        body = json.loads(request.content)
        response = required(body)
        if defect == "wrong_provider":
            response["provider"] = "mech_evidence"
        elif defect == "wrong_operation":
            response["operation_id"] = "different"
        elif defect == "nonobject_payload":
            response["model_call"]["payload"] = []
        else:
            response["model_call"].pop("request_hash")
        return httpx.Response(200, json=response)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError):
            run(client, state, lambda *a, **k: pytest.fail("Malformed service step must not call model"))
    assert state["model_receipts"] == []


def test_zero_model_operation_rejects_a_model_request_and_http_errors_do_not_echo_private_body():
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(one_step_service)) as client:
        with pytest.raises(DomainError) as error:
            run(client, {}, None)
        assert error.value.code == "professional_rpc_unexpected_model_request"
    state = {}
    with httpx.Client(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda _: httpx.Response(403, text="SYNTHETIC-PRIVATE-BODY")),
    ) as client:
        with pytest.raises(DomainError) as error:
            run(client, state, None)
        assert error.value.code == "professional_rpc_http_error"
    assert "SYNTHETIC-PRIVATE-BODY" not in json.dumps(state) + str(error.value)


def test_capabilities_read_actual_service_identity_without_importing_a_provider():
    capability = {
        "schema_version": SCHEMA_VERSION,
        "provider": "biocheck",
        "provider_version": "method-v1",
        "operations": ["generate", "verify"],
    }
    with httpx.Client(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=capability)),
    ) as client:
        assert get_capabilities(client, provider="biocheck") == capability
        with pytest.raises(DomainError):
            get_capabilities(client, provider="mech_evidence")


def test_real_loopback_http_lost_response_recovers_without_duplicate_model_send():
    bodies, sends, state = [], [], {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            bodies.append(body)
            if len(bodies) == 1:
                # Pure method accepted the envelope, but its HTTP response was lost.
                self.close_connection = True
                return
            envelope = json.loads(body)
            result = completed(envelope) if envelope["model_receipts"] else required(envelope)
            response = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def call(*args, **kwargs):
            sends.append(kwargs["call_id"])
            return {"received": True}

        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
            assert run(client, state, call)["status"] == "completed"
            assert run(client, state, call)["status"] == "completed"
        assert bodies[0] == bodies[1] and len(bodies) == 3 and len(sends) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_biocheck_http_proxy_keeps_existing_report_and_raw_stage_mapping_without_local_checkout():
    operations, stages = [], []
    packet = build_packet(
        "SYNTHETIC question",
        [{"key": "E1", "text": "A activates B.", "record_ref": {"adopted_version": "v1"}}],
        {"research_scope": {"unresolved_checks": ["patents"]}},
    )
    draft = "A activates B [E1]."
    review = {
        "draft": draft, "status": "PASS", "problem_units": [], "verification_errors": [],
        "units": [{"unit_id": "U1", "start": 0, "end": len(draft), "text": draft,
                   "claims": [{"kind": "fact", "verdict": "supported"}]}],
    }

    def execute(operation, arguments, call=None):
        operations.append((operation, copy.deepcopy(arguments)))
        if operation == "generate":
            call("plan_bound", "SYNTHETIC plan", {"assessment": arguments["packet"]["assessment"]},
                 None, call_id="remote-plan")
            call("write_bound", "SYNTHETIC write", {"assessment": arguments["packet"]["assessment"]},
                 None, call_id="remote-write")
            result = {"draft": draft, "plan": {"points": []}}
        elif operation == "verify":
            call("verify", "SYNTHETIC check", {"draft": draft}, None, call_id="remote-verify")
            result = copy.deepcopy(review)
        else:
            assert operation == "audit_support"
            call("support_audit", "SYNTHETIC audit", {"claims": []}, None, call_id="remote-audit")
            result = {"status": "PASS", "original_status": "PASS", "checks": []}
        return {"provider_version": "http-method-v1", "result": result}

    def call(stage, instruction, payload, *args, call_id=None):
        stages.append((stage, call_id, copy.deepcopy(payload)))
        return {}

    loaded = load_biocheck_http(execute)
    settings = SimpleNamespace(biocheck_project_dir=None)
    report = run_biocheck(settings, packet, call, loaded=loaded, repair=False)
    assert report["status"] == "PASS" and report["provider_version"] == "http-method-v1"
    audit = audit_biocheck_support(settings, packet, report["verification"], call, loaded=loaded)
    assert audit["provider_version"] == "http-method-v1"
    assert [item[0] for item in operations] == ["generate", "verify", "audit_support"]
    assert [(stage, call_id) for stage, call_id, _ in stages] == [
        ("biocheck_plan", "remote-plan"), ("biocheck_write", "remote-write"),
        ("biocheck_check", "remote-verify"), ("biocheck_support_audit", "remote-audit"),
    ]
    assert stages[-1][2]["assessment"]["research_scope"] == packet["assessment"]["research_scope"]


def test_mech_http_proxy_preserves_fixed_material_gate_schema_and_actual_passage_binding():
    stages, invocations = [], []
    packet = build_packet(
        "SYNTHETIC mechanism",
        [{"key": "E1", "text": "A activates B.", "record_ref": {"adopted_version": "v1"}}],
    )
    packet["mechanism_case"] = {
        "question": packet["question"], "mechanism": "A -> B", "scope": {},
        "materials": [{"id": "E1", "version": "v1", "text": "A activates B."}],
    }

    def execute(operation, arguments, call=None):
        invocations.append(copy.deepcopy(arguments))
        assert operation == "evaluate" and arguments["mode"] == "biomedical_research"
        schema = {"type": "object", "additionalProperties": False}
        materials = [{"id": "E1", "version": "v1", "passages": [
            {"id": "p1", "start": 0, "end": 14, "text": "A activates B."}
        ]}]
        call("local_review", "SYNTHETIC local", {"materials": materials}, schema, call_id="remote-local")
        return {
            "provider_version": "http-mech-v1",
            "result": {"overall": {}, "evidence": [{"id": "O1", "references": [
                {"material_id": "E1", "version": "v1", "id": "p1"}
            ]}]},
        }

    def call(stage, instruction, payload, schema, *, call_id):
        stages.append((stage, call_id, schema))
        return {}

    loaded = load_mech_evidence_http(execute)
    result = evaluate_mechanism(SimpleNamespace(), packet, call, loaded=loaded)
    assert result["status"] == "completed" and result["provider_version"] == "http-mech-v1"
    assert stages == [
        ("mech_local_review", "remote-local", {"type": "object", "additionalProperties": False})
    ]
    assert result["passage_bindings"][0]["quote"] == "A activates B."
    packet["mechanism_case"]["materials"][0]["version"] = "different"
    invalid = evaluate_mechanism(SimpleNamespace(), packet, call, loaded=loaded)
    assert invalid["status"] == "needs_input" and len(invocations) == 1


def test_guard_blocks_http_before_send_without_changing_model_receipts():
    requests, state = [], {}

    def guard():
        raise DomainError("analysis_paused", "SYNTHETIC cancellation", 409)

    with httpx.Client(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda request: requests.append(request) or one_step_service(request)),
    ) as client:
        with pytest.raises(DomainError) as error:
            run(client, state, lambda *a, **k: pytest.fail("Paused"), guard=guard)
    assert error.value.code == "analysis_paused"
    assert requests == [] and state["model_receipts"] == []
    assert state["exchanges"][0]["status"] == "not_sent"


def test_service_ignoring_a_received_receipt_cannot_call_the_same_model_step_again():
    requests, calls, state = [], [], {}

    def handler(request):
        requests.append(request.content)
        return httpx.Response(200, json=required(json.loads(request.content)))

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as error:
            run(client, state, lambda *a, **k: calls.append(k["call_id"]) or {})
    assert error.value.code == "professional_rpc_receipt_rejected"
    assert len(requests) == 2 and len(calls) == 1


def test_callback_limit_is_shared_across_pure_replay_steps():
    calls, state = [], {}

    def handler(request):
        body = json.loads(request.content)
        value = required(body)
        value["model_call"]["call_id"] += ":" + str(len(body["model_receipts"]))
        return httpx.Response(200, json=value)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as error:
            run(client, state, lambda *a, **k: calls.append(k["call_id"]) or {}, max_model_calls=1)
    assert error.value.code == "professional_rpc_callback_limit" and len(calls) == 1


def test_deadline_crossed_during_sent_checkpoint_is_known_not_sent_and_never_automatically_reposted():
    state, requests, expired = {}, [], False

    def checkpoint():
        nonlocal expired
        if state.get("exchanges") and state["exchanges"][-1]["status"] == "sent":
            expired = True

    def guard():
        if expired:
            raise DomainError("analysis_time_budget", "SYNTHETIC deadline crossed during commit", 409)

    with httpx.Client(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(lambda request: requests.append(request) or one_step_service(request)),
    ) as client:
        for _ in range(2):
            with pytest.raises(DomainError) as error:
                run(
                    client, state, lambda *a, **k: pytest.fail("No model step"),
                    checkpoint=checkpoint, guard=guard,
                )
            assert error.value.code == "analysis_time_budget"
    assert requests == [] and state["model_receipts"] == []
    assert state["exchanges"][0]["status"] == "not_sent"
    assert state["exchanges"][0]["attempts"] == 0


@pytest.mark.parametrize("status", ["not_applicable", "error", "needs_input", "failed"])
def test_native_applicability_and_limited_error_code_remain_distinct_without_model_calls(status):
    state = {}

    def handler(request):
        response = completed(json.loads(request.content))
        response.pop("result")
        response.update(status=status, error_code="professional_task_not_applicable",
                        reason="SYNTHETIC this task is outside the method's scope")
        return httpx.Response(200, json=response)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        if status in {"not_applicable", "needs_input"}:
            result = run(client, state, lambda *a, **k: pytest.fail("No model for terminal feedback"))
            assert result["status"] == status
            assert result["error_code"] == "professional_task_not_applicable"
            assert "result" not in result
        else:
            with pytest.raises(DomainError) as error:
                run(client, state, lambda *a, **k: pytest.fail("Input or method error"))
            assert error.value.code == "professional_task_not_applicable"
    assert state["model_receipts"] == []
    assert state["exchanges"][0]["response"]["status"] == status
