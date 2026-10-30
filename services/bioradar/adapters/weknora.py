"""Read WeKnora candidates and adopt only authorized, versioned Fabric originals."""

import copy
import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from bioradar.api.errors import DomainError

MAX_RESPONSE_BYTES = 2_000_000
REQUEST_SECONDS = 20
MAX_HEADER_BYTES = 8192
MAX_HEADER_LINES = 32
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
        "knowledge_reads": 0,
        "truncated": False,
        "error_code": error,
        "usage": {"embedding_rerank_tokens": None, "monetary_cost": None},
        "scope": "configured_knowledge_bases_then_authorized_fabric_versions",
    }


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate metadata key")
        value[key] = item
    return value


def _metadata(item):
    metadata = item.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("invalid metadata")
    result = copy.deepcopy(metadata)
    values = [result.pop("source_metadata", None), item.get("source_metadata")]
    for nested in values:
        if nested is None:
            continue
        if isinstance(nested, str):
            nested = json.loads(nested, object_pairs_hook=_unique_object)
        if not isinstance(nested, dict):
            raise ValueError("invalid source metadata")
        if any(key in result and result[key] != value for key, value in nested.items()):
            raise ValueError("conflicting source metadata")
        result.update(nested)
    return result


def _declared_identity(metadata):
    version = metadata.get("version_id")
    if "version_id" in metadata and (not isinstance(version, str) or not _VERSION.fullmatch(version)):
        raise ValueError("invalid declared version")
    raw_hash = metadata.get("raw_sha256")
    if "raw_sha256" in metadata and (not isinstance(raw_hash, str) or not _VERSION.fullmatch(raw_hash)):
        raise ValueError("invalid declared hash")
    source_id, native = metadata.get("source_id"), metadata.get("source_record_id")
    if "source_id" in metadata or "source_record_id" in metadata:
        if (
            not isinstance(source_id, str)
            or not _IDENTIFIER.fullmatch(source_id)
            or not isinstance(native, str)
            or not 1 <= len(native) <= 400
            or any(ord(char) < 32 for char in native)
        ):
            raise ValueError("invalid declared source")
        if source_id == "legacy_documents":
            parts = native.split(":", 2)
            if (
                len(parts) != 3
                or parts[0] != "legacy"
                or not all(_IDENTIFIER.fullmatch(value) for value in parts[1:])
            ):
                raise ValueError("invalid original source")
            if (
                metadata.get("source_site", parts[1]) != parts[1]
                or metadata.get("source_uuid", parts[2]) != parts[2]
            ):
                raise ValueError("conflicting original source")
        elif "source_uuid" in metadata or metadata.get("source_site", source_id) != source_id:
            raise ValueError("conflicting original source")
        return source_id, native
    if "source_uuid" in metadata:
        site, identifier = metadata.get("source_site"), metadata["source_uuid"]
        if not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in (site, identifier)):
            raise ValueError("invalid original source")
        return "legacy_documents", f"legacy:{site}:{identifier}"
    return None


def _frontmatter(content):
    """Read the collector's bounded plain header; never interpret YAML objects."""
    if not isinstance(content, str):
        raise ValueError("missing source header")
    lines = content.split("\n", MAX_HEADER_LINES + 1)
    if not lines or lines[0].rstrip("\r") != "---":
        raise ValueError("missing source header")
    values, used = {}, 0
    for line in lines[1 : MAX_HEADER_LINES + 1]:
        used += len(line.encode("utf-8")) + 1
        if used > MAX_HEADER_BYTES:
            raise ValueError("source header budget")
        if line.rstrip("\r") == "---":
            break
        match = re.fullmatch(r"([a-z_]+):[ \t]*(.*?)[\r]?", line)
        if match is None or match[1] in values:
            raise ValueError("invalid source header")
        if match[1] in {"source_id", "source_record_id", "source_uuid", "version_id", "raw_sha256"}:
            raise ValueError("unsupported source header declaration")
        values[match[1]] = match[2].strip()
    else:
        raise ValueError("unclosed source header")
    site, url = values.get("source_site"), values.get("source_url")
    if not isinstance(site, str) or not _IDENTIFIER.fullmatch(site):
        raise ValueError("missing original source")
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 4000
        or any(char.isspace() or ord(char) < 32 for char in url)
    ):
        raise ValueError("invalid original URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid original URL")
    return {"source_site": site, "source_url": url}


def _response(session, method, url, *, headers, budget, reads, payload=None):
    maximum = budget.get("max_tool_calls")
    if maximum is not None:
        reader = budget.get("reader")
        fabric_reads = max(0, getattr(reader, "requests_made", 0) - budget["reader_start"])
        if reads.get("tool_requests", 0) + fabric_reads >= maximum:
            raise DomainError("analysis_tool_budget", "候选复合请求达到固定工具预算", 422)
        if reader is not None:
            cap = (
                getattr(reader, "requests_made", 0)
                + maximum
                - reads.get("tool_requests", 0)
                - fabric_reads
                - 1
            )
            previous_cap = getattr(reader, "max_requests", None)
            reader.max_requests = cap if previous_cap is None else min(cap, previous_cap)
    remaining = budget["deadline"] - time.monotonic()
    if remaining <= 0 or budget["bytes"] >= MAX_RESPONSE_BYTES:
        raise DomainError("weknora_response_budget", "候选读取达到时间或字节预算", 422)
    if method == "GET":
        reads["knowledge_reads"] += 1
    reads["tool_requests"] = reads.get("tool_requests", 0) + 1
    with session.stream(
        method,
        url,
        headers=headers,
        json=payload,
        timeout=httpx.Timeout(remaining, connect=min(5, remaining)),
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            return response.status_code, b""
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            budget["bytes"] += len(chunk)
            if budget["bytes"] > MAX_RESPONSE_BYTES or time.monotonic() >= budget["deadline"]:
                raise DomainError("weknora_response_budget", "候选读取达到时间或字节预算", 422)
    return response.status_code, raw


def _knowledge_identity(session, settings, item, metadata, budget, reads):
    identifier, kb = item.get("knowledge_id"), item.get("knowledge_base_id")
    if (
        not isinstance(identifier, str)
        or str(UUID(identifier)) != identifier
        or not isinstance(kb, str)
        or kb not in settings.weknora_knowledge_base_ids
    ):
        raise ValueError("knowledge_identity_invalid")
    status, raw = _response(
        session,
        "GET",
        settings.weknora_url + "/api/v1/knowledge/" + identifier,
        headers={"X-API-Key": settings.weknora_api_key.get_secret_value()},
        budget=budget,
        reads=reads,
    )
    if status != 200:
        raise DomainError("weknora_knowledge_http_error", "知识详情当前不可读取", 502)
    value = json.loads(raw, object_pairs_hook=_unique_object)
    detail = value.get("data") if isinstance(value, dict) and value.get("success") is True else None
    if (
        not isinstance(detail, dict)
        or detail.get("id") != identifier
        or detail.get("knowledge_base_id") != kb
    ):
        raise ValueError("knowledge_identity_mismatch")
    if detail.get("enable_status") != "enabled" or detail.get("deleted_at") is not None:
        raise ValueError("knowledge_not_enabled")
    detail_metadata = _metadata(detail)
    identity = _frontmatter(detail_metadata.get("content"))
    declared = dict(metadata)
    custom = detail.get("custom_metadata")
    if custom is not None and not isinstance(custom, dict):
        raise ValueError("source_metadata_invalid")
    identity_keys = {
        "source_id",
        "source_record_id",
        "source_uuid",
        "source_site",
        "source_url",
        "version_id",
        "raw_sha256",
    }
    for values in (detail_metadata, custom or {}, identity):
        for key in identity_keys & values.keys():
            if key in declared and declared[key] != values[key]:
                raise ValueError("source_metadata_conflict")
            declared[key] = values[key]
    _declared_identity(declared)
    return declared


def _selected_original(reader, metadata, evidence, *, source_ids, cutoff, is_demo, reads):
    site, url = metadata["source_site"], metadata["source_url"]
    matches = {}
    for entry in evidence:
        if not isinstance(entry, dict) or entry.get("source_url") != url:
            continue
        facts = entry.get("facts") or {}
        if not isinstance(facts, dict) or facts.get("source_site") != site:
            continue
        binding, reference = entry.get("history_binding"), entry.get("record_ref")
        if not isinstance(binding, dict) or not isinstance(reference, dict):
            return {"binding_status": "unbound", "binding_reason": "selected_identity_invalid"}, None
        try:
            identity = _declared_identity(binding)
            raw_hash = binding["raw_sha256"]
            version = binding["version_id"]
            if (
                identity is None
                or not _VERSION.fullmatch(raw_hash)
                or entry.get("source_id") != binding["source_id"]
                or binding.get("is_demo") is not is_demo
                or reference.get("provider") != "data_fabric_history"
                or reference.get("adopted_version") != version
                or reference.get("content_hash") != raw_hash
                or json.loads(reference["object_id"])
                != [binding["source_id"], binding["source_record_id"], is_demo]
            ):
                raise ValueError()
            if binding["source_id"] == "legacy_documents":
                if binding["source_record_id"].split(":", 2)[1] != site:
                    raise ValueError()
            elif binding["source_id"] != site:
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError):
            return {"binding_status": "unbound", "binding_reason": "selected_identity_invalid"}, None
        key = (binding["source_id"], binding["source_record_id"], version, raw_hash, is_demo)
        matches[key] = binding
    if len(matches) != 1:
        reason = "selected_original_not_unique" if matches else "selected_original_not_found"
        return {"binding_status": "unbound", "binding_reason": reason}, None
    binding = next(iter(matches.values()))
    for key in ("source_id", "source_record_id", "version_id", "raw_sha256"):
        if key in metadata and metadata[key] != binding[key]:
            return {"binding_status": "unbound", "binding_reason": "selected_identity_mismatch"}, None
    if "source_uuid" in metadata and (
        binding["source_id"] != "legacy_documents"
        or binding["source_record_id"].split(":", 2)[2] != metadata["source_uuid"]
    ):
        return {"binding_status": "unbound", "binding_reason": "selected_identity_mismatch"}, None
    fixed = dict(metadata)
    fixed.update({key: binding[key] for key in ("source_id", "source_record_id", "version_id", "raw_sha256")})
    result, original = _bind(
        reader, fixed, source_ids=source_ids, cutoff=cutoff, is_demo=is_demo, reads=reads
    )
    if original is not None:
        if original.get("facts", {}).get("source_site") != site:
            return {"binding_status": "unbound", "binding_reason": "source_identity_mismatch"}, None
        result.update(binding_reason="selected_original_by_url", search_version_verified=False)
    return result, original


def _bind(reader, metadata, *, source_ids, cutoff, is_demo, reads):
    """A search hint selects an original; its chunk text and offsets are never adopted."""
    try:
        identity = _declared_identity(metadata)
    except ValueError:
        return {"binding_status": "unbound", "binding_reason": "source_metadata_invalid"}, None
    if identity is None:
        return {"binding_status": "unbound", "binding_reason": "source_identity_missing"}, None
    source_id, native = identity
    version = metadata.get("version_id")
    if source_id not in source_ids:
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
        if expected_hash and original["raw_sha256"] != expected_hash:
            return {"binding_status": "unbound", "binding_reason": "version_hash_mismatch"}, None
        if (
            source_id == "legacy_documents"
            and original.get("facts", {}).get("source_site") != native.split(":", 2)[1]
        ):
            return {"binding_status": "unbound", "binding_reason": "source_identity_mismatch"}, None
        if "source_url" in metadata and original.get("source_url") != metadata["source_url"]:
            return {"binding_status": "unbound", "binding_reason": "source_url_mismatch"}, None
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
    selected_evidence=(),
    cutoff=None,
    is_demo=False,
    limit=3,
    client=None,
    timeout_seconds=None,
    max_tool_calls=None,
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
    budget = {
        "deadline": time.monotonic() + seconds,
        "bytes": 0,
        "max_tool_calls": max_tool_calls,
        "reader": reader,
        "reader_start": getattr(reader, "requests_made", 0),
    }
    try:
        try:
            status, raw = _response(
                session,
                "POST",
                settings.weknora_url + "/api/v1/knowledge-search",
                payload={"query": query, "knowledge_base_ids": settings.weknora_knowledge_base_ids},
                headers={"X-API-Key": settings.weknora_api_key.get_secret_value()},
                budget=budget,
                reads=result,
            )
        except DomainError as exc:
            result.update(status="failed", error_code=exc.code)
            return result
        result["http_status"] = status
        if status != 200:
            result.update(status="failed", error_code="weknora_http_error")
            return result
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
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
            if (
                "knowledge_base_id" in item
                and item["knowledge_base_id"] not in settings.weknora_knowledge_base_ids
            ):
                candidate.update(
                    binding_status="not_authorized", binding_reason="knowledge_base_not_selected"
                )
                result["candidates"].append(candidate)
                continue
            try:
                metadata = _metadata(item)
                direct = _declared_identity(metadata)
            except (ValueError, TypeError, RecursionError):
                binding, original = (
                    {"binding_status": "unbound", "binding_reason": "source_metadata_invalid"},
                    None,
                )
            else:
                arguments = {
                    "source_ids": set(source_ids),
                    "cutoff": cutoff or datetime.now(UTC),
                    "is_demo": is_demo,
                    "reads": result,
                }
                if direct is not None:
                    binding, original = _bind(reader, metadata, **arguments)
                else:
                    try:
                        metadata = _knowledge_identity(session, settings, item, metadata, budget, result)
                        binding, original = _selected_original(
                            reader, metadata, selected_evidence, **arguments
                        )
                    except DomainError as exc:
                        binding, original = {"binding_status": "failed", "binding_reason": exc.code}, None
                    except httpx.HTTPError:
                        binding, original = (
                            {
                                "binding_status": "failed",
                                "binding_reason": "weknora_knowledge_transport_error",
                            },
                            None,
                        )
                    except (ValueError, TypeError, KeyError, RecursionError):
                        binding, original = (
                            {
                                "binding_status": "unbound",
                                "binding_reason": "weknora_knowledge_identity_invalid",
                            },
                            None,
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
        result["usage"]["knowledge_reads"] = result["knowledge_reads"]
        if owned:
            session.close()
