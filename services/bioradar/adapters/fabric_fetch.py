"""One visible Fabric fetch submission and fixed-run polling; no collector or retry queue."""

import copy
from datetime import datetime
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bioradar.api.errors import DomainError

FETCH_VERSION = "datafabric.history.fetch.v1"


class _Continuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cursor: str = Field(min_length=1, max_length=4000)
    read_count: int = Field(ge=0, le=100_000_000)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: Literal[
        "clinicaltrials", "pubmed", "fda_crl", "europe_pmc", "who_don", "sec_companyfacts", "external_archive"
    ]
    query: str = Field(min_length=1, max_length=500)
    source_record_ids: list[str] = Field(default_factory=list, max_length=100)
    max_records: int = Field(default=10, ge=1, le=100)
    max_pages: int = Field(default=3, ge=1, le=10)
    deadline_seconds: int = Field(default=60, ge=5, le=120)
    updated_since: str | None = None
    updated_until: str | None = None
    continuation: _Continuation | None = None
    is_demo: bool = False
    demo_instance_id: str | None = Field(default=None, min_length=1, max_length=200)


def _result(status, *, run=None, count=0, request_status="not_sent", error=None):
    return {
        "status": status,
        "run_id": run["id"] if run is not None else None,
        "run": copy.deepcopy(run),
        "tool_requests": count,
        "error_code": error,
        "usage": {"request_status": request_status},
    }


def _run(value, source_id, *, is_demo=False, demo_instance_id=None):
    if isinstance(value, dict) and isinstance(value.get("run"), dict):
        value = value["run"]
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not value["id"]
        or value.get("source_id") != source_id
        or value.get("is_demo") is not is_demo
        or value.get("kind") != "fetch"
        or value.get("state") not in {"queued", "running", "completed", "partial", "failed", "enqueue_failed"}
        or not isinstance(value.get("version_ids"), list)
        or len(value["version_ids"]) > 100
        or any(
            not isinstance(version, str)
            or len(version) != 64
            or any(char not in "0123456789abcdef" for char in version)
            for version in value["version_ids"]
        )
        or len(set(value["version_ids"])) != len(value["version_ids"])
        or not isinstance(value.get("coverage"), dict)
    ):
        raise DomainError("history_fetch_response_invalid", "补采回执不是同源真实fetch运行", 502)
    if is_demo and (
        value.get("fetch_mode") != "simulated"
        or not demo_instance_id
        or value.get("demo_instance_id") != demo_instance_id
        or not isinstance(value.get("input_sha256"), str)
        or len(value["input_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in value["input_sha256"])
    ):
        raise DomainError("history_fetch_response_invalid", "模拟补采回执与隔离实例或固定包不符", 502)
    return value


def demo_fetch_available(settings):
    # Use the same authorized reader against the explicitly bound isolated instance.
    return bool(
        getattr(settings, "environment", None) == "test"
        and getattr(settings, "data_fabric_demo_fetch_url", None)
        and settings.data_fabric_demo_fetch_url == settings.data_fabric_url
        and getattr(settings, "data_fabric_demo_fetch_instance_id", None)
        and settings.data_fabric_history_fetch_token
    )


def submit_fetch(
    settings, request, *, call_key, is_demo=False, mode="current", timeout_seconds=30, client=None
):
    """The caller checkpoints this exact request/key first; this function sends at most once."""
    if mode != "current" or is_demo and not demo_fetch_available(settings):
        return _result("unavailable", error="history_fetch_current_real_only")
    token = getattr(settings, "data_fabric_history_fetch_token", None)
    base = getattr(settings, "data_fabric_url", None)
    if not token or not base:
        return _result("unavailable", error="history_fetch_not_configured")
    if timeout_seconds <= 0:
        return _result("failed", error="analysis_time_budget")
    if not isinstance(call_key, str) or not 1 <= len(call_key) <= 200:
        raise DomainError("history_fetch_input_invalid", "补采缺少稳定调用编号", 422)
    body = normalize_fetch_request(request)
    if is_demo:
        body = normalize_fetch_request(
            {
                **body,
                "is_demo": True,
                "demo_instance_id": settings.data_fabric_demo_fetch_instance_id,
            }
        )
        base = settings.data_fabric_demo_fetch_url
    elif body.get("is_demo") or body.get("demo_instance_id") or body["source_id"] == "external_archive":
        return _result("unavailable", error="history_fetch_current_real_only")
    own = client is None
    session = client or httpx.Client(trust_env=False, follow_redirects=False)
    try:
        response = session.post(
            str(base).rstrip("/") + "/api/history/fetch",
            json=body,
            headers={"Authorization": "Bearer " + token.get_secret_value(), "Idempotency-Key": call_key},
            timeout=httpx.Timeout(timeout_seconds, connect=min(5, timeout_seconds)),
        )
        if response.status_code not in {200, 201, 202}:
            unknown = response.status_code >= 500
            return _result(
                "unconfirmed" if unknown else "failed",
                count=1,
                request_status="unknown" if unknown else "received",
                error="history_fetch_outcome_unconfirmed" if unknown else "history_fetch_http_error",
            )
        if len(response.content) > 8_000_000:
            return _result(
                "unconfirmed", count=1, request_status="unknown", error="history_fetch_outcome_unconfirmed"
            )
        try:
            run = _run(
                response.json(),
                body["source_id"],
                is_demo=is_demo,
                demo_instance_id=body.get("demo_instance_id"),
            )
        except (DomainError, ValueError):
            return _result(
                "unconfirmed", count=1, request_status="unknown", error="history_fetch_outcome_unconfirmed"
            )
        status = "pending" if run["state"] in {"queued", "running"} else run["state"]
        return _result(status, run=run, count=1, request_status="received")
    except httpx.HTTPError:
        return _result(
            "unconfirmed", count=1, request_status="unknown", error="history_fetch_outcome_unconfirmed"
        )
    finally:
        if own:
            session.close()


def poll_fetch(reader, run_id, *, source_id, is_demo=False, demo_instance_id=None):
    """Resume an already submitted run with the existing authorized GET reader."""
    if not isinstance(run_id, str) or not run_id or len(run_id) > 200:
        raise DomainError("history_fetch_input_invalid", "补采运行编号无效", 422)
    run = _run(
        reader._get("/api/history/runs/" + quote(run_id, safe="")),
        source_id,
        is_demo=is_demo,
        demo_instance_id=demo_instance_id,
    )
    if run["id"] != run_id:
        raise DomainError("history_fetch_response_invalid", "补采回执运行编号已变化", 502)
    status = "pending" if run["state"] in {"queued", "running"} else run["state"]
    return _result(status, run=run, request_status="received")


def normalize_fetch_request(request):
    """Validate before a durable send intent; return only the real FetchRequest body."""
    try:
        normalized = _Request.model_validate(request)
        if normalized.is_demo:
            if not normalized.demo_instance_id or any(
                getattr(normalized, name) is not None
                for name in ("continuation", "updated_since", "updated_until")
            ):
                raise ValueError(
                    "isolated demo fetch requires an instance and has no historical continuation"
                )
        elif normalized.demo_instance_id:
            raise ValueError("a demo instance cannot be sent as a live fetch")
        times = {}
        for field in ("updated_since", "updated_until"):
            text = getattr(normalized, field)
            if text is not None:
                value = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if value.tzinfo is None:
                    raise ValueError("fetch date requires timezone")
                times[field] = value
        if times.get("updated_since") and times.get("updated_until"):
            if times["updated_since"] > times["updated_until"]:
                raise ValueError("fetch interval is reversed")
    except (ValidationError, ValueError, TypeError):
        raise DomainError("history_fetch_input_invalid", "补采参数不符合已发布来源契约", 422) from None
    return normalized.model_dump(
        mode="json", exclude_none=True, exclude={"is_demo"} if not normalized.is_demo else set()
    )


def collect_fetch_version_bindings(reader, run, *, cutoff, version_ids=None):
    """Resolve exact run versions through the existing bounded, authorized collection."""
    checked = _run(
        run,
        run.get("source_id"),
        is_demo=run.get("is_demo", False),
        demo_instance_id=run.get("demo_instance_id"),
    )
    wanted = list(checked["version_ids"] if version_ids is None else version_ids)
    if (
        checked["state"] not in {"completed", "partial"}
        or len(set(wanted)) != len(wanted)
        or not set(wanted).issubset(checked["version_ids"])
    ):
        raise DomainError("history_fetch_binding_invalid", "补采版本绑定范围与原运行不符", 422)
    result = {
        "run_id": checked["id"],
        "source_id": checked["source_id"],
        "is_demo": checked["is_demo"],
        "mode": "current",
        "cutoff": cutoff.isoformat(),
        "requested_version_ids": wanted,
        "bindings": {},
        "missing_version_ids": [],
    }
    if wanted:
        collection = reader.collect(
            source_id=checked["source_id"],
            is_demo=checked["is_demo"],
            mode="current",
            cutoff=cutoff,
            run_id=checked["id"],
        )
        coverage = collection.get("coverage", {})
        try:
            as_of = coverage.get("as_of")
            as_of = datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        except ValueError:
            as_of = None
        if (
            collection.get("source_id") != checked["source_id"]
            or collection.get("is_demo") is not checked["is_demo"]
            or collection.get("cutoff") != cutoff
            or coverage.get("run_id") != checked["id"]
            or as_of != cutoff
        ):
            raise DomainError("history_fetch_binding_invalid", "原件集合不是同一补采范围和截止时间", 502)
        for record in collection["items"]:
            version = record["version_id"]
            if version not in wanted:
                continue  # A newer head never substitutes for the requested run version.
            binding = {
                "source_id": record["source_id"],
                "source_record_id": record["source_record_id"],
                "version_id": version,
                "is_demo": record["is_demo"],
            }
            if version in result["bindings"] and result["bindings"][version] != binding:
                raise DomainError("history_fetch_binding_ambiguous", "同一补采版本返回了不同原生身份", 502)
            result["bindings"][version] = binding
    result["missing_version_ids"] = sorted(set(wanted) - result["bindings"].keys())
    return result


def validate_fetch_version_bindings(run, value, *, cutoff, version_ids=None):
    """Validate saved metadata before reusing it; never infer a native ID."""
    checked = _run(
        run,
        run.get("source_id"),
        is_demo=run.get("is_demo", False),
        demo_instance_id=run.get("demo_instance_id"),
    )
    required = set(checked["version_ids"] if version_ids is None else version_ids)
    invalid = not isinstance(value, dict) or any(
        value.get(key) != expected
        for key, expected in {
            "run_id": checked["id"],
            "source_id": checked["source_id"],
            "mode": "current",
            "cutoff": cutoff.isoformat(),
        }.items()
    )
    if invalid or value.get("is_demo") is not checked["is_demo"]:
        raise DomainError("history_fetch_binding_invalid", "已存补采绑定的范围发生变化", 409)
    wanted, bindings = value.get("requested_version_ids"), value.get("bindings")
    if (
        not isinstance(wanted, list)
        or any(not isinstance(v, str) for v in wanted)
        or len(set(wanted)) != len(wanted)
        or not set(wanted).issubset(checked["version_ids"])
        or not required.issubset(wanted)
        or not isinstance(bindings, dict)
        or not set(bindings).issubset(wanted)
        or value.get("missing_version_ids") != sorted(set(wanted) - bindings.keys())
    ):
        raise DomainError("history_fetch_binding_invalid", "已存补采版本归属不完整或已变化", 409)
    for version, binding in bindings.items():
        if (
            not isinstance(binding, dict)
            or set(binding) != {"source_id", "source_record_id", "version_id", "is_demo"}
            or binding.get("version_id") != version
            or binding.get("source_id") != checked["source_id"]
            or binding.get("is_demo") is not checked["is_demo"]
            or not isinstance(binding.get("source_record_id"), str)
            or not binding["source_record_id"]
        ):
            raise DomainError("history_fetch_binding_invalid", "已存原生版本绑定不符合原运行", 409)
    if required - bindings.keys():
        raise DomainError(
            "history_fetch_binding_missing", "补采目标版本缺少确切原生身份，未用新版本替代", 409
        )
    return bindings


def read_fetch_version(reader, run, version_id, *, cutoff, binding=None):
    """Read the actual native/version pair obtained from the same known fetch run."""
    checked = _run(
        run,
        run.get("source_id"),
        is_demo=run.get("is_demo", False),
        demo_instance_id=run.get("demo_instance_id"),
    )
    if version_id not in checked["version_ids"]:
        raise DomainError("history_fetch_response_invalid", "原文版本不属于已收补采运行", 422)
    if (
        not isinstance(binding, dict)
        or set(binding) != {"source_id", "source_record_id", "version_id", "is_demo"}
        or binding.get("source_id") != checked["source_id"]
        or binding.get("version_id") != version_id
        or binding.get("is_demo") is not checked["is_demo"]
        or not isinstance(binding.get("source_record_id"), str)
        or not binding["source_record_id"]
    ):
        raise DomainError("history_fetch_binding_invalid", "缺少同源确切原生版本绑定，未猜测编号", 422)
    return reader.read_version(binding, mode="current", cutoff=cutoff)
