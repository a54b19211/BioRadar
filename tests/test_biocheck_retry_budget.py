"""New professional repair stages share the existing host receipt/budget machinery."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.biocheck import audit_biocheck_support, build_packet, run_biocheck
from bioradar.adapters.reasoning_model import ModelCallReceipt, ModelCompletion, ModelUsage
from bioradar.api.errors import DomainError
from test_kdd_finishing_budget import local_biocheck as local_biocheck  # noqa: F401
from test_kdd_finishing_budget import response, state


def test_opt_in_arguments_freeze_once_and_existing_plans_stay_legacy():
    service, _, progress = state("pipeline")
    capabilities = {
        "schema_version": "bioradar.professional.v1",
        "provider": "biocheck",
        "provider_version": "SYNTHETIC-v1",
        "features": ["bounded_plan_repair", "bounded_verify_contract_repair"],
        "plan_retry_policy": {
            "argument": "max_plan_retries",
            "default": 0,
            "maximum": 1,
            "stages": ["replan_plain", "replan_bound"],
            "diagnostic_limit": 8,
        },
        "verify_retry_policy": {
            "argument": "max_verify_retries",
            "default": 0,
            "maximum": 1,
            "stages": ["verify_repair"],
            "diagnostic_limit": 8,
        },
    }
    loaded = (SimpleNamespace(capabilities=capabilities, provider_version="SYNTHETIC-v1"), "SYNTHETIC-v1")
    original = copy.deepcopy(progress["final_report_plan"])
    service._freeze_report_retries("synthetic", "lock", progress, loaded)
    assert progress["final_report_plan"] == original
    progress["final_report_plan"]["retry_policy_pending"] = True
    service._freeze_report_retries("synthetic", "lock", progress, loaded)
    assert progress["final_report_plan"]["max_plan_retries"] == 1
    assert progress["final_report_plan"]["max_verify_retries"] == 1
    saved = copy.deepcopy(progress["final_report_plan"])
    capabilities["features"] = []
    service._freeze_report_retries("synthetic", "lock", progress, loaded)
    assert progress["final_report_plan"] == saved


def test_native_two_contract_repairs_then_received_replay_uses_zero_new_posts(local_biocheck, monkeypatch):
    service, fixed, progress = state("pipeline")
    fixed["model_parameters"]["max_calls"] = fixed["budget"]["max_calls"] = 6
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("No real model HTTP"))
    posts = []

    def complete(settings, **kwargs):
        stage = kwargs["stage"]
        posts.append(stage)
        parsed = response(
            {
                "biocheck_plan_repair": "biocheck_plan",
                "biocheck_verify_repair": "biocheck_check",
            }.get(stage, stage)
        )
        if stage == "biocheck_plan":
            parsed["points"][0]["support"][0]["segment_id"] = "S999"
        if stage == "biocheck_check":
            parsed["reviews"][0]["claims"][0].pop("verdict")
        receipt = ModelCallReceipt(
            call_id=kwargs["call_id"],
            stage=stage,
            input_hash=kwargs["input_hash"],
            prompt_version=kwargs["prompt_version"],
            requested_model="SYNTHETIC-model",
            configured_model_revision=None,
            status="completed",
            started_at=datetime.now(UTC),
            max_output_tokens=settings.semantic_llm_max_output_tokens,
            usage=ModelUsage(prompt_tokens=64, completion_tokens=64, total_tokens=128),
        )
        return ModelCompletion(content=json.dumps(parsed), parsed=parsed, receipt=receipt)

    service.model_call = complete
    packet = build_packet(
        fixed["question"],
        progress["evidence"],
        service._report_context(fixed, progress, progress["query_policy"]["reading"]),
    )
    callback = service._shared_callback("synthetic", "lock", fixed, progress)
    report = run_biocheck(
        None,
        packet,
        callback,
        repair=False,
        loaded=local_biocheck,
        max_plan_retries=1,
        max_verify_retries=1,
    )
    assert report["status"] == "PASS"
    audit = audit_biocheck_support(None, packet, report["verification"], callback, loaded=local_biocheck)
    assert audit["status"] == "PASS"
    assert posts == [
        "biocheck_plan",
        "biocheck_plan_repair",
        "biocheck_write",
        "biocheck_check",
        "biocheck_verify_repair",
        "biocheck_support_audit",
    ]
    saved_calls = copy.deepcopy(progress["calls"])
    progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    replay = run_biocheck(
        None,
        packet,
        callback,
        repair=False,
        loaded=local_biocheck,
        max_plan_retries=1,
        max_verify_retries=1,
    )
    assert replay == report
    assert (
        audit_biocheck_support(
            None,
            packet,
            replay["verification"],
            callback,
            loaded=local_biocheck,
        )
        == audit
    )
    assert len(posts) == 6 and progress["calls"] == saved_calls


@pytest.mark.parametrize(
    "stage,remaining_calls",
    [
        ("biocheck_plan_repair", 3),
        ("biocheck_verify_repair", 1),
        ("biocheck_recheck_repair", 1),
    ],
)
def test_contract_repair_keeps_mandatory_following_calls_before_sending(stage, remaining_calls):
    service, fixed, progress = state("research")
    fixed["model_parameters"]["max_calls"] = fixed["budget"]["max_calls"] = remaining_calls
    service.model_call = lambda *a, **k: pytest.fail("Repair must not spend required finishing slots")
    with pytest.raises(DomainError) as error:
        service._call(
            "synthetic",
            "lock",
            stage,
            {"type": "object"},
            fixed,
            progress,
            {"question": fixed["question"], "evidence": []},
        )
    assert error.value.code == "analysis_model_budget" and not progress["calls"]
