"""Reading refreshes track new content; transport receipts never become repeated source copies."""

import copy
import json

from bioradar.research.investigations.analysis import (
    compact_query_context,
    freeze_payload,
    normalize_policy_synthesis,
    semantic_reading_state,
)


def neighborhood(operation="neighbors"):
    return {
        "operation": operation,
        "run": {"id": "transport", "attempts": ["not reading material"]},
        "binding": {
            "source_id": "primekg_graph",
            "version_id": "fixed-v1",
            "raw_sha256": "fixed-hash",
            "first_seen_at": "2026-10-29T00:00:00Z",
        },
        "read_cutoff": "a transport clock",
        "neighborhood": {
            "nodes": [{"id": "N1", "name": "TARGET"}, {"id": "N2", "name": "OTHER"}],
            "edges": [
                {
                    "id": "R1",
                    "source": {"id": "N1"},
                    "target": {"id": "N2"},
                    "relation_type": "protein_protein",
                    "binding_status": "background",
                    "evidence": [],
                }
            ],
            "limitations": ["Background only; no clinical conclusion."],
            "coverage": {
                "scope": "selected_published_subgraph",
                "truncated": True,
                "selection_truncated": True,
                "stop_reason": "source_selection_limit",
                "returned_edges": 1,
            },
            "seeds_found": [{"id": "N1", "namespace": "test.fixed"}],
            "seeds_missing": [{"id": "N3", "namespace": "test.fixed"}],
        },
    }


def context():
    gap = {
        "reason": "Causal experiment missing",
        "missing_experiment": "Test inhibition under the same condition",
    }
    return {
        "coverage_checks": [
            {
                "id": "mech:gap:1",
                "question": "；".join(gap.values()),
                "status": "unknown",
                "required": True,
                "evidence_ids": [],
                "reason": "Duplicate generic explanation",
            }
        ],
        "mechanism_evaluation": {
            "scope": {"species": "mouse"},
            "critical_gaps": [gap],
            "conflicts": [],
            "claims": [{"id": "C1", "text": "SYNTHETIC condition", "opposing_observation_ids": ["L2"]}],
            "joins": [{"id": "J1", "claim_ids": ["C1"], "opposing_observation_ids": ["L2"]}],
            "evidence": [
                {
                    "id": "L2",
                    "relation": "opposes",
                    "limitations": ["Only in this condition"],
                    "references": [
                        {
                            "id": "P2",
                            "material_id": "E1",
                            "version": "fixed-v1",
                            "text": "Repeated original text",
                        }
                    ],
                }
            ],
            "evidence_map": {"E1": {"text": "Repeated original text"}},
        },
        "query_history": [
            {
                "action_id": "capture-1",
                "provider": "fabric_graph",
                "status": "read",
                "new_versions": 0,
                "plan": {"operation": "capture", "source_id": "primekg_graph", "seed_id": "N1"},
                "graph_context": neighborhood("capture"),
                "cost": {"tool_calls": 1, "tokens": None},
            }
        ],
        "graph_context": [neighborhood("lookup"), neighborhood("capture"), neighborhood()],
    }


def test_compaction_keeps_conditions_opposing_links_fixed_references_and_unknown_cost():
    raw = context()
    before = copy.deepcopy(raw)
    compact = compact_query_context(raw)
    assert raw == before
    assert "graph_context" not in compact["query_history"][0]
    assert compact["query_history"][0]["cost"]["tokens"] is None
    mechanism = compact["mechanism_evaluation"]
    assert mechanism["claims"][0]["opposing_observation_ids"] == ["L2"]
    assert mechanism["joins"][0]["opposing_observation_ids"] == ["L2"]
    assert mechanism["evidence"][0]["limitations"] == ["Only in this condition"]
    reference = mechanism["evidence"][0]["references"][0]
    assert reference == {"id": "P2", "material_id": "E1", "version": "fixed-v1"}
    assert compact["coverage_checks"][0]["mechanism_gap_index"] == 0
    assert mechanism["critical_gaps"] == raw["mechanism_evaluation"]["critical_gaps"]
    assert len(compact["graph_context"]) == 1
    graph = compact["graph_context"][0]
    assert graph["coverage"] == raw["graph_context"][-1]["neighborhood"]["coverage"]
    assert graph["seeds_found"] == raw["graph_context"][-1]["neighborhood"]["seeds_found"]
    assert graph["seeds_missing"] == raw["graph_context"][-1]["neighborhood"]["seeds_missing"]
    assert graph["read_cutoff"] == raw["graph_context"][-1]["read_cutoff"]
    assert graph["binding"]["first_seen_at"] == "2026-10-29T00:00:00Z"


def test_lookup_capture_and_technical_changes_do_not_trigger_reading():
    evidence = [{"key": "E1", "selection_hash": "selection", "text_sha256": "text"}]
    empty = {"graph_context": []}
    technical = {"graph_context": [neighborhood("lookup"), neighborhood("capture")]}
    assert semantic_reading_state(evidence, empty) == semantic_reading_state(evidence, technical)
    raw = context()
    original = semantic_reading_state(evidence, raw)
    raw["graph_context"][-1]["read_cutoff"] = "new clock"
    raw["graph_context"][-1]["binding"]["first_seen_at"] = "2026-10-30T01:00:00Z"
    raw["graph_context"][-1]["binding"]["version_id"] = "same content in another capture"
    raw["graph_context"][-1]["neighborhood"]["nodes"].reverse()
    raw["graph_context"].append(copy.deepcopy(raw["graph_context"][-1]))
    assert semantic_reading_state(evidence, raw) == original


def test_real_neighborhood_evidence_and_professional_gap_changes_trigger_reading():
    evidence = [{"key": "E1", "selection_hash": "selection", "text_sha256": "text"}]
    raw = context()
    original = semantic_reading_state(evidence, raw)
    raw["graph_context"][-1]["neighborhood"]["edges"][0]["relation_type"] = "different_relation"
    assert semantic_reading_state(evidence, raw) != original
    raw = context()
    raw["graph_context"][-1]["neighborhood"]["coverage"]["truncated"] = False
    assert semantic_reading_state(evidence, raw) != original
    raw = context()
    raw["graph_context"][-1]["neighborhood"]["seeds_missing"] = []
    assert semantic_reading_state(evidence, raw) != original
    raw = context()
    raw["mechanism_evaluation"]["critical_gaps"][0]["missing_experiment"] = "A new required control"
    assert semantic_reading_state(evidence, raw) != original
    raw = context()
    evidence[0]["text_sha256"] = "new fixed source text"
    assert semantic_reading_state(evidence, raw) != original


def test_synthesis_encodes_compact_document_and_graph_history_without_changing_legacy_documents():
    fixed = {"question": "TARGET evidence", "as_of": "2026-10-30T00:00:00Z", "search_scope": ["papers"]}
    document = {
        "plan": {"source_id": "papers", "terms": ["TARGET"], "reason": "Not an original fact"},
        "status": "read",
        "returned": 1,
        "new_versions": 1,
        "evidence_keys": ["E1"],
    }
    graph = {
        "provider": "fabric_graph",
        "plan": {
            "provider": "fabric_graph",
            "source_id": "primekg_graph",
            "operation": "neighbors",
            "seed_ids": ["N1"],
            "binding": {"version_id": "fixed-v1", "raw_sha256": "fixed-hash"},
            "direction": "both",
            "max_depth": 1,
            "max_edges": 40,
        },
        "status": "read",
        "new_versions": 0,
    }
    raw = context()
    raw["query_history"] = [document, graph]
    compact = compact_query_context(raw)
    payload = freeze_payload("synthesize", fixed, [], queries=compact["query_history"], query_context=compact)
    assert json.loads(json.dumps(payload, ensure_ascii=False).encode()) == payload
    legacy = freeze_payload("synthesize", fixed, [], queries=[document])
    assert (
        payload["queries"][0]
        == legacy["queries"][0]
        == {
            "source_id": "papers",
            "terms": ["TARGET"],
            "status": "read",
            "returned": 1,
            "new_versions": 1,
            "evidence_keys": ["E1"],
        }
    )
    assert payload["queries"][1]["operation"] == "neighbors"
    assert payload["queries"][1]["seed_ids"] == ["N1"]
    assert payload["queries"][1]["binding"] == graph["plan"]["binding"]
    assert payload["queries"][1]["max_edges"] == 40
    assert "terms" not in payload["queries"][1]
    assert payload["graph_context"] == compact["graph_context"]
    assert payload["as_of"] == fixed["as_of"]
    assert payload["as_of"] != payload["graph_context"][0]["read_cutoff"]
    assert "不是临床或机制支持证据" in payload["graph_context_qualification"]


def test_synthesis_short_claim_references_map_only_to_unique_actual_ids_without_mutating_input():
    raw = {
        "claims": [
            {"id": "C1_TARGET_MECHANISM", "text": "Original conditional claim", "evidence_links": ["E1"]},
            {"id": "C2_OTHER_RESULT", "text": "Original opposing claim", "evidence_links": ["E2"]},
        ],
        "explanations": [
            {"text": "Original explanation", "for_claim_ids": ["C1"], "against_claim_ids": ["C2"]}
        ],
        "conditional_answer": {"answer": "Original qualified answer"},
        "update": {"stance": "initial", "changed_claims": []},
    }
    original = copy.deepcopy(raw)
    normalized, adjustments = normalize_policy_synthesis(raw)
    assert raw == original
    assert normalized["claims"] == original["claims"]
    assert normalized["conditional_answer"] == original["conditional_answer"]
    assert normalized["update"] == original["update"]
    assert normalized["explanations"][0] == {
        "text": "Original explanation",
        "for_claim_ids": ["C1_TARGET_MECHANISM"],
        "against_claim_ids": ["C2_OTHER_RESULT"],
    }
    assert adjustments == [
        {
            "path": ["explanations", 0, "for_claim_ids", 0],
            "from": "C1",
            "to": "C1_TARGET_MECHANISM",
            "reason": "unique_short_claim_reference",
        },
        {
            "path": ["explanations", 0, "against_claim_ids", 0],
            "from": "C2",
            "to": "C2_OTHER_RESULT",
            "reason": "unique_short_claim_reference",
        },
    ]


def test_synthesis_ambiguous_short_claim_reference_remains_unresolved():
    raw = {
        "claims": [{"id": "C1_FIRST"}, {"id": "C1_SECOND"}],
        "explanations": [{"for_claim_ids": ["C1"], "against_claim_ids": []}],
    }
    normalized, adjustments = normalize_policy_synthesis(raw)
    assert normalized == raw and adjustments == []
    assert normalized is not raw


def test_synthesis_unknown_or_nonexact_short_claim_references_remain_unresolved():
    raw = {
        "claims": [{"id": "C1_DETAIL"}, {"id": "C01_DETAIL"}, {"id": "C0_DETAIL"}],
        "explanations": [{"for_claim_ids": ["C9", "c1", "C01", "C0", " C1", "C1X"], "against_claim_ids": []}],
    }
    original = copy.deepcopy(raw)
    normalized, adjustments = normalize_policy_synthesis(raw)
    assert normalized == original and raw == original and adjustments == []


def test_synthesis_existing_full_claim_identity_has_priority_over_longer_prefix_matches():
    raw = {
        "claims": [{"id": "C1"}, {"id": "C1_DETAIL"}],
        "explanations": [{"for_claim_ids": ["C1", "C1_DETAIL"], "against_claim_ids": []}],
    }
    normalized, adjustments = normalize_policy_synthesis(raw)
    assert normalized == raw and adjustments == []


def test_report_scope_freezes_each_round_and_never_retrofits_old_receipts():
    from bioradar.research.investigations.analysis_service import AnalysisService

    fixed = {
        "query_context_version": "semantic-v2",
        "query_policy": {"effective": "query_planner"},
        "search_scope": ["source"],
        "graph_scope": [],
        "evidence_time": {"mode": "operational_replay", "cutoff": "2026-01-01T00:00:00Z"},
    }
    progress = {
        "calls": [],
        "queries": [
            {
                "plan": {"source_id": "source", "terms": ["literal"]},
                "status": "failed",
                "error_code": "source_timeout",
                "returned": None,
            }
        ],
        "query_policy": {
            "checks": [{"id": "support", "status": "unknown", "evidence_ids": []}],
            "status": "insufficient",
            "reason": "scope_exhausted",
        },
    }
    preview = AnalysisService._report_context(fixed, progress, {})
    assert "report_research_scopes" not in progress
    scope = preview["research_scope"]
    assert scope["source_failures"][0]["error_code"] == "source_timeout"
    assert scope["source_failures"][0]["returned"] is None
    assert scope["unresolved_checks"][0]["status"] == "unknown"
    assert AnalysisService._report_context(fixed, progress, {}, freeze=True)["research_scope"] == scope
    progress["queries"].append({"plan": {"source_id": "source"}, "status": "empty"})
    assert AnalysisService._report_context(fixed, progress, {}, freeze=True)["research_scope"] == scope
    progress["active_report_round"] = 1
    assert (
        len(AnalysisService._report_context(fixed, progress, {}, freeze=True)["research_scope"]["queries"])
        == 2
    )
    old = {"calls": [{"stage": "biocheck_check", "call_key": "biocheck_check", "status": "received"}]}
    assert "research_scope" not in AnalysisService._report_context(fixed, old, {}, freeze=True)
    fixed["query_policy"]["effective"] = "baseline"
    assert "research_scope" not in AnalysisService._report_context(fixed, progress, {}, freeze=True)


def test_budget_fallback_never_swallows_an_unknown_model_receipt():
    from datetime import UTC, datetime

    from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt
    from bioradar.research.investigations.analysis_service import AnalysisService

    for status in ("not_sent", "unconfirmed", "failed", "completed"):
        receipt = ModelCallReceipt(
            call_id="synthetic",
            stage="revise",
            input_hash="synthetic",
            prompt_version="synthetic",
            requested_model="synthetic",
            configured_model_revision=None,
            status=status,
            started_at=datetime.now(UTC),
            max_output_tokens=128,
            error_code="analysis_time_budget",
        )
        error = ModelCallError("analysis_time_budget", "SYNTHETIC receipt state", receipt)
        assert AnalysisService._report_budget_stop(error) is (status == "not_sent")


def test_supervised_resumption_does_not_allow_a_second_or_unbounded_window():
    from datetime import UTC, datetime, timedelta

    import pytest
    from bioradar.api.errors import DomainError
    from bioradar.research.investigations.analysis import analysis_budget

    now = datetime.now(UTC)
    start = (now - timedelta(seconds=2000)).isoformat()
    fixed = {
        "budget": {"max_seconds": 1800, "max_calls": 24, "max_tokens": 524288},
        "model_parameters": {"timeout_seconds": 1},
    }
    attempt = {
        "kind": "supervised_continuation",
        "original_started_at": start,
        "started_at": now.isoformat(),
        "max_seconds": 1800,
        "authorization": "SYNTHETIC operator",
    }
    for records in ([attempt, attempt], [{**attempt, "max_seconds": 1801}]):
        with pytest.raises(DomainError) as error:
            analysis_budget(fixed, {"started_at": start, "execution_resumptions": records}, now)
        assert error.value.code == "analysis_resumption_invalid"


def test_support_audit_reserve_keeps_sources_scope_and_draft_without_repeated_assessments():
    from bioradar.research.investigations.analysis import support_audit_input_allowance

    packet = {
        "evidence": [{"id": "E1", "version": "fixed-v1", "text": "SYNTHETIC fixed evidence."}],
        "assessment": {
            "research_scope": {
                "source_ids": ["synthetic-source"],
                "unresolved_checks": [{"id": "counterevidence", "status": "unknown"}],
            }
        },
    }
    draft, limit = "SYNTHETIC bounded draft [E1].", 131072
    repeated = copy.deepcopy(packet)
    repeated["assessment"].update(
        mechanism_evaluation={"critical_gaps": [{"reason": "SYNTHETIC repeated mechanism " * 10000}]},
        radar_analysis={"claims": [{"text": "SYNTHETIC repeated structured finding " * 10000}]},
    )
    with_scope = copy.deepcopy(repeated)
    with_scope["assessment"]["research_scope"]["unresolved_checks"].append(
        {"id": "patents", "status": "unknown", "reason": "SYNTHETIC actual unsearched scope " * 1000}
    )
    with_evidence = copy.deepcopy(repeated)
    with_evidence["evidence"][0]["text"] += " SYNTHETIC additional fixed text." * 1000
    before = copy.deepcopy((packet, repeated, with_scope, with_evidence))

    reserve = support_audit_input_allowance(packet, draft, limit)
    assert reserve < limit
    assert support_audit_input_allowance(repeated, draft, limit) == reserve
    assert support_audit_input_allowance(with_scope, draft, limit) > reserve
    assert support_audit_input_allowance(with_evidence, draft, limit) > reserve
    assert support_audit_input_allowance(repeated, draft + " Longer checked draft." * 1000, limit) > reserve
    assert (packet, repeated, with_scope, with_evidence) == before
