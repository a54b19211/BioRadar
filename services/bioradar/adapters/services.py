"""HTTP calls to the professional services: query-planner, BioCheck (潘倩), MechEvidence (汪鑫怡).

Each operation is one synchronous request. The service calls its own model and returns `llm_calls`,
which the radar adds to the analysis ledger. A timeout is not retried: the service may have paid
for model calls already, so the step is reported as failed instead."""

import httpx

from bioradar.config import Settings


class ServiceError(Exception):
    def __init__(self, service: str, code: str, message: str = "", calls=()):
        super().__init__(f"{service}: {code} {message}".strip())
        self.service, self.code = service, code
        self.calls = list(calls)  # model calls the service made before failing; still billed


def _post(service, url, token, path, body, timeout):
    if not url:
        raise ServiceError(service, "not_configured")
    try:
        response = httpx.post(
            url.rstrip("/") + path, json=body, timeout=timeout, headers={"Authorization": f"Bearer {token}"}
        )
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
        )
    return response.json()


def decide(settings: Settings, body: dict) -> dict:
    return _post("planner", settings.planner_url, settings.planner_token, "/v1/decide", body, 30)


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


def biocheck_v2(settings: Settings, body: dict) -> dict:
    """Extraction consistency, contract v2 / checker v7: one measurement request, rules only."""
    return _post(
        "biocheck",
        settings.biocheck_url,
        settings.biocheck_token,
        "/v2/extraction-consistency",
        body,
        settings.service_timeout_seconds,
    )


EXTRACTION_PATHS = {
    "extraction_consistency.v2": "/v2/extraction-consistency",
    "extraction_consistency.v3": "/v3/extraction-consistency",
}


def biocheck_extraction(settings: Settings, body: dict) -> dict:
    """Extraction consistency at the endpoint of the request's own contract: v2 (checkers v7-v10) or v3
    (checker v11, per-dimension results); a contract this client does not know is never sent."""
    path = EXTRACTION_PATHS.get(body.get("contract_version"))
    if path is None:
        raise ServiceError("biocheck", "invalid_request")
    return _post(
        "biocheck",
        settings.biocheck_url,
        settings.biocheck_token,
        path,
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
