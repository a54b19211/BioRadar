"""The radar's own model calls. One function over the official `openai` client (any OpenAI-compatible
/chat/completions: DeepSeek, vLLM, 通义); the prompt description, the format retry, the error codes and the
call record are the radar's."""

import hashlib
import json
import time

import httpx
import openai
from pydantic import BaseModel, ValidationError

from bioradar import guard
from bioradar.config import Settings


def describe(model_cls: type[BaseModel]) -> str:
    """A short field list for the prompt; the full JSON Schema is noisy and is not enforced anyway."""
    schema = model_cls.model_json_schema()
    defs = schema.get("$defs", {})

    def fields(node, indent=""):
        lines = []
        for name, prop in node.get("properties", {}).items():
            ref = prop.get("$ref") or prop.get("items", {}).get("$ref")
            kind = prop.get("type", "object")
            if "enum" in prop:
                kind = "|".join(map(str, prop["enum"]))
            elif "anyOf" in prop:
                kind = "|".join(p.get("type", "object") for p in prop["anyOf"])
            note = f" — {prop['description']}" if prop.get("description") else ""
            lines.append(f"{indent}{name}: {kind}{note}")
            if ref:
                lines += fields(defs[ref.split("/")[-1]], indent + "  ")
        return lines

    return "\n".join(fields(schema))


# Resent after these statuses, waiting BACKOFF seconds before each resend (seen 2026-11-03: DeepSeek 503
# for ~25 minutes). The resend loop is here, not the client's max_retries, because every request - a resend
# too - must pass guard.check and the caller's before_send first and counts against the request budget.
REFUSED = (429, 503)
BACKOFF = (5, 20, 60)


class SendBlocked(Exception):
    """Raised by a caller's before_send: this request may not go out (`reason`); the ones before it did."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def client() -> httpx.Client:
    """The one place an HTTP client for model calls is made (tests give it a stub transport)."""
    return httpx.Client()


def build_body(settings: Settings, messages: list, *, max_tokens: int, thinking: bool) -> dict:
    """The request body: the provider's per-call thinking switch, temperature 0 when not thinking."""
    body = {"model": settings.llm_model, "messages": list(messages), "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    if settings.llm_provider == "deepseek":
        body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    elif settings.llm_provider == "vllm":  # local vLLM serving Qwen reads the switch from its chat template
        body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    if not thinking:
        body["temperature"] = 0
    return body


def post(settings: Settings, body: dict, call: dict, limit: int | None = None, before_send=None) -> dict:
    """Send one request, resent after a refusal; guard.check and before_send(requests already sent in this
    call) run right before every send and may raise SendBlocked. Returns the decoded JSON body; an unanswered
    request and a non-2xx status raise openai errors, a body that is not JSON ValueError. `call` keeps the
    request count and one http entry (status or exception name) per request."""
    extra = {k: body[k] for k in ("thinking", "chat_template_kwargs") if k in body}
    plain = {k: v for k, v in body.items() if k not in extra}
    with client() as http:
        api = openai.OpenAI(base_url=settings.llm_base_url or None, api_key=settings.llm_api_key or "-",
                            timeout=settings.llm_timeout_seconds, max_retries=0, http_client=http)
        for pause in (*BACKOFF, None):
            sent = call.get("requests", 0)
            if limit and sent >= limit:
                raise SendBlocked("request_budget")
            try:
                guard.check()
            except guard.Revoked:
                raise SendBlocked("revoked") from None  # what was already sent is returned for settlement
            if before_send is not None:
                before_send(sent)
            call["requests"] = sent + 1
            try:
                raw = api.chat.completions.with_raw_response.create(**plain, extra_body=extra or None)
            except openai.APIStatusError as exc:
                call.setdefault("http", []).append(exc.status_code)
                if exc.status_code not in REFUSED or pause is None or (limit and sent + 1 >= limit):
                    raise
            except openai.APIError as exc:
                call.setdefault("http", []).append(type(exc).__name__)
                raise
            else:
                call.setdefault("http", []).append(raw.status_code)
                return raw.http_response.json()
            time.sleep(pause)


def chat_json(
    settings: Settings,
    stage: str,
    system: str,
    payload: dict,
    model_cls: type[BaseModel],
    *,
    max_tokens: int = 4096,
    thinking: bool = False,
    max_requests: int | None = None,
    before_send=None,
):
    """Return (parsed object or None, call record). Never raises for model-side failures. `max_requests`
    caps the HTTP requests (format retry and resends included) at what the caller's budget has left.
    `before_send(sent)` runs right before every HTTP request - the first, a resend, the format retry - with
    the number already sent; when it raises SendBlocked the call stops there: error send_blocked, `blocked`
    the reason, and everything about the requests already sent (count, usage, outputs) as it was."""
    messages = [
        {"role": "system", "content": system + "\n\n只返回一个JSON对象，字段：\n" + describe(model_cls)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    call = {
        "stage": stage,
        "service": "radar",
        "model": settings.llm_model,
        "finish_reason": None,
        "usage": {},
        "seconds": 0.0,
        "error": None,
        "attempts": 0,
    }
    started = time.monotonic()
    for attempt in range(2):
        body = build_body(settings, messages, max_tokens=max_tokens, thinking=thinking)
        if max_requests and call.get("requests", 0) >= max_requests:
            call["error"] = call["error"] or "request_budget"
            break
        call["attempts"] = attempt + 1
        try:
            data = post(settings, body, call, max_requests, before_send)
        except SendBlocked as exc:
            call["error"], call["blocked"] = "send_blocked", exc.reason
            break
        except openai.APITimeoutError:
            call["error"] = "timeout"  # not resent: the provider may have billed it
            break
        except openai.APIError as exc:
            call["error"] = f"http_error:{getattr(exc, 'status_code', '')}"
            break
        except ValueError:
            call["error"] = "response_not_json"
            break
        choice = (data.get("choices") or [None])[0] if isinstance(data, dict) else None
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            # seen 2026-11-03: HTTP 200 with a null body; not resent, it may have been billed
            call["error"] = "response_without_choices"
            break
        content = choice["message"].get("content") or ""
        call["finish_reason"] = choice.get("finish_reason")
        for key, value in (data.get("usage") or {}).items():
            if isinstance(value, int):
                call["usage"][key] = call["usage"].get(key, 0) + value
        if call["finish_reason"] == "length" and not content.strip():
            call["error"] = "output_empty_after_limit"
            break
        output = {
            "attempt": attempt + 1,
            "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "mappings": [],
        }
        call.setdefault("outputs", []).append(output)  # every answer as given, kept (llm_outputs)
        try:
            raw = json.loads(content)
            parsed = model_cls.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            call["error"], call["detail"] = "output_invalid", str(exc)[:300]
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": f"上面的JSON不合格：{str(exc)[:1500]}\n请按字段说明重新输出完整JSON。",
                },
            ]
            continue
        call["error"] = None
        call["seconds"] = round(time.monotonic() - started, 2)
        return parsed, call
    call["seconds"] = round(time.monotonic() - started, 2)
    return None, call
