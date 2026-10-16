"""Adopt the supported V1 data.batch subset through the existing validator.

Correlation and authorization are supplied by the caller's trusted call record.
The provider's authorization fields and is_demo flag never grant permission.
Referenced evidence is joined by its entire immutable ref, never array position.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bioradar.adapters.exchange import ExchangeValidator
from bioradar.api.errors import DomainError
from bioradar.research.mining.models import EngineRecord, NormalizedSnapshot, SnapshotCoverage, canonical_hash

REF_KEYS = {
    "provider",
    "object_type",
    "object_id",
    "adopted_version",
    "version_source",
    "content_hash",
    "authorization_ref",
}


def _references(value: Any):
    if isinstance(value, dict):
        if REF_KEYS.issubset(value):
            yield value
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _ref_key(ref: dict) -> tuple[str, str, str, str]:
    return ref["provider"], ref["object_type"], ref["object_id"], ref["adopted_version"]


def _sha256_matches(text: str, declared: str) -> bool | None:
    """Opaque provider hashes are retained, never presented as locally verified."""
    candidate = declared.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
        return hashlib.sha256(text.encode()).hexdigest() == candidate.lower()
    return None


def normalize_provider_batch(
    document: dict[str, Any],
    *,
    expected_call_id: str,
    expected_input_hash: str,
    authorization: dict[str, str],
    source_instance: str,
    allowed_source_ids: set[str],
    contract_dir: Path = Path("contracts"),
    allow_demo: bool = False,
    snapshot_id: str | None = None,
    received_at: datetime | None = None,
) -> NormalizedSnapshot:
    """Convert complete scoped batches; received_at can freeze a captured local receipt.

    Every record is observed by Radar at the trusted local received_at. Provider
    observations remain upstream metadata and never override the local receipt.
    Replay supplies the captured receipt; it is never provider available_at.
    """
    if not source_instance or not allowed_source_ids or not authorization.get("grant_ref"):
        raise DomainError("source_scope_required", "导入需要服务器定义的来源及授权范围", 403)
    checked = ExchangeValidator(contract_dir).validate(
        document,
        call_id=expected_call_id,
        capability="data.batch",
        input_hash=expected_input_hash,
    )
    received_at = received_at or datetime.now(UTC)
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise DomainError("observation_time_invalid", "本地采用观测时间需要时区", 422)
    if checked["request"]["authorization"] != authorization:
        raise DomainError("provider_authorization_mismatch", "供应方结果与本次授权范围不一致", 403)
    if checked["is_demo"] and not allow_demo:
        raise DomainError("fixture_not_allowed", "测试示例不能进入真实材料采用", 403)
    if checked["execution"]["status"] != "succeeded":
        raise DomainError("provider_incomplete", "供应方执行尚未完整成功，不能采用为完成结果", 503)
    if checked["result"]["outcome"] not in {"available", "empty"}:
        raise DomainError("provider_not_evaluated", "供应方未提供可采用分析结果", 503)
    payload = checked["result"]["payload"]
    if any(ref["authorization_ref"] != authorization["grant_ref"] for ref in _references(payload)):
        raise DomainError("provider_authorization_mismatch", "供应方引用超出本次授权范围", 403)
    rows = payload["analysis_records"]
    changes = payload["change_events"]
    for event in changes:
        if event["kind"] in {"permission_changed", "identity_corrected"}:
            raise DomainError(
                "provider_change_requires_reconciliation", "来源授权或身份变更需要先核对，不能仅冻结记录", 409
            )
        if event["kind"] in {"withdrawn", "deleted"}:
            expected_flag = "retracted" if event["kind"] == "withdrawn" else "deleted"
            matches = [
                row for row in rows if canonical_hash(row["record_ref"]) == canonical_hash(event["subject"])
            ]
            if not matches or any(expected_flag not in row["record_flags"] for row in matches):
                raise DomainError(
                    "provider_change_requires_reconciliation", "撤回或删除事件尚未绑定匹配版本及状态标记", 409
                )
            recorded_at = datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00"))
            for row in matches:
                available_at = row["available_at"]
                if (
                    available_at is None
                    or datetime.fromisoformat(available_at.replace("Z", "+00:00")) < recorded_at
                ):
                    raise DomainError(
                        "provider_change_requires_reconciliation", "撤回或删除版本的可见时间尚未完成核对", 409
                    )
    if not rows and payload["records"]:
        raise DomainError("provider_analysis_unavailable", "证据包尚未提供引擎所需分析记录", 503)
    if checked["result"]["outcome"] == "empty" and (rows or payload["records"]):
        raise DomainError("provider_protocol_error", "空结果声明与供应方材料不一致", 502)
    if checked["result"]["outcome"] == "available" and not rows:
        raise DomainError("provider_protocol_error", "可用结果缺少分析记录，不能改成空成功", 502)
    evidence: dict[tuple[str, str, str, str], dict] = {}
    for item in payload["records"]:
        key = _ref_key(item["ref"])
        if key in evidence and canonical_hash(evidence[key]) != canonical_hash(item):
            raise DomainError("provider_version_conflict", "同一证据版本包含不一致内容", 502)
        match = _sha256_matches(item["quote"], item["quote_hash"])
        if match is False:
            raise DomainError("provider_quote_hash_mismatch", "证据引用文本与哈希不一致", 502)
        evidence[key] = item
    records: list[EngineRecord] = []
    identities: dict[tuple[str, str, str, str], str] = {}
    gaps = {"provider_coverage_not_verified", "provider_normalization_original_offset_map_not_supplied"}
    if changes:
        gaps.add("provider_change_events_not_consumed_as_cdc")
    if any(event["kind"] in {"withdrawn", "deleted"} for event in changes):
        gaps.add("provider_source_change_requires_business_impact_reconciliation")
    try:
        for row in rows:
            if row["source_id"] not in allowed_source_ids:
                raise DomainError("source_not_authorized", "分析记录来源超出当前授权范围", 403)
            key = _ref_key(row["record_ref"])
            content_hash = canonical_hash(row)
            if key in identities:
                if identities[key] != content_hash:
                    raise DomainError("provider_version_conflict", "同一记录版本包含不一致分析内容", 502)
                continue
            identities[key] = content_hash
            linked = []
            source_refs = [row["record_ref"], *row["lineage_refs"]]
            for ref in row["evidence_refs"]:
                item = evidence.get(_ref_key(ref))
                if item is None or canonical_hash(item["ref"]) != canonical_hash(ref):
                    raise DomainError("provider_evidence_unavailable", "分析引用未提供匹配版本的证据", 502)
                if not any(canonical_hash(item["source_record"]) == canonical_hash(s) for s in source_refs):
                    raise DomainError(
                        "provider_evidence_mismatch", "证据原文版本与分析记录或其来源不一致", 502
                    )
                if row["text_status"] == "inline_authorized" and item["quote"]:
                    if item["quote"] not in row["normalized_text"]:
                        raise DomainError(
                            "provider_evidence_mismatch", "引用片段未在该版本授权文本中找到", 502
                        )
                if _sha256_matches(item["quote"], item["quote_hash"]) is None:
                    gaps.add("provider_quote_hash_algorithm_unverified")
                linked.append(item)
            if not linked:
                gaps.add("provider_observation_missing_using_local_received_at")
            source_urls = {e["source_url"] for e in linked if e["source_url"]}
            lineage = [*row["lineage_refs"], payload["coverage_ref"]]
            if checked["raw_result_ref"] is not None:
                lineage.append(checked["raw_result_ref"])
            # V1 normalized_text has no normalization algorithm or raw-offset map.
            data = dict(row)
            data.update(
                {
                    "source_instance": source_instance,
                    "observed_at": received_at,
                    "publication_time_verified": row["published_at"] is not None,
                    "source_url": next(iter(source_urls)) if len(source_urls) == 1 else None,
                    "ingestion_mode": "fixture" if checked["is_demo"] else "sample_export",
                    "normalization_version": "provider.data.batch.normalized-text.v1"
                    if row["text_status"] == "inline_authorized"
                    else None,
                    "lineage_refs": list({canonical_hash(r): r for r in lineage}.values()),
                    "upstream_status": {
                        "provider_call_id": expected_call_id,
                        "provider_input_hash": expected_input_hash,
                        "provider_name": checked["provider"]["name"],
                        "provider_version": checked["provider"]["version"],
                        "provider_verification": checked["provider"]["verification"],
                        "provider_evidence_observed_at_by_ref": json.dumps(
                            {canonical_hash(e["ref"]): e["observed_at"] for e in linked},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "provider_change_events_hash": canonical_hash(sorted(changes, key=canonical_hash)),
                        "provider_change_event_count": str(len(changes)),
                    },
                }
            )
            records.append(EngineRecord.model_validate(data))
        records.sort(key=lambda record: record.record_ref.version_key)
        return NormalizedSnapshot(
            snapshot_id=snapshot_id
            or "provider-batch-"
            + canonical_hash(
                {
                    "source_instance": source_instance,
                    "is_demo": checked["is_demo"],
                    "received_at": received_at.isoformat(),
                    "call_id": expected_call_id,
                    "input_hash": expected_input_hash,
                    "payload_hash": canonical_hash(payload),
                }
            )[:24],
            is_demo=checked["is_demo"],
            source_instance=source_instance,
            observed_at=received_at,
            coverage_mode="manual_package",
            records=tuple(records),
            coverage=tuple(
                SnapshotCoverage(
                    source_instance=source_instance,
                    source_id=source,
                    mode="manual_package",
                    scope_description="Explicit V1 records; upstream coverage_ref retained as lineage",
                    record_count=count,
                    reasons=("Coverage completeness and stable incremental cursor not verified by import",),
                )
                for source, count in sorted(Counter(record.source_id for record in records).items())
            ),
            metadata_gaps=tuple(sorted(gaps)),
        )
    except (ValueError, TypeError, ValidationError):
        raise DomainError("provider_protocol_error", "供应方分析记录不符合引擎采用模型", 502) from None
