"""Compare two selected adopted packages, without inferring source events."""

from datetime import datetime
from typing import Literal

from bioradar.research.mining.models import EngineRecord, FrozenModel, NormalizedSnapshot, ProviderRef
from bioradar.research.mining.prepare import snapshot_hash

COMPARISON_VERSION = "bioradar-comparison-v0.1"
ComparisonStatus = Literal["added", "content_updated", "same", "not_seen"]


class RecordIdentity(FrozenModel):
    source_instance: str
    provider: str
    object_type: str
    object_id: str


class SnapshotComparisonItem(FrozenModel):
    identity: RecordIdentity
    before_ref: ProviderRef | None = None
    after_ref: ProviderRef | None = None
    status: ComparisonStatus
    changed_fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


class SnapshotComparison(FrozenModel):
    schema_version: Literal["bioradar.engine.comparison.v1"] = "bioradar.engine.comparison.v1"
    comparison_version: Literal["bioradar-comparison-v0.1"] = COMPARISON_VERSION
    source_instance: str
    is_demo: bool
    before_hash: str
    after_hash: str
    before_observed_at: datetime
    after_observed_at: datetime
    scope: Literal["selected_adopted_records"] = "selected_adopted_records"
    items: tuple[SnapshotComparisonItem, ...]
    counts: dict[ComparisonStatus, int]
    limitations: tuple[str, ...]


def _records(snapshot: NormalizedSnapshot) -> dict[tuple[str, str, str, str], EngineRecord]:
    records = {}
    for record in snapshot.records:
        identity = (record.source_instance, *record.record_ref.identity_key)
        prior = records.get(identity)
        if prior is not None and prior != record:
            raise ValueError("comparison_requires_one_unambiguous_adopted_record_per_identity")
        records[identity] = record
    return records


def _comparable_fields(record: EngineRecord) -> dict:
    fields = record.model_dump(mode="json", exclude={"observed_at"})
    # Local receipts/call correlations change on an identical read. Other source
    # status and availability fields remain visible as metadata differences.
    for key in ("provider_call_id", "provider_input_hash"):
        fields["upstream_status"].pop(key, None)
    return fields


def compare_snapshots(before: NormalizedSnapshot, after: NormalizedSnapshot) -> SnapshotComparison:
    """The runtime authorizes both packages before calling this ordinary function.

    Missing records carry no per-record read outcome. A failed whole read remains
    an adapter error; it must never be converted to an empty successful package.
    """
    if before.source_instance != after.source_instance:
        raise ValueError("comparison_requires_the_same_source_instance")
    if before.is_demo != after.is_demo:
        raise ValueError("comparison_cannot_mix_synthetic_and_real_packages")
    old, new = _records(before), _records(after)
    items = []
    counts = {status: 0 for status in ("added", "content_updated", "same", "not_seen")}
    for identity in sorted(old.keys() | new.keys()):
        left, right = old.get(identity), new.get(identity)
        changes: tuple[str, ...] = ()
        if left is None:
            status = "added"
            reasons = ["new_to_selected_adopted_set_not_new_research_or_event"]
            assert right is not None
            if right.ingestion_mode == "historical_backfill" or "historical_backfill" in right.record_flags:
                reasons.append("historical_backfill")
            if not right.publication_time_verified:
                reasons.append("publication_time_semantics_unverified")
        elif right is None:
            status = "not_seen"
            reasons = ["not_in_selected_after_package_not_deletion_or_read_failure"]
        else:
            if (
                left.record_ref.version_key == right.record_ref.version_key
                and left.record_ref.content_hash != right.record_ref.content_hash
            ):
                raise ValueError("comparison_source_version_has_conflicting_content_hash")
            previous, current = _comparable_fields(left), _comparable_fields(right)
            changes = tuple(sorted(key for key in previous if previous[key] != current[key]))
            raw_changed = left.document_hash != right.document_hash
            text_comparable = (
                left.text_status == right.text_status == "inline_authorized"
                and left.normalization_version == right.normalization_version
            )
            text_changed = text_comparable and left.normalized_text_hash != right.normalized_text_hash
            status = "content_updated" if raw_changed or text_changed else "same"
            reasons = []
            if raw_changed:
                reasons.append("adopted_source_document_hash_changed_not_new_research")
            if text_changed:
                reasons.append("adopted_text_changed_under_same_normalization_version")
            if not raw_changed and not text_changed:
                reasons.append("source_content_fingerprint_unchanged")
            if left.normalization_version != right.normalization_version:
                reasons.append("normalization_changed_text_is_not_a_source_event")
            if not text_comparable:
                reasons.append("adopted_text_not_comparable")
            if "record_ref" in changes:
                reasons.append("source_reference_metadata_changed")
            if "record_flags" in changes:
                reasons.append("source_status_annotations_changed")
            if changes and status == "same":
                reasons.append("metadata_or_enrichment_changed_not_new_research")
        counts[status] += 1
        items.append(
            SnapshotComparisonItem(
                identity=RecordIdentity(
                    source_instance=identity[0],
                    provider=identity[1],
                    object_type=identity[2],
                    object_id=identity[3],
                ),
                before_ref=left.record_ref if left else None,
                after_ref=right.record_ref if right else None,
                status=status,
                changed_fields=changes,
                reason_codes=tuple(reasons),
            )
        )
    return SnapshotComparison(
        source_instance=before.source_instance,
        is_demo=before.is_demo,
        before_hash=snapshot_hash(before),
        after_hash=snapshot_hash(after),
        before_observed_at=before.observed_at,
        after_observed_at=after.observed_at,
        items=tuple(items),
        counts=counts,
        limitations=(
            "selected_packages_only_no_automatic_direction_filter_or_full_source_sync",
            "record_counts_are_not_independent_study_counts",
            "adoption_time_is_not_publication_or_event_time",
            "missing_record_is_not_deletion_and_has_no_per_record_read_outcome",
            "identity_or_professional_enrichment_is_not_new_research",
        ),
    )
