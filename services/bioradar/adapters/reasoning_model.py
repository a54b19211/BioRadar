"""One bounded JSON call to the configured internal model; no tools or automatic retries."""

import hashlib
import json
import math
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from bioradar.api.config import Settings
from bioradar.api.errors import DomainError

ModelStage = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
MAX_RESPONSE_BYTES = 524_288


class ModelLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    normal_calls: Literal[3] = 3
    max_comparison_calls: Literal[1] = 1
    max_revision_calls: Literal[2] = 2
    max_calls: int = 6
    max_total_tokens: int = 262144
    max_seconds: int = 1800
    max_input_bytes: int
    max_output_tokens: int
    timeout_seconds: int
    cost_unit: Literal["tokens_only_price_unknown"] = "tokens_only_price_unknown"


class ModelStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    configured: bool
    model: str | None
    model_revision: str | None
    limits: ModelLimits
    qualification: str = "配置不证明调用成功；模型别名不保证权重固定，金额费率未知。"


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ModelCallReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: str
    stage: ModelStage
    input_hash: str
    prompt_version: str
    requested_model: str | None
    configured_model_revision: str | None
    provider_model: str | None = None
    provider_response_id: str | None = None
    system_fingerprint: str | None = None
    status: Literal["not_sent", "completed", "failed", "unconfirmed"] = "not_sent"
    error_code: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    elapsed_ms: int = 0
    request_sha256: str | None = None
    request_bytes: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    response_sha256: str | None = None
    http_status: int | None = None
    finish_reason: str | None = None
    max_output_tokens: int
    usage: ModelUsage = ModelUsage()
    monetary_cost: None = None
    cost_qualification: str = "没有已核费率；仅报告实际token，未知不补零。"
    weights_qualification: str = "provider fingerprint缺失时不能声称已固定模型权重。"


class ModelCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    parsed: dict[str, Any]
    receipt: ModelCallReceipt


class ModelCallError(DomainError):
    def __init__(self, code: str, message: str, receipt: ModelCallReceipt, status_code=503):
        super().__init__(code, message, status_code)
        self.receipt = receipt


def model_parameters(settings: Settings) -> dict[str, Any]:
    """The non-secret configuration used both on the wire and in frozen analysis inputs."""
    parameters = {
        "model": settings.semantic_llm_model,
        "model_revision": settings.semantic_llm_model_revision,
        "max_output_tokens": settings.semantic_llm_max_output_tokens,
        "max_input_bytes": settings.semantic_llm_max_input_bytes,
        "timeout_seconds": settings.semantic_llm_timeout_seconds,
        "endpoint_sha256": hashlib.sha256(
            json.dumps(settings.semantic_llm_base_url, ensure_ascii=False).encode()
        ).hexdigest(),
        "temperature": settings.semantic_llm_temperature,
        "top_p": settings.semantic_llm_top_p,
        "top_k": settings.semantic_llm_top_k,
        "static_options": {"enable_thinking": settings.semantic_llm_enable_thinking},
        "output_format": "json_schema",
    }

    if settings.semantic_llm_provider == "deepseek":
        thinking = settings.semantic_llm_enable_thinking
        parameters.update(
            provider="deepseek",
            reasoning_effort=settings.semantic_llm_reasoning_effort if thinking else None,
            temperature=None if thinking else settings.semantic_llm_temperature,
            top_p=settings.semantic_llm_top_p if thinking else None,
            top_k=None,
            output_format="json_object",
        )
    return parameters


def normalize_model_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Fill only historical sampling defaults; missing transport details remain unknown."""
    core = {
        key: parameters.get(key)
        for key in (
            "model",
            "model_revision",
            "max_output_tokens",
            "max_input_bytes",
            "timeout_seconds",
            "endpoint_sha256",
        )
    }
    core.update(
        temperature=parameters.get("temperature", 0.0),
        top_p=parameters.get("top_p", 1.0),
        top_k=parameters.get("top_k"),
        output_format=parameters.get("output_format", "json_object"),
        static_options={
            "enable_thinking": parameters.get("static_options", {}).get("enable_thinking", False)
        },
    )
    provider = parameters.get("provider", "vllm")
    # Preserve the original vllm core shape and hashes; missing provider means that old profile.
    if provider != "vllm":
        core.update(provider=provider, reasoning_effort=parameters.get("reasoning_effort"))
    return core


def model_status(settings: Settings) -> ModelStatus:
    parameters = model_parameters(settings)
    return ModelStatus(
        enabled=settings.semantic_llm_enabled,
        configured=bool(
            settings.semantic_llm_base_url and settings.semantic_llm_api_key and settings.semantic_llm_model
        ),
        model=parameters["model"],
        model_revision=parameters["model_revision"],
        limits=ModelLimits(
            max_calls=settings.analysis_max_calls,
            max_total_tokens=settings.analysis_max_tokens,
            max_seconds=settings.analysis_max_seconds,
            max_input_bytes=parameters["max_input_bytes"],
            max_output_tokens=parameters["max_output_tokens"],
            timeout_seconds=parameters["timeout_seconds"],
        ),
    )


def _json_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def _parse_json(value):
    def reject_constant(_):
        raise ValueError("nonfinite JSON constant")

    return json.loads(
        value, object_pairs_hook=_json_pairs, parse_float=_finite_float, parse_constant=reject_constant
    )


def _usage(value) -> ModelUsage:
    if not isinstance(value, dict):
        return ModelUsage()
    counts = {key: value.get(key) for key in ModelUsage.model_fields}
    if any(v is not None and (type(v) is not int or v < 0) for v in counts.values()):
        raise ValueError("invalid token usage")
    if all(v is not None for v in counts.values()) and (
        counts["prompt_tokens"] + counts["completion_tokens"] != counts["total_tokens"]
    ):
        raise ValueError("inconsistent token usage")
    return ModelUsage(**counts)


def encode_request(settings: Settings, *, stage, system_prompt, payload, response_schema) -> bytes:
    """Encode the same wire request for sending, byte limits and conservative reservations."""
    parameters = model_parameters(settings)
    body = {
        "model": parameters["model"],
        "stream": False,
        "max_tokens": parameters["max_output_tokens"],
        "messages": [
            {
                "role": "system",
                "content": system_prompt
                + "\\n只返回符合以下JSON Schema的一个对象，不返回隐藏思维链。\\n"
                + json.dumps(response_schema, ensure_ascii=False, sort_keys=True, allow_nan=False),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
            },
        ],
    }
    if parameters.get("provider", "vllm") == "deepseek":
        body["thinking"] = {
            "type": "enabled" if parameters["static_options"]["enable_thinking"] else "disabled"
        }
        body["response_format"] = {"type": parameters["output_format"]}
        if parameters["reasoning_effort"] is not None:
            body["reasoning_effort"] = parameters["reasoning_effort"]
    else:
        body["chat_template_kwargs"] = parameters["static_options"]
        body["response_format"] = {
            "type": parameters["output_format"],
            "json_schema": {"name": stage, "schema": response_schema, "strict": True},
        }
    for key in ("temperature", "top_p", "top_k"):
        if parameters[key] is not None:
            body[key] = parameters[key]
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def complete_json(
    settings: Settings,
    *,
    stage: ModelStage,
    system_prompt: str,
    payload: dict,
    response_schema: dict,
    call_id: str,
    input_hash: str,
    prompt_version: str,
    client: httpx.Client | None = None,
) -> ModelCompletion:
    """Caller first persists its call intent; this function never reissues a POST."""
    began = time.monotonic()
    parameters = model_parameters(settings)
    receipt = ModelCallReceipt(
        call_id=call_id,
        stage=stage,
        input_hash=input_hash,
        prompt_version=prompt_version,
        requested_model=parameters["model"],
        configured_model_revision=parameters["model_revision"],
        started_at=datetime.now(UTC),
        max_output_tokens=parameters["max_output_tokens"],
    )

    def fail(code, message, *, state="failed", status=503):
        receipt.status = state
        receipt.error_code = code
        receipt.completed_at = datetime.now(UTC)
        receipt.elapsed_ms = max(0, round((time.monotonic() - began) * 1000))
        raise ModelCallError(code, message, receipt, status)

    configured = model_status(settings)
    if not configured.enabled or not configured.configured:
        fail("model_not_configured", "语义模型未启用或配置不完整", state="not_sent")
    try:
        encoded = encode_request(
            settings,
            stage=stage,
            system_prompt=system_prompt,
            payload=payload,
            response_schema=response_schema,
        )
    except (ValueError, TypeError, RecursionError):
        fail("model_input_invalid", "模型输入不是受支持的有限JSON对象", state="not_sent", status=422)
    receipt.request_bytes = len(encoded)
    if len(encoded) > parameters["max_input_bytes"]:
        fail("model_input_budget", "模型输入超过字节预算，请缩小采用范围", state="not_sent", status=422)
    receipt.request_sha256 = hashlib.sha256(encoded).hexdigest()
    owned = client is None
    session = client or httpx.Client(trust_env=False, follow_redirects=False)
    timeout = httpx.Timeout(parameters["timeout_seconds"], connect=min(5, parameters["timeout_seconds"]))
    try:
        try:
            # Full URL comes exclusively from validated server configuration, never a model/tool result.
            with session.stream(
                "POST",
                settings.semantic_llm_base_url + "/chat/completions",
                content=encoded,
                headers={
                    "Authorization": "Bearer " + settings.semantic_llm_api_key.get_secret_value(),
                    "Content-Type": "application/json",
                },
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                receipt.http_status = response.status_code
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        fail("model_response_budget", "模型响应超过读取预算，未采用结果")
                    if time.monotonic() - began > parameters["timeout_seconds"]:
                        fail(
                            "model_timeout_unconfirmed",
                            "模型请求超过期限，是否已计费未知，不自动重发",
                            state="unconfirmed",
                        )
                receipt.response_sha256 = hashlib.sha256(raw).hexdigest()
        except httpx.HTTPError:
            fail("model_transport_unconfirmed", "模型请求结果不明，不自动重发", state="unconfirmed")
        if receipt.http_status == 429:
            fail("model_rate_limited", "模型服务限流，本次未采用结果", status=429)
        if receipt.http_status in {401, 403}:
            fail("model_access_denied", "模型服务拒绝访问，请检查服务端配置", status=503)
        if receipt.http_status != 200:
            fail("model_http_error", "模型服务返回失败，本次未采用结果")
        try:
            response_body = _parse_json(bytes(raw))
            if not isinstance(response_body, dict):
                raise ValueError("invalid response")
            model = response_body.get("model")
            response_id = response_body.get("id")
            fingerprint = response_body.get("system_fingerprint")
            if (
                not isinstance(model, str)
                or not 1 <= len(model) <= 200
                or response_id is not None
                and (not isinstance(response_id, str) or len(response_id) > 256)
                or fingerprint is not None
                and (not isinstance(fingerprint, str) or len(fingerprint) > 256)
            ):
                raise ValueError("invalid provider identifiers")
            choices = response_body["choices"]
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ValueError("invalid choice")
            choice = choices[0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            receipt.provider_model = model
            receipt.provider_response_id = response_id
            receipt.system_fingerprint = fingerprint
            receipt.usage = _usage(response_body.get("usage"))
            receipt.finish_reason = choice.get("finish_reason")
            content = message.get("content")
        except (KeyError, IndexError, TypeError, ValueError, RecursionError):
            fail("model_protocol_error", "模型响应格式或用量字段无效")
        if receipt.provider_model != parameters["model"]:
            fail("model_identity_mismatch", "模型服务返回的模型标识与配置不一致")
        if message.get("refusal"):
            fail("model_refused", "模型拒绝本次分析，未生成研判")
        if receipt.finish_reason == "length":
            fail("model_output_truncated", "模型输出达到token上限，截断结果不作为成功")
        if receipt.finish_reason != "stop" or message.get("tool_calls"):
            fail("model_output_incomplete", "模型未按固定JSON任务完成，未执行额外工具")
        if (
            receipt.usage.completion_tokens is not None
            and receipt.usage.completion_tokens > receipt.max_output_tokens
        ):
            fail("model_output_budget", "模型返回用量超过请求预算，未采用结果")
        try:
            parsed = _parse_json(content)
            if not isinstance(parsed, dict):
                raise ValueError("expected object")
        except (ValueError, TypeError, RecursionError):
            fail("model_output_invalid", "模型没有返回合法JSON对象，未生成研判")
        receipt.status = "completed"
        receipt.completed_at = datetime.now(UTC)
        receipt.elapsed_ms = max(0, round((time.monotonic() - began) * 1000))
        return ModelCompletion(content=content, parsed=parsed, receipt=receipt)
    finally:
        if owned:
            session.close()
