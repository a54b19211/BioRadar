"""Synthetic frozen risk findings; PASS/quiet/environment signals alone never resolve risk."""

import copy
import hashlib

import pytest
from bioradar.adapters.biocheck import _feedback_binding, build_packet
from bioradar.research.mining.models import canonical_hash
from bioradar.topics.risk import assess_risk_reassessment, risk_scope_stamp
from bioradar.topics.tracking import update_tracking
from test_mining import AS_OF, record
from test_opus_tracking import DEFINITION, value

pytestmark = pytest.mark.ENGINE
SUPPORT = "SYNTHETIC surveillance found zero linked cases within District Z during interval T."
CONTROL = (
    "SYNTHETIC surveillance covered all facilities in District Z during interval T with validated sampling."
)
DECISION = "SYNTHETIC risk is resolved only within District Z during interval T under validated surveillance."


def bound_analysis(scope=None):
    """Explicit synthetic professional receipts, not real-world risk declarations."""
    source = record("SYNTHETIC-control", SUPPORT + "\n" + CONTROL)
    text = SUPPORT + "\n" + CONTROL
    evidence = {
        "key": "E1",
        "source_id": "fixture-source",
        "record_ref": source.record_ref.model_dump(mode="json"),
        "text": text,
        "start": 0,
        "end": len(text),
        "evidence_role": "source_material",
        "source_context": {"material_kind": "official_report"},
    }
    if scope is None:
        tracking = update_tracking(
            [value(record("SYNTHETIC-risk", "北江医院收治7例不明原因肺炎。"))],
            DEFINITION,
            {},
            as_of=AS_OF,
        )
        scope = risk_scope_stamp("synthetic-topic", 1, 1, DEFINITION, tracking)
    finding = {
        "scope": scope,
        "decision": "resolved_within_scope",
        "decision_claim_id": "C0",
        "decision_statement": DECISION,
        "supporting_claim_ids": ["C1"],
        "scope_control_claim_ids": ["C2"],
        "opposing_claim_ids": [],
        "scope_coverage": "full_declared_scope",
        "remaining_critical_gaps": [],
        "conditions": ["Only District Z during interval T under validated surveillance."],
        "reason": "SYNTHETIC complete scoped observation and sampling coverage, not silence or denial.",
    }
    claims = []
    for identifier, statement in (("C0", DECISION), ("C1", SUPPORT), ("C2", CONTROL)):
        quote = SUPPORT if identifier == "C0" else statement
        start = text.index(quote)
        claims.append(
            {
                "id": identifier,
                "text": statement,
                "kind": "inference" if identifier == "C0" else "source_statement",
                "scope": "District Z interval T",
                "conditions": [],
                "status": "supported",
                "evidence_links": [
                    {
                        "evidence_key": "E1",
                        "role": "supports",
                        "quote": quote,
                        "start": start,
                        "end": start + len(quote),
                        "reason": "SYNTHETIC literal source",
                    }
                ],
            }
        )
    packet = build_packet(
        "SYNTHETIC scoped risk",
        [evidence],
        {
            "research_scope": {"topic_risk_scope": scope, "topic_risk_definition": DEFINITION},
            "radar_analysis": {"topic_risk_reassessment": finding},
        },
    )
    draft = "\n\n".join((DECISION, SUPPORT, CONTROL))
    units, offset = [], 0
    for index, statement in enumerate((DECISION, SUPPORT, CONTROL)):
        units.append(
            {
                "unit_id": f"U{index}",
                "start": offset,
                "end": offset + len(statement),
                "text": statement,
                "verdict": "supported",
                "issues": [],
                "claims": [
                    {
                        "kind": "fact",
                        "span": statement,
                        "verdict": "supported",
                        "evidence": [{"evidence_id": "E1", "version": packet["evidence"][0]["version"]}],
                    }
                ],
            }
        )
        offset += len(statement) + 2
    verification = {"draft": draft, "status": "PASS", "units": units, "verification_errors": []}
    audit, _ = _feedback_binding(packet, verification, None)
    audit.update(
        status="PASS",
        original_status="PASS",
        provider_version="SYNTHETIC-method-v1",
        targets=[[f"U{i}", 0] for i in range(3)],
        verification_errors=[],
        checks=[
            {
                "unit_id": f"U{i}",
                "claim_index": 0,
                "verdict": "supported",
                "evidence": [{"evidence_id": "E1", "version": packet["evidence"][0]["version"]}],
            }
            for i in range(3)
        ],
    )
    operations = {}
    for operation in ("verify", "audit_support"):
        args = (
            {"packet": packet, "draft": draft}
            if operation == "verify"
            else {
                "packet": packet,
                "review": verification,
                "targets": audit["targets"],
            }
        )
        actual = (
            verification
            if operation == "verify"
            else {
                "status": "PASS",
                "original_status": "PASS",
                "checks": audit["checks"],
                "verification_errors": [],
            }
        )
        operations[operation] = {
            "provider_version": "SYNTHETIC-method-v1",
            "status": "completed",
            "binding": {
                "provider": "biocheck",
                "operation_id": operation,
                "operation": operation,
                "arguments_hash": canonical_hash(args),
            },
            "exchanges": [
                {
                    "status": "received",
                    "request": {"operation_id": operation, "operation": operation, "arguments": args},
                    "response": {
                        "operation_id": operation,
                        "provider": "biocheck",
                        "schema_version": "bioradar.professional.v1",
                        "status": "completed",
                        "provider_version": "SYNTHETIC-method-v1",
                        "result": actual,
                    },
                }
            ],
        }
    return {
        "id": "synthetic-analysis",
        "state": "completed",
        "fixed_input": {
            "origin": {"source": "topic", "topic_risk_scope": scope, "topic_risk_definition": DEFINITION}
        },
        "progress": {
            "evidence": [evidence],
            "report_risk_packets": {"0": packet},
            "professional_operations": operations,
            "calls": [],
        },
        "result": {
            "claims": claims,
            "topic_risk_reassessment": finding,
            "report": {
                "final_text": draft,
                "text_sha256": hashlib.sha256(draft.encode()).hexdigest(),
                "status": "PASS",
                "verification": verification,
                "support_audit": audit,
                "topic_risk_binding": {
                    "scope": scope,
                    "reassessment": finding,
                    "report_round": 0,
                    "packet_sha256": canonical_hash(packet),
                },
            },
        },
    }


def test_exact_scoped_receipts_accept_distinct_arguments_from_one_fixed_original():
    analysis = bound_analysis()
    before = copy.deepcopy(analysis)
    scope = analysis["fixed_input"]["origin"]["topic_risk_scope"]
    result = assess_risk_reassessment(analysis, expected_scope=scope)
    assert result["status"] == "accepted", result
    assert result["decision"] == "resolved_within_scope"
    assert result["reassessment"]["conditions"]
    assert (
        len(result["basis_refs"]) == 1
    )  # Same reliable original and passage may cover different checked arguments.
    assert result["professional_receipts"]["verify"]["operation_id"] == "verify"
    assert analysis == before


@pytest.mark.parametrize(
    "change,reason",
    [
        ("scope", "risk_declared_scope_not_checked"),
        ("packet", "risk_scope_or_packet_mismatch"),
        ("draft", "professional_review_not_passed_for_exact_text"),
        ("audit", "professional_binding_invalid"),
        ("unknown", "professional_completed_receipt_missing"),
        ("missing_verified_statement", "risk_decision_statement_not_verified"),
        ("same_basis", "risk_and_scope_require_distinct_checked_statements"),
        ("context_only", "risk_basis_not_source_bound_and_audited"),
        ("social_media", "risk_basis_not_source_bound_and_audited"),
        ("graph_kind", "risk_basis_not_source_bound_and_audited"),
        ("unverified_report", "risk_basis_not_source_bound_and_audited"),
        ("wrong_current_version", "risk_basis_fixed_version_mismatch"),
        ("wrong_audit_version", "risk_basis_not_source_bound_and_audited"),
    ],
)
def test_pass_is_not_enough_if_scope_receipts_or_actual_risk_basis_do_not_bind(change, reason):
    analysis = bound_analysis()
    scope = copy.deepcopy(analysis["fixed_input"]["origin"]["topic_risk_scope"])
    report = analysis["result"]["report"]
    if change == "scope":
        scope["observation_revision"] += 1
    elif change == "packet":
        analysis["progress"]["report_risk_packets"]["0"]["question"] = "Changed frozen context"
    elif change == "draft":
        report["final_text"] += " Changed after verification."
    elif change == "audit":
        report["support_audit"]["review_sha256"] = "f" * 64
    elif change == "unknown":
        analysis["progress"]["professional_operations"]["audit_support"]["exchanges"][-1]["status"] = (
            "unknown"
        )
    elif change == "missing_verified_statement":
        report["verification"]["units"][0]["claims"][0]["span"] = "SYNTHETIC unrelated phrase"
        rebind_professional(analysis)
    elif change == "same_basis":
        analysis["result"]["claims"][2]["text"] = SUPPORT
        analysis["result"]["claims"][2]["evidence_links"] = copy.deepcopy(
            analysis["result"]["claims"][1]["evidence_links"]
        )
    elif change == "wrong_current_version":
        analysis["progress"]["evidence"][0]["record_ref"]["adopted_version"] = "f" * 64
    elif change == "wrong_audit_version":
        for check in report["support_audit"]["checks"]:
            check["evidence"][0]["version"] = "wrong-version"
    elif change in {"social_media", "graph_kind", "unverified_report"}:
        context = analysis["progress"]["evidence"][0]["source_context"]
        context.update(
            {"statement_status": "unverified_report"}
            if change == "unverified_report"
            else {"material_kind": "graph" if change == "graph_kind" else "social_media"}
        )
    elif change == "context_only":
        analysis["progress"]["evidence"][0]["evidence_role"] = "graph_background"
    result = assess_risk_reassessment(analysis, expected_scope=scope)
    assert result["status"] == "rejected" and reason in result["errors"], result


def rebind_professional(analysis):
    report, progress = analysis["result"]["report"], analysis["progress"]
    packet, review = progress["report_risk_packets"]["0"], report["verification"]
    binding, _ = _feedback_binding(packet, review, None)
    report["support_audit"].update(binding)
    verify = progress["professional_operations"]["verify"]
    verify["exchanges"][-1]["response"]["result"] = copy.deepcopy(review)
    audit = progress["professional_operations"]["audit_support"]
    audit["exchanges"][-1]["request"]["arguments"]["review"] = copy.deepcopy(review)
    audit["binding"]["arguments_hash"] = canonical_hash(audit["exchanges"][-1]["request"]["arguments"])


def test_unknown_analysis_waits_and_final_receipt_is_consumed_without_any_send(monkeypatch):
    import httpx

    analysis = bound_analysis()
    scope = analysis["fixed_input"]["origin"]["topic_risk_scope"]
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("No risk-review request"))
    analysis["state"] = "blocked"
    assert assess_risk_reassessment(analysis, expected_scope=scope)["status"] == "pending"
    analysis["state"] = "completed"
    first = assess_risk_reassessment(analysis, expected_scope=scope)
    assert first["status"] == "accepted"
    assert first == assess_risk_reassessment(analysis, expected_scope=scope)


def test_resolution_cools_but_new_semantic_risk_reopens_without_upgrade_of_truth():
    from datetime import timedelta

    first = value(record("SYNTHETIC-first", "北江医院收治7例不明原因肺炎。", source_family="same-source"))
    initial = update_tracking([first], DEFINITION, {}, as_of=AS_OF)
    resolved = copy.deepcopy(initial)
    resolved["risk"].update(
        status="resolved", critical_gaps=[], assessment={"decision": "resolved_within_scope"}
    )
    quiet = update_tracking([first], DEFINITION, resolved, as_of=AS_OF + timedelta(days=1))
    quiet = update_tracking([first], DEFINITION, quiet, as_of=AS_OF + timedelta(days=2))
    assert quiet["risk"]["status"] == "resolved" and quiet["lifecycle"] == "dormant"
    assert quiet["cadence_hours"] > 1 and not quiet["model_update"]["needed"]
    denial = value(record("SYNTHETIC-denial", "北江医院辟谣7例不明原因肺炎。", source_family="same-source"))
    reaction = update_tracking([first, denial], DEFINITION, quiet, as_of=AS_OF + timedelta(days=3))
    assert reaction["risk"]["status"] == "resolved"  # Denial alone neither resolves nor creates disease.
    changed = value(
        record(
            "SYNTHETIC-new", "北江医院收治9例不明原因肺炎，传播方式尚未确认。", source_family="same-source"
        )
    )
    reopened = update_tracking(
        [first, denial, changed], DEFINITION, reaction, as_of=AS_OF + timedelta(days=4)
    )
    assert reopened["risk"]["status"] == "unresolved" and reopened["lifecycle"] == "warming"
    assert reopened["risk"]["prior_resolution"] == {"decision": "resolved_within_scope"}
    assert reopened["cadence_hours"] == 1 and reopened["model_update"]["needed"]
    assert reopened["evidence"]["level"] != "independent_sources"


def test_source_bound_new_numeric_period_reopens_even_without_narrative_event_anchors():
    from datetime import timedelta

    item = value(record("SYNTHETIC-count", "SYNTHETIC source-bound surveillance count."))
    item["observation"] = {
        "kind": "metric",
        "metric_id": "SYNTHETIC_count",
        "metric_version": "SYNTHETIC-v1",
        "value": {"lower": "3", "upper": "3"},
        "unit": "count",
        "status": "active",
        "period_start": "2026-01-01",
        "period_end": "2026-01-08",
        "population": "SYNTHETIC covered population",
        "region": "SYNTHETIC region",
        "basis": {"kind": "source_bound", "ref": item["record_ref"]},
    }
    first = update_tracking([item], DEFINITION, {}, as_of=AS_OF)
    assert first["risk"]["status"] == "unresolved"
    reviewed = copy.deepcopy(first)
    reviewed["risk"].update(
        status="resolved", critical_gaps=[], assessment={"decision": "resolved_within_scope"}
    )
    same = update_tracking([item], DEFINITION, reviewed, as_of=AS_OF + timedelta(hours=1))
    assert same["risk"]["status"] == "resolved"
    changed = copy.deepcopy(item)
    changed["observation"]["value"] = {"lower": "4", "upper": "4"}
    assert (
        update_tracking([changed], DEFINITION, same, as_of=AS_OF + timedelta(hours=2))["risk"]["status"]
        == "unresolved"
    )


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("failed", "analysis_execution_failed_pending_review"),
        ("unreviewed_material", "new_material_not_semantically_reviewed"),
    ],
)
def test_completed_professional_pass_cannot_close_a_failed_or_unreviewed_analysis(fault, reason):
    analysis = bound_analysis()
    if fault == "failed":
        analysis["state"] = "failed"
    else:
        analysis["progress"]["query_policy"] = {
            "refresh_not_semantically_reviewed": {
                "reason": "SYNTHETIC new material arrived after the last semantic read"
            }
        }
    before = copy.deepcopy(analysis)
    scope = analysis["fixed_input"]["origin"]["topic_risk_scope"]
    result = assess_risk_reassessment(analysis, expected_scope=scope)
    assert result["status"] == "rejected" and result["errors"] == [reason]
    assert analysis == before and analysis["result"]["report"]["status"] == "PASS"


def test_disappearing_old_risk_or_reappearing_same_statement_is_not_a_new_risk_event():
    from datetime import timedelta

    a = value(record("SYNTHETIC-a", "北江医院收治7例不明原因肺炎。", source_family="same-source"))
    b = value(record("SYNTHETIC-b", "南山医院收治3例不明原因肺炎。", source_family="another-source"))
    state = update_tracking([a, b], DEFINITION, {}, as_of=AS_OF)
    state["risk"].update(
        status="resolved", critical_gaps=[], assessment={"decision": "resolved_within_scope"}
    )
    reviewed_basis = copy.deepcopy(state["risk"])
    fewer = update_tracking([a], DEFINITION, state, as_of=AS_OF + timedelta(hours=1))
    assert fewer["risk"]["status"] == "resolved"
    assert fewer["risk"]["basis_sha256"] == reviewed_basis["basis_sha256"]
    assert fewer["risk"]["basis_atoms"] == reviewed_basis["basis_atoms"]
    assert "reopened_reason" not in fewer["risk"]
    returned = update_tracking([a, b], DEFINITION, fewer, as_of=AS_OF + timedelta(hours=2))
    assert returned["risk"]["status"] == "resolved"
    assert returned["risk"]["basis_sha256"] == reviewed_basis["basis_sha256"]
