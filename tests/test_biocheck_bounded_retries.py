"""Bounded native plan/verify repairs keep old arguments and durable callback identities."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.biocheck import (
    check_biocheck,
    load_biocheck_http,
    run_biocheck,
    supports_bounded_retry,
)
from bioradar.adapters.professional_rpc import execute_professional
from bioradar.api.errors import DomainError
from test_biocheck_coordination import ControlledCalls, model_error
from test_biocheck_coordination import current_biocheck as current_biocheck
from test_biocheck_coordination import packet as native_packet
from test_professional_rpc import completed, required
from test_shared_research_adapters import packet, review

pytestmark = pytest.mark.ENGINE
VERSION = "synthetic-bounded-method-v1"


def capabilities():
    return {
        "schema_version": "bioradar.professional.v1",
        "provider": "biocheck",
        "provider_version": VERSION,
        "operations": ["generate", "verify"],
        "features": ["bounded_plan_repair", "bounded_verify_contract_repair"],
        "plan_retry_policy": {
            "argument": "max_plan_retries",
            "default": 0,
            "maximum": 1,
            "stages": ["replan_plain", "replan_bound"],
            "diagnostic_limit": 8,
        },
        "verify_retry_policy": {
            "argument": "max_verify_retries",
            "default": 0,
            "maximum": 1,
            "stages": ["verify_repair"],
            "diagnostic_limit": 8,
        },
        "provider_extra": {"retained": True},
    }


def test_proxy_keeps_legacy_arguments_and_exposes_only_pinned_supported_retry_capabilities():
    requests = []

    def execute(operation, arguments, call=None):
        requests.append((operation, copy.deepcopy(arguments)))
        return {"provider_version": VERSION, "result": {"draft": "SYNTHETIC"}}

    caps = capabilities()
    core, _ = load_biocheck_http(execute, VERSION, capabilities=caps)
    caps["provider_extra"]["retained"] = False
    assert core.capabilities["provider_extra"]["retained"] is True
    assert supports_bounded_retry(core, "plan") and supports_bounded_retry(core, "verify")
    data = packet()
    core.generate(data, None)
    core.verify(data, "SYNTHETIC", None, max_verify_retries=0)
    assert requests == [
        ("generate", {"packet": data, "method": "bound"}),
        ("verify", {"packet": data, "draft": "SYNTHETIC"}),
    ]
    core.generate(data, None, max_plan_retries=1)
    core.verify(data, "SYNTHETIC", None, max_verify_retries=1)
    assert requests[-2][1]["max_plan_retries"] == requests[-1][1]["max_verify_retries"] == 1
    legacy, _ = load_biocheck_http(execute, VERSION)
    before = len(requests)
    with pytest.raises(DomainError, match="未声明"):
        legacy.verify(data, "SYNTHETIC", None, max_verify_retries=1)
    assert len(requests) == before


@pytest.mark.parametrize("defect", ["version", "missing_version", "feature", "policy"])
def test_capability_optin_requires_the_actual_version_feature_and_exact_bounded_policy(defect):
    caps = capabilities()
    if defect == "version":
        caps["provider_version"] = "different-method"
    elif defect == "missing_version":
        caps.pop("provider_version")
    elif defect == "feature":
        caps["features"] = []
    else:
        caps["plan_retry_policy"]["maximum"] = True
        caps["verify_retry_policy"]["maximum"] = 2
    core, _ = load_biocheck_http(lambda *a, **k: pytest.fail("No RPC allowed"), VERSION, capabilities=caps)
    assert not supports_bounded_retry(core, "plan")
    assert not supports_bounded_retry(core, "verify")


def test_explicit_check_and_recheck_route_repair_callbacks_to_distinct_stages():
    stages = []

    def host(stage, instruction, payload):
        stages.append(stage)
        if stage == "biocheck_write":
            return {"draft": "SYNTHETIC initial"}
        if stage == "biocheck_verify_repair":
            return review(payload["draft"], status="FAIL", problems=["U1"])
        if stage == "biocheck_revise":
            return {"draft": "SYNTHETIC revised"}
        if stage == "biocheck_recheck_repair":
            return review(payload["draft"])
        return {"synthetic_contract_input": stage}

    class Methods:
        def generate(self, data, call, method="bound", *, max_plan_retries=0):
            assert max_plan_retries == 1
            first = call("plan_bound", "SYNTHETIC", data)
            planned = call("replan_bound", "SYNTHETIC", {**data, "plan_repair": {"failed_plan": first}})
            written = call("write_bound", "SYNTHETIC", data)
            return {"method": method, "plan": planned, "draft": written["draft"]}

        def verify(self, data, draft, call, *, max_verify_retries=0):
            assert max_verify_retries == 1
            failed = call("verify", "SYNTHETIC", {**data, "draft": draft})
            return call(
                "verify_repair",
                "SYNTHETIC",
                {**data, "draft": draft, "verify_repair": {"failed_output": failed}},
            )

        def revise(self, data, initial, call):
            return call("revise", "SYNTHETIC", {"draft": initial["draft"]})

    result = run_biocheck(
        None,
        packet(),
        host,
        loaded=(Methods(), VERSION),
        max_plan_retries=1,
        max_verify_retries=1,
    )
    assert result["status"] == "PASS" and result["final_text"] == "SYNTHETIC revised"
    assert stages == [
        "biocheck_plan",
        "biocheck_plan_repair",
        "biocheck_write",
        "biocheck_check",
        "biocheck_verify_repair",
        "biocheck_revise",
        "biocheck_recheck",
        "biocheck_recheck_repair",
    ]


@pytest.mark.parametrize("code", ["verification_contract_invalid", "verification_repair_exhausted"])
@pytest.mark.parametrize("mode", ["run", "check"])
def test_exhausted_or_invalid_initial_verification_keeps_draft_without_revision(code, mode):
    revisions = []

    def verify(*args, **kwargs):
        raise DomainError(code, "SYNTHETIC known contract failure", 502)

    methods = SimpleNamespace(
        generate=lambda *a, **k: {"draft": "SYNTHETIC retained"},
        verify=verify,
        revise=lambda *a, **k: revisions.append(True),
    )
    args = (None, packet(), lambda *a, **k: pytest.fail("No hidden model retry"))
    if mode == "run":
        result = run_biocheck(*args, loaded=(methods, VERSION), max_verify_retries=1)
    else:
        result = check_biocheck(
            args[0], args[1], "SYNTHETIC retained", args[2], loaded=(methods, VERSION), max_verify_retries=1
        )
    assert result["status"] == "failed" and result["error_code"] == code
    assert result["final_text"] == "SYNTHETIC retained"
    assert result["verification"] is None and result["revision"] is None and revisions == []


@pytest.mark.parametrize("outcome", ["known", "unknown"])
def test_failed_recheck_keeps_checked_original_and_unknown_never_becomes_known_failure(outcome):
    calls = []
    failure = (
        DomainError("verification_repair_exhausted", "SYNTHETIC known exhausted contract", 502)
        if outcome == "known"
        else model_error("verification_repair_exhausted", "biocheck_recheck_repair", "unconfirmed")
    )

    def verify(data, draft, call, **kwargs):
        calls.append(draft)
        if len(calls) == 1:
            return review(draft, status="FAIL", problems=["U1"])
        raise failure

    methods = SimpleNamespace(
        verify=verify,
        revise=lambda *a, **k: {"draft": "SYNTHETIC unverified revision"},
    )
    if outcome == "unknown":
        with pytest.raises(DomainError) as raised:
            check_biocheck(
                None,
                packet(),
                "SYNTHETIC checked original",
                lambda *a: {},
                loaded=(methods, VERSION),
                max_verify_retries=1,
            )
        assert raised.value is failure
    else:
        result = check_biocheck(
            None,
            packet(),
            "SYNTHETIC checked original",
            lambda *a: {},
            loaded=(methods, VERSION),
            max_verify_retries=1,
        )
        assert result["final_text"] == result["verification"]["draft"] == "SYNTHETIC checked original"
        assert result["repair_error_code"] == "verification_repair_exhausted"
        assert result["unverified_revision"]["draft"] == "SYNTHETIC unverified revision"
    assert len(calls) == 2


def test_http_recheck_restart_replays_received_and_preserves_explicit_repair_identity():
    data = packet()
    states, ledger, posts, sends = {}, {}, [], []
    operation = "initial"
    interrupted = False

    def handler(request):
        body = json.loads(request.content)
        posts.append(copy.deepcopy(body))
        assert body["arguments"]["max_verify_retries"] == 1
        receipts = body["model_receipts"]
        if len(receipts) == 2:
            return httpx.Response(
                200, json=completed(body, review(body["arguments"]["draft"]), version=VERSION)
            )
        stage = "verify" if not receipts else "verify_repair"
        response = required(body, stage=stage, version=VERSION)
        response["model_call"]["call_id"] = stage
        response["model_call"]["request_hash"] = "opaque-" + stage
        response["model_call"]["payload"] = copy.deepcopy(body["arguments"])
        return httpx.Response(200, json=response)

    def host(stage, instruction, payload, schema=None, *, call_id):
        nonlocal interrupted
        identity = (call_id, stage)
        if identity not in ledger:
            sends.append(identity)
            ledger[identity] = {"SYNTHETIC received": stage}
        if operation == "recheck" and stage == "biocheck_recheck" and not interrupted:
            interrupted = True
            raise RuntimeError("SYNTHETIC exit after the received model was checkpointed")
        return copy.deepcopy(ledger[identity])

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler)) as client:

        def execute(name, arguments, call=None):
            assert name == "verify"
            opid = "synthetic-" + operation

            def callback(stage, instruction, payload, schema, *, call_id):
                return call(stage, instruction, payload, schema, call_id="biocheck:" + opid + ":" + call_id)

            return execute_professional(
                client,
                provider="biocheck",
                operation_id=opid,
                operation=name,
                arguments=arguments,
                call=callback,
                state=states.setdefault(opid, {}),
                checkpoint=lambda: None,
                provider_version=VERSION,
            )

        core = load_biocheck_http(execute, VERSION, capabilities=capabilities())
        assert (
            check_biocheck(
                None, data, "SYNTHETIC initial", host, repair=False, loaded=core, max_verify_retries=1
            )["status"]
            == "PASS"
        )
        operation = "recheck"
        with pytest.raises(RuntimeError):
            check_biocheck(
                None,
                data,
                "SYNTHETIC revised",
                host,
                repair=False,
                loaded=core,
                max_verify_retries=1,
                verify_target="biocheck_recheck",
            )
        # New callback object after restart; routing cannot depend on its previous verify count.
        core = load_biocheck_http(execute, VERSION, capabilities=capabilities())
        result = check_biocheck(
            None,
            data,
            "SYNTHETIC revised",
            host,
            repair=False,
            loaded=core,
            max_verify_retries=1,
            verify_target="biocheck_recheck",
        )
        assert result["status"] == "PASS"
        before = (len(posts), len(sends))
        again = check_biocheck(
            None,
            data,
            "SYNTHETIC revised",
            host,
            repair=False,
            loaded=core,
            max_verify_retries=1,
            verify_target="biocheck_recheck",
        )
        assert again == result and (len(posts), len(sends)) == before
    assert [stage for _, stage in sends] == [
        "biocheck_check",
        "biocheck_verify_repair",
        "biocheck_recheck",
        "biocheck_recheck_repair",
    ]
    assert len(ledger) == 4


def test_native_bounded_repairs_keep_original_materials_and_only_fix_report_contract(current_biocheck):
    settings, loaded = current_biocheck
    native = ControlledCalls()
    stages = []
    failed_plan = failed_output = plan_evidence = verify_evidence = None

    def host(stage, instruction, payload):
        nonlocal failed_plan, failed_output, plan_evidence, verify_evidence
        stages.append(stage)
        if stage == "biocheck_plan":
            plan_evidence = copy.deepcopy(payload["evidence"])
            failed_plan = native(stage, instruction, payload)
            failed_plan["points"][0]["support"][0]["segment_id"] = "S999"
            return copy.deepcopy(failed_plan)
        if stage == "biocheck_plan_repair":
            repair = payload["plan_repair"]
            assert repair["failed_plan"] == failed_plan
            assert repair["attempt"] == repair["max_attempts"] == 1
            assert 0 < len(repair["diagnostics"]) <= 8
            assert payload["evidence"] == plan_evidence
            return native("biocheck_plan", instruction, payload)
        if stage == "biocheck_check":
            verify_evidence = copy.deepcopy(payload["evidence"])
            failed_output = native(stage, instruction, payload)
            failed_output["reviews"][0]["claims"][0].pop("verdict")
            return copy.deepcopy(failed_output)
        if stage == "biocheck_verify_repair":
            repair = payload["verify_repair"]
            assert repair["failed_output"] == failed_output
            assert repair["attempt"] == repair["max_attempts"] == 1
            assert 0 < len(repair["diagnostics"]) <= 8
            assert payload["draft"] == native.draft
            assert payload["evidence"] == verify_evidence
            return native("biocheck_check", instruction, payload)
        return native(stage, instruction, payload)

    result = run_biocheck(
        settings,
        native_packet(),
        host,
        loaded=loaded,
        repair=False,
        max_plan_retries=1,
        max_verify_retries=1,
    )
    assert result["status"] == "PASS" and result["final_text"] == native.draft
    assert stages == [
        "biocheck_plan",
        "biocheck_plan_repair",
        "biocheck_write",
        "biocheck_check",
        "biocheck_verify_repair",
    ]


@pytest.mark.parametrize("phase", ["plan", "verify"])
@pytest.mark.parametrize("optin", [0, 1])
def test_native_typed_terminal_rejection_preserves_draft_and_never_revises(
    current_biocheck,
    phase,
    optin,
):
    settings, loaded = current_biocheck
    native = ControlledCalls()
    stages = []

    def host(stage, instruction, payload):
        stages.append(stage)
        if stage in {"biocheck_plan", "biocheck_plan_repair"}:
            value = native("biocheck_plan", instruction, payload)
            if phase == "plan":
                value["points"][0]["support"][0]["segment_id"] = "S999"
            return value
        if stage in {"biocheck_check", "biocheck_verify_repair"}:
            value = native("biocheck_check", instruction, payload)
            value["reviews"][0]["claims"][0].pop("verdict")
            return value
        return native(stage, instruction, payload)

    result = run_biocheck(
        settings,
        native_packet(),
        host,
        loaded=loaded,
        max_plan_retries=optin,
        max_verify_retries=optin,
    )
    expected = {
        ("plan", 0): "plan_reference_invalid",
        ("plan", 1): "plan_repair_exhausted",
        ("verify", 0): "verification_contract_invalid",
        ("verify", 1): "verification_repair_exhausted",
    }
    assert result["status"] == "failed" and result["error_code"] == expected[phase, optin]
    assert result["final_text"] == (None if phase == "plan" else native.draft)
    assert result["verification"] is None and result["revision"] is None
    assert "biocheck_revise" not in stages
    assert stages.count("biocheck_plan_repair") == (optin if phase == "plan" else 0)
    assert stages.count("biocheck_verify_repair") == (optin if phase == "verify" else 0)


@pytest.mark.parametrize(
    "code",
    [
        "analysis_model_budget",
        "analysis_token_budget",
        "analysis_time_budget",
        "analysis_tool_budget",
        "model_input_budget",
    ],
)
def test_native_optin_unsent_verify_repair_budget_retains_actual_generated_draft(current_biocheck, code):
    settings, loaded = current_biocheck
    native = ControlledCalls()
    stages = []

    def host(stage, instruction, payload):
        stages.append(stage)
        if stage == "biocheck_verify_repair":
            raise DomainError(code, "SYNTHETIC host guard: no new model or HTTP request", 422)
        value = native(stage, instruction, payload)
        if stage == "biocheck_check":
            value["reviews"][0]["claims"][0].pop("verdict")
        return value

    result = run_biocheck(settings, native_packet(), host, loaded=loaded, max_verify_retries=1)
    assert result["final_text"] == native.draft
    assert result["status"] == "failed" and result["error_code"] == code
    assert result["verification"] is None and result["revision"] is None
    assert result["verification_stop_reason"] == "not_sent_budget"
    assert stages == ["biocheck_plan", "biocheck_write", "biocheck_check", "biocheck_verify_repair"]


@pytest.mark.parametrize("request_status", ["not_sent", "unconfirmed"])
def test_native_verify_repair_model_receipt_unknown_cannot_become_budget_fallback(
    current_biocheck,
    request_status,
):
    settings, loaded = current_biocheck
    native = ControlledCalls()
    failure = model_error("analysis_time_budget", "biocheck_verify_repair", request_status)

    def host(stage, instruction, payload):
        if stage == "biocheck_verify_repair":
            raise failure
        value = native(stage, instruction, payload)
        if stage == "biocheck_check":
            value["reviews"][0]["claims"][0].pop("verdict")
        return value

    if request_status == "unconfirmed":
        with pytest.raises(DomainError) as caught:
            run_biocheck(settings, native_packet(), host, loaded=loaded, max_verify_retries=1)
        assert caught.value is failure
    else:
        result = run_biocheck(settings, native_packet(), host, loaded=loaded, max_verify_retries=1)
        assert result["status"] == "failed" and result["final_text"] == native.draft
        assert result["verification_stop_reason"] == "not_sent_budget"


def test_native_optin_recheck_repair_tool_budget_preserves_previously_checked_draft(current_biocheck):
    settings, loaded = current_biocheck
    native = ControlledCalls(contradicted=True)
    stages = []

    def host(stage, instruction, payload):
        stages.append(stage)
        if stage == "biocheck_recheck_repair":
            raise DomainError("analysis_tool_budget", "SYNTHETIC no remaining professional HTTP", 422)
        value = native(stage, instruction, payload)
        if stage == "biocheck_recheck":
            value["reviews"][0]["claims"][0].pop("verdict")
        return value

    result = run_biocheck(settings, native_packet(), host, loaded=loaded, max_verify_retries=1)
    assert result["status"] == "FAIL" and result["final_text"] == native.draft
    assert result["verification"]["draft"] == native.draft and result["revision"] is None
    assert result["repair_error_code"] == "analysis_tool_budget"
    assert result["unverified_revision"]["draft"] == native.revised
    assert stages[-2:] == ["biocheck_recheck", "biocheck_recheck_repair"]
