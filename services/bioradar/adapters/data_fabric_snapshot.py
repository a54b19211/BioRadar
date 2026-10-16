"""Bounded document observations from DataFabric, never a CDC or source authority.

Only explicit document UUIDs can be fetched. A downloaded observation can also be
converted offline. Source instance, grants and source-site scope come from the
operator/server, not the upstream body. No attachment or Agent endpoint is used.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from bioradar.api.errors import DomainError
from bioradar.research.mining.models import (
    EngineRecord,
    Locator,
    NormalizedSnapshot,
    ProviderRef,
    RuleReadiness,
    SnapshotCoverage,
    TimePrecision,
    canonical_hash,
)

OBSERVATION_FORMAT = "bioradar.readonly-datafabric.observation.v0.1"
NORMALIZATION_VERSION = "data-fabric.clinicaltrials.upstream-summary.slice.v1"
GAPS = (
    "provider_immutable_version_not_exposed",
    "source_url_not_exposed",
    "upstream_first_seen_at_unknown",
    "upstream_available_at_unknown",
    "publication_time_semantics_unverified",
    "source_independence_unresolved",
    "study_cluster_unresolved",
    "entity_mapping_unresolved",
    "historical_backfill_state_unknown",
    "correction_retraction_state_unknown",
)


@dataclass(frozen=True)
class SnapshotReadLimits:
    max_documents: int = 20
    max_response_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 10 * 1024 * 1024
    request_timeout_seconds: float = 15
    total_timeout_seconds: float = 60

    def __post_init__(self) -> None:
        values = (
            self.max_documents,
            self.max_response_bytes,
            self.max_total_bytes,
            self.request_timeout_seconds,
            self.total_timeout_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Snapshot read limits must be positive and finite")


def _protocol_error() -> DomainError:
    return DomainError("provider_protocol_error", "数据服务文档或观测包格式不一致", 502)


def _document_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise _protocol_error()
    try:
        parsed = UUID(value)
    except ValueError:
        raise _protocol_error() from None
    # Reject noncanonical path spellings and anything that could select /sites.
    if str(parsed) != value:
        raise _protocol_error()
    return value


def _observed_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _protocol_error()
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _protocol_error() from None
    if result.tzinfo is None:
        raise _protocol_error()
    return result


def publication_precision(raw: str) -> TimePrecision:
    """Recognize syntax only; DataFabric's publication-time semantics are unknown."""
    value = raw.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
            return "day"
        if re.fullmatch(r"\d{4}-\d{2}", value):
            date.fromisoformat(value + "-01")
            return "month"
        if re.fullmatch(r"\d{4}", value):
            date.fromisoformat(value + "-01-01")
            return "year"
        if "T" in value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return "instant"
    except ValueError:
        pass
    return "unknown"


def _scope(source_instance: str, authorization_ref: str, allowed_source_sites: set[str]) -> None:
    if not source_instance or not authorization_ref or not allowed_source_sites:
        raise DomainError("source_scope_required", "采用文档需要服务器定义的来源及授权范围", 403)


def _narrative_projection(raw: str, source_site: str) -> tuple[str | None, Locator, tuple[str, ...]]:
    """Reuse one known upstream prose section; never mine raw JSON/module keys.

    This is a versioned adoption projection, not a general cleaner. The source
    locator remains an offset in the original raw_content. Normalized quote
    offsets are independently measured in the exact adopted substring.
    """
    unavailable = Locator(type="unavailable", reason="Verified upstream narrative section not available")
    if source_site != "clinicaltrials":
        return None, unavailable, ("cleaned_text", "source_narrative_projection_not_enabled")
    summary = re.search(r"(?m)^##\s+研究摘要\s*$", raw)
    raw_data = re.search(r"(?m)^##\s+原始数据\s*$", raw)
    if summary is None or raw_data is None or raw_data.start() <= summary.end():
        return None, unavailable, ("cleaned_text", "clinicaltrials_summary_section_missing")
    start, end = summary.end(), raw_data.start()
    while start < end and raw[start].isspace():
        start += 1
    while end > start and raw[end - 1].isspace():
        end -= 1
    # The known upstream formatter inserts a horizontal rule before raw data.
    if raw[:end].endswith("---") and (end == 3 or raw[end - 4] == "\n"):
        end -= 3
        while end > start and raw[end - 1].isspace():
            end -= 1
    text = raw[start:end]
    if not text or text.lstrip().startswith(("{", "[")):
        return None, unavailable, ("cleaned_text", "clinicaltrials_summary_is_not_usable_prose")
    return (
        text,
        Locator(type="offset", start=start, end=end),
        (
            "clinicaltrials_summary_only_not_full_report",
            "raw_json_and_other_source_sections_not_scanned",
        ),
    )


def _record(
    detail: dict[str, Any],
    *,
    source_instance: str,
    authorization_ref: str,
    allowed_source_sites: set[str],
    observed_at: datetime,
    expected_uuid: str,
    call_id: str,
    input_hash: str,
    list_record: dict[str, Any] | None = None,
) -> EngineRecord:
    if not isinstance(detail, dict) or detail.get("uuid") != expected_uuid:
        raise _protocol_error()
    required = (
        "uuid",
        "title",
        "source_site",
        "raw_content",
        "publish_date",
        "doc_status",
        "extraction_status",
    )
    if any(not isinstance(detail.get(key), str) for key in required):
        raise _protocol_error()
    if detail["source_site"] not in allowed_source_sites:
        raise DomainError("source_not_authorized", "文档来源超出当前授权范围", 403)
    if list_record and (list_record.get("uuid"), list_record.get("source_site")) != (
        expected_uuid,
        detail["source_site"],
    ):
        raise _protocol_error()
    raw_text = detail["raw_content"]
    text, source_locator, projection_gaps = _narrative_projection(raw_text, detail["source_site"])
    payload_hash = canonical_hash(detail)
    ref = ProviderRef(
        provider=f"data_fabric:{source_instance}",
        object_type="document",
        object_id=expected_uuid,
        adopted_version=payload_hash,
        version_source="export_hash",
        content_hash=payload_hash,
        authorization_ref=authorization_ref,
    )
    statuses = {
        key: detail[key]
        for key in (
            "doc_status",
            "extraction_status",
            "doc_type",
            "parse_method",
            "language",
            "extraction_model",
        )
        if isinstance(detail.get(key), str)
    }
    statuses.update({"provider_call_id": call_id, "provider_input_hash": input_hash})
    if list_record and isinstance(list_record.get("updated_at"), str):
        statuses["list_updated_at_unbound_to_content"] = list_record["updated_at"]
    available = ["document_hash", "source_id", "record_ref", "published_time_raw", "upstream_status"]
    usable_text = text is not None
    if usable_text:
        available.extend(("normalized_text", "locator"))
    return EngineRecord(
        record_ref=ref,
        evidence_refs=(ref,),
        document_hash=hashlib.sha256(raw_text.encode()).hexdigest(),
        source_instance=source_instance,
        source_id=detail["source_site"],
        observed_at=observed_at,
        published_time_raw=detail["publish_date"],
        time_precision=publication_precision(detail["publish_date"]),
        # Syntax does not prove that publish_date means first public availability.
        published_at=None,
        publication_time_verified=False,
        first_seen_at=None,
        available_at=None,
        ingestion_mode="sample_export",
        text_status="inline_authorized" if usable_text else "unavailable",
        normalized_text=text if usable_text else None,
        normalization_version=NORMALIZATION_VERSION if usable_text else None,
        locator=source_locator,
        missing_fields=GAPS + projection_gaps + (() if usable_text else ("normalized_text",)),
        available_fields=tuple(sorted(available)),
        upstream_status=statuses,
        rule_readiness=tuple(
            RuleReadiness(
                rule=rule,
                state="exploratory_only" if rule == "R4" and usable_text else "unsupported",
                reasons=("Text only; identity, temporal facts and independent units unresolved",),
            )
            for rule in ("R1", "R2", "R3", "R4")
        ),
    )


def _snapshot(
    records: list[EngineRecord],
    *,
    source_instance: str,
    authorization_ref: str,
    allowed_source_sites: set[str],
    observed_at: datetime,
    snapshot_id: str | None,
) -> NormalizedSnapshot:
    records.sort(key=lambda r: r.record_ref.version_key)
    content = {
        "schema_version": "bioradar.engine.snapshot.v1",
        "normalization_version": NORMALIZATION_VERSION,
        "publication_time_verified": False,
        "metadata_gaps": list(GAPS),
        "source_instance": source_instance,
        "authorization_ref": authorization_ref,
        "allowed_source_sites": sorted(allowed_source_sites),
        "records": [
            {
                "ref": r.record_ref.model_dump(mode="json"),
                "normalization_version": r.normalization_version,
                "normalized_text_hash": r.normalized_text_hash,
                "source_locator": r.locator.model_dump(mode="json"),
            }
            for r in records
        ],
    }
    return NormalizedSnapshot(
        snapshot_id=snapshot_id or "data-fabric-" + canonical_hash(content)[:24],
        is_demo=False,
        source_instance=source_instance,
        coverage_mode="bounded_sample",
        observed_at=observed_at,
        records=tuple(records),
        coverage=tuple(
            SnapshotCoverage(
                source_instance=source_instance,
                source_id=site,
                mode="bounded_sample",
                scope_description="Explicit UUID observation manifest; allowed sites="
                + ",".join(sorted(allowed_source_sites)),
                record_count=count,
                is_consistent_snapshot=False,
                reasons=("No provider snapshot token, CDC cursor or complete denominator",),
            )
            for site, count in sorted(Counter(r.source_id for r in records).items())
        ),
        metadata_gaps=tuple(
            sorted(
                {
                    *GAPS,
                    "bounded_uuid_observation_not_cdc_or_full_inventory",
                    *(gap for row in records for gap in row.missing_fields),
                }
            )
        ),
    )


def load_data_fabric_observation(
    document: dict[str, Any],
    *,
    source_instance: str,
    authorization_ref: str,
    allowed_source_sites: set[str],
    snapshot_id: str | None = None,
    limits: SnapshotReadLimits = SnapshotReadLimits(),
) -> NormalizedSnapshot:
    """Offline conversion of a bounded captured GET package; no network calls."""
    _scope(source_instance, authorization_ref, allowed_source_sites)
    if not isinstance(document, dict) or document.get("sample_format") != OBSERVATION_FORMAT:
        raise _protocol_error()
    entries = document.get("documents")
    if not isinstance(entries, list) or not entries or len(entries) > limits.max_documents:
        raise DomainError("invalid_observation_manifest", "观测包需要非空且有界的显式文档清单", 422)
    seen: set[str] = set()
    records: list[EngineRecord] = []
    total_bytes = 0
    try:
        for entry in entries:
            if not isinstance(entry, dict):
                raise _protocol_error()
            doc_uuid = _document_uuid(entry.get("document_uuid"))
            if doc_uuid in seen:
                raise DomainError("duplicate_document", "观测清单包含重复文档", 422)
            seen.add(doc_uuid)
            detail = entry.get("detail")
            observation = entry.get("detail_observation", {})
            if not isinstance(observation, dict) or (
                observation.get("method"),
                observation.get("path"),
                observation.get("status_code"),
                observation.get("outcome"),
            ) != ("GET", "/api/documents/" + doc_uuid, 200, "available"):
                raise DomainError("provider_incomplete", "文档读取尚未取得完整成功结果", 503)
            size = len(json.dumps(detail, ensure_ascii=False, allow_nan=False).encode())
            total_bytes += size
            if size > limits.max_response_bytes or total_bytes > limits.max_total_bytes:
                raise DomainError("provider_response_too_large", "文档观测超出读取字节上限", 502)
            if observation.get("canonical_payload_sha256") != canonical_hash(detail):
                raise DomainError("provider_content_mismatch", "文档内容与导出观测哈希不一致", 502)
            request_hash = canonical_hash({"path": "/api/documents/" + doc_uuid, "params": {}})
            if (
                not isinstance(observation.get("call_id"), str)
                or not observation["call_id"]
                or (observation.get("input_hash") != request_hash)
            ):
                raise DomainError("provider_call_mismatch", "文档观测与读取调用不一致", 502)
            records.append(
                _record(
                    detail,
                    source_instance=source_instance,
                    authorization_ref=authorization_ref,
                    allowed_source_sites=allowed_source_sites,
                    observed_at=_observed_at(observation.get("observed_at")),
                    expected_uuid=doc_uuid,
                    call_id=observation["call_id"],
                    input_hash=request_hash,
                    list_record=entry.get("list_record"),
                )
            )
        calls = document.get("calls")
        if (
            not isinstance(calls, list)
            or not calls
            or len(calls) > len(entries) + 1
            or any(
                not isinstance(call, dict)
                or call.get("method") != "GET"
                or call.get("path") not in {"/api/documents", *("/api/documents/" + value for value in seen)}
                or call.get("outcome") != "available"
                or call.get("status_code") != 200
                for call in calls
            )
        ):
            raise DomainError("provider_incomplete", "观测包包含未允许或未完成的上游读取", 503)
        ledger = {call.get("call_id"): canonical_hash(call) for call in calls}
        if any(
            ledger.get(entry["detail_observation"]["call_id"]) != canonical_hash(entry["detail_observation"])
            for entry in entries
        ):
            raise DomainError("provider_call_mismatch", "文档观测缺少一致的调用记录", 502)
        return _snapshot(
            records,
            source_instance=source_instance,
            authorization_ref=authorization_ref,
            allowed_source_sites=allowed_source_sites,
            observed_at=_observed_at(document.get("finished_at")),
            snapshot_id=snapshot_id,
        )
    except (TypeError, ValueError, ValidationError):
        raise _protocol_error() from None


class DataFabricSnapshotReader:
    """Fetch explicit UUIDs from a server-configured client; errors never become []."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        source_instance: str,
        authorization_ref: str,
        allowed_source_sites: set[str],
        limits: SnapshotReadLimits = SnapshotReadLimits(),
    ):
        _scope(source_instance, authorization_ref, allowed_source_sites)
        if (
            client.base_url.scheme not in {"http", "https"}
            or client.base_url.username
            or client.base_url.password
            or client.base_url.path != "/"
            or client.base_url.query
            or client.base_url.fragment
        ):
            raise DomainError("invalid_provider_configuration", "数据服务地址需由服务器安全配置", 422)
        self.client = client
        self.source_instance = source_instance
        self.authorization_ref = authorization_ref
        self.allowed_source_sites = set(allowed_source_sites)
        self.limits = limits

    def read_snapshot(
        self, explicit_uuids: list[str], *, snapshot_id: str | None = None
    ) -> NormalizedSnapshot:
        if not explicit_uuids or len(explicit_uuids) > self.limits.max_documents:
            raise DomainError("invalid_document_manifest", "需提供数量受限的显式文档UUID清单", 422)
        uuids = [_document_uuid(value) for value in explicit_uuids]
        if len(set(uuids)) != len(uuids):
            raise DomainError("duplicate_document", "文档清单包含重复UUID", 422)
        deadline = time.monotonic() + self.limits.total_timeout_seconds
        records: list[EngineRecord] = []
        total_bytes = 0
        for doc_uuid in sorted(uuids):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainError("provider_timeout", "文档读取超过总时长，未采用不完整结果", 503, True)
            chunks: list[bytes] = []
            response_bytes = 0
            try:
                with self.client.stream(
                    "GET",
                    "/api/documents/" + doc_uuid,
                    follow_redirects=False,
                    timeout=min(self.limits.request_timeout_seconds, remaining),
                ) as response:
                    if response.status_code != 200:
                        raise DomainError("provider_unavailable", "文档读取失败，未取得完整证据包", 503, True)
                    for chunk in response.iter_bytes():
                        response_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if (
                            response_bytes > self.limits.max_response_bytes
                            or total_bytes > self.limits.max_total_bytes
                        ):
                            raise DomainError("provider_response_too_large", "文档响应超过读取字节上限", 502)
                        if time.monotonic() > deadline:
                            raise DomainError(
                                "provider_timeout", "文档读取超过总时长，未采用不完整结果", 503, True
                            )
                        chunks.append(chunk)
                detail = json.loads(b"".join(chunks))
                records.append(
                    _record(
                        detail,
                        source_instance=self.source_instance,
                        authorization_ref=self.authorization_ref,
                        allowed_source_sites=self.allowed_source_sites,
                        observed_at=datetime.now(UTC),
                        expected_uuid=doc_uuid,
                        call_id=str(uuid4()),
                        input_hash=canonical_hash({"path": "/api/documents/" + doc_uuid, "params": {}}),
                    )
                )
            except httpx.TimeoutException:
                raise DomainError("provider_timeout", "文档读取超时，未取得完整证据包", 503, True) from None
            except httpx.HTTPError:
                raise DomainError(
                    "provider_unavailable", "文档读取失败，未取得完整证据包", 503, True
                ) from None
            except (TypeError, ValueError, ValidationError):
                raise _protocol_error() from None
        return _snapshot(
            records,
            source_instance=self.source_instance,
            authorization_ref=self.authorization_ref,
            allowed_source_sites=self.allowed_source_sites,
            observed_at=datetime.now(UTC),
            snapshot_id=snapshot_id,
        )
