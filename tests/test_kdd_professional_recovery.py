"""A received professional result finishes its nullable query receipt without replaying providers."""

import copy
from types import SimpleNamespace

import httpx
import pytest
from bioradar.research.investigations import kdd, professional
from bioradar.research.investigations.analysis_models import AnalysisQueryReceipt
from bioradar.research.mining.models import canonical_hash

pytestmark = pytest.mark.ENGINE


@pytest.mark.parametrize("saved_usage", [None, {}, {"saved_marker": "retained"}])
def test_completed_professional_action_restores_nullable_usage_without_new_requests(monkeypatch, saved_usage):
    identifier = "SYNTHETIC already-received action"
    mechanism = "SYNTHETIC A acts through M."
    source = {
        "id": "sources:synthetic",
        "version": "fixed-source-v1",
        "kind": "source_documents",
        "snapshot_ids": ["synthetic-fixed-snapshot"],
        "scope": {"direction": "research", "mode": "current", "cutoff": "2026-01-01T00:00:00+00:00"},
    }
    arguments = {
        "scope": dict(source["scope"]),
        "input_refs": [{"id": source["id"], "version": source["version"]}],
        "capability_id": "mech_evidence.evaluate",
        "capability_version": "saved-provider-v1",
    }
    action = {"id": identifier, "kind": "professional_assess", "arguments": arguments}
    receipt = AnalysisQueryReceipt(
        index=0,
        provider="professional",
        kind="professional_assess",
        action_id=identifier,
        status="prepared",
        plan=arguments,
    ).model_dump(mode="json")
    assert receipt["usage"] is None
    receipt["usage"] = saved_usage
    assessment = {
        "status": "completed",
        "provider": "mech_evidence",
        "passage_bindings": [],
        "qualification": "SYNTHETIC saved result, not a scientific evaluation",
    }
    digest = canonical_hash(assessment)
    artifact = {
        "scope": source["scope"],
        "source_artifact_id": source["id"],
        "source_version": source["version"],
        "snapshot_ids": source["snapshot_ids"],
        "id": "professional:" + digest[:24],
        "version": digest,
        "kind": "professional_assessment",
        "provider": "mech_evidence",
        "status": "completed",
        "qualification": "Professional result, not scientific approval.",
    }
    calls = [
        {
            "call_id": "saved-model-" + str(index),
            "action_id": identifier,
            "stage": "mech_local_review",
            "status": "received",
            "receipt": {
                "status": "completed",
                "usage": {"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100},
            },
        }
        for index in range(3)
    ]
    progress = {
        "queries": [receipt],
        "calls": calls,
        "query_policy": {},
        "kdd_artifacts": [source, artifact],
        "mechanism_evaluation": assessment,
        "kdd_mechanism_basis": canonical_hash([source["version"], mechanism]),
        "professional_operations": {
            "saved-op": {"action_id": identifier, "actual_cost": {"tool_calls": 5, "reserved_tool_calls": 0}},
        },
    }
    fixed = {
        "query_context_version": "semantic-v2",
        "query_policy": {"direction": "research", "effective": "query_planner"},
        "evidence_time": {"mode": "current", "cutoff": source["scope"]["cutoff"]},
        "budget": {"max_queries": 6, "max_calls": 8, "max_tokens": 10000, "max_seconds": 1800},
        "model_parameters": {"max_calls": 8, "timeout_seconds": 30},
    }
    before_calls = copy.deepcopy(calls)
    before_artifacts = copy.deepcopy(progress["kdd_artifacts"])
    before_operations = copy.deepcopy(progress["professional_operations"])
    service = SimpleNamespace(
        _guard=lambda *args, **kwargs: None,
        _checkpoint=lambda *args, **kwargs: None,
        _saved_policy_reading=lambda state: SimpleNamespace(mechanism_candidate=mechanism),
    )
    service._mechanism = lambda *args: professional.mechanism(service, *args)
    monkeypatch.setattr(kdd, "source_snapshot", lambda *args: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("A completed received professional action must send no provider/model HTTP")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    restored = kdd.execute_action(service, "synthetic-analysis", "unused-lock", fixed, progress, action, 0)
    assert restored["status"] == "read"
    assert restored["cost"]["model_calls"] == 3 and restored["cost"]["tokens"] == 300
    assert restored["cost"]["tool_calls"] == 5
    assert restored["usage"]["model_call_ids"] == [call["call_id"] for call in calls]
    if saved_usage:
        assert restored["usage"]["saved_marker"] == "retained"
    assert progress["calls"] == before_calls and progress["kdd_artifacts"] == before_artifacts
    assert progress["professional_operations"] == before_operations
    repeated = kdd.execute_action(service, "synthetic-analysis", "unused-lock", fixed, progress, action, 0)
    assert repeated == restored
    assert progress["calls"] == before_calls


@pytest.mark.parametrize("operation_status", ["completed", "model_required"])
def test_completed_mechanism_rpc_restores_before_new_work_budget_reservation(monkeypatch, operation_status):
    import json

    from bioradar.adapters.biocheck import build_packet
    from bioradar.adapters.mech_evidence import evaluate_mechanism
    from bioradar.api.config import Settings
    from bioradar.research.investigations.analysis_service import AnalysisService
    from pydantic import SecretStr
    from test_kdd_finishing_budget import state

    host, fixed, progress = state("research")
    host.settings = Settings(
        mech_evidence_transport="http",
        mech_evidence_service_url="http://synthetic.invalid",
        mech_evidence_service_token=SecretStr("SYNTHETIC service key"),
    )
    fixed["providers"]["mech_evidence"] = {
        "transport": "http",
        "available": True,
        "version": "SYNTHETIC-method-v1",
        "service_endpoint": "http://synthetic.invalid",
    }
    fixed["budget"]["max_calls"] = fixed["model_parameters"]["max_calls"] = 8
    progress["provider_versions"] = {"mech_evidence": "SYNTHETIC-method-v1"}
    progress["active_professional_action"] = "SYNTHETIC-mechanism-action"
    progress["evidence"][0]["facts"] = {"source_kind": "primary"}
    progress["calls"] = [
        {
            "call_id": "SYNTHETIC-received-" + str(index),
            "status": "received",
            "stage": stage,
            "receipt": {
                "status": "completed",
                "usage": {"prompt_tokens": total - 50, "completion_tokens": 50, "total_tokens": total},
            },
        }
        for index, (stage, total) in enumerate(
            zip(
                ["read_plan", "mech_decomposition", "mech_local_review", "mech_mechanism_review"],
                [36134, 3620, 19306, 22748],
                strict=True,
            )
        )
    ]
    calls_before = copy.deepcopy(progress["calls"])
    host._use_provider = lambda *args: None
    host._load_professional = lambda *args: professional.load_provider(host, *args)
    host._shared_callback = lambda *args: forbidden
    reading = SimpleNamespace(mechanism_candidate="SYNTHETIC A acts through M.")
    wire = []
    original_client = httpx.Client

    def forbidden(*args, **kwargs):
        raise AssertionError("Confirmed professional response needs no model callback")

    def completed(request):
        wire.append(request.method)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "schema_version": "bioradar.professional.v1",
                "provider": "mech_evidence",
                "operation_id": body["operation_id"],
                "provider_version": "SYNTHETIC-method-v1",
                "status": "completed",
                "result": {"overall": {"qualification": "SYNTHETIC only"}, "evidence": []},
            },
        )

    monkeypatch.setattr(
        professional.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(completed), **kwargs),
    )
    packet = build_packet(
        fixed["question"],
        progress["evidence"],
        {
            "mechanism": reading.mechanism_candidate,
            "qualification": "machine_proposed_candidate_not_source_fact",
        },
    )
    # Save the real RPC envelope/response through the transport, then simulate the crash
    # before its derived mechanism_evaluation field was checkpointed.
    loaded = professional.load_provider(host, "SYNTHETIC-analysis", "lock", fixed, progress, "mech_evidence")
    expected = evaluate_mechanism(host.settings, packet, forbidden, loaded=loaded)
    assert expected["status"] == "completed" and wire == ["POST"]
    saved_operation = next(
        state
        for state in progress["professional_operations"].values()
        if (state.get("binding") or {}).get("operation") == "evaluate"
    )
    saved_operation["status"] = operation_status
    assert saved_operation["exchanges"][-1]["response"]["status"] == "completed"
    assert "mechanism_evaluation" not in progress
    assert not AnalysisService._optional_budget(host, fixed, progress, 3, following=1)
    professional.mechanism(host, "SYNTHETIC-analysis", "lock", fixed, progress, reading)
    assert progress["mechanism_evaluation"] == expected
    assert wire == ["POST"] and progress["calls"] == calls_before

    # A different workflow slot or fixed input version cannot borrow this receipt.
    for changed in ("action", "version"):
        progress.pop("mechanism_evaluation")
        if changed == "action":
            progress["active_professional_action"] = "SYNTHETIC-another-action"
        else:
            progress["active_professional_action"] = "SYNTHETIC-mechanism-action"
            progress["evidence"][0]["record_ref"]["adopted_version"] = "SYNTHETIC-different-source-version"
        professional.mechanism(host, "SYNTHETIC-analysis", "lock", fixed, progress, reading)
        assert progress["mechanism_evaluation"]["status"] == "skipped_budget"
        assert wire == ["POST"] and progress["calls"] == calls_before
