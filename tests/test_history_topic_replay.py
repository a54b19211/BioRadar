"""SYNTHETIC historical topic time controls. No business material or model calls."""

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bioradar.research.mining.models import EngineRecord, ProviderRef
from bioradar.research.mining.topic_series import project_contribution
from bioradar.topics.tracking import update_tracking

DAY = datetime(2021, 4, 2, tzinfo=UTC)
MODE = "public_history_reconstruction"


def scope():
    return {
        "domain": "public_health",
        "cadence_hours": 24,
        "source_sites": [],
        "terms_all": [],
        "terms_any": [],
        "profiles": ["text_exposure"],
    }


def sample(*, available=None, proof=True):
    available = available or DAY - timedelta(days=1)
    text = "SYNTHETIC hospital reports an unresolved observation."
    raw_hash = hashlib.sha256(text.encode()).hexdigest()
    ref = ProviderRef(
        provider="data_fabric_history",
        object_type="source_record",
        object_id="SYNTHETIC-source-1",
        adopted_version="a" * 64,
        version_source="provider",
        content_hash=raw_hash,
        authorization_ref="SYNTHETIC-GRANT",
    )
    availability = {
        "available_at": {"value": available.isoformat(), "precision": "instant"},
        "verified_by": "SYNTHETIC-archive-review",
        "verified_at": "2026-10-01T00:00:00Z",
        "uri": "https://example.invalid/SYNTHETIC-archive",
        "sha256": "b" * 64,
    }
    record = EngineRecord(
        record_ref=ref,
        document_hash=raw_hash,
        source_instance="fixture",
        source_id="fixture-source",
        observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        available_at=available,
        public_available_at=available if proof else None,
        availability_proof=availability if proof else None,
        source_family="SYNTHETIC-origin",
        source_independence_status="confirmed",
        published_at=available,
        published_time_raw=available.isoformat(),
        time_precision="instant",
        publication_time_verified=True,
        text_status="inline_authorized",
        normalized_text=text,
        normalization_version="SYNTHETIC-v1",
    )
    history = {
        "source_id": "fixture-source",
        "source_record_id": "SYNTHETIC-source-1",
        "version_id": "a" * 64,
        "raw_sha256": raw_hash,
        "facts": {},
        "record_kind": "document",
        "title": "SYNTHETIC control",
        "field_locators": {},
        "raw_media_type": "application/json",
        "parser_version": "SYNTHETIC-v1",
        "first_seen_at": "2026-10-01T00:00:00Z",
        "received_at": "2026-10-01T00:00:00Z",
    }
    return record, history


def value(**options):
    record, history = sample(**options)
    return project_contribution(
        record,
        history,
        scope(),
        as_of=DAY,
        evidence_time={"mode": MODE, "cutoff": DAY.isoformat()},
    )


def test_public_history_exposure_uses_verified_public_time_without_changing_native_receipt():
    item = value()
    original = copy.deepcopy(item)
    assert item["exposure_available_at"] == (DAY - timedelta(days=1)).isoformat()
    assert item["exposure_time_basis"] == MODE
    first = update_tracking([item], scope(), None, as_of=DAY)
    assert first["heat"]["score"] == pytest.approx(2 ** (-24 / 36), abs=1e-8)
    second = update_tracking([item], scope(), first, as_of=DAY + timedelta(days=1))
    assert second["heat"]["score"] == pytest.approx(2 ** (-48 / 36), abs=1e-8)
    assert second["model_update"]["needed"] is False
    assert item == original and item["received_at"] == "2026-10-01T00:00:00Z"


def test_public_history_missing_or_future_proof_is_not_clamped_to_day():
    for options in ({"proof": False}, {"available": DAY + timedelta(days=1)}):
        with pytest.raises(ValueError, match="availability"):
            value(**options)


def test_operational_receipt_after_cutoff_is_not_public_history_exposure():
    record, history = sample()
    with pytest.raises(ValueError, match="availability"):
        project_contribution(
            record,
            history,
            scope(),
            as_of=DAY,
            evidence_time={"mode": "operational_replay", "cutoff": DAY.isoformat()},
        )


def test_reaction_keeps_existing_lifecycle_awake_without_time_only_model_update():
    record, history = sample()
    item = project_contribution(record, history, scope(), as_of=DAY)
    item["attention"]["environment_signals"] = [{"kind": "reaction", "text": "SYNTHETIC deletion reaction"}]
    previous = update_tracking([item], scope(), None, as_of=DAY)
    for days in (1, 2, 3):
        previous = update_tracking([item], scope(), previous, as_of=DAY + timedelta(days=days))
        assert previous["lifecycle"] != "dormant"
        assert previous["cadence_hours"] == 1
        assert previous["model_update"]["needed"] is False


def test_tracking_decision_and_decay_rule_bytes_are_frozen():
    text = (Path(__file__).resolve().parents[1] / "services/bioradar/topics/tracking.py").read_text()
    rules = text[text.index("    def decay(keys):") : text.index("\ndef hypothesis_comparison")]
    assert (
        hashlib.sha256(rules.encode()).hexdigest()
        == "e031f84e7b4c97e8b7e8c51a621f9ce752a4e6174caf7d5f84b7acd66f43e563"
    )
