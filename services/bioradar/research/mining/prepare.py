"""Deterministic adoption, availability and contribution preparation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bioradar.research.mining.models import (
    EngineRecord,
    ExcludedRecord,
    MiningPolicy,
    NormalizedSnapshot,
    ProviderRef,
    SectionProvenance,
    TimeExpression,
    canonical_hash,
)


def ref_key(ref: ProviderRef) -> str:
    return json.dumps(ref.identity_key, ensure_ascii=False, separators=(",", ":"))


def version_key(ref: ProviderRef) -> str:
    return json.dumps(ref.version_key, ensure_ascii=False, separators=(",", ":"))


def normalized_records(snapshot: NormalizedSnapshot) -> tuple[EngineRecord, ...]:
    seen: dict[tuple, EngineRecord] = {}
    for record in snapshot.records:
        key = (record.source_instance, *record.record_ref.version_key)
        prior = seen.get(key)
        if prior is not None and canonical_hash(prior) != canonical_hash(record):
            raise ValueError("conflicting payloads for the same adopted identity and version")
        seen[key] = record
    return tuple(seen[key] for key in sorted(seen))


def snapshot_hash(snapshot: NormalizedSnapshot) -> str:
    value = snapshot.model_dump(mode="json", exclude={"snapshot_id", "records"})
    value["records"] = [record.model_dump(mode="json") for record in normalized_records(snapshot)]
    # Precise compatibility for the nullable provenance facet introduced after
    # the first adopted v1 snapshot. Non-null facet evidence always changes hash.
    for record in value["records"]:
        if not record.get("metric_observations"):
            record.pop("metric_observations", None)
        if record["labels"].get("provenance") is None:
            record["labels"].pop("provenance", None)
    value["coverage"] = sorted(value["coverage"], key=canonical_hash)
    value["metadata_gaps"] = sorted(value["metadata_gaps"])
    return canonical_hash(value)


def time_interval(expression: TimeExpression) -> tuple[datetime, datetime] | None:
    """Day/month/year are intervals. An instant is a zero-width interval."""
    if expression.precision == "instant" and expression.instant is not None:
        if expression.instant.tzinfo is None:
            return None
        value = expression.instant.astimezone(UTC)
        return value, value
    raw = (expression.raw or "").strip()
    if expression.precision == "day":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                low = datetime.fromisoformat(raw).replace(tzinfo=UTC)
            except ValueError:
                return None
        elif expression.instant is not None:
            low = expression.instant.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            return None
        return low, low + timedelta(days=1)
    if expression.precision == "month" and re.fullmatch(r"\d{4}-\d{2}", raw):
        try:
            year, month = map(int, raw.split("-"))
            low = datetime(year, month, 1, tzinfo=UTC)
            high = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
            return low, high
        except ValueError:
            return None
    if expression.precision == "year" and re.fullmatch(r"\d{4}", raw):
        try:
            year = int(raw)
            return datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
        except ValueError:
            return None
    return None


def publication_interval(record: EngineRecord) -> tuple[datetime, datetime] | None:
    if not record.publication_time_verified:
        return None
    return time_interval(
        TimeExpression(
            raw=record.published_time_raw,
            precision=record.time_precision,
            instant=record.published_at,
        )
    )


def analysis_interval(record: EngineRecord, policy: MiningPolicy):
    if (
        policy.time_mode == "availability_replay"
        and policy.availability_basis == "public_history_reconstruction"
    ):
        if public_availability(record) is None:
            return None
        raw = (record.availability_proof or {}).get("available_at")
        if isinstance(raw, dict) and raw.get("precision") != "instant":
            return time_interval(
                TimeExpression(raw=raw.get("value"), precision=raw.get("precision", "unknown"))
            )
        upper = public_availability(record)
        return upper, upper
    return publication_interval(record)


def window_for(record: EngineRecord, policy: MiningPolicy) -> str:
    interval = analysis_interval(record, policy)
    if (
        policy.time_mode == "availability_replay"
        and policy.availability_basis == "public_history_reconstruction"
        and interval
    ):
        # Conservative bucket entry uses the latest possible public time. The
        # proof interval remains available for coarse-granularity bin exclusion.
        upper = public_availability(record)
        interval = (upper, upper)
    if interval is None:
        return "undated"
    low, high = interval
    assert policy.current_start and policy.current_end and policy.baseline_start and policy.baseline_end
    if low == high:
        if policy.current_start <= low <= policy.current_end:
            return "current"
        if policy.baseline_start <= low < policy.baseline_end:
            return "baseline"
    else:
        if policy.current_start <= low and high <= policy.current_end:
            return "current"
        if policy.baseline_start <= low and high <= policy.baseline_end:
            return "baseline"
        if low < policy.current_end and high > policy.baseline_start:
            return "ambiguous"
    return "stock"


def public_availability(record: EngineRecord) -> datetime | None:
    """Latest possible public time, only with the actual verified archival proof."""
    proof = record.availability_proof or {}
    if not all(proof.get(key) for key in ("verified_by", "verified_at", "uri", "sha256")):
        return None
    raw = proof.get("available_at")
    try:
        if isinstance(raw, str):
            upper = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        elif isinstance(raw, dict):
            precision = raw.get("precision", "unknown")
            if precision == "instant":
                upper = datetime.fromisoformat(str(raw.get("value", "")).replace("Z", "+00:00"))
            else:
                interval = time_interval(TimeExpression(raw=raw.get("value"), precision=precision))
                upper = interval[1] - timedelta(microseconds=1) if interval else None
        else:
            return None
        if upper is None or upper.tzinfo is None or record.public_available_at != upper:
            return None
        return upper.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def availability_time(record: EngineRecord, policy: MiningPolicy) -> datetime | None:
    if policy.availability_basis == "public_history_reconstruction":
        return public_availability(record)
    return record.available_at


def section_visible(provenance: SectionProvenance, record: EngineRecord, policy: MiningPolicy) -> bool:
    if provenance.available_at is not None:
        if provenance.available_at.tzinfo is None or provenance.available_at > policy.as_of:
            return False
    if policy.time_mode == "availability_replay":
        return provenance.available_at is not None and (
            policy.availability_basis == "public_history_reconstruction"
            and public_availability(record) is not None
            and public_availability(record) <= policy.as_of
            or policy.availability_basis != "public_history_reconstruction"
            and record.observed_at <= policy.as_of
        )
    return True


def confirmed_ref(record: EngineRecord, ref: ProviderRef, policy: MiningPolicy) -> bool:
    for entity in record.canonical_entity_refs:
        if (
            entity.canonical_entity_ref
            and entity.canonical_entity_ref.identity_key == ref.identity_key
            and entity.mapping_status == "confirmed"
            and entity.mapping_ref is not None
        ):
            if entity.mapping_available_at is not None:
                return (
                    entity.mapping_available_at.tzinfo is not None
                    and entity.mapping_available_at <= policy.as_of
                )
            return policy.time_mode != "availability_replay"
    return False


@dataclass(frozen=True)
class PreparedRecord:
    record: EngineRecord
    window: str
    unit_key: str
    confirmed_study: bool
    confirmed_source: bool
    growth_eligible: bool
    reasons: tuple[str, ...]
    policy: MiningPolicy | None = None


@dataclass(frozen=True)
class PreparedSnapshot:
    snapshot: NormalizedSnapshot
    policy: MiningPolicy
    records: tuple[PreparedRecord, ...]
    history: tuple[PreparedRecord, ...]
    excluded_records: tuple[ExcludedRecord, ...]
    truncated_count: int
    limitations: tuple[str, ...]

    @property
    def coverage_complete(self) -> bool:
        return (
            not self.truncated_count
            and self.snapshot.coverage_mode == "complete"
            and bool(self.snapshot.coverage)
            and all(c.mode == "complete" and c.is_consistent_snapshot for c in self.snapshot.coverage)
            and (
                self.policy.time_mode != "availability_replay"
                or self.snapshot.observed_at <= self.policy.as_of
            )
            and {(i.record.source_instance, i.record.source_id) for i in self.records}
            <= {(c.source_instance, c.source_id) for c in self.snapshot.coverage}
        )


def prepare(snapshot: NormalizedSnapshot, policy: MiningPolicy) -> PreparedSnapshot:
    records = normalized_records(snapshot)
    excluded: list[ExcludedRecord] = []
    eligible: list[EngineRecord] = []
    limitations = set(snapshot.metadata_gaps)
    if snapshot.coverage_mode != "complete":
        limitations.add("counts_and_absence_are_limited_to_the_adopted_scope")
    for record in records:
        reason = None
        interval = publication_interval(record)
        if interval and interval[0] > policy.as_of:
            reason = "future_publication"
        if policy.time_mode == "availability_replay":
            available = availability_time(record, policy)
            if available is None:
                reason = "availability_unknown_for_replay"
            elif available > policy.as_of or (
                policy.availability_basis != "public_history_reconstruction"
                and record.observed_at > policy.as_of
            ):
                reason = "version_not_observed_by_replay_cutoff"
        elif record.observed_at > policy.as_of:
            limitations.add("retrospective_publication_analysis_is_not_historical_availability_replay")
        if reason:
            excluded.append(ExcludedRecord(record_ref=record.record_ref, reason=reason))
        else:
            eligible.append(record)
    # Preserve all eligible adopted versions for assertion/withdrawal comparisons.
    versions: dict[tuple, list[EngineRecord]] = {}
    for record in eligible:
        versions.setdefault(record.record_ref.identity_key, []).append(record)
    latest: list[EngineRecord] = []
    ambiguous_versions: set[tuple] = set()
    for key, group in sorted(versions.items()):
        group.sort(
            key=lambda r: (availability_time(r, policy) or r.observed_at, r.record_ref.adopted_version)
        )
        latest.append(group[-1])
        if len(group) > 1 and (availability_time(group[-1], policy) or group[-1].observed_at) == (
            availability_time(group[-2], policy) or group[-2].observed_at
        ):
            ambiguous_versions.add(key)
            limitations.add("source_version_order_ambiguous")
    before_limit = len(latest)
    for record in latest[policy.limits.max_records :]:
        excluded.append(ExcludedRecord(record_ref=record.record_ref, reason="record_budget_exhausted"))
    latest = latest[: policy.limits.max_records]
    retained = {r.record_ref.identity_key for r in latest}
    truncated = before_limit - len(latest)
    if truncated:
        limitations.add("record_budget_exhausted")
    source_complete = {
        (c.source_instance, c.source_id): c.mode == "complete"
        and c.is_consistent_snapshot
        and (policy.time_mode != "availability_replay" or snapshot.observed_at <= policy.as_of)
        and not truncated
        for c in snapshot.coverage
    }

    def prepared(record: EngineRecord) -> PreparedRecord:
        reasons: set[str] = set()
        window = window_for(record, policy)
        study_ok = record.study_cluster_ref is not None and record.study_independence_status == "confirmed"
        if study_ok and policy.time_mode == "availability_replay" and policy.availability_basis is not None:
            study_ok = confirmed_ref(record, record.study_cluster_ref, policy)
        source_ok = record.source_family is not None and record.source_independence_status == "confirmed"
        if not study_ok:
            reasons.add("study_independence_unresolved")
        if not source_ok:
            reasons.add("source_independence_unresolved")
        if window in {"undated", "ambiguous"}:
            reasons.add("publication_time_unknown" if window == "undated" else "time_window_ambiguous")
        if "new_source" in record.record_flags:
            reasons.add("new_source_baseline_warmup")
        if record.record_ref.identity_key in ambiguous_versions:
            reasons.add("source_version_order_ambiguous")
        if not source_complete.get((record.source_instance, record.source_id), False):
            reasons.add("coverage_not_complete")
        if "deleted" in record.record_flags or "retracted" in record.record_flags:
            reasons.add("record_deleted_or_retracted")
        if window == "current" and (
            "historical_backfill" in record.record_flags or record.ingestion_mode == "historical_backfill"
        ):
            reasons.add("historical_backfill_not_a_recent_event")
        growth = window in {"current", "baseline"} and not reasons
        unit = (
            "study:" + ref_key(record.study_cluster_ref) if study_ok else "document:" + record.document_hash
        )
        return PreparedRecord(
            record, window, unit, study_ok, source_ok, growth, tuple(sorted(reasons)), policy
        )

    selected = tuple(prepared(r) for r in latest)
    history = tuple(prepared(r) for r in eligible if r.record_ref.identity_key in retained)
    return PreparedSnapshot(
        snapshot,
        policy,
        selected,
        history,
        tuple(excluded),
        truncated,
        tuple(sorted(limitations)),
    )
