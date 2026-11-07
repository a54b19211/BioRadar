"""HTTP calls to the professional services: query-planner, BioCheck (潘倩), MechEvidence (汪鑫怡).

Each operation is one synchronous request. The service calls its own model and returns `llm_calls`,
which the radar adds to the analysis ledger. A timeout is not retried: the service may have paid
for model calls already, so the step is reported as failed instead."""

import httpx

from bioradar import guard
from bioradar.config import Settings


class ServiceError(Exception):
    def __init__(self, service: str, code: str, message: str = "", calls=(), body=None):
        super().__init__(f"{service}: {code} {message}".strip())
        self.service, self.code = service, code
        self.calls = list(calls)  # model calls the service made before failing; still billed
        self.body = body if isinstance(body, dict) else {}  # the service's whole error answer
        self.reads: list[int] = []  # the material_reads of the request that failed (set by the sender)


def _post(service, url, token, path, body, timeout, method="POST"):
    if not url:
        raise ServiceError(service, "not_configured")
    guard.check()
    try:
        target, headers = url.rstrip("/") + path, {"Authorization": f"Bearer {token}"}
        if method == "GET":
            response = httpx.get(target, timeout=timeout, headers=headers)
        else:
            response = httpx.post(target, json=body, timeout=timeout, headers=headers)
    except httpx.TimeoutException as exc:
        raise ServiceError(service, "timeout") from exc
    except httpx.HTTPError as exc:
        raise ServiceError(service, "unreachable", str(exc)[:200]) from exc
    if response.status_code >= 400:
        try:
            error = response.json()
        except ValueError:
            error = {}
        raise ServiceError(
            service,
            error.get("error_code") or f"http_{response.status_code}",
            str(error.get("message", ""))[:300],
            error.get("llm_calls") or (),
            error,
        )
    return response.json()


def decide(settings: Settings, body: dict) -> dict:
    return _post("planner", settings.planner_url, settings.planner_token, "/v1/decide", body, 30)


def planner_health(settings: Settings) -> dict:
    """GET /health of query-planner: {status, version, kinds, channels}; a planner older than 0.2.0 answers
    only {status}."""
    return _post("planner", settings.planner_url, settings.planner_token, "/health", None, 10, "GET")


def biocheck(settings: Settings, operation: str, body: dict) -> dict:
    """operation: generate | verify | revise"""
    return _post(
        "biocheck",
        settings.biocheck_url,
        settings.biocheck_token,
        f"/v1/{operation}",
        body,
        settings.service_timeout_seconds,
    )


def mechevidence_evaluator(settings: Settings) -> dict:
    """GET /v2/evaluator: who would evaluate now ({contract_version, evaluator: {..., evaluator_id}, ...})."""
    return _post(
        "mechevidence",
        settings.mechevidence_url,
        settings.mechevidence_token,
        "/v2/evaluator",
        None,
        30,
        "GET",
    )


def mechevidence_v2(settings: Settings, body: dict) -> dict:
    """POST /v2/evaluate: the version-bound contract. A 409 evaluator_changed and a 502 come back as
    ServiceError with the service's whole answer in .body and its model calls in .calls."""
    return _post(
        "mechevidence",
        settings.mechevidence_url,
        settings.mechevidence_token,
        "/v2/evaluate",
        body,
        settings.service_timeout_seconds,
    )


def mechevidence(settings: Settings, body: dict) -> dict:
    return _post(
        "mechevidence",
        settings.mechevidence_url,
        settings.mechevidence_token,
        "/v1/evaluate",
        body,
        settings.service_timeout_seconds,
    )
