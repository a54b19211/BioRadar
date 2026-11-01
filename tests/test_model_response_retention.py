"""Captured provider content and one Mech technical retry; every provider response is synthetic."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bioradar.adapters.reasoning_model import ModelCallError, complete_json, decode_received_content
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import analysis_budget
from test_optional_policy_reading import harness
from test_reasoning_model import completion, invoke

PRIVATE = "PRIVATE_ASSISTANT_RESPONSE_SENTINEL"
REASONING = "PRIVATE_PROVIDER_REASONING_SENTINEL"


@pytest.mark.parametrize(
    ("content", "category"),
    [
        ('{"secret":"' + PRIVATE + '",}', "json_syntax"),
        ("[]", "non_object"),
        ('{"x":1,"x":2}', "duplicate_member"),
        ('{"x":NaN}', "nonfinite_number"),
        ('{"x":"\x00"}', "json_syntax"),
        (None, "wrong_type"),
    ],
)
def test_bad_content_is_saved_exactly_with_safe_diagnostics_and_known_usage(content, category):
    saved, requests = [], []
    body = completion(content)
    body["choices"][0]["message"]["reasoning_content"] = REASONING

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=body, headers={"x-private": "PRIVATE_HEADER"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelCallError) as failure:
            invoke(client, on_response=lambda value: saved.append(copy.deepcopy(value)))
    assert len(requests) == len(saved) == 1
    assert json.loads(saved[0]["content_json"]) == content
    assert saved[0]["receipt"]["usage"]["total_tokens"] == 17
    assert saved[0]["receipt"]["finish_reason"] == "stop"
    assert REASONING not in json.dumps(saved) and "PRIVATE_HEADER" not in json.dumps(saved)
    receipt = failure.value.receipt
    assert receipt.status == "failed" and receipt.usage.total_tokens == 17
    assert receipt.output_diagnostic.category == category
    public = receipt.model_dump_json()
    assert PRIVATE not in public and REASONING not in public
    if category == "json_syntax":
        assert receipt.output_diagnostic.position is not None
        assert receipt.output_diagnostic.line >= 1 and receipt.output_diagnostic.column >= 1
    with pytest.raises(ModelCallError) as replay:
        decode_received_content(saved[0])
    assert replay.value.receipt.model_dump(mode="json") == receipt.model_dump(mode="json")
    assert len(requests) == 1


def test_capture_finishes_before_parsing_and_success_can_replay_without_transport():
    saved = []

    class Interrupted(BaseException):
        pass

    def capture(value):
        saved.append(copy.deepcopy(value))
        raise Interrupted

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as client:
        with pytest.raises(Interrupted):
            invoke(client, on_response=capture)
    replay = decode_received_content(saved[0])
    assert replay.parsed == {"answer": "SYNTHETIC"}
    assert replay.receipt.usage.total_tokens == 17
    broken = copy.deepcopy(saved[0])
    broken["content_json"] += " "
    with pytest.raises(DomainError) as failure:
        decode_received_content(broken)
    assert failure.value.code == "model_saved_response_invalid"


def host(monkeypatch, contents, *, finish_reason="stop", after_response=None, timeout=False):
    service, fixed, progress = harness(monkeypatch)
    requests = []

    def respond(request):
        requests.append(request)
        if timeout:
            raise httpx.ReadTimeout("SYNTHETIC unconfirmed", request=request)
        content = contents[min(len(requests) - 1, len(contents) - 1)]
        if after_response:
            after_response(fixed, progress)
        body = completion(content, model=service.settings.semantic_llm_model)
        body["choices"][0]["finish_reason"] = finish_reason
        body["choices"][0]["message"]["reasoning_content"] = REASONING
        return httpx.Response(200, json=body)

    def model(settings, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            return complete_json(settings, client=client, **kwargs)

    service.model_call = model
    callback = service._shared_callback("SYNTHETIC-analysis", "token", fixed, progress)
    return service, fixed, progress, requests, callback


def run(callback, stage="mech_mechanism_review", *, key="SYNTHETIC-method-step", payload=None):
    return callback(
        stage,
        "SYNTHETIC fixed instruction",
        payload or {"task": "SYNTHETIC fixed scientific request"},
        {"type": "object"},
        call_id=key,
    )


def test_mech_retry_is_a_separate_identical_wire_with_both_costs_and_received_first(monkeypatch):
    service, fixed, progress, requests, callback = host(monkeypatch, ['{"bad":', '{"result":"ok"}'])
    assert run(callback) == {"result": "ok"}
    assert len(requests) == 2 and requests[0].content == requests[1].content
    original, retry = progress["calls"]
    assert original["status"] == "failed" and retry["status"] == "received"
    assert original["call_id"] != retry["call_id"] and retry["retry_of_call_id"] == original["call_id"]
    assert original["model_parameters"] == retry["model_parameters"]
    assert retry["receipt"]["request_sha256"] == original["receipt"]["request_sha256"]
    assert hashlib.sha256(requests[1].content).hexdigest() == original["receipt"]["request_sha256"]
    assert json.loads(original["private_response"]["content_json"]) == '{"bad":'
    usage = analysis_budget(fixed, progress, datetime.now(UTC))["usage"]
    assert usage["known_total_tokens"] == usage["total_tokens"] == 34
    assert usage["calls"] == 2 and usage["unknown_usage_calls"] == 0
    assert REASONING not in json.dumps(progress)
    progress["started_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fixed["budget"]["max_tokens"] = 1
    assert run(callback) == {"result": "ok"}
    assert len(requests) == 2
    with pytest.raises(DomainError) as changed:
        run(callback, payload={"task": "SYNTHETIC changed request"})
    assert changed.value.code == "analysis_call_input_changed" and len(requests) == 2


def test_failed_retry_is_never_retried_and_other_mech_stages_share_one_slot(monkeypatch):
    _, _, progress, requests, callback = host(monkeypatch, ["{"])
    for _ in range(2):
        with pytest.raises(ModelCallError):
            run(callback)
    assert len(requests) == 2 and len(progress["calls"]) == 2
    with pytest.raises(ModelCallError):
        run(callback, "mech_local_review", key="SYNTHETIC-other-stage")
    assert len(requests) == 3 and len(progress["calls"]) == 3
    assert sum("retry_of_call_id" in c for c in progress["calls"]) == 1


@pytest.mark.parametrize("stage", ["biocheck_write", "biocheck_verify_claim", "biocheck_verify_paragraph"])
def test_pan_stages_never_enter_host_format_retry(monkeypatch, stage):
    _, _, progress, requests, callback = host(monkeypatch, ["{", "{}"])
    with pytest.raises(ModelCallError):
        run(callback, stage)
    assert len(requests) == 1 and "mech_format_retry" not in progress


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{"x":NaN}', None])
def test_ambiguous_or_wrong_protocol_content_is_not_eligible(monkeypatch, content):
    _, _, progress, requests, callback = host(monkeypatch, [content, "{}"])
    with pytest.raises(ModelCallError):
        run(callback)
    assert len(requests) == 1 and "mech_format_retry" not in progress


def test_length_unknown_and_valid_json_scientific_shape_do_not_trigger_format_retry(monkeypatch):
    for options in [{"finish_reason": "length"}, {"timeout": True}]:
        _, _, progress, requests, callback = host(monkeypatch, ["{}"], **options)
        with pytest.raises(ModelCallError):
            run(callback)
        assert len(requests) == 1 and "mech_format_retry" not in progress
    _, _, progress, requests, callback = host(monkeypatch, ['{"scientific_schema_will_reject":true}'])
    assert run(callback) == {"scientific_schema_will_reject": True}
    assert len(requests) == 1 and "mech_format_retry" not in progress


def test_retry_never_reduces_original_output_cap_to_fit_leftover_budget(monkeypatch):
    def consume(fixed, progress):
        fixed["budget"]["max_tokens"] = 500

    _, fixed, progress, requests, callback = host(monkeypatch, ["{", "{}"], after_response=consume)
    with pytest.raises(DomainError) as failure:
        run(callback)
    assert failure.value.code == "analysis_token_budget"
    assert len(requests) == 1 and len(progress["calls"]) == 1
    assert progress["mech_format_retry"]["status"] == "not_sent"
    from bioradar.delivery.analysis_diagnostics import build_analysis_diagnostics

    diagnostic = build_analysis_diagnostics({"id": "SYNTHETIC"}, progress=progress)["mech_format_retry"]
    assert diagnostic["status"] == "not_sent" and diagnostic["error_code"] == "analysis_token_budget"
    fixed["budget"]["max_tokens"] = 524288
    with pytest.raises(DomainError):
        run(callback)
    assert len(requests) == 1


def test_old_failed_without_saved_content_cannot_enter_the_new_policy(monkeypatch):
    service, fixed, progress, requests, callback = host(monkeypatch, ["{", "{}"])
    with pytest.raises(ModelCallError):
        service._call(
            "SYNTHETIC-analysis",
            "token",
            "mech_mechanism_review",
            {"type": "object"},
            fixed,
            progress,
            {"task": "SYNTHETIC fixed scientific request"},
            system_prompt="SYNTHETIC fixed instruction",
            call_key="SYNTHETIC-method-step",
        )
    progress["calls"][0].pop("private_response")
    with pytest.raises(ModelCallError):
        run(callback)
    assert len(requests) == 1 and "mech_format_retry" not in progress


def test_retry_preserves_parameters_while_transport_deadline_shrinks(monkeypatch):
    def elapsed(fixed, progress):
        progress["started_at"] = (
            datetime.now(UTC) - timedelta(seconds=fixed["budget"]["max_seconds"] - 120)
        ).isoformat()

    _, _, progress, requests, callback = host(monkeypatch, ["{", "{}"], after_response=elapsed)
    assert run(callback) == {}
    original, retry = progress["calls"]
    assert requests[0].content == requests[1].content
    assert original["model_parameters"] == retry["model_parameters"]
    assert retry["transport_timeout_seconds"] < original["model_parameters"]["timeout_seconds"]
    assert requests[1].extensions["timeout"]["read"] == retry["transport_timeout_seconds"]
