"""Pure professional HTTP replay; model authorization and receipts remain in the host ledger."""

import copy
import hashlib
import json
import re

import httpx

from bioradar.api.errors import DomainError

SCHEMA_VERSION = "bioradar.professional.v1"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _canonical(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError):
        raise DomainError("professional_rpc_input_invalid", "专业服务输入不是有效JSON对象", 422) from None


def _failure(code, message="专业服务回执未确认，已保留本轮输入与模型回执", status=502):
    return DomainError(code, message, status)


def _response_json(response):
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise _failure("professional_rpc_response_limit", "专业服务响应超过读取上限")
    try:
        value = response.json()
        _canonical(value)
    except (ValueError, TypeError):
        raise _failure("professional_rpc_protocol_invalid", "专业服务响应不是有效JSON") from None
    if not isinstance(value, dict):
        raise _failure("professional_rpc_protocol_invalid", "专业服务响应不是JSON对象")
    return value


def get_capabilities(client, *, provider, guard=None):
    """Read actual service identity without importing its project or contacting a model."""
    if guard:
        guard()
    try:
        response = client.get("/v1/capabilities")
    except httpx.TransportError:
        raise _failure("professional_rpc_unavailable", "专业服务暂时无法连接", 503) from None
    if response.status_code != 200:
        raise _failure("professional_rpc_unavailable", "专业服务未返回能力信息", 503)
    value = _response_json(response)
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("provider") != provider
        or not isinstance(value.get("provider_version"), str)
        or not value["provider_version"]
        or not isinstance(value.get("operations"), list)
        or any(not isinstance(item, str) or not item for item in value["operations"])
    ):
        raise _failure("professional_rpc_protocol_invalid", "专业服务身份或能力信息不符合约定")
    return copy.deepcopy(value)


def _service_error_code(value, default):
    code = value.get("error_code")
    if (
        isinstance(code, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,119}", code)
        and not code.startswith(("analysis_", "model_", "history_"))
    ):
        return code
    return default


def _checked_response(value, *, provider, operation_id, version, request):
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("operation_id") != operation_id
        or value.get("provider") != provider
        or not isinstance(value.get("provider_version"), str)
        or not value["provider_version"]
    ):
        raise _failure("professional_rpc_protocol_invalid", "专业服务回执身份不符合本轮输入")
    if version is not None and value["provider_version"] != version:
        raise _failure("analysis_provider_changed", "专业方法版本已改变，旧回执保留，请另起尝试", 409)
    status = value.get("status")
    if status == "completed":
        if not isinstance(value.get("result"), dict):
            raise _failure("professional_rpc_protocol_invalid", "专业服务结果不是JSON对象")
        return value
    if status == "not_applicable":
        return {
            **value,
            "error_code": _service_error_code(value, "professional_task_not_applicable"),
        }
    if status in {"error", "needs_input", "failed"}:
        code = _service_error_code(value, "professional_rpc_operation_failed")
        raise _failure(code, "专业服务明确返回输入或方法错误，未作为研究结论采用", 422)
    if status != "model_required":
        raise _failure("professional_rpc_protocol_invalid", "专业服务返回未知执行状态")
    call = value.get("model_call")
    if (
        not isinstance(call, dict)
        or not isinstance(call.get("call_id"), str)
        or not call["call_id"]
        or len(call["call_id"]) > 1024
        or not isinstance(call.get("stage"), str)
        or not call["stage"]
        or not isinstance(call.get("instruction"), str)
        or not call["instruction"]
        or not isinstance(call.get("payload"), dict)
        or call.get("schema") is not None
        and not isinstance(call["schema"], dict)
    ):
        raise _failure("professional_rpc_protocol_invalid", "专业服务模型步骤不符合约定")
    # Each provider owns its canonical model identity. Echo it unchanged; the
    # host ledger independently binds the actual request and its model settings.
    if not isinstance(call.get("request_hash"), str) or not call["request_hash"]:
        raise _failure("professional_rpc_protocol_invalid", "专业服务模型请求缺少哈希")
    if any(item["call_id"] == call["call_id"] for item in request["model_receipts"]):
        raise _failure("professional_rpc_receipt_rejected", "专业服务未采用已提供的同一步模型回执", 409)
    return value


def _post_exchange(client, exchange, checkpoint, guard, max_request_bytes):
    if exchange.get("status") in {"failed", "not_sent"}:
        raise _failure(exchange["error_code"], "专业HTTP步骤已明确停止，未自动再次发送", 409)
    content = _canonical(exchange["request"])
    if len(content) > max_request_bytes:
        raise _failure("professional_rpc_request_limit", "专业服务请求超过本轮传输上限", 422)
    # These POSTs only replay pure methods. A model is called solely through call(),
    # below; one identical HTTP replay cannot send or charge another model request.
    while exchange.get("attempts", 0) < 2:
        intent_saved = False
        try:
            if guard:
                guard()
            exchange["attempts"] = exchange.get("attempts", 0) + 1
            intent_saved = True
            exchange["status"] = "sent"
            checkpoint()
            # A durable checkpoint can cross the deadline or observe cancellation.
            # Recheck immediately before POST, after that potentially slow write.
            if guard:
                guard()
        except DomainError as exc:
            if intent_saved:
                exchange["attempts"] -= 1
            exchange.update(status="not_sent", error_code=exc.code)
            checkpoint()
            raise
        try:
            response = client.post(
                "/v1/execute", content=content, headers={"Content-Type": "application/json"}
            )
        except httpx.TransportError:
            exchange.update(status="unknown", error_code="professional_rpc_outcome_unconfirmed")
            checkpoint()
            continue
        if response.status_code != 200:
            exchange.update(
                status="failed",
                error_code="professional_rpc_http_error",
                http_status=response.status_code,
            )
            checkpoint()
            raise _failure("professional_rpc_http_error", "专业服务明确拒绝或未完成请求")
        try:
            value = _response_json(response)
        except DomainError as exc:
            exchange.update(status="failed", error_code=exc.code)
            checkpoint()
            raise
        exchange.update(status="received", response=copy.deepcopy(value))
        exchange.pop("error_code", None)
        checkpoint()
        return value
    raise _failure("professional_rpc_outcome_unconfirmed", "专业HTTP重放仍未确认，保留进度等待对账", 409)


def execute_professional(
    client,
    *,
    provider,
    operation_id,
    operation,
    arguments,
    call,
    state,
    checkpoint,
    provider_version=None,
    guard=None,
    max_model_calls=32,
    max_request_bytes=2 * 1024 * 1024,
):
    """Run a pure RPC continuation using the host's existing model callback.

    state is one progress.professional_operations[operation_id] dictionary.
    checkpoint() durably saves that same host progress. call accepts the original
    method stage/instruction/payload/schema and keyword call_id; it alone decides
    whether a model receipt can be replayed or a new request may be sent.
    Authentication stays on client and never enters state or a public result.
    """
    if (
        not isinstance(state, dict)
        or not isinstance(arguments, dict)
        or not isinstance(operation_id, str)
        or not operation_id
        or not isinstance(operation, str)
        or not operation
        or provider not in {"biocheck", "mech_evidence"}
        or type(max_model_calls) is not int
        or not 0 <= max_model_calls <= 64
    ):
        raise _failure("professional_rpc_input_invalid", "专业服务调用输入不完整", 422)
    binding = {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "operation_id": operation_id,
        "operation": operation,
        "arguments_hash": hashlib.sha256(_canonical(arguments)).hexdigest(),
    }
    if state.get("binding") not in (None, binding):
        raise _failure("professional_rpc_input_changed", "同一专业操作的固定输入已改变", 409)
    if (
        provider_version is not None
        and state.get("provider_version") is not None
        and state["provider_version"] != provider_version
    ):
        raise _failure("analysis_provider_changed", "专业方法版本已改变，旧回执保留", 409)
    state.setdefault("binding", binding)
    state.setdefault("provider_version", provider_version)
    receipts = state.setdefault("model_receipts", [])
    exchanges = state.setdefault("exchanges", [])
    checkpoint()
    try:
        for _ in range(max_model_calls + 2):
            request = {
                "schema_version": SCHEMA_VERSION,
                "operation_id": operation_id,
                "operation": operation,
                "provider_version": state["provider_version"],
                "arguments": copy.deepcopy(arguments),
                "model_receipts": copy.deepcopy(receipts),
            }
            digest = hashlib.sha256(_canonical(request)).hexdigest()
            exchange = None
            if exchanges:
                previous = exchanges[-1]
                if previous["request_hash"] == digest or (
                    previous.get("response") is not None
                    and previous["request"]["model_receipts"] == receipts
                ):
                    # The first response pins a version. Its saved model-required
                    # step/result remains authoritative despite that pin changing
                    # the next envelope's version from null to the actual value.
                    exchange = previous
            if exchange is None:
                exchange = {"request": request, "request_hash": digest, "status": "prepared", "attempts": 0}
                exchanges.append(exchange)
                checkpoint()
            value = exchange.get("response")
            if value is None:
                value = _post_exchange(client, exchange, checkpoint, guard, max_request_bytes)
            value = _checked_response(
                value,
                provider=provider,
                operation_id=operation_id,
                version=state["provider_version"],
                request=exchange["request"],
            )
            state["provider_version"] = value["provider_version"]
            state.update(status=value["status"])
            state.pop("error_code", None)
            checkpoint()
            if value["status"] in {"completed", "not_applicable"}:
                return copy.deepcopy(value)
            required = value["model_call"]
            if len(receipts) >= max_model_calls:
                raise _failure("professional_rpc_callback_limit", "专业服务本操作超过模型步骤上限", 409)
            if call is None:
                raise _failure("professional_rpc_unexpected_model_request", "本专业操作约定为零模型调用", 409)
            # Persist the exact required step first. After a host receipt is received,
            # a crash here re-enters the SAME call_id in the existing host ledger.
            output = call(
                required["stage"],
                required["instruction"],
                copy.deepcopy(required["payload"]),
                copy.deepcopy(required.get("schema")),
                call_id=required["call_id"],
            )
            if not isinstance(output, dict):
                raise _failure("professional_rpc_model_output_invalid", "统一模型回执不是JSON对象")
            _canonical(output)
            receipts.append(
                {
                    "call_id": required["call_id"],
                    "request_hash": required["request_hash"],
                    "output": copy.deepcopy(output),
                }
            )
            checkpoint()
        raise _failure("professional_rpc_callback_limit", "专业服务本操作超过有界重放步骤", 409)
    except DomainError as exc:
        state.update(status="blocked", error_code=exc.code)
        checkpoint()
        raise
