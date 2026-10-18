"""The real HTTP adapter with synthetic provider failures; no provider network or database."""

import copy
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import search_analysis_records
from bioradar.adapters.reasoning_model import (
    ModelCallError,
    complete_json,
    model_parameters,
    model_status,
    normalize_model_parameters,
)
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from pydantic import ValidationError
from test_graph import original_record

pytestmark = pytest.mark.ENGINE


def settings(**changes):
    values = {
        "semantic_llm_enabled": True,
        "semantic_llm_base_url": "http://model.test/v1",
        "semantic_llm_api_key": "SYNTHETIC-SERVICE-SECRET",
        "semantic_llm_model": "synthetic-model",
    }
    values.update(changes)
    return Settings(**values)


def completion(content='{"answer":"SYNTHETIC"}', **changes):
    result = {
        "id": "synthetic-provider-response",
        "model": "synthetic-model",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
    }
    result.update(changes)
    return result


def invoke(client, **changes):
    values = {
        "stage": "read_plan",
        "system_prompt": "SYNTHETIC analysis instructions.",
        "payload": {"question": "SYNTHETIC question", "evidence": [{"key": "e1", "text": "SYNTHETIC"}]},
        "response_schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        "call_id": "synthetic-call-1",
        "input_hash": "a" * 64,
        "prompt_version": "synthetic-prompt-v1",
        "client": client,
    }
    values.update(changes)
    configured = values.pop("settings", None) or settings()
    return complete_json(configured, **values)


def test_complete_json_freezes_actual_wire_hash_usage_and_model_alias():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url == "http://model.test/v1/chat/completions"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["temperature"] == 0 and body["top_p"] == 1
        assert "top_k" not in body
        assert body["max_tokens"] == 4096
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "read_plan",
                "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
                "strict": True,
            },
        }
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = invoke(client)
        assert not client.is_closed
    assert len(requests) == 1
    assert result.parsed == {"answer": "SYNTHETIC"}
    assert result.receipt.status == "completed"
    assert result.receipt.request_sha256 == hashlib.sha256(requests[0].content).hexdigest()
    assert result.receipt.usage.total_tokens == 17
    assert result.receipt.provider_response_id == "synthetic-provider-response"
    assert result.receipt.requested_model == result.receipt.provider_model == "synthetic-model"
    assert result.receipt.system_fingerprint is None and result.receipt.monetary_cost is None
    assert result.receipt.input_hash == "a" * 64
    assert result.receipt.completed_at >= result.receipt.started_at


@pytest.mark.parametrize("stage", ["revise", "recheck", "compare"])
def test_optional_stages_preserve_bounded_single_call_receipts(stage):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = invoke(client, stage=stage)
    assert len(requests) == 1
    assert result.receipt.stage == stage and result.receipt.status == "completed"
    assert result.receipt.request_sha256 == hashlib.sha256(requests[0].content).hexdigest()
    assert json.loads(requests[0].content)["max_tokens"] == 4096
    assert json.loads(requests[0].content)["response_format"]["json_schema"]["name"] == stage


def test_additional_unbounded_stage_is_rejected_before_dispatch():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValidationError):
            invoke(client, stage="retry_forever")
    assert requests == []


def test_model_status_does_not_expose_endpoint_or_credentials():
    status = model_status(settings())
    assert status.enabled and status.configured
    assert status.limits.normal_calls == 3
    assert status.limits.max_comparison_calls == 1
    assert status.limits.max_revision_calls == 2
    assert status.limits.max_calls == 6
    assert status.model_revision is None
    serialized = status.model_dump_json()
    assert "model.test" not in serialized and "SYNTHETIC-SERVICE-SECRET" not in serialized
    assert not model_status(Settings()).configured


@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [
        (400, {"unsupported_schema": "DO-NOT-LEAK"}, "model_http_error"),
        (401, {"credential": "DO-NOT-LEAK"}, "model_access_denied"),
        (403, {"credential": "DO-NOT-LEAK"}, "model_access_denied"),
        (429, {"private": "DO-NOT-LEAK"}, "model_rate_limited"),
        (500, {"private": "DO-NOT-LEAK"}, "model_http_error"),
        (307, {"private": "DO-NOT-LEAK"}, "model_http_error"),
    ],
)
def test_http_failures_are_explicit_never_retried_or_echoed(code, body, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, json=body, headers={"location": "http://untrusted.test"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelCallError) as failure:
            invoke(client)
    assert len(calls) == 1 and failure.value.code == expected
    assert failure.value.receipt.status == "failed" and failure.value.receipt.http_status == code
    assert "DO-NOT-LEAK" not in str(failure.value)
    assert "SYNTHETIC-SERVICE-SECRET" not in failure.value.receipt.model_dump_json()


def test_timeout_after_dispatch_remains_unconfirmed_without_second_post():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("SYNTHETIC-SERVICE-SECRET", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelCallError) as failure:
            invoke(client)
    assert len(calls) == 1 and failure.value.receipt.status == "unconfirmed"
    assert failure.value.code == "model_transport_unconfirmed"
    assert "SYNTHETIC-SERVICE-SECRET" not in str(failure.value)


def test_not_configured_and_input_budget_fail_before_any_network():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelCallError) as failure:
            invoke(client, settings=Settings())
        assert failure.value.code == "model_not_configured" and failure.value.receipt.status == "not_sent"
        with pytest.raises(ModelCallError) as failure:
            invoke(
                client, payload={"private": "X" * 2000}, settings=settings(semantic_llm_max_input_bytes=1024)
            )
        assert failure.value.code == "model_input_budget" and failure.value.receipt.status == "not_sent"
    assert calls == []


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', "[]", '{"x":NaN}', '{"x":1e9999}', "not JSON"])
def test_inner_output_must_be_one_finite_unambiguous_json_object(content):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(content)))
    ) as client:
        with pytest.raises(ModelCallError) as failure:
            invoke(client)
    assert failure.value.code == "model_output_invalid"
    assert failure.value.receipt.provider_response_id == "synthetic-provider-response"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"model": "unexpected-model"}, "model_identity_mismatch"),
        ({"model": ["bad"]}, "model_protocol_error"),
        ({"choices": ["not-a-choice"]}, "model_protocol_error"),
        ({"choices": [{"message": "not-a-message"}]}, "model_protocol_error"),
        ({"usage": {"prompt_tokens": True}}, "model_protocol_error"),
        ({"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 4}}, "model_protocol_error"),
        ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, "model_output_truncated"),
        (
            {"choices": [{"finish_reason": "stop", "message": {"refusal": "SYNTHETIC", "content": "{}"}}]},
            "model_refused",
        ),
        (
            {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{}]}}]},
            "model_output_incomplete",
        ),
        (
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5000, "total_tokens": 5010}},
            "model_output_budget",
        ),
    ],
)
def test_provider_shape_refusal_truncation_and_budget_are_not_empty_success(change, expected):
    value = completion(**change)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))) as client:
        with pytest.raises(ModelCallError) as failure:
            invoke(client)
    assert failure.value.code == expected and failure.value.receipt.status == "failed"


def test_response_bytes_are_bounded_and_missing_usage_is_unknown(monkeypatch):
    import bioradar.adapters.reasoning_model as module

    value = completion()
    value.pop("usage")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))) as client:
        result = invoke(client)
    assert result.receipt.usage.prompt_tokens is None and result.receipt.usage.total_tokens is None
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 128)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as client:
        with pytest.raises(ModelCallError) as failure:
            invoke(client)
    assert failure.value.code == "model_response_budget"


def test_analysis_search_preserves_literal_terms_exact_versions_and_partial_scope():
    record = original_record("synthetic-doc", {"title": "SYNTHETIC evidence", "body": "SYNTHETIC"})
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/history/records"
        assert request.url.params.get_list("terms") == ["100% response", "SYNTHETIC"]
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.history.collection.v1",
                "items": [record],
                "coverage": {"complete": True, "max_seq": 1},
                "next_cursor": "synthetic-next",
            },
        )

    with httpx.Client(base_url="http://fabric.test", transport=httpx.MockTransport(handler)) as client:
        reader = FabricHistoryReader(client, authorization_ref="synthetic-grant")
        page = search_analysis_records(
            reader,
            source_id="fixture-source",
            is_demo=True,
            terms=["SYNTHETIC", "100% response"],
            cutoff=datetime(2026, 10, 18, tzinfo=UTC),
        )
    assert len(requests) == 1 and page["items"][0]["version_id"] == record["version_id"]
    assert page["coverage"]["complete"] is False and page["coverage"]["has_more"] is True
    assert page["coverage"]["search_scope"].endswith("not_the_web")


def test_analysis_search_rejects_wrong_source_or_mutated_original():
    record = original_record("synthetic-doc", {"body": "SYNTHETIC"})
    for mutate in ("source", "raw"):
        bad = copy.deepcopy(record)
        if mutate == "source":
            bad["source_id"] = "not-authorized-source"
        else:
            bad["raw_content"] += "changed"
        with httpx.Client(
            base_url="http://fabric.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "schema_version": "datafabric.history.collection.v1",
                        "items": [bad],
                        "coverage": {},
                        "next_cursor": None,
                    },
                )
            ),
        ) as client:
            reader = FabricHistoryReader(client, authorization_ref="synthetic-grant")
            with pytest.raises(DomainError):
                search_analysis_records(
                    reader,
                    source_id="fixture-source",
                    is_demo=True,
                    terms=["SYNTHETIC"],
                    cutoff=datetime(2026, 10, 18, tzinfo=UTC),
                )


def test_analysis_search_rejects_unbounded_or_naive_queries_before_network():
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        reader = FabricHistoryReader(client, authorization_ref="synthetic-grant")
        with pytest.raises(DomainError):
            search_analysis_records(
                reader, source_id="fixture-source", is_demo=True, terms=["x"] * 4, cutoff=datetime.now()
            )
    assert calls == []


def test_configured_sampling_is_frozen_on_wire_without_retaining_reasoning():
    configured = settings(
        semantic_llm_enable_thinking=True,
        semantic_llm_temperature=0.6,
        semantic_llm_top_p=0.95,
        semantic_llm_top_k=20,
        semantic_llm_max_output_tokens=12_288,
        semantic_llm_timeout_seconds=150,
    )
    frozen = model_parameters(configured)
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == frozen["model"]
        assert body["temperature"] == frozen["temperature"] == 0.6
        assert body["top_p"] == frozen["top_p"] == 0.95
        assert body["top_k"] == frozen["top_k"] == 20
        assert body["chat_template_kwargs"] == frozen["static_options"] == {"enable_thinking": True}
        assert body["max_tokens"] == frozen["max_output_tokens"] == 12_288
        assert request.extensions["timeout"]["read"] == frozen["timeout_seconds"] == 150
        response = completion()
        response["choices"][0]["message"]["reasoning_content"] = "SYNTHETIC-HIDDEN-THINKING"
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = invoke(client, settings=configured)
    assert len(requests) == 1
    assert result.receipt.request_sha256 == hashlib.sha256(requests[0].content).hexdigest()
    assert result.receipt.max_output_tokens == 12_288
    assert "SYNTHETIC-HIDDEN-THINKING" not in result.model_dump_json()
    assert "SYNTHETIC-SERVICE-SECRET" not in json.dumps(frozen)
    assert "model.test" not in json.dumps(frozen)


def test_legacy_parameter_defaults_cannot_inherit_current_thinking_configuration():
    original = model_parameters(settings())
    legacy = {
        key: value
        for key, value in original.items()
        if key not in {"temperature", "top_p", "top_k", "static_options", "output_format"}
    }
    legacy.update(max_calls=6, job_timeout_seconds=330)
    normalized = normalize_model_parameters(legacy)
    assert json.dumps(normalized, sort_keys=True) == json.dumps(
        {**original, "output_format": "json_object"}, sort_keys=True
    )
    assert normalized != original
    current = model_parameters(
        settings(
            semantic_llm_enable_thinking=True,
            semantic_llm_temperature=0.6,
            semantic_llm_top_p=0.95,
            semantic_llm_top_k=20,
        )
    )
    assert normalized != current
    assert "max_calls" not in normalized and "job_timeout_seconds" not in normalized
    assert normalize_model_parameters({})["timeout_seconds"] is None
    assert normalize_model_parameters({})["endpoint_sha256"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"semantic_llm_temperature": -0.1},
        {"semantic_llm_temperature": float("nan")},
        {"semantic_llm_temperature": float("inf")},
        {"semantic_llm_top_p": 0},
        {"semantic_llm_top_p": 1.1},
        {"semantic_llm_top_k": 0},
        {"semantic_llm_top_k": 1001},
        {"semantic_llm_max_output_tokens": 32_769},
        {"semantic_llm_timeout_seconds": 181},
    ],
)
def test_model_sampling_and_increased_budgets_are_still_bounded(changes):
    with pytest.raises(ValidationError):
        settings(**changes)


def test_json_schema_preserves_required_optional_and_defaults_without_fallback():
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "optional_count": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
        },
        "required": ["answer"],
        "additionalProperties": False,
    }
    original = copy.deepcopy(schema)
    requests = []

    def handler(request):
        requests.append(request)
        assert json.loads(request.content)["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "read_plan", "schema": original, "strict": True},
        }
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = invoke(client, response_schema=schema)
    assert schema == original
    assert len(requests) == 1
    assert result.receipt.request_sha256 == hashlib.sha256(requests[0].content).hexdigest()
    assert model_parameters(settings())["output_format"] == "json_schema"
    assert normalize_model_parameters({})["output_format"] == "json_object"


def test_existing_vllm_core_shape_is_identical_without_provider_or_effort_keys():
    expected = {
        "model": "synthetic-model",
        "model_revision": None,
        "max_output_tokens": 4096,
        "max_input_bytes": 65_536,
        "timeout_seconds": 45,
        "endpoint_sha256": hashlib.sha256(json.dumps("http://model.test/v1").encode()).hexdigest(),
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": None,
        "static_options": {"enable_thinking": False},
        "output_format": "json_schema",
    }
    assert model_parameters(settings()) == expected
    assert model_parameters(settings(semantic_llm_reasoning_effort="max")) == expected
    assert normalize_model_parameters(expected) == expected
    assert normalize_model_parameters({**expected, "provider": "vllm", "reasoning_effort": "max"}) == expected


@pytest.mark.parametrize("effort", ["high", "max"])
@pytest.mark.parametrize("output_tokens,timeout_seconds", [(12_288, 150), (32_768, 180)])
def test_deepseek_thinking_uses_explicit_json_profile_and_only_effective_parameters(
    effort, output_tokens, timeout_seconds
):
    configured = settings(
        semantic_llm_provider="deepseek",
        semantic_llm_model="deepseek-flash",
        semantic_llm_enable_thinking=True,
        semantic_llm_reasoning_effort=effort,
        semantic_llm_temperature=0.6,
        semantic_llm_top_p=0.95,
        semantic_llm_top_k=20,
        semantic_llm_max_output_tokens=output_tokens,
        semantic_llm_timeout_seconds=timeout_seconds,
    )
    frozen = model_parameters(configured)
    assert frozen["provider"] == "deepseek" and frozen["reasoning_effort"] == effort
    assert frozen["temperature"] is None and frozen["top_k"] is None
    assert frozen["output_format"] == "json_object"
    assert normalize_model_parameters(frozen) == frozen
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert set(body) == {
            "model",
            "max_tokens",
            "stream",
            "response_format",
            "messages",
            "thinking",
            "reasoning_effort",
            "top_p",
        }
        assert body["model"] == "deepseek-flash" and body["max_tokens"] == output_tokens
        assert request.extensions["timeout"]["read"] == timeout_seconds
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == effort and body["top_p"] == 0.95
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0]["content"].startswith("SYNTHETIC analysis instructions.")
        assert "JSON Schema" in body["messages"][0]["content"]
        response = completion(model="deepseek-flash")
        response["choices"][0]["message"]["reasoning_content"] = "DO-NOT-RETAIN-PRIVATE-REASONING"
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = invoke(client, settings=configured)
    assert len(requests) == 1
    assert result.receipt.request_sha256 == hashlib.sha256(requests[0].content).hexdigest()
    assert result.receipt.provider_model == "deepseek-flash"
    assert result.receipt.max_output_tokens == frozen["max_output_tokens"] == output_tokens
    assert "DO-NOT-RETAIN-PRIVATE-REASONING" not in result.model_dump_json()


def test_deepseek_disabled_thinking_cannot_be_reenabled_by_effort_or_ignored_top_p():
    configured = settings(
        semantic_llm_provider="deepseek",
        semantic_llm_model="deepseek-flash",
        semantic_llm_enable_thinking=False,
        semantic_llm_reasoning_effort="max",
        semantic_llm_temperature=0.4,
        semantic_llm_top_p=0.5,
    )
    frozen = model_parameters(configured)
    assert frozen["reasoning_effort"] is None and frozen["top_p"] is None
    assert frozen["temperature"] == 0.4

    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["temperature"] == 0.4
        assert all(key not in body for key in ("reasoning_effort", "top_p", "top_k", "chat_template_kwargs"))
        return httpx.Response(200, json=completion(model="deepseek-flash"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert invoke(client, settings=configured).receipt.status == "completed"


def test_unknown_frozen_provider_is_preserved_to_prevent_a_vllm_match():
    legacy = model_parameters(settings())
    unknown = normalize_model_parameters({**legacy, "provider": "unknown-service"})
    assert unknown["provider"] == "unknown-service"
    assert unknown != legacy
    deepseek = model_parameters(settings(semantic_llm_provider="deepseek", semantic_llm_enable_thinking=True))
    missing_effort = {key: value for key, value in deepseek.items() if key != "reasoning_effort"}
    assert normalize_model_parameters(missing_effort)["reasoning_effort"] is None
    assert normalize_model_parameters(missing_effort) != deepseek


@pytest.mark.parametrize(
    "changes",
    [
        {"semantic_llm_provider": "auto-detect"},
        {"semantic_llm_reasoning_effort": "ultra"},
        {
            "semantic_llm_provider": "deepseek",
            "semantic_llm_model": "deepseek-flash",
            "semantic_llm_enable_thinking": True,
            "semantic_llm_top_p": 0.5,
        },
    ],
)
def test_provider_and_flash_sampling_configuration_reject_ambiguous_values(changes):
    with pytest.raises(ValidationError):
        settings(**changes)
