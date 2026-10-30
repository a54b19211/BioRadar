"""Single reference-protocol repair uses the real host ledger; callbacks here are synthetic."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt, ModelCompletion, ModelUsage
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import validate_policy_reading
from test_optional_policy_reading import harness, reading


def setup(monkeypatch, outcome="valid"):
    service, fixed, progress = harness(monkeypatch)
    fixed["query_policy"]["research_contract_version"] = "bioradar-research-input-v1"
    progress["query_policy"]["reading_evidence_hash"] = "SYNTHETIC-prior-read"
    calls = []
    bad = reading()
    bad["claims"][0]["evidence_links"][0]["passage_id"] = "E1:p002"
    # Two rejected links exercise complete feedback, not only the first exception.
    bad["claims"].append({**copy.deepcopy(bad["claims"][0]), "id": "C2"})

    def reply(config, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        stage = kwargs["stage"]
        if stage == "read_plan_repair":
            payload = kwargs["payload"]
            assert payload["invalid_output"] == bad
            assert payload["allowed_reference_ids"] == {"E1": ["E1:p001"]}
            assert [error["claim_id"] for error in payload["reference_errors"]] == ["C1", "C2"]
            assert all(error["passage_id"] == "E1:p002" for error in payload["reference_errors"])
            assert payload["original_request"]["evidence"][0]["passages"][0]["id"] == "E1:p001"
        now = datetime.now(UTC)
        receipt = ModelCallReceipt(
            call_id=kwargs["call_id"],
            stage=stage,
            input_hash=kwargs["input_hash"],
            prompt_version=kwargs["prompt_version"],
            requested_model=config.semantic_llm_model,
            configured_model_revision=fixed["model_revision"],
            status="unconfirmed" if outcome == "unknown" and stage == "read_plan_repair" else "completed",
            error_code="model_transport_unconfirmed"
            if outcome == "unknown" and stage == "read_plan_repair"
            else None,
            started_at=now,
            completed_at=now,
            elapsed_ms=1,
            usage=ModelUsage(
                prompt_tokens=110000 if outcome == "budget" else 100,
                completion_tokens=32000 if outcome == "budget" else 50,
                total_tokens=142000 if outcome == "budget" else 150,
            ),
            max_output_tokens=config.semantic_llm_max_output_tokens,
        )
        if receipt.status == "unconfirmed":
            raise ModelCallError("model_transport_unconfirmed", "SYNTHETIC unknown transport", receipt, 409)
        if outcome == "time" and stage == "read_plan":
            progress["started_at"] = (now - timedelta(hours=2)).isoformat()
        value = reading() if stage == "read_plan_repair" and outcome == "valid" else copy.deepcopy(bad)
        return ModelCompletion(content=json.dumps(value), parsed=value, receipt=receipt)

    service.model_call = reply
    if outcome == "budget":
        fixed["budget"]["max_tokens"] = 160000
    return service, fixed, progress, calls, bad


def test_reference_repair_preserves_original_revalidates_and_replays_received_without_budget(monkeypatch):
    service, fixed, progress, calls, original = setup(monkeypatch)
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert [c["stage"] for c in calls] == ["read_plan", "read_plan_repair"]
    assert progress["calls"][0]["status"] == "received"
    assert progress["calls"][0]["output"] == original
    repaired = progress["query_policy"]["reading"]
    validate_policy_reading(repaired, progress["evidence"], fixed["search_scope"])
    assert repaired["claims"][0]["evidence_links"][0]["passage_id"] == "E1:p001"
    assert next(iter(progress["reading_repairs"].values()))["status"] == "validated"
    progress["started_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fixed["budget"]["max_tokens"] = 1
    progress["query_policy"]["refresh_not_semantically_reviewed"] = True
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert len(calls) == 2
    assert "refresh_not_semantically_reviewed" not in progress["query_policy"]


@pytest.mark.parametrize("outcome", ["invalid", "budget", "time"])
def test_known_failed_optional_reference_repair_keeps_old_reading_and_new_evidence(monkeypatch, outcome):
    service, fixed, progress, calls, original = setup(monkeypatch, outcome)
    previous = copy.deepcopy(progress["query_policy"]["reading"])
    evidence = copy.deepcopy(progress["evidence"])
    assert not service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert progress["query_policy"]["reading"] == previous
    assert progress["query_policy"]["reading_evidence_hash"] == "SYNTHETIC-prior-read"
    assert progress["query_policy"]["refresh_not_semantically_reviewed"] is True
    assert progress["evidence"] == evidence
    assert progress["calls"][0]["output"] == original
    entry = next(iter(progress["reading_repairs"].values()))
    assert entry["reference_errors"][0]["passage_id"] == "E1:p002"
    assert entry["status"] == ("invalid" if outcome == "invalid" else "not_sent")
    assert len(calls) == (2 if outcome == "invalid" else 1)
    if outcome == "time":
        assert progress["calls"][-1]["receipt"]["status"] == "not_sent"
    before = len(calls)
    assert not service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert len(calls) == before


def test_unknown_repair_is_blocked_and_never_falls_back_or_resends(monkeypatch):
    service, fixed, progress, calls, _ = setup(monkeypatch, "unknown")
    for attempt in range(2):
        with pytest.raises(DomainError) as exc:
            service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
        assert exc.value.code == (
            "model_transport_unconfirmed" if attempt == 0 else "model_call_outcome_unconfirmed"
        )
        assert "refresh_not_semantically_reviewed" not in progress["query_policy"]
        assert next(iter(progress["reading_repairs"].values()))["status"] == "unconfirmed"
    assert len(calls) == 2 and progress["calls"][-1]["status"] == "unconfirmed"


def test_bad_reference_without_previous_reading_remains_hard_failure_after_one_attempt(monkeypatch):
    service, fixed, progress, calls, _ = setup(monkeypatch, "invalid")
    progress["query_policy"]["reading"] = None
    progress["query_policy"]["phase"] = "initial"
    with pytest.raises(DomainError) as exc:
        service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert exc.value.code == "analysis_passage_invalid"
    assert len(calls) == 2
    assert progress["query_policy"]["reading"] is None
    assert next(iter(progress["reading_repairs"].values()))["status"] == "invalid"
