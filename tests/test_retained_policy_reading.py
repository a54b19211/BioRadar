"""A retained draft cannot rebind a previous reading to a new same-named passage."""

import copy
import hashlib

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import (
    freeze_passages,
    freeze_payload,
    semantic_reading_state,
)
from bioradar.research.investigations.analysis_models import PolicyReading
from bioradar.research.investigations.reading_execution import retained_policy_reading
from bioradar.research.mining.models import canonical_hash
from test_final_assessment_binding import enhanced, evidence, frozen


def saved_reading(*, repaired=False):
    fixed = frozen()
    fixed.update(
        question="SYNTHETIC fixed question",
        as_of="2026-10-01T00:00:00Z",
        search_scope=["SYNTHETIC-source"],
        query_context_version="semantic-v2",
        evidence_time={"mode": "current", "cutoff": "2026-10-01T00:00:00Z"},
    )
    rows = evidence()
    reading = enhanced()
    reading.pop("update")
    reading["next_queries"] = []
    parsed = PolicyReading.model_validate(reading).model_dump(mode="json")
    context = {"graph_context": [], "quality_feedback": None, "mechanism_evaluation": None}
    fingerprint = canonical_hash(semantic_reading_state(rows, context))
    key = "policy-read:" + fingerprint
    payload = freeze_payload("read_plan", fixed, rows, query_context=context)
    original = {
        "stage": "read_plan",
        "call_key": key,
        "call_id": "SYNTHETIC-original",
        "status": "received",
        "receipt": {"status": "completed"},
        "payload": payload,
        "output": copy.deepcopy(reading),
    }
    progress = {
        "evidence": rows,
        "calls": [original],
        "query_policy": {
            "reading_kind": "policy",
            "reading": parsed,
            "reading_evidence_hash": fingerprint,
            "refresh_not_semantically_reviewed": True,
        },
    }
    if repaired:
        original["output"]["claims"][0]["evidence_links"][0]["passage_id"] = "E1:p999"
        repair_key = key + ":structure-repair"
        repair = {
            "stage": "read_plan_repair",
            "call_key": repair_key,
            "call_id": "SYNTHETIC-repair",
            "status": "received",
            "receipt": {"status": "completed"},
            "payload": {
                "original_request": copy.deepcopy(payload),
                "invalid_output": copy.deepcopy(original["output"]),
            },
            "output": reading,
        }
        progress["calls"].append(repair)
        progress["reading_repairs"] = {
            key: {
                "status": "validated",
                "original_call_key": key,
                "repair_call_key": repair_key,
                "repair_call_id": repair["call_id"],
            }
        }
    return fixed, progress


@pytest.mark.parametrize("repaired", [False, True])
def test_retained_reading_is_exactly_bound_to_original_received_payload_and_validated_repair(repaired):
    fixed, progress = saved_reading(repaired=repaired)
    before = copy.deepcopy((fixed, progress))
    parsed, original = retained_policy_reading(fixed, progress)
    assert parsed.model_dump(mode="json") == progress["query_policy"]["reading"]
    assert original is progress["calls"][0]
    assert (fixed, progress) == before


@pytest.mark.parametrize("change", ["version", "text"])
def test_same_named_p001_with_a_new_valid_selection_version_or_text_cannot_bind_old_reading(change):
    fixed, progress = saved_reading()
    item = progress["evidence"][0]
    if change == "version":
        item["record_ref"]["adopted_version"] = "SYNTHETIC-new-version"
    else:
        item["text"] = "SYNTHETIC replacement words in the same p001 ID."
        item["end"] = len(item["text"])
        item["text_sha256"] = hashlib.sha256(item["text"].encode()).hexdigest()
    progress["evidence"][0] = freeze_passages(item)
    assert progress["evidence"][0]["passages"][0]["id"] == "E1:p001"
    before = copy.deepcopy(progress)
    assert retained_policy_reading(fixed, progress) is None
    assert progress == before


@pytest.mark.parametrize("fault", ["missing", "unknown", "changed_state", "unreconstructable_context"])
def test_unproven_original_receipt_or_context_never_becomes_a_retained_draft(fault):
    fixed, progress = saved_reading()
    if fault == "missing":
        progress["calls"] = []
    elif fault == "unknown":
        progress["calls"][0]["status"] = "unconfirmed"
    elif fault == "changed_state":
        progress["query_policy"]["reading"]["claims"][0]["text"] += " SYNTHETIC changed"
    else:
        progress["calls"][0]["payload"]["mechanism_query_context"] = {"SYNTHETIC": "compact-only"}
    before = copy.deepcopy(progress)
    assert retained_policy_reading(fixed, progress) is None
    assert progress == before


@pytest.mark.parametrize(
    "fault", ["not_validated", "unknown", "different_request", "different_invalid_output"]
)
def test_repaired_reading_requires_its_actual_validated_received_original_input(fault):
    fixed, progress = saved_reading(repaired=True)
    key = progress["calls"][0]["call_key"]
    if fault == "not_validated":
        progress["reading_repairs"][key]["status"] = "invalid"
    elif fault == "unknown":
        progress["calls"][1]["status"] = "unconfirmed"
    elif fault == "different_request":
        progress["calls"][1]["payload"]["original_request"]["question"] += " SYNTHETIC other"
    else:
        progress["calls"][1]["payload"]["invalid_output"]["claims"][0]["text"] += " changed"
    assert retained_policy_reading(fixed, progress) is None


def test_unexpected_domain_outcome_is_not_swallowed_by_the_pure_guard(monkeypatch):
    import bioradar.research.investigations.analysis as analysis

    fixed, progress = saved_reading()
    failure = DomainError("professional_outcome_unconfirmed", "SYNTHETIC unknown", 502)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(analysis, "validate_policy_reading", fail)
    with pytest.raises(DomainError) as raised:
        retained_policy_reading(fixed, progress)
    assert raised.value is failure
