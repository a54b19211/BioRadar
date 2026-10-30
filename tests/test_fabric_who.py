"""Offline Radar boundaries over one fixed public WHO original and its adopted cohort.

The cohort fixture is a minimal-field projection of the eight actual adopted
records, not eight new extracts. No upstream extraction code or network is used.
"""

import copy
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_who import _quoted_count, metric_observations, validate_counts
from bioradar.research.mining.kdd import run_kdd
from bioradar.research.mining.models import KddPolicy, NormalizedSnapshot, SectionProvenance

pytestmark = pytest.mark.ENGINE
FIXTURES = Path(__file__).parent / "fixtures" / "who"
CUTOFF = datetime(2026, 10, 30, 12, tzinfo=UTC)


def public_record():
    return json.loads((FIXTURES / "somalia_2022_fixed.json").read_text())


def derive_metrics(record, *, as_of=CUTOFF):
    reader = FabricHistoryReader(None, authorization_ref="offline-public-source-boundary")
    provenance = SectionProvenance(
        schema_version="bioradar.fabric.kdd-fields.v1",
        provider_ref=reader.ref(record),
        available_at=datetime.fromisoformat(record["received_at"]),
    )
    return metric_observations(reader, record, provenance, as_of=as_of)


def test_public_who_fixed_version_and_locations_cannot_be_relabelled():
    record = public_record()
    before = copy.deepcopy(record)
    assert hashlib.sha256(record["raw_content"].encode()).hexdigest() == record["raw_sha256"]
    original = json.loads(record["raw_content"])
    validate_counts(record, original)
    assert record == before
    for field, value in [("value", 7797), ("disease", "different disease")]:
        changed = copy.deepcopy(record)
        changed["facts"]["count_statements"][2][field] = value
        with pytest.raises(ValueError, match="fixed version"):
            validate_counts(changed, original)
    changed = copy.deepcopy(record)
    changed["facts"]["count_statements"][2]["time_scope"]["start"] = "2022-07-10"
    with pytest.raises(ValueError, match="fixed version"):
        validate_counts(changed, original)


def test_explicit_reporting_interval_is_preserved_and_as_of_is_not_a_day():
    record = public_record()
    before = copy.deepcopy(record)
    metrics, gaps = derive_metrics(record)
    assert [(m.metric_id, str(m.value_lower)) for m in metrics] == [
        ("case_count", "7796"),
        ("death_count", "37"),
    ]
    assert all(m.period_start == datetime(2022, 1, 1, tzinfo=UTC) for m in metrics)
    assert all(m.period_end == datetime(2022, 7, 11, tzinfo=UTC) for m in metrics)
    assert all(m.denominator is None and m.binding_status == "source_bound" for m in metrics)
    assert metrics == derive_metrics(record, as_of=datetime(2027, 1, 1, tzinfo=UTC))[0]
    assert "who_counts_source_reported_not_independent_events_or_population_rates" in gaps
    assert record == before
    # Controlled counterexample to a public row: even fully specified place/disease
    # does not permit changing a point-in-time count into a one-day interval.
    point = copy.deepcopy(record)
    point["facts"]["count_statements"] = [copy.deepcopy(record["facts"]["count_statements"][2])]
    point["facts"]["count_statements"][0]["time_scope"] = {
        "kind": "as_of",
        "as_of": "2022-07-10",
        "explicit_interval": False,
        "start": None,
        "end": None,
    }
    rejected, point_gaps = derive_metrics(point)
    assert rejected == []
    assert "who_point_or_unspecified_count_not_an_interval" in point_gaps


def test_adopted_eight_notices_keep_six_typed_observations_and_original_scope():
    snapshot = NormalizedSnapshot.model_validate_json(
        (FIXTURES / "adopted_eight_notice_projection.json").read_text()
    )
    before = snapshot.model_dump(mode="json")
    assert len(snapshot.records) == snapshot.coverage[0].record_count == 8
    assert sorted(len(r.metric_observations) for r in snapshot.records) == [0, 0, 0, 0, 0, 0, 2, 4]
    result = run_kdd(snapshot, KddPolicy(kdd_direction="public_health", as_of=CUTOFF))
    data = result.data_preparation
    kinds = Counter(unit["unit_kind"] for unit in data["units"])
    assert data["input_count"] == 8
    assert data["qualified_count"] == 14
    assert kinds["native_notice_publication"] == 8
    assert sum(kinds.values()) - kinds["native_notice_publication"] == 6
    original_refs = {
        json.dumps(r.record_ref.model_dump(mode="json"), sort_keys=True) for r in snapshot.records
    }
    notice_units = [u for u in data["units"] if u["unit_kind"] == "native_notice_publication"]
    assert {json.dumps(u["members"][0]["record_ref"], sort_keys=True) for u in notice_units} == original_refs
    assert data["denominator"]["population_total"] is None
    assert data["denominator"]["population_rate_permitted"] is False
    assert snapshot.model_dump(mode="json") == before


def test_source_grouped_count_quote_requires_consistent_three_digit_groups():
    # The space-separated examples are actual tokens from public WHO 2023-DON498.
    for quote, expected in [
        ("146 878", 146878),
        ("68 346", 68346),
        ("13 339", 13339),
        ("68\u00a0346", 68346),
        ("13\u202f339", 13339),
        ("1,234,567", 1234567),
    ]:
        assert _quoted_count(quote) == expected
    for quote in ["146 87", "1,234 567", "14,68,78", "1 23 456", "1.234", "146 878 cases"]:
        assert _quoted_count(quote) is None
