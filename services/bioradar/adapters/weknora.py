"""Read WeKnora candidates and adopt only authorized, versioned Fabric originals."""

import copy
import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime

import httpx

from bioradar.api.errors import DomainError

MAX_RESPONSE_BYTES = 2_000_000
REQUEST_SECONDS = 20
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_VERSION = re.compile(r"[0-9a-f]{64}")


def provider_status(settings):
    configured = bool(
        settings.weknora_url
        and settings.weknora_api_key
        and settings.weknora_api_key.get_secret_value()
        and settings.weknora_knowledge_base_ids
    )
    return {
        "provider": "weknora",
        "configured": configured,
        "available": configured,
        "status": "ready" if configured else "unavailable",
        "version": hashlib.sha256(
            json.dumps(
                {"url": settings.weknora_url, "knowledge_base_ids": settings.weknora_knowledge_base_ids},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if configured
        else None,
        "error_code": None if configured else "weknora_not_configured",
        "qualification": "配置就绪不证明调用成功；搜索含向量与重排，服务未返回的用量保持未知。",
    }


def _envelope(query, status, *, error=None):
    return {
        "provider": "weknora",
        "status": status,
        "query": query,
        "candidates": [],
        "records": [],
        "returned": 0,
        "lookup_requests": 0,
        "version_reads": 0,
        "truncated": False,
        "error_code": error,
        "usage": {"embedding_rerank_tokens": None, "monetary_cost": None},
        "scope": "configured_knowledge_bases_then_authorized_fabric_versions",
    }


def _metadata(item):
    metadata = item.get("metadata") or {}
    if not isinstance(metadata, dict):
        return {}
    result = copy.deepcopy(metadata)
    nested = item.get("source_metadata") or result.pop("source_metadata", None)
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except (ValueError, TypeError):
            return {}
    if isinstance(nested, dict):
        if any(key in result and result[key] != value for key, value in nested.items()):
            return {}
        result.update(nested)
    return result


def _bind(reader, metadata, *, source_ids, cutoff, is_demo, reads):
    """A search hint selects an original; its chunk text and offsets are never adopted."""
    source_id, native = metadata.get("source_id"), metadata.get("source_record_id")
    version = metadata.get("version_id")
    if not source_id or not native:
        site, uuid = metadata.get("source_site"), metadata.get("source_uuid")
        if not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in (site, uuid)):
            return {"binding_status": "unbound", "binding_reason": "source_identity_missing"}, None
        source_id, native = "legacy_documents", f"legacy:{site}:{uuid}"
        if site not in source_ids:
            return {"binding_status": "not_authorized", "binding_reason": "source_not_selected"}, None
    if (
        not isinstance(source_id, str)
        or not _IDENTIFIER.fullmatch(source_id)
        or not isinstance(native, str)
        or not 1 <= len(native) <= 400
        or any(ord(char) < 32 for char in native)
    ):
        return {"binding_status": "unbound", "binding_reason": "source_identity_invalid"}, None
    if source_id not in source_ids:
        return {"binding_status": "not_authorized", "binding_reason": "source_not_selected"}, None
    if source_id == "legacy_documents":
        parts = native.split(":", 2)
        if len(parts) != 3 or parts[0] != "legacy":
            return {"binding_status": "unbound", "binding_reason": "original_source_identity_missing"}, None
        if metadata.get("source_site") not in (None, parts[1]):
            return {"binding_status": "unbound", "binding_reason": "source_identity_mismatch"}, None
        if parts[1] not in source_ids:
            return {"binding_status": "not_authorized", "binding_reason": "source_not_selected"}, None
    if reader is None:
        return {"binding_status": "unbound", "binding_reason": "fabric_not_connected"}, None
    binding = {
        "source_id": source_id,
        "source_record_id": native,
        "is_demo": is_demo,
    }
    exact_version = isinstance(version, str) and _VERSION.fullmatch(version) is not None
    try:
        records_read = getattr(reader, "records_read", 0)
        if records_read >= getattr(reader, "max_records", 3):
            raise DomainError("history_read_budget", "原件身份绑定达到记录读取上限", 422)
        if exact_version:
            binding["version_id"] = version
        else:
            reads["lookup_requests"] += 1
            page = reader._get(
                "/api/history/records",
                {
                    "source_id": source_id,
                    "is_demo": str(is_demo).lower(),
                    "mode": "current",
                    "cutoff": cutoff.isoformat(),
                    "source_record_ids": [native],
                    "limit": 1,
                },
            )
            if (
                not isinstance(page, dict)
                or page.get("schema_version") != "datafabric.history.collection.v1"
                or not isinstance(page.get("items"), list)
                or len(page["items"]) > 1
            ):
                raise DomainError("provider_protocol_error", "Fabric 候选绑定返回格式无效", 502)
            if not page["items"]:
                return {"binding_status": "not_found", "binding_reason": "no_published_fabric_record"}, None
            head = page["items"][0]
            if (
                not isinstance(head, dict)
                or head.get("source_id") != source_id
                or head.get("source_record_id") != native
                or head.get("is_demo") is not is_demo
                or not isinstance(head.get("version_id"), str)
                or not _VERSION.fullmatch(head["version_id"])
            ):
                raise DomainError("provider_protocol_error", "Fabric 候选绑定身份不一致", 502)
            binding["version_id"] = head["version_id"]
        reader.records_read = records_read + 1
        reads["version_reads"] += 1
        original = reader.read_version(binding, mode="current", cutoff=cutoff)
        if (
            original.get("source_id") != source_id
            or original.get("source_record_id") != native
            or original.get("version_id") != binding["version_id"]
            or original.get("is_demo") is not is_demo
        ):
            return {"binding_status": "unbound", "binding_reason": "version_identity_mismatch"}, None
        expected_hash = metadata.get("raw_sha256")
        if exact_version and expected_hash and original["raw_sha256"] != expected_hash:
            return {"binding_status": "unbound", "binding_reason": "version_hash_mismatch"}, None
        if source_id == "legacy_documents" and original.get("facts", {}).get("source_site") not in (
            None,
            native.split(":", 2)[1],
        ):
            return {"binding_status": "unbound", "binding_reason": "source_identity_mismatch"}, None
        reference = reader.ref(original).model_dump(mode="json")
    except DomainError as exc:
        return {"binding_status": "failed", "binding_reason": exc.code}, None
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"binding_status": "failed", "binding_reason": "fabric_binding_invalid"}, None
    return {
        "binding_status": "bound",
        "binding_reason": "exact_fabric_version" if exact_version else "current_fabric_version",
        "search_version_verified": exact_version,
        "record_ref": reference,
        "history_binding": binding,
    }, original


def search_candidates(
    settings,
    query,
    *,
    reader=None,
    source_ids=(),
    cutoff=None,
    is_demo=False,
    limit=3,
    client=None,
    timeout_seconds=None,
):
    """One POST, no retry; callers persist the query receipt before any adoption."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise DomainError("weknora_query_invalid", "WeKnora 查询需要有界的非空文字", 422)
    if type(limit) is not int or not 1 <= limit <= 3:
        raise DomainError("weknora_query_invalid", "WeKnora 每次最多采用三条候选", 422)
    if timeout_seconds is not None and (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise DomainError("weknora_time_budget", "本轮剩余时限不足以读取候选", 422)
    seconds = REQUEST_SECONDS if timeout_seconds is None else min(REQUEST_SECONDS, timeout_seconds)
    if not provider_status(settings)["available"]:
        return _envelope(query, "unavailable", error="weknora_not_configured")
    result = _envelope(query, "completed")
    owned = client is None
    session = client or httpx.Client(trust_env=False, follow_redirects=False)
    began = time.monotonic()
    try:
        with session.stream(
            "POST",
            settings.weknora_url + "/api/v1/knowledge-search",
            json={"query": query, "knowledge_base_ids": settings.weknora_knowledge_base_ids},
            headers={"X-API-Key": settings.weknora_api_key.get_secret_value()},
            timeout=httpx.Timeout(seconds, connect=min(5, seconds)),
            follow_redirects=False,
        ) as response:
            result["http_status"] = response.status_code
            if response.status_code != 200:
                result.update(status="failed", error_code="weknora_http_error")
                return result
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES or time.monotonic() - began > seconds:
                    result.update(status="failed", error_code="weknora_response_budget")
                    return result
        try:
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or value.get("success") is not True
                or not isinstance(value.get("data"), list)
                or any(not isinstance(item, dict) for item in value["data"])
            ):
                raise ValueError("invalid search response")
        except (ValueError, TypeError, RecursionError):
            result.update(status="failed", error_code="weknora_protocol_error")
            return result
        result["returned"] = len(value["data"])
        result["truncated"] = len(value["data"]) > limit
        seen = set()
        for item in value["data"][:limit]:
            score = item.get("score")
            try:
                valid_score = score is None or (type(score) in {int, float} and math.isfinite(score))
            except OverflowError:
                valid_score = False
            if not valid_score:
                result.update(status="failed", error_code="weknora_protocol_error")
                result["candidates"], result["records"] = [], []
                return result
            candidate = {
                name: copy.deepcopy(item[name])
                for name in ("id", "knowledge_id", "knowledge_title", "knowledge_base_id", "score")
                if name in item
            }
            metadata = _metadata(item)
            binding, original = _bind(
                reader,
                metadata,
                source_ids=set(source_ids),
                cutoff=cutoff or datetime.now(UTC),
                is_demo=is_demo,
                reads=result,
            )
            candidate.update(binding)
            # Unbound snippets have no Radar source authorization or verified location.
            # Retain only candidate identifiers and titles for a later explicit lookup.
            if original is not None:
                identity = (original["source_id"], original["source_record_id"], original["version_id"])
                if identity not in seen:
                    result["records"].append(original)
                    seen.add(identity)
            result["candidates"].append(candidate)
        if not value["data"]:
            result["status"] = "empty"
        return result
    except httpx.HTTPError:
        result.update(status="unconfirmed", error_code="weknora_transport_unconfirmed")
        return result
    finally:
        result["usage"]["lookup_requests"] = result["lookup_requests"]
        result["usage"]["version_reads"] = result["version_reads"]
        if owned:
            session.close()
