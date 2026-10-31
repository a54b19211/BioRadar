"""Reading ID normalization and bounded correction; callbacks are synthetic and never HTTP."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt, ModelCompletion, ModelUsage
from bioradar.api.errors import DomainError
from bioradar.delivery.analysis_diagnostics import build_analysis_diagnostics
from test_optional_policy_reading import harness, reading


def setup(monkeypatch, reference="C1", *, mode="valid", prompt_tokens=30000):
    service, fixed, progress = harness(monkeypatch)
    fixed["query_policy"]["research_contract_version"] = "bioradar-research-input-v1"
    progress["query_policy"].update(phase="initial", reading=None)
    raw = reading()
    raw["claims"][0]["id"] = "C1_DECLARED"
    raw["explanations"] = [
        {
            "id": "H1",
            "text": "SYNTHETIC bounded explanation",
            "for_claim_ids": [reference],
            "against_claim_ids": [],
            "discriminating_evidence_needed": "SYNTHETIC future independent check",
        }
    ]
    calls = []

    def reply(config, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        stage = kwargs["stage"]
        value = copy.deepcopy(raw)
        if stage == "read_plan_repair":
            payload = kwargs["payload"]
            assert payload["allowed_claim_ids"] == [claim["id"] for claim in raw["claims"]]
            assert payload["field_paths"] == [["explanations", 0, "for_claim_ids", 0]]
            assert "SYNTHETIC bounded explanation" not in json.dumps(payload)
            assert "SYNTHETIC fixed source statement." not in json.dumps(payload)
            change = {"path": payload["field_paths"][0], "operation": "replace", "value": "C1_DECLARED"}
            if mode == "remove":
                change = {"path": change["path"], "operation": "remove"}
            elif mode == "rewrite":
                change["path"] = ["claims", 0, "text"]
            elif mode == "invalid":
                change["value"] = "C999"
            value = {"changes": [change]}
        now = datetime.now(UTC)
        unknown = stage == "read_plan_repair" and mode == "unknown"
        receipt = ModelCallReceipt(
            call_id=kwargs["call_id"],
            stage=stage,
            input_hash=kwargs["input_hash"],
            prompt_version=kwargs["prompt_version"],
            requested_model=config.semantic_llm_model,
            configured_model_revision=fixed["model_revision"],
            status="unconfirmed" if unknown else "completed",
            error_code="model_transport_unconfirmed" if unknown else None,
            started_at=now,
            completed_at=now,
            elapsed_ms=1,
            usage=ModelUsage(
                prompt_tokens=prompt_tokens, completion_tokens=50, total_tokens=prompt_tokens + 50
            ),
            max_output_tokens=config.semantic_llm_max_output_tokens,
        )
        if unknown:
            raise ModelCallError("model_transport_unconfirmed", "SYNTHETIC unknown receipt", receipt, 409)
        return ModelCompletion(content=json.dumps(value), parsed=value, receipt=receipt)

    service.model_call = reply
    return service, fixed, progress, calls, raw


def test_unique_short_reference_uses_zero_extra_calls_and_keeps_received_output(monkeypatch):
    service, fixed, progress, calls, raw = setup(monkeypatch)
    original = copy.deepcopy(raw)
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert [call["stage"] for call in calls] == ["read_plan"]
    assert progress["calls"][0]["output"] == raw == original
    assert progress["query_policy"]["reading"]["claims"] == original["claims"]
    assert progress["query_policy"]["reading"]["explanations"][0]["for_claim_ids"] == ["C1_DECLARED"]
    changes = next(iter(progress["model_output_adjustments"].values()))
    assert changes == [
        {
            "path": ["explanations", 0, "for_claim_ids", 0],
            "from": "C1",
            "to": "C1_DECLARED",
            "reason": "unique_short_claim_reference",
        }
    ]
    progress["started_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fixed["budget"]["max_tokens"] = 1
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert len(calls) == 1 and not progress.get("reading_repairs")


def test_unknown_reference_uses_one_compact_repair_and_received_replay(monkeypatch):
    service, fixed, progress, calls, raw = setup(monkeypatch, "C99")
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert [call["stage"] for call in calls] == ["read_plan", "read_plan_repair"]
    entry = next(iter(progress["reading_repairs"].values()))
    assert entry["status"] == "validated"
    assert (
        entry["repair_input_token_estimate"]["tokens"] * 5 <= entry["original_input_token_estimate"]["tokens"]
    )
    assert progress["calls"][0]["output"] == raw
    diagnosis = build_analysis_diagnostics({"id": "SYNTHETIC"}, progress=progress)[
        "reading_validation_errors"
    ]
    assert diagnosis[0]["error_code"] == "analysis_output_invalid"
    assert diagnosis[0]["field_errors"] == [
        {"path": ["explanations", 0, "for_claim_ids", 0], "error_code": "unknown_claim"}
    ]
    assert "SYNTHETIC bounded explanation" not in json.dumps(diagnosis)
    progress["started_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fixed["budget"]["max_tokens"] = 1
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["remove", "rewrite", "invalid"])
def test_reference_patch_cannot_delete_links_rewrite_claims_or_insert_unknown_ids(monkeypatch, mode):
    service, fixed, progress, calls, raw = setup(monkeypatch, "C99", mode=mode)
    with pytest.raises(DomainError):
        service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert len(calls) == 2
    assert progress["calls"][0]["output"] == raw
    assert progress["query_policy"]["reading"] is None
    assert next(iter(progress["reading_repairs"].values()))["status"] == "invalid"


def test_unknown_reference_budget_and_unknown_receipt_do_not_resend(monkeypatch):
    service, fixed, progress, calls, raw = setup(monkeypatch, "C99", prompt_tokens=100)
    for _ in range(2):
        with pytest.raises(DomainError) as failure:
            service._policy_reading("SYNTHETIC", "token", fixed, progress)
        assert failure.value.code == "analysis_token_budget"
    assert len(calls) == 1 and progress["calls"][0]["output"] == raw
    assert next(iter(progress["reading_repairs"].values()))["status"] == "not_sent"

    service, fixed, progress, calls, raw = setup(monkeypatch, "C99", mode="unknown")
    for expected in ["model_transport_unconfirmed", "model_call_outcome_unconfirmed"]:
        with pytest.raises(DomainError) as failure:
            service._policy_reading("SYNTHETIC", "token", fixed, progress)
        assert failure.value.code == expected
    assert len(calls) == 2 and progress["calls"][0]["output"] == raw


def test_duplicate_claim_ids_are_diagnosed_without_entering_semantic_repair(monkeypatch):
    service, fixed, progress, calls, raw = setup(monkeypatch, "C99")
    raw["claims"].append(copy.deepcopy(raw["claims"][0]))
    with pytest.raises(DomainError) as failure:
        service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert failure.value.code == "analysis_output_invalid"
    assert len(calls) == 1 and not progress.get("reading_repairs")
    errors = next(iter(progress["reading_validation_errors"].values()))["field_errors"]
    assert {"path": ["claims"], "error_code": "duplicate_claim_id"} in errors


def test_ambiguous_short_id_never_becomes_an_automatic_binding(monkeypatch):
    service, fixed, progress, calls, raw = setup(monkeypatch, "C1", mode="invalid")
    raw["claims"].append({**copy.deepcopy(raw["claims"][0]), "id": "C1_SECOND"})
    with pytest.raises(DomainError):
        service._policy_reading("SYNTHETIC", "token", fixed, progress)
    assert len(calls) == 2
    assert progress["calls"][0]["output"] == raw
    assert not progress.get("model_output_adjustments")
    assert progress["query_policy"]["reading"] is None


def test_retained_reading_reconstructs_the_same_normalization_from_original_received():
    from bioradar.research.investigations.analysis import normalize_policy_synthesis
    from bioradar.research.investigations.analysis_models import PolicyReading
    from bioradar.research.investigations.reading_execution import retained_policy_reading
    from test_retained_policy_reading import saved_reading

    fixed, progress = saved_reading()
    raw = reading()
    raw["claims"][0]["id"] = "C1_DECLARED"
    raw["explanations"] = [
        {
            "id": "H1",
            "text": "SYNTHETIC unchanged explanation",
            "for_claim_ids": ["C1"],
            "against_claim_ids": [],
            "discriminating_evidence_needed": "SYNTHETIC further source",
        }
    ]
    progress["calls"][0]["output"] = copy.deepcopy(raw)
    normalized, _ = normalize_policy_synthesis(raw)
    progress["query_policy"]["reading"] = PolicyReading.model_validate(normalized).model_dump(mode="json")
    original = copy.deepcopy(progress)
    retained, received = retained_policy_reading(fixed, progress)
    assert retained.model_dump(mode="json") == progress["query_policy"]["reading"]
    assert received["output"] == raw
    assert progress == original
