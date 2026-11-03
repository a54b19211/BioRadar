"""The radar's own model calls. One function, OpenAI-compatible /chat/completions."""

import json
import time

import httpx
from pydantic import BaseModel, ValidationError

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


REFUSED = (429, 503)  # the provider turned the request away without running it, so a resend is safe
BACKOFF = (5, 20, 60)  # seconds before each resend; seen 2026-11-03: DeepSeek 503 for ~25 minutes


def post(settings: Settings, body: dict, call: dict) -> dict:
    for wait in (*BACKOFF, None):
        call["requests"] = call.get("requests", 0) + 1
        response = httpx.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=body,
            timeout=settings.llm_timeout_seconds,
        )
        if response.status_code not in REFUSED or wait is None:
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
):
    """Return (parsed object or None, call record). Never raises for model-side failures."""
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
        if not thinking:
            body["temperature"] = 0
        call["attempts"] = attempt + 1
        try:
            data = post(settings, body, call)
        except httpx.TimeoutException:
            call["error"] = "timeout"  # not resent: the provider may have billed it
            break
        except httpx.HTTPError as exc:
            call["error"] = f"http_error:{getattr(getattr(exc, 'response', None), 'status_code', '')}"
            break
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        call["finish_reason"] = choice.get("finish_reason")
        for key, value in (data.get("usage") or {}).items():
            if isinstance(value, int):
                call["usage"][key] = call["usage"].get(key, 0) + value
        if call["finish_reason"] == "length" and not content.strip():
            call["error"] = "output_empty_after_limit"
            break
        try:
            parsed = model_cls.model_validate(json.loads(content))
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
