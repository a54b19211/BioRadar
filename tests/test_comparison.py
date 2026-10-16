"""User-visible adoption differences, distinct from scientific/source events."""

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from bioradar.research.comparison import compare_snapshots
from bioradar.research.mining.models import NormalizedSnapshot

pytestmark = pytest.mark.ENGINE
EXAMPLES = Path(__file__).resolve().parents[1] / "contracts" / "examples"


def packages():
    return tuple(
        NormalizedSnapshot.model_validate_json((EXAMPLES / filename).read_text())
        for filename in ("engine-fixture.snapshot.json", "engine-fixture-v2.snapshot.json")
    )


def test_teaching_example_distinguishes_revision_enrichment_backfill_and_absence():
    before, after = packages()
    result = compare_snapshots(before, after)
    assert result.counts == {"added": 1, "content_updated": 1, "same": 14, "not_seen": 1}
    rows = {item.identity.object_id: item for item in result.items}
    assert rows["current-0"].status == "content_updated"
    assert rows["current-0"].before_ref.adopted_version == "v1"
    assert rows["current-0"].after_ref.adopted_version == "v2"
    assert rows["current-1"].status == "same"
    assert "canonical_entity_refs" in rows["current-1"].changed_fields
    assert rows["current-2"].status == "not_seen" and rows["current-2"].after_ref is None
    historical = rows["historical-2004"]
    assert historical.status == "added" and "historical_backfill" in historical.reason_codes
    record = next(r for r in after.records if r.record_ref == historical.after_ref)
    assert record.published_at.year == 2004
    assert "record_counts_are_not_independent_study_counts" in result.limitations


def test_repeat_observation_and_new_call_id_are_not_content_updates():
    before, _ = packages()
    record = before.records[0].model_copy(update={"upstream_status": {"provider_call_id": "old"}})
    before = before.model_copy(update={"records": (record,)})
    reread = record.model_copy(
        update={
            "observed_at": record.observed_at + timedelta(days=1),
            "upstream_status": {"provider_call_id": "new", "provider_input_hash": "new-call"},
        }
    )
    after = before.model_copy(
        update={"observed_at": before.observed_at + timedelta(days=1), "records": (reread,)}
    )
    result = compare_snapshots(before, after)
    assert result.counts["same"] == 1 and result.items[0].changed_fields == ()
    assert result.before_hash != result.after_hash


def test_empty_successful_package_is_absence_not_deletion_or_read_failure():
    before, _ = packages()
    after = before.model_copy(update={"records": (), "coverage": (), "coverage_mode": "manual_package"})
    result = compare_snapshots(before, after)
    assert result.counts["not_seen"] == len(before.records)
    assert all("not_deletion_or_read_failure" in item.reason_codes[0] for item in result.items)
    assert "read_failed" not in result.counts and "deleted" not in result.counts


def test_structure_identity_preserves_providers_and_colons_in_ids():
    package, _ = packages()
    original = package.records[0]
    a = original.model_copy(update={"record_ref": original.record_ref.model_copy(update={"provider": "a:b"})})
    b = original.model_copy(
        update={
            "record_ref": original.record_ref.model_copy(
                update={"provider": "a", "object_type": "b:document"}
            ),
        }
    )
    before = package.model_copy(update={"records": (a, b), "coverage": ()})
    after = before.model_copy(update={"records": (a,)})
    result = compare_snapshots(before, after)
    assert result.counts["same"] == 1 and result.counts["not_seen"] == 1
    assert len(result.items) == 2


def test_normalizer_change_or_unavailable_text_does_not_claim_source_event():
    package, _ = packages()
    original = package.records[0]
    before = package.model_copy(update={"records": (original,), "coverage": ()})
    text = original.normalized_text + " Different adopted representation."
    changed = original.model_copy(
        update={
            "normalization_version": "fixture-normalizer-v2",
            "normalized_text": text,
            "normalized_text_hash": hashlib.sha256(text.encode()).hexdigest(),
        }
    )
    result = compare_snapshots(before, before.model_copy(update={"records": (changed,)}))
    assert result.items[0].status == "same"
    assert "normalization_changed_text_is_not_a_source_event" in result.items[0].reason_codes
    absent_text = original.model_copy(
        update={
            "text_status": "unavailable",
            "normalized_text": None,
            "normalized_text_hash": None,
            "normalization_version": None,
        }
    )
    result = compare_snapshots(before, before.model_copy(update={"records": (absent_text,)}))
    assert result.items[0].status == "same" and "adopted_text_not_comparable" in result.items[0].reason_codes


def test_same_normalizer_text_change_and_explicit_source_flags_are_visible():
    before, _ = packages()
    original = before.records[0]
    before = before.model_copy(update={"records": (original,), "coverage": ()})
    text = original.normalized_text + " Revised adopted text."
    ref = original.record_ref.model_copy(update={"adopted_version": "v2", "content_hash": "new-export"})
    updated = original.model_copy(
        update={
            "record_ref": ref,
            "normalized_text": text,
            "normalized_text_hash": hashlib.sha256(text.encode()).hexdigest(),
        }
    )
    result = compare_snapshots(before, before.model_copy(update={"records": (updated,)}))
    assert result.items[0].status == "content_updated"
    flagged = original.model_copy(update={"record_flags": ("retracted",)})
    result = compare_snapshots(before, before.model_copy(update={"records": (flagged,)}))
    assert result.items[0].status == "same"
    assert "source_status_annotations_changed" in result.items[0].reason_codes


def test_ambiguous_versions_conflicting_hash_or_incompatible_packages_reject():
    before, _ = packages()
    original = before.records[0]
    duplicate = original.model_copy(
        update={"record_ref": original.record_ref.model_copy(update={"adopted_version": "v2"})}
    )
    with pytest.raises(ValueError, match="unambiguous"):
        compare_snapshots(before, before.model_copy(update={"records": (original, duplicate)}))
    conflict = original.model_copy(
        update={"record_ref": original.record_ref.model_copy(update={"content_hash": "different"})}
    )
    with pytest.raises(ValueError, match="conflicting_content_hash"):
        compare_snapshots(before, before.model_copy(update={"records": (conflict,)}))
    with pytest.raises(ValueError, match="source_instance"):
        compare_snapshots(before, before.model_copy(update={"source_instance": "different-instance"}))
    with pytest.raises(ValueError, match="synthetic_and_real"):
        compare_snapshots(before, before.model_copy(update={"is_demo": False}))


def test_order_and_exact_duplicate_observations_do_not_change_comparison():
    before, after = packages()
    expected = compare_snapshots(before, after)
    before = before.model_copy(update={"records": (*reversed(before.records), before.records[0])})
    after = after.model_copy(update={"records": tuple(reversed(after.records))})
    assert compare_snapshots(before, after) == expected
