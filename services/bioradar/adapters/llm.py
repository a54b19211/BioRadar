"""The radar's own model calls. One function, OpenAI-compatible /chat/completions."""

import hashlib
import json
import time

import httpx
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


# Usually refused before the model runs, but that is not guaranteed (it may still be billed), so resends are
# few and each one counts against the caller's request budget.
REFUSED = (429, 503)
BACKOFF = (5, 20, 60)  # seconds before each resend; seen 2026-11-03: DeepSeek 503 for ~25 minutes


class SendBlocked(Exception):
    """Raised by a caller's before_send: this request may not go out (`reason`); the ones before it did."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def post(settings: Settings, body: dict, call: dict, limit: int | None = None, before_send=None) -> dict:
    """POST one request, resent after a refusal; before_send(requests already sent in this call) runs right
    before every send and may raise SendBlocked."""
    for wait in (*BACKOFF, None):
        try:
            guard.check()
        except guard.Revoked:
            raise SendBlocked("revoked") from None  # what was already sent is returned for settlement
        if before_send is not None:
            before_send(call.get("requests", 0))
        call["requests"] = call.get("requests", 0) + 1
        try:
            response = httpx.post(
                settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=body,
                timeout=settings.llm_timeout_seconds,
            )
        except httpx.HTTPError as exc:  # no answer to this request (a timeout, a dropped connection)
            call.setdefault("http", []).append(type(exc).__name__)
            raise
        call.setdefault("http", []).append(response.status_code)  # one entry per request, kept
        if response.status_code not in REFUSED or wait is None or (limit and call["requests"] >= limit):
            break
        time.sleep(wait)
    response.raise_for_status()
    return response.json()


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
        body = {
            "model": settings.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if settings.llm_provider == "deepseek":
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        elif settings.llm_provider == "vllm":
            # local vLLM serving Qwen: the same per-call switch, as its chat template reads it
            body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
        if not thinking:
            body["temperature"] = 0
        if max_requests and call.get("requests", 0) >= max_requests:
            call["error"] = call["error"] or "request_budget"
            break
        call["attempts"] = attempt + 1
        try:
            data = post(settings, body, call, max_requests, before_send)
        except SendBlocked as exc:
            call["error"], call["blocked"] = "send_blocked", exc.reason
            break
        except httpx.TimeoutException:
            call["error"] = "timeout"  # not resent: the provider may have billed it
            break
        except httpx.HTTPError as exc:
            call["error"] = f"http_error:{getattr(getattr(exc, 'response', None), 'status_code', '')}"
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
            aliases = getattr(model_cls, "source_aliases", None)
            if aliases is not None:
                raw, output["mappings"] = aliases(raw)  # measure.v10: mechanical, recorded
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
