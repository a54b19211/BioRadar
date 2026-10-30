"""A supplementary read must retain its normal output allowance or the valid prior read."""

import copy
import hashlib
from datetime import UTC, datetime

import pytest
from bioradar.adapters.reasoning_model import ModelCallReceipt, ModelCompletion, ModelUsage
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import freeze_passages
from bioradar.research.investigations.analysis_models import AnalysisEvidence
from test_kdd_finishing_budget import state


def reading():
    return {
        "claims": [
            {
                "id": "C1",
                "text": "SYNTHETIC fixed source statement.",
                "kind": "source_statement",
                "scope": "fixed source only",
                "conditions": [],
                "status": "supported",
                "evidence_links": [
                    {
                        "evidence_key": "E1",
                        "passage_id": "E1:p001",
                        "role": "supports",
                        "reason": "SYNTHETIC source",
                    }
                ],
            }
        ],
        "explanations": [],
        "gaps": [],
        "next_queries": [],
        "conditional_answer": {"answer": "SYNTHETIC prior bounded answer", "conditions": [], "unknowns": []},
        "coverage_checks": [],
    }


def harness(monkeypatch):
    import bioradar.research.investigations.query_policy as policy

    service, fixed, progress = state("pipeline")
    text = "SYNTHETIC fixed source statement."
    progress["evidence"] = [
        freeze_passages(
            AnalysisEvidence(
                key="E1",
                material_entry_id=None,
                snapshot_id="SYNTHETIC-snapshot",
                record_ref={
                    "provider": "SYNTHETIC",
                    "object_type": "document",
                    "object_id": "source-1",
                    "adopted_version": "fixed-v1",
                    "version_source": "provider",
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "authorization_ref": "SYNTHETIC-grant",
                },
                source_id="synthetic-source",
                title="SYNTHETIC fixed source",
                text=text,
                start=0,
                end=len(text),
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            ).model_dump(mode="json")
        )
    ]
    progress["query_policy"].update(phase="followup", reading=reading(), initial_plans=[])
    service._final_token_allowance = lambda *args: 10000
    service._synthesis_payload = lambda *args: {"task": "synthesize", "SYNTHETIC": "required final stage"}
    monkeypatch.setattr(policy, "build_policy_context", lambda *args, **kwargs: {"checks": []})
    return service, fixed, progress


def test_optional_read_skips_rather_than_shrinking_normal_output(monkeypatch):
    service, fixed, progress = harness(monkeypatch)
    previous = copy.deepcopy(progress["query_policy"]["reading"])
    normal = fixed["model_parameters"]["max_output_tokens"]
    captured = {}
    actual_budget = service._optional_budget

    def narrow_budget(fixed, progress, *args, **kwargs):
        captured.update(kwargs)
        # This permits a tiny response but is one token short of the requested normal cap.
        fixed["budget"]["max_tokens"] = 10000 + kwargs["request_tokens"] + kwargs["following_tokens"] - 1
        return actual_budget(fixed, progress, *args, **kwargs)

    service._optional_budget = narrow_budget
    service.model_call = lambda *args, **kwargs: pytest.fail("No optional POST with insufficient full cap")
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True) is False
    assert captured["request_tokens"] > normal
    assert progress["calls"] == []
    assert progress["query_policy"]["reading"] == previous
    assert progress["query_policy"]["refresh_not_semantically_reviewed"] is True
    assert progress["query_policy"]["refresh_stopped_reason"] == "analysis_finishing_budget_reserved"


def test_sufficient_optional_read_sends_normal_cap(monkeypatch):
    service, fixed, progress = harness(monkeypatch)
    observed = []

    def synthetic(config, **kwargs):
        observed.append(config.semantic_llm_max_output_tokens)
        now = datetime.now(UTC)
        return ModelCompletion(
            content="SYNTHETIC bounded reading JSON",
            parsed=reading(),
            receipt=ModelCallReceipt(
                call_id=kwargs["call_id"],
                stage=kwargs["stage"],
                input_hash=kwargs["input_hash"],
                prompt_version=kwargs["prompt_version"],
                requested_model=config.semantic_llm_model,
                configured_model_revision=fixed["model_revision"],
                status="completed",
                started_at=now,
                completed_at=now,
                elapsed_ms=1,
                usage=ModelUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                max_output_tokens=config.semantic_llm_max_output_tokens,
            ),
        )

    service.model_call = synthetic
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True) is True
    assert observed == [32768]
    assert progress["calls"][0]["status"] == "received"


def saved_optional(service, fixed, progress, *, status, receipt_status, error_code):
    captured = []

    def stop_before_send(*args, **kwargs):
        captured.append((args, kwargs))
        raise DomainError("SYNTHETIC-capture", "capture optional read inputs", 409)

    original_call = service._call
    service._call = stop_before_send
    with pytest.raises(DomainError, match="capture optional"):
        service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    service._call = original_call
    args, kwargs = captured[0]
    now = datetime.now(UTC)
    progress["calls"] = [
        {
            "stage": "read_plan",
            "call_key": kwargs["call_key"],
            "call_id": "SYNTHETIC-saved-read",
            "request_signature": "SYNTHETIC-earlier-projection-signature",
            "input_hash": "SYNTHETIC-input",
            "prompt_version": fixed["prompt_version"],
            "status": status,
            "model_parameters": {**fixed["model_parameters"], "max_output_tokens": 3990},
            "receipt": ModelCallReceipt(
                call_id="SYNTHETIC-saved-read",
                stage="read_plan",
                input_hash="SYNTHETIC-input",
                prompt_version=fixed["prompt_version"],
                requested_model=fixed["model"],
                configured_model_revision=fixed["model_revision"],
                status=receipt_status,
                error_code=error_code,
                started_at=now,
                completed_at=now,
                elapsed_ms=25937,
                http_status=200 if receipt_status == "failed" else None,
                finish_reason="length" if receipt_status == "failed" else None,
                max_output_tokens=3990,
                usage=ModelUsage(prompt_tokens=25636, completion_tokens=3990, total_tokens=29626)
                if receipt_status == "failed"
                else ModelUsage(),
            ).model_dump(mode="json"),
        }
    ]
    return progress["calls"][0]


def test_known_http200_truncated_optional_read_keeps_prior_and_failed_receipt(monkeypatch):
    service, fixed, progress = harness(monkeypatch)
    previous = copy.deepcopy(progress["query_policy"]["reading"])
    failed = saved_optional(
        service,
        fixed,
        progress,
        status="failed",
        receipt_status="failed",
        error_code="model_output_truncated",
    )
    original = copy.deepcopy(failed)
    service.model_call = lambda *args, **kwargs: pytest.fail("Known failed optional read cannot be resent")
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True) is False
    assert progress["calls"] == [original]
    assert progress["query_policy"]["reading"] == previous
    assert progress["query_policy"]["optional_read_failures"][0]["error_code"] == "model_output_truncated"


def test_unknown_optional_read_remains_blocked_without_fallback(monkeypatch):
    service, fixed, progress = harness(monkeypatch)
    saved = saved_optional(
        service,
        fixed,
        progress,
        status="unconfirmed",
        receipt_status="unconfirmed",
        error_code="model_transport_unconfirmed",
    )
    saved.pop("request_signature")
    with pytest.raises(DomainError) as error:
        service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True)
    assert error.value.code == "model_call_outcome_unconfirmed"
    assert "refresh_stopped_reason" not in progress["query_policy"]


def test_prepared_optional_read_with_small_cap_is_not_sent_or_retried(monkeypatch):
    service, fixed, progress = harness(monkeypatch)
    saved = saved_optional(
        service,
        fixed,
        progress,
        status="prepared",
        receipt_status="not_sent",
        error_code="analysis_token_budget",
    )
    saved.pop("receipt")
    service.model_call = lambda *args, **kwargs: pytest.fail("Prepared reduced cap cannot send")
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True) is False
    assert saved["status"] == "failed" and saved["receipt"]["status"] == "not_sent"
    assert saved["model_parameters"]["max_output_tokens"] == 3990
    assert service._policy_reading("SYNTHETIC", "token", fixed, progress, optional=True) is False
