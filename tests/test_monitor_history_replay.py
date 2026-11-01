"""Synthetic fixed versions exercise historical time without current source reads."""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import build_snapshot, project_record
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_service import AnalysisService
from bioradar.research.investigations.kdd import policy_for
from bioradar.research.monitor.service import MonitorService
from test_engine_persistence import GRANT
from test_graph import original_record

CUTOFF = datetime(2020, 1, 3, tzinfo=UTC)
MODE = "public_history_reconstruction"


def historical_record(name="SYNTHETIC-history", *, body="SYNTHETIC notice", day="2020-01-01"):
    row = original_record(name, {"body": body}, source="external_archive", demo=True)
    row["availability_proof"] = {
        "available_at": {"value": day, "precision": "day"},
        "verified_by": "synthetic-source-proof",
        "verified_at": "2026-10-10T00:00:00Z",
        "uri": "https://example.invalid/synthetic-proof",
        "sha256": row["raw_sha256"],
    }
    return row


def reader():
    return FabricHistoryReader(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Time projection must not issue current HTTP requests")
            )
        ),
        authorization_ref=GRANT,
    )


def test_public_projection_qualifies_fields_at_proven_upper_but_keeps_receipt_clock():
    source = reader()
    record = historical_record()
    projected = project_record(source, record, as_of=CUTOFF, mode=MODE, cutoff=CUTOFF, kdd=True)
    assert projected.observed_at.year == 2026
    assert projected.public_available_at == datetime(2020, 1, 1, 23, 59, 59, 999999, tzinfo=UTC)
    assert projected.upstream_status["source_field_available_at"] == projected.public_available_at.isoformat()
    data = build_snapshot([projected], is_demo=True, as_of=CUTOFF, mode=MODE)
    assert data.observed_at == projected.observed_at
    assert data.records[0].normalized_text == record["clean_text"]


def test_equal_public_upper_keeps_prior_version_without_lexical_replacement():
    from bioradar.adapters.fabric_monitor import select_history_versions

    source = reader()
    old = historical_record(body="SYNTHETIC original statement")
    changed = historical_record(body="SYNTHETIC different statement")
    collection = {
        "items": [changed, old],
        "source_id": "external_archive",
        "is_demo": True,
        "cutoff": CUTOFF,
        "coverage": {"complete": True},
        "excluded_counts": {},
    }
    selected, ambiguous = select_history_versions(source, collection, previous=[old])
    assert selected == [old]
    assert {ref["adopted_version"] for ref in ambiguous} == {old["version_id"], changed["version_id"]}
    selected, ambiguous = select_history_versions(source, collection)
    assert selected == [] and len(ambiguous) == 2
    empty = {**collection, "items": [], "coverage": {"complete": False}}
    assert select_history_versions(source, empty, previous=[old])[0] == [old]


def test_historical_saved_kdd_policy_cannot_silently_use_current_time_axis():
    fixed = {
        "query_policy": {"direction": "public_health"},
        "evidence_time": {"mode": MODE, "cutoff": CUTOFF.isoformat()},
    }
    valid = policy_for(fixed)
    assert valid.availability_basis == MODE and valid.time_mode == "availability_replay"
    fixed["kdd_mining_policy"] = valid.model_copy(update={"availability_basis": None}).model_dump(mode="json")
    with pytest.raises(DomainError) as caught:
        policy_for(fixed)
    assert caught.value.code == "analysis_dataset_scope_changed"


def test_forward_parent_is_separate_from_exact_cutoff_retry_identity():
    old = {
        "mode": MODE,
        "cutoff": "2020-01-01T23:59:59+00:00",
        "history_series_id": "SYNTHETIC-series",
        "history_scope_hash": "SYNTHETIC-scope",
    }
    new = {**old, "cutoff": CUTOFF.isoformat()}
    assert not AnalysisService._same_historical_context(old, new)
    assert AnalysisService._forward_historical_context(old, new)
    assert not AnalysisService._forward_historical_context(new, old)
    assert not AnalysisService._forward_historical_context(old, {**new, "history_scope_hash": "other"})
    assert not AnalysisService._forward_historical_context({**old, "mode": "current"}, new)


def test_historical_feature_adoption_does_not_request_current_weknora():
    source = reader()
    record = project_record(source, historical_record(), as_of=CUTOFF, kdd=True)
    data = build_snapshot([record], is_demo=True, as_of=CUTOFF)
    monitor = MonitorService.__new__(MonitorService)
    monitor.settings = SimpleNamespace(weknora_kdd_url="http://synthetic.invalid")
    monitor.actor = "synthetic-actor"
    monitor.engine = SimpleNamespace(
        _authorize_snapshot=lambda *args: None,
        adopt_snapshot=lambda *args: (201, {"id": "synthetic-snapshot"}),
        create_run=lambda *args: (201, {"id": "synthetic-run"}),
        dispatch=lambda identifier: None,
    )
    monitor._saved_response = lambda *args: None
    saves = []
    monitor._save_batch = lambda identifier, **values: saves.append(values)
    monitor._feature_snapshot = lambda *args: pytest.fail("Current features cannot enter historical inputs")
    policy = policy_for(
        {
            "query_policy": {"direction": "public_health"},
            "evidence_time": {"mode": MODE, "cutoff": CUTOFF.isoformat()},
        }
    )
    payload = {
        "snapshot": data.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "historical_context": {"mode": MODE, "cutoff": CUTOFF.isoformat()},
    }
    monitor._adopt_and_queue("synthetic-batch", payload)
    receipt = next(item["payload"]["feature_adoption"] for item in saves if "payload" in item)
    assert receipt["status"] == "not_adopted"
    assert "historical_external_features_not_proven_at_cutoff" in receipt["gaps"]


def test_public_native_fields_do_not_backdate_entity_mapping_or_allow_missing_cutoff():
    from bioradar.adapters.fabric_monitor import search_records
    from bioradar.research.mining.models import KddPolicy
    from bioradar.research.mining.prepare import confirmed_ref

    source = reader()
    row = historical_record()
    original = original_record(
        "SYNTHETIC-sec",
        {
            "body": "SYNTHETIC filing",
            "cik": 1234,
            "taxonomy": "us-gaap",
            "tag": "Revenues",
            "units": {},
            "financial_observations": [],
        },
        source="sec_companyfacts",
        demo=True,
    )
    original["availability_proof"] = {**row["availability_proof"], "sha256": original["raw_sha256"]}
    original["facts"]["concept"] = "Revenues"
    original["field_locators"]["concept"] = {"json_pointer": "/tag"}
    projected = project_record(source, original, as_of=CUTOFF, mode=MODE, cutoff=CUTOFF, kdd=True)
    assert projected.canonical_entity_refs
    entity = projected.canonical_entity_refs[0]
    assert entity.mapping_available_at.year == 2026
    policy = KddPolicy(
        as_of=CUTOFF, kdd_direction="business", time_mode="availability_replay", availability_basis=MODE
    )
    assert not confirmed_ref(projected, entity.canonical_entity_ref, policy)
    with pytest.raises(DomainError):
        search_records(source, source_id="external_archive", is_demo=True, term="SYNTHETIC", mode=MODE)


def test_historical_projection_filters_later_environment_metadata_without_altering_original():
    import copy

    from bioradar.research.mining.signals import assess_records

    source = reader()
    row = historical_record(body="SYNTHETIC hospital notice reports an unverified observation.")
    row["document_evidence"] = {
        "schema_version": "datafabric.document.evidence.v1",
        "material_kind": "official_notice",
        "text_level": "full_text",
        "statement_status": "source_reported",
        "information_events": [
            {
                "event_type": "reported_deletion",
                "description": "SYNTHETIC later annotation",
                "detected_at": "2026-10-10T00:00:00Z",
                "basis_locator": {"quote": row["clean_text"]},
            }
        ],
    }
    original = copy.deepcopy(row)
    historical = project_record(source, row, as_of=CUTOFF, mode=MODE, cutoff=CUTOFF, kdd=True)
    current = project_record(source, row, as_of=CUTOFF, kdd=True)
    assert historical.signal_context.document_evidence["information_events"] == []
    assert current.signal_context.document_evidence["information_events"]
    assert assess_records([historical])["environment_signals"] == []
    assert row == original and historical.record_ref == current.record_ref
    assert historical.normalized_text == current.normalized_text == row["clean_text"]


def test_history_weknora_search_is_unavailable_even_for_prepared_candidate(monkeypatch):
    from bioradar.adapters import weknora, weknora_kdd

    def forbidden(*args, **kwargs):
        pytest.fail("Current WeKnora index has no historical contract")

    monkeypatch.setattr(weknora, "search_candidates", forbidden)
    monkeypatch.setattr(weknora_kdd, "search_candidates", forbidden)
    service = AnalysisService.__new__(AnalysisService)
    checkpoints = []
    service._checkpoint = lambda *args, **kwargs: checkpoints.append(args[2])
    fixed = {
        "evidence_time": {"mode": MODE, "cutoff": CUTOFF.isoformat()},
        "search_scope": ["external_archive"],
        "budget": {"max_queries": 3},
        "query_policy": {},
    }
    progress = {"queries": [], "evidence": [], "query_policy": {"phase": "initial"}}
    action = {
        "id": "SYNTHETIC-history-weknora",
        "kind": "document_search",
        "arguments": {"provider": "weknora", "source_id": "external_archive", "terms": ["SYNTHETIC"]},
    }
    receipt = service._execute_policy_action(
        "SYNTHETIC-analysis", "SYNTHETIC-lease", fixed, progress, action, 0
    )
    assert receipt["status"] == "unavailable"
    assert receipt["error_code"] == "history_current_index_unavailable"
    assert receipt["cost"]["tool_calls"] == 0 and receipt["records"] == []
    assert (
        service._weknora_retrieval(
            "SYNTHETIC-analysis", "SYNTHETIC-lease", fixed, progress, [], phase="initial"
        )
        is progress
    )
    assert checkpoints  # The unavailable outcome is recorded, not an empty successful search.
