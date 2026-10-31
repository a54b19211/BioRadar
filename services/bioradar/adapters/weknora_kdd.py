"""Fixed-version WeKnora retrieval and vector features; Fabric stays the original authority."""

from __future__ import annotations

import copy
import hashlib
import json
import time

import httpx

from bioradar.api.errors import DomainError
from bioradar.research.mining.models import (
    Embedding,
    NormalizedSnapshot,
    ProviderRef,
    SectionProvenance,
    canonical_hash,
)

CONTRACT = "weknora-kdd-v1"
FEATURE_VERSION = "bioradar.weknora.features.v1"
MAX_RESPONSE_BYTES = 16_777_216


def retrieval_scope(settings, *, direction, is_demo=False):
    """Resolve the actual KB authorization scope for both retrieval and feature reads."""
    real = getattr(settings, "weknora_kdd_knowledge_base_id", None)
    demo = getattr(settings, "weknora_kdd_demo_knowledge_base_id", None)
    selected = demo if is_demo else real
    if not selected or real and demo and real == demo:
        raise DomainError("weknora_kdd_not_configured", "真实与演示检索须使用各自明确的知识库", 422)
    configured = getattr(settings, "weknora_kdd_source_scope", None)
    scope = (
        getattr(settings, "weknora_kdd_demo_source_scope", "simulation")
        if is_demo
        else configured if configured is not None else direction
    )
    if not isinstance(scope, str) or not scope.strip():
        raise DomainError("weknora_kdd_not_configured", "检索授权范围未明确", 422)
    return {"knowledge_base_id": selected, "source_scope": scope, "is_demo": is_demo}


def search_status(settings, *, direction, is_demo=False):
    try:
        scope = retrieval_scope(settings, direction=direction, is_demo=is_demo)
    except DomainError as exc:
        return {"available": False, "is_demo": is_demo, "error_code": exc.code}
    available = bool(
        getattr(settings, "weknora_kdd_url", None) and getattr(settings, "weknora_kdd_token", None)
    )
    return {"available": available, **scope}


def _member(record, *, is_demo=False):
    ref = record.record_ref
    try:
        source, native, demo = json.loads(ref.object_id)
    except (ValueError, TypeError):
        raise DomainError("weknora_identity_missing", "向量成员须有固定 Fabric 原件身份", 422) from None
    if source != record.source_id or demo is not is_demo or ref.provider != "data_fabric_history":
        raise DomainError(
            "weknora_identity_missing", "向量成员须为同一真实或演示范围的已采用 Fabric 固定版本", 422
        )
    return {
        "source_instance": record.source_instance,
        "source_id": source,
        "source_record_id": native,
        "version_id": ref.adopted_version,
        "raw_sha256": ref.content_hash,
    }


def _identity(value):
    return tuple(
        value.get(key)
        for key in ("source_instance", "source_id", "source_record_id", "version_id", "raw_sha256")
    )


def _post(settings, path, body, *, timeout_seconds=30, client=None):
    url = getattr(settings, "weknora_kdd_url", None)
    token = getattr(settings, "weknora_kdd_token", None)
    if not url or not token:
        raise DomainError("weknora_kdd_not_configured", "未配置固定版本检索与向量服务", 503)
    if timeout_seconds <= 0:
        raise DomainError("analysis_time_budget", "发送前研究时限已结束", 409)
    own = client is None
    session = client or httpx.Client(trust_env=False)
    deadline = time.monotonic() + timeout_seconds
    try:
        with session.stream(
            "POST",
            url + path,
            json=body,
            headers={"Authorization": "Bearer " + token.get_secret_value()},
            timeout=httpx.Timeout(timeout_seconds, connect=min(5, timeout_seconds)),
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise DomainError(
                    "weknora_kdd_http_error",
                    "固定版本检索或向量读取失败",
                    502,
                    response.status_code >= 500,
                )
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    raise DomainError("weknora_kdd_response_budget", "向量读取超过时间或响应大小限制", 422)
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("contract_version") != CONTRACT:
            raise ValueError()
        return value
    except httpx.HTTPError:
        raise DomainError(
            "weknora_kdd_transport_unconfirmed", "检索或向量请求的返回结果未知，未自动重发", 502
        ) from None
    except (ValueError, TypeError):
        raise DomainError("weknora_kdd_response_invalid", "检索或向量回执格式无效，未采用结果", 502) from None
    finally:
        if own:
            session.close()


def features_for_snapshot(settings, snapshot, *, direction, timeout_seconds=30, client=None):
    """Only exact member/text/version matches can receive an embedding.

    An incomplete feature response preserves every original record with an explicit
    missing feature reason; it never narrows the declared analysis population.
    """
    snapshot = NormalizedSnapshot.model_validate(snapshot)
    scope = retrieval_scope(settings, direction=direction, is_demo=snapshot.is_demo)
    kb, source_scope = scope["knowledge_base_id"], scope["source_scope"]
    if not 1 <= len(snapshot.records) <= 100:
        raise DomainError("weknora_feature_scope", "向量读取需明确 1 至 100 个固定成员", 422)
    members = [_member(record, is_demo=snapshot.is_demo) for record in snapshot.records]
    if len({_identity(x) for x in members}) != len(members):
        raise DomainError("weknora_feature_scope", "固定成员重复，未把版本重复计入向量样本", 422)
    response = _post(
        settings,
        "/api/v1/kdd/features",
        {
            "knowledge_base_id": kb, "source_scope": source_scope,
            "members": members, "include_chunks": False,
            **({"is_demo": True} if snapshot.is_demo else {}),
        },
        timeout_seconds=timeout_seconds,
        client=client,
    )
    if (
        response.get("requested_count") != len(members)
        or not isinstance(response.get("features"), list)
        or not isinstance(response.get("excluded"), list)
        or response.get("population") != "explicit_members_only"
    ):
        raise DomainError("weknora_feature_scope", "向量回执缺少实际成员范围", 502)
    expected = {_identity(value) for value in members}
    features = {}
    for feature in response["features"]:
        if not isinstance(feature, dict) or not isinstance(feature.get("identity"), dict):
            raise DomainError("weknora_feature_identity", "向量回执缺少固定成员身份", 502)
        key = _identity(feature["identity"])
        if (
            key not in expected or key in features or feature.get("source_scope") != source_scope
            or feature.get("is_demo", False) is not snapshot.is_demo
        ):
            raise DomainError("weknora_feature_identity", "向量回执含重复、越界或不匹配成员", 502)
        features[key] = feature
    if response.get("returned_count") != len(features):
        raise DomainError("weknora_feature_scope", "向量回执数量与实际返回不符", 502)
    adopted, excluded, records = [], [], []
    for record, member in zip(snapshot.records, members, strict=True):
        feature = features.get(_identity(member))
        reason = None
        if feature is None:
            reason = "feature_not_returned"
        elif feature.get("status") != "ready":
            reason = "feature_" + str(feature.get("status", "unavailable"))
        elif feature.get("text") != record.normalized_text:
            reason = "feature_text_differs_from_fixed_analysis_text"
        elif feature.get("content_sha256") != hashlib.sha256(feature["text"].encode()).hexdigest():
            reason = "feature_text_hash_mismatch"
        else:
            provenance = feature.get("embedding_provenance") or {}
            if (
                provenance.get("verified") is not True
                or not provenance.get("model_version")
                or not provenance.get("model_id")
                or any(
                    not isinstance(feature.get(key), str) or not feature[key].strip()
                    for key in ("knowledge_id", "publication_id", "index_version", "indexed_text_sha256")
                )
                or type(feature.get("chunk_count")) is not int
                or type(feature.get("vector_count")) is not int
                or not 0 < feature["vector_count"] <= feature["chunk_count"]
                or feature.get("aggregation") != "mean_l2_normalized_enabled_text_chunks_v1"
                or not isinstance(feature.get("chunking"), dict)
            ):
                reason = "feature_model_or_aggregation_unverified"
        if reason is not None:
            excluded.append({"member": member, "reason": reason})
            records.append(record)
            continue
        provenance = feature["embedding_provenance"]
        try:
            model = {
                "provider": provenance["provider"],
                "model_id": provenance["model_id"],
                "model_version": provenance["model_version"],
                "artifact_version": provenance.get("artifact_version"),
                "dimension": provenance["dimension"],
                "aggregation": feature["aggregation"],
                "chunking": feature["chunking"],
            }
            feature_ref = ProviderRef.model_validate(
                {
                    **record.record_ref.model_dump(mode="json"),
                    "provider": "weknora_kdd",
                    "object_type": "document_embedding",
                    "object_id": feature["knowledge_id"],
                    "adopted_version": feature["index_version"],
                    "content_hash": canonical_hash(
                        {"identity": member, "model": model, "vector": feature["embedding"]}
                    ),
                }
            )
            embedding = Embedding(
                provider="weknora_kdd",
                model_version=canonical_hash(model),
                dimension=provenance["dimension"],
                vector=feature["embedding"],
                text_hash=feature["content_sha256"],
                provenance=SectionProvenance(schema_version=FEATURE_VERSION, provider_ref=feature_ref),
            )
        except (ValueError, TypeError, KeyError):
            excluded.append({"member": member, "reason": "feature_vector_invalid"})
            records.append(record)
            continue
        status = dict(record.upstream_status)
        status.update(
            weknora_index_version=feature["index_version"], embedding_aggregation=feature["aggregation"]
        )
        records.append(record.model_copy(update={"embedding": embedding, "upstream_status": status}))
        adopted.append(
            {
                "member": member,
                "knowledge_id": feature["knowledge_id"],
                "publication_id": feature["publication_id"],
                "index_version": feature["index_version"],
                "content_sha256": feature["content_sha256"],
                "indexed_text_sha256": feature["indexed_text_sha256"],
                "model": model,
                "model_fingerprint": embedding.model_version,
                "chunk_count": feature["chunk_count"],
                "vector_count": feature["vector_count"],
            }
        )
    result = snapshot.model_copy(
        update={
            "snapshot_id": snapshot.snapshot_id + "-vectors-" + canonical_hash(adopted)[:12],
            "records": tuple(records),
            "metadata_gaps": tuple(
                sorted(
                    set(snapshot.metadata_gaps)
                    | {
                        "weknora_vector_features_explicit_members_only",
                        *("weknora_feature:" + row["reason"] for row in excluded),
                    }
                )
            ),
        }
    )
    return result, {
        "schema_version": FEATURE_VERSION,
        **scope,
        "requested_members": members,
        "scope_fingerprint": response.get("scope_fingerprint"),
        "adopted": adopted,
        "excluded": excluded,
        "provider_exclusions": response["excluded"],
        "population": "explicit_members_only",
        "statistical_unit": "document_version",
        "method": "versioned_mean_document_vectors_not_independent_research_counts",
        "tool_requests": 1,
        "model_token_usage": None,
    }


def _search_candidate_identifiers(item):
    """Preserve bounded provider identifiers for diagnosis, never search text or metadata."""
    if not isinstance(item, dict):
        return {}
    fields = ("source_instance", "source_id", "source_record_id", "version_id", "raw_sha256")
    identity = item.get("identity")
    result = {}
    if isinstance(identity, dict):
        result["identity"] = {
            key: value for key in fields
            if isinstance(value := identity.get(key), str)
            and 0 < len(value) <= 400 and not any(ord(character) < 32 for character in value)
        }
    for key in (
        "id", "candidate_id", "knowledge_id", "publication_id", "chunk_id", "index_version", "source_scope"
    ):
        value = item.get(key)
        if isinstance(value, str) and 0 < len(value) <= 400 and not any(ord(c) < 32 for c in value):
            result[key] = value
    if type(item.get("is_demo")) is bool:
        result["is_demo"] = item["is_demo"]
    return result


def search_candidates(
    settings,
    query,
    *,
    direction,
    reader,
    source_ids,
    cutoff,
    is_demo=False,
    limit=3,
    timeout_seconds=30,
    max_tool_calls=64,
    selected_evidence=(),
    client=None,
):
    """Search actual KB candidates, then read and validate the exact Fabric originals."""
    from bioradar.adapters.weknora import _bind

    scope = retrieval_scope(settings, direction=direction, is_demo=is_demo)
    kb, source_scope = scope["knowledge_base_id"], scope["source_scope"]
    if max_tool_calls < 1:
        raise DomainError("weknora_kdd_not_configured", "检索范围或工具预算不足", 422)
    try:
        found = _post(
            settings,
            "/api/v1/kdd/search",
            {
                "knowledge_base_id": kb,
                "source_scope": source_scope,
                **({"is_demo": True} if is_demo else {}),
                "query_text": query,
                "top_k": min(max(limit, 1), 20),
                "rerank": True,
            },
            timeout_seconds=timeout_seconds,
            client=client,
        )
    except DomainError as exc:
        not_sent = exc.code in {"weknora_kdd_not_configured", "analysis_time_budget"}
        unknown = exc.code == "weknora_kdd_transport_unconfirmed"
        return {
            "status": "unconfirmed" if unknown else "failed",
            "candidates": [],
            "records": [],
            "tool_requests": 0 if not_sent else 1,
            "error_code": exc.code,
            "usage": {
                "request_status": "not_sent" if not_sent else "unconfirmed" if unknown else "failed",
                "embedding_rerank_tokens": None,
                "scope": source_scope,
                "knowledge_base_id": kb,
            },
        }
    values = found.get("results")
    if not isinstance(values, list) or len(values) > min(max(limit, 1), 20):
        raise DomainError("weknora_kdd_response_invalid", "检索候选范围不符合请求", 502)
    candidates, records = [], []
    reads = {"lookup_requests": 0, "version_reads": 0, "knowledge_reads": 0, "tool_requests": 1}
    before = getattr(reader, "requests_made", 0)
    if reader is not None:
        composite_limit = before + max(0, max_tool_calls - 1)
        previous_limit = getattr(reader, "max_requests", None)
        reader.max_requests = (
            composite_limit if previous_limit is None else min(previous_limit, composite_limit)
        )
    expected_instance = (
        (getattr(reader, "demo_source_instance", None) or "fixture")
        if is_demo else getattr(reader, "source_instance", None)
    )
    for item in values:
        identity = item.get("identity") if isinstance(item, dict) else None
        if (
            not isinstance(identity, dict)
            or identity.get("source_instance") != expected_instance
            or item.get("source_scope") != source_scope
            or item.get("is_demo", False) is not is_demo
            or not all(identity.get(k) for k in ("source_id", "source_record_id", "version_id", "raw_sha256"))
        ):
            mismatches = []
            if not isinstance(identity, dict):
                mismatches.append("identity_missing")
            else:
                if not identity.get("source_instance"):
                    mismatches.append("source_instance_missing")
                elif identity["source_instance"] != expected_instance:
                    mismatches.append("source_instance_mismatch")
                for key in ("source_id", "source_record_id", "version_id", "raw_sha256"):
                    if not identity.get(key):
                        mismatches.append(key + "_missing")
            if isinstance(item, dict):
                if item.get("source_scope") != source_scope:
                    mismatches.append("source_scope_mismatch")
                if item.get("is_demo", False) is not is_demo:
                    mismatches.append("is_demo_mismatch")
            candidates.append({
                "binding_status": "unbound", "binding_reason": "fixed_source_identity_missing",
                "binding_mismatch_codes": mismatches, **_search_candidate_identifiers(item),
            })
            continue
        candidate, original = _bind(
            reader,
            identity,
            source_ids=source_ids,
            cutoff=cutoff,
            is_demo=is_demo,
            reads=reads,
        )
        candidate.update(_search_candidate_identifiers(item))
        if type(item.get("score")) in (int, float):
            candidate["score"] = item["score"]
        if candidate.get("binding_status") != "bound":
            candidate["binding_mismatch_codes"] = [candidate["binding_reason"]]
        candidates.append(candidate)
        if original is not None and not any(x["version_id"] == original["version_id"] for x in records):
            records.append(original)
    execution = found.get("execution") or {}
    degraded = bool(execution.get("degraded"))
    return {
        "status": "completed" if records else "empty" if not values and not degraded else "unavailable",
        "degraded": degraded,
        "excluded": copy.deepcopy(found.get("excluded", [])),
        "candidates": candidates,
        "records": records,
        "tool_requests": 1,
        "usage": {
            "actual_execution": found.get("execution"),
            "embedding_rerank_tokens": None,
            "scope": source_scope,
            "knowledge_base_id": kb,
        },
        "identity_reads": reads,
        "error_code": (
            None
            if records or not values and not degraded
            else "weknora_index_coverage_unverified"
            if degraded
            else "weknora_candidates_not_bound"
        ),
    }
