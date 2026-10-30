"""Versioned research input safety and fixed unread manifests; no model or source calls."""

import copy
import hashlib
import json

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import (
    freeze_passages,
    research_task_context,
    validate_policy_reading,
)
from bioradar.research.investigations.analysis_models import ModelReading, ModelSynthesis, PolicyReading
from bioradar.research.investigations.query_policy import (
    build_coverage_checks,
    build_policy_context,
    build_read_manifest,
    document_read_candidates,
    normalize_policy_coverage,
)

pytestmark = pytest.mark.ENGINE


def fixed(direction="public_health"):
    return {
        "question": "SYNTHETIC 未确认病原聚集是否应预警",
        "is_demo": True,
        "as_of": "2026-10-31T00:00:00+00:00",
        "evidence_time": {"mode": "current", "cutoff": "2026-10-31T00:00:00+00:00"},
        "search_scope": ["external_archive"],
        "budget": {"max_queries": 6},
        "query_policy": {
            "effective": "query_planner",
            "schema_version": "query-planner.external.v2",
            "direction": direction,
            "research_contract_version": "bioradar-research-input-v1",
        },
    }


def ref(name, version):
    return {
        "provider": "data_fabric_history",
        "object_type": "source_record",
        "object_id": json.dumps(["external_archive", name, True]),
        "adopted_version": version * 64,
        "version_source": "provider",
        "content_hash": "a" * 64,
        "authorization_ref": "SYNTHETIC fixed source",
    }


def evidence():
    text = "SYNTHETIC 据称存在聚集病例，尚未确认病原或传播方式。"
    return freeze_passages(
        {
            "key": "E1",
            "material_entry_id": None,
            "record_ref": ref("first", "1"),
            "source_id": "external_archive",
            "snapshot_id": "SYNTHETIC-snapshot",
            "title": "SYNTHETIC source",
            "text": text,
            "start": 0,
            "end": len(text),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    )


def reading():
    return {
        "claims": [
            {
                "id": "C1",
                "text": "据称存在聚集病例，待独立核实。",
                "kind": "source_statement",
                "scope": "该固定原文的转述",
                "conditions": ["真实性尚未证实"],
                "status": "supported",
                "evidence_links": [
                    {
                        "evidence_key": "E1",
                        "passage_id": "E1:p001",
                        "role": "supports",
                        "reason": "原文仅报告待核传言。",
                    }
                ],
            }
        ],
        "explanations": [],
        "gaps": [],
        "next_queries": [],
        "conditional_answer": {"answer": "先核实来源。", "conditions": [], "unknowns": ["病原未知"]},
        "warning_assessment": {
            "decision": "alert",
            "supporting_claim_ids": ["C1"],
            "opposing_claim_ids": [],
            "critical_unknowns": ["病原未知"],
            "next_actions": ["取得独立医院原始确认"],
            "reason": "有具体待核说法，需要立即核实。",
        },
        "competing_hypotheses": [
            {
                "id": "H1",
                "explanation": "季节性聚集或其他病原仍待区分",
                "supporting_claim_ids": [],
                "opposing_claim_ids": [],
                "discriminating_evidence_needed": "独立原始检测结果",
                "untested_conditions": ["未取得检测"],
            }
        ],
    }


def test_unknown_and_conflicting_model_coverage_are_unknown_without_mutating_receipt():
    data = reading()
    data["coverage_checks"] = [
        {"id": "answer_scope", "status": "passed", "evidence_ids": ["E1"]},
        {"id": "answer_scope", "status": "failed", "evidence_ids": ["E1"]},
        {"id": "invented-id", "status": "passed", "evidence_ids": ["E1"]},
    ]
    before = copy.deepcopy(data)
    declared = [{"id": "answer_scope", "status": "unknown"}]
    normalized, issues = normalize_policy_coverage(data, declared, [evidence()])
    assert data == before
    assert normalized["coverage_checks"] == [
        {
            "id": "answer_scope",
            "status": "unknown",
            "evidence_ids": [],
            "reason": "模型重复报告同一覆盖编号，状态保留未知。",
        }
    ]
    assert {item["check_id"] for item in issues} == {"answer_scope", "invented-id"}
    validate_policy_reading(normalized, [evidence()], ["external_archive"])
    checks = build_coverage_checks(fixed(), {"evidence": [evidence()]}, reading=normalized)
    assert next(x for x in checks if x["id"] == "answer_scope")["status"] == "unknown"
    with pytest.raises(DomainError, match="重复"):
        normalize_policy_coverage(data, declared + declared, [evidence()])


def test_coverage_recovery_does_not_relax_actual_passages_or_hypothesis_claim_identity():
    data = reading()
    validated = validate_policy_reading(data, [evidence()], ["external_archive"])
    assert validated.claims[0].text.startswith("据称")
    assert "真实性" in validated.claims[0].conditions[0]
    wrong = copy.deepcopy(data)
    wrong["claims"][0]["evidence_links"][0]["passage_id"] = "fabricated-position"
    with pytest.raises(DomainError) as exc:
        validate_policy_reading(wrong, [evidence()], ["external_archive"])
    assert exc.value.code == "analysis_passage_invalid"
    wrong = copy.deepcopy(data)
    wrong["competing_hypotheses"][0]["supporting_claim_ids"] = ["not-a-claim"]
    with pytest.raises(DomainError) as exc:
        validate_policy_reading(wrong, [evidence()], ["external_archive"])
    assert exc.value.code == "analysis_output_invalid"
    wrong = copy.deepcopy(data)
    wrong["warning_assessment"]["supporting_claim_ids"] = []
    with pytest.raises(DomainError):
        validate_policy_reading(wrong, [evidence()], ["external_archive"])


def test_new_task_contract_has_direction_checks_and_leaves_old_baseline_schema_untouched():
    for direction, required in [
        ("public_health", {"warning_judgment", "independent_confirmation", "pathogen_uncertainty"}),
        ("business", {"orders", "corrections", "cash_receipts", "comparable_periods"}),
    ]:
        f = fixed(direction)
        checks = build_coverage_checks(f, {"evidence": []})
        assert required.issubset({x["id"] for x in checks})
        assert research_task_context(f)["direction"] == direction
    for model in [ModelReading, ModelSynthesis]:
        assert "warning_assessment" not in model.model_fields
        assert "competing_hypotheses" not in model.model_fields
    old = fixed()
    old["query_policy"].pop("research_contract_version")
    assert research_task_context(old) == {}
    old["query_policy"]["effective"] = "baseline"
    assert research_task_context(old) == {}
    value = reading()
    value.pop("warning_assessment")
    value.pop("competing_hypotheses")
    assert "warning_assessment" not in PolicyReading.model_validate(value).model_dump(mode="json")


def test_fixed_manifest_prioritizes_actual_corrections_and_never_invents_missing_version():
    old, correction = ref("old", "1"), ref("correction", "2")
    scope = {
        "query_policy": {"checks": [{"id": "corrections"}, {"id": "answer_scope"}]},
        "kdd_source_version_refs": [
            {"record_ref": old},
            {
                "record_ref": correction,
                "signal_context": {"source_relations": [{"kind": "correction", "target_ref": old}]},
            },
            {"source_id": "external_archive", "source_record_id": "not-fixed", "version_id": None},
        ],
        "evidence": [{"record_ref": old}],
        "queries": [],
    }
    before = copy.deepcopy(scope)
    manifest = build_read_manifest(fixed("business"), scope)
    assert len(manifest) == 2 and scope == before
    current = next(x for x in manifest if x["source_record_id"] == "correction")
    assert current["priority"] == "critical" and current["status"] == "unread"
    assert current["reason"] == "correction"
    actions = document_read_candidates(fixed("business"), scope, manifest, scope["query_policy"]["checks"])
    assert len(actions) == 1 and actions[0]["kind"] == "document_read"
    assert actions[0]["arguments"]["records"] == [
        {
            "source_record_id": "correction",
            "version_id": "2" * 64,
        }
    ]
    assert actions[0]["arguments"]["scope"]["is_demo"] is True
    assert all(x["status"] != "read" or x["source_record_id"] == "old" for x in manifest)


def test_malformed_graph_candidate_still_fails_hard_after_model_coverage_fix():
    candidate = {
        "id": "SYNTHETIC-illegal-covers",
        "kind": "graph_neighbors",
        "arguments": {},
        "covers": ["not-declared"],
    }
    with pytest.raises(DomainError) as exc:
        build_policy_context(
            fixed(),
            {"evidence": [], "queries": []},
            phase="initial",
            plans=[],
            graph_candidates=[candidate],
        )
    assert exc.value.code == "analysis_policy_coverage_invalid"


def test_automatic_wait_clock_is_clipped_to_window_and_manual_keeps_original_deadline():
    from datetime import UTC, datetime, timedelta

    from bioradar.research.investigations.analysis import analysis_budget

    started = datetime(2026, 10, 31, tzinfo=UTC)
    f = {
        "model_parameters": {"timeout_seconds": 60},
        "budget": {"max_calls": 16, "max_tokens": 524288, "max_seconds": 100, "max_queries": 16},
        "execution_budget": {"version": "automatic-buckets-v1", "pool": "monitor"},
    }
    p = {
        "started_at": started.isoformat(),
        "calls": [],
        "queries": [],
        "budget_wait_intervals": [
            {
                "reason": "analysis_automatic_budget_wait",
                "started_at": (started + timedelta(seconds=10)).isoformat(),
                "ended_at": (started + timedelta(seconds=90)).isoformat(),
            }
        ],
    }
    now = started + timedelta(seconds=100)
    usage = analysis_budget(f, p, now)["usage"]
    assert usage["remaining_seconds"] == 80 and usage["elapsed_seconds"] == 20
    assert usage["calls"] == usage["budgeted_tokens"] == 0
    p["finished_at"] = (started + timedelta(seconds=5)).isoformat()
    usage = analysis_budget(f, p, now)["usage"]
    assert usage["elapsed_seconds"] == 5 and usage["remaining_seconds"] == 80
    f["execution_budget"]["pool"] = "manual"
    assert analysis_budget(f, p, now)["usage"]["remaining_seconds"] == 0


def test_new_action_limits_are_fixed_and_visible_without_counting_windows_as_versions(monkeypatch):
    from types import SimpleNamespace

    from bioradar.adapters import query_planner
    from bioradar.research.investigations.query_policy import freeze_policy

    monkeypatch.setattr(
        query_planner, "provider_status", lambda settings: {"available": True, "version": "v"}
    )
    monkeypatch.setattr(query_planner, "load_query_planner", lambda settings: (None, "v"))
    cfg = SimpleNamespace(
        analysis_query_policy="query_planner",
        analysis_kdd_enabled=True,
        analysis_query_policy_v2_max_steps=32,
        analysis_v2_action_limits={"document_read": 16, "source_fetch": 2},
    )
    policy = freeze_policy(cfg)
    assert policy["stop"] == "observed_checks_reading_v2" and policy["max_steps"] == 32
    assert policy["action_limits"] == {"document_read": 16, "source_fetch": 2}
    cfg.analysis_v2_action_limits["document_read"] = 1
    assert policy["action_limits"]["document_read"] == 16
    f = fixed()
    f["query_policy"].update(policy)
    f["reading_limits"] = {"max_versions": 2}
    f["providers"] = {
        "weknora": {
            "available": True,
            "is_demo": True,
            "knowledge_base_id": "SYNTHETIC-demo",
            "source_scope": "simulation",
        }
    }
    e = evidence()
    p = {"queries": [], "evidence": [e, {**e, "key": "E2", "start": 10}]}
    context = build_policy_context(
        f,
        p,
        phase="initial",
        plans=[
            {
                "source_id": "external_archive",
                "terms": ["SYNTHETIC"],
                "reason": "SYNTHETIC query",
            }
        ],
    )
    assert context["action_limits"] == policy["action_limits"]
    assert any(item["arguments"].get("provider") == "weknora" for item in context["candidates"])


def test_new_model_projection_removes_exact_body_copy_and_unread_tail_but_preserves_native_numbers():
    from bioradar.research.investigations.analysis import model_evidence

    e = evidence()
    full = e["text"] + "未读尾段" * 2000
    e["facts"] = {"full_text": full, "patient_count": 12, "structured": {"cash": "12.50"}}
    e["field_locators"] = {
        "full_text": {"json_pointer": "/full_text"},
        "patient_count": {"json_pointer": "/patient_count"},
        "structured": {"json_pointer": "/structured"},
    }
    before = copy.deepcopy(e)
    legacy = model_evidence([e])[0]
    new = model_evidence([e], include_source_context=True)[0]
    assert legacy["facts"]["full_text"] == full
    assert "full_text" not in new["facts"] and "未读尾段" not in json.dumps(new, ensure_ascii=False)
    assert new["facts"] == {"patient_count": 12, "structured": {"cash": "12.50"}}
    assert new["field_locators"]["patient_count"] == {"json_pointer": "/patient_count"}
    assert new["passages"] == legacy["passages"]
    assert e == before
