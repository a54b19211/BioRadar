"""Evidence-bound topic risk decisions, independent of analysis and topic services."""

import copy
import hashlib
import re
from typing import Annotated, Literal

from pydantic import Field

from bioradar.api.errors import DomainError
from bioradar.research.engine.schemas import RuntimeModel
from bioradar.research.mining.models import canonical_hash

LocalId = Annotated[str, Field(min_length=1, max_length=80)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TopicRiskScopeStamp(RuntimeModel):
    topic_id: str = Field(min_length=1, max_length=36)
    scope_revision: int = Field(ge=1)
    observation_revision: int = Field(ge=0)
    definition_sha256: Sha256
    risk_basis_sha256: Sha256


class TopicRiskReassessment(RuntimeModel):
    scope: TopicRiskScopeStamp
    decision: Literal["unresolved", "resolved_within_scope"]
    decision_claim_id: LocalId
    decision_statement: str = Field(min_length=1, max_length=2000)
    supporting_claim_ids: list[LocalId] = Field(min_length=1, max_length=6)
    scope_control_claim_ids: list[LocalId] = Field(min_length=1, max_length=6)
    opposing_claim_ids: list[LocalId] = Field(default_factory=list, max_length=6)
    scope_coverage: Literal["full_declared_scope", "partial", "unknown"]
    remaining_critical_gaps: list[str] = Field(default_factory=list, max_length=8)
    conditions: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=2000)


class TopicRiskReviewRequest(RuntimeModel):
    expected_revision: int = Field(ge=1)
    evaluation_id: str = Field(min_length=1, max_length=36)
    analysis_id: str = Field(min_length=1, max_length=36)


def risk_scope_stamp(topic_id, scope_revision, observation_revision, definition, tracking):
    """Stamp the actual adopted topic basis, never an invented model scope."""
    basis = (tracking or {}).get("risk", {}).get("basis_sha256")
    if not basis:
        return None
    return TopicRiskScopeStamp(
        topic_id=topic_id,
        scope_revision=scope_revision,
        observation_revision=observation_revision,
        definition_sha256=canonical_hash(definition),
        risk_basis_sha256=basis,
    ).model_dump(mode="json")


def _completed_rpc(progress, operation, packet, review, audit=None):
    """Find an already received operation for these exact inputs; never invoke it."""
    for identifier, state in progress.get("professional_operations", {}).items():
        binding = state.get("binding") or {}
        exchanges = state.get("exchanges") or []
        if not exchanges or binding.get("provider") != "biocheck" or binding.get("operation") != operation:
            continue
        exchange = exchanges[-1]
        request, response = exchange.get("request") or {}, exchange.get("response") or {}
        args = request.get("arguments") or {}
        if (
            exchange.get("status") != "received"
            or response.get("status") != "completed"
            or binding.get("operation_id") != identifier
            or request.get("operation_id") != identifier
            or request.get("operation") != operation
            or response.get("provider") != "biocheck"
            or response.get("schema_version") != "bioradar.professional.v1"
            or response.get("operation_id") != identifier
            or not state.get("provider_version")
            or response.get("provider_version") != state["provider_version"]
            or binding.get("arguments_hash") != canonical_hash(args)
            or args.get("packet") != packet
        ):
            continue
        actual = response.get("result")
        if operation == "verify":
            matches = args.get("draft") == review["draft"] and actual == review
        else:
            matches = (
                args.get("review") == review
                and args.get("targets") == audit.get("targets")
                and isinstance(actual, dict)
                and actual
                and all(audit.get(key) == value for key, value in actual.items())
            )
        if matches:
            return {"operation_id": identifier, "provider_version": state["provider_version"]}
    return None


def _source_links(claim, evidence):
    """Only current literal source passages may ground risk and scope controls."""
    result = set()
    if claim.get("status") != "supported" or claim.get("kind") != "source_statement":
        return result
    for link in claim.get("evidence_links", []):
        item = evidence.get(link.get("evidence_key"))
        if not item or item.get("evidence_role", "source_material") != "source_material":
            continue
        context = item.get("source_context") or {}
        if (
            context.get("material_kind")
            in {"social_media", "graph", "social_post", "repost", "graph_background"}
            or context.get("statement_status") == "unverified_report"
        ):
            continue
        text, start, end = item.get("text"), link.get("start"), link.get("end")
        offset = item.get("start", 0)
        if (
            link.get("role") == "supports"
            and isinstance(text, str)
            and type(start) is int
            and type(end) is int
            and offset <= start < end <= offset + len(text)
            and text[start - offset : end - offset] == link.get("quote")
        ):
            result.add((item["key"], start, end))
    return result


def _checked_statement(statement, review, audit, *, required_sources=()):
    """Require the actual verified claim span, and fact-audit coverage for its sources."""
    for unit in review.get("units", []):
        if statement not in unit.get("text", "") or unit.get("verdict") != "supported":
            continue
        for index, claim in enumerate(unit.get("claims", [])):
            if claim.get("span") != statement or claim.get("verdict") != "supported":
                continue
            if not required_sources:
                return True
            sources = {e.get("evidence_id") for e in claim.get("evidence", [])}
            if not sources.intersection(required_sources) or [unit["unit_id"], index] not in audit.get(
                "targets", []
            ):
                continue
            for check in audit.get("checks", []):
                if (
                    check.get("unit_id") == unit["unit_id"]
                    and check.get("claim_index") == index
                    and check.get("verdict") == "supported"
                    and sources.intersection(required_sources).intersection(
                        e.get("evidence_id")
                        for e in check.get("evidence", [])
                        if e.get("version")
                        == audit.get("evidence_versions", {}).get(e.get("evidence_id"), {}).get("version")
                    )
                ):
                    return True
    return False


def assess_risk_reassessment(analysis, *, expected_scope):
    """Adopt no truth from PASS alone: check the scoped finding and actual receipts.

    Input is the authorized stored analysis, including its original fixed_input/progress.
    This function makes no network, model, database or mutation calls.
    """
    from bioradar.adapters.biocheck import _feedback_binding
    from bioradar.research.investigations.research_assessment import validate_claim_assessments

    receipt = {
        "status": "rejected",
        "analysis_id": analysis["id"],
        "errors": [],
        "scope": copy.deepcopy(expected_scope),
        "qualification": "machine_verified_resolution_within_frozen_scope_not_general_medical_clearance",
    }
    if analysis.get("state") in {"queued", "running", "blocked"}:
        return {**receipt, "status": "pending", "errors": ["analysis_in_progress"]}
    if analysis.get("state") not in {"completed", "needs_review"}:
        return {**receipt, "errors": ["analysis_execution_failed_pending_review"]}
    result = analysis.get("result") or {}
    report = result.get("report") or {}
    finding = result.get("topic_risk_reassessment")
    if not finding:
        return {**receipt, "errors": ["risk_reassessment_not_provided"]}
    try:
        expected = TopicRiskScopeStamp.model_validate(expected_scope).model_dump(mode="json")
        value = TopicRiskReassessment.model_validate(finding).model_dump(mode="json")
        if value["decision"] == "resolved_within_scope" and (
            analysis.get("progress", {}).get("query_policy") or {}
        ).get("refresh_not_semantically_reviewed"):
            raise ValueError("new_material_not_semantically_reviewed")
        origin = analysis["fixed_input"].get("origin") or {}
        saved = report["topic_risk_binding"]
        packet = analysis["progress"]["report_risk_packets"][str(saved["report_round"])]
        declared = packet.get("assessment", {}).get("research_scope", {})
        if (
            declared.get("topic_risk_scope") != expected
            or canonical_hash(declared.get("topic_risk_definition")) != expected["definition_sha256"]
            or declared.get("topic_risk_definition") != origin.get("topic_risk_definition")
            or packet.get("assessment", {}).get("radar_analysis", {}).get("topic_risk_reassessment") != value
        ):
            raise ValueError("risk_declared_scope_not_checked")
        if (
            value["scope"] != expected
            or origin.get("topic_risk_scope") != expected
            or saved.get("scope") != expected
            or saved.get("reassessment") != value
            or saved.get("packet_sha256") != canonical_hash(packet)
        ):
            raise ValueError("risk_scope_or_packet_mismatch")
        validate_claim_assessments(
            {"topic_risk_reassessment": value}, result["claims"], topic_risk_scope=expected
        )
        review, audit = report["verification"], report["support_audit"]
        text = report["final_text"]
        binding, errors = _feedback_binding(packet, review, audit)
        if errors or any(
            key not in audit or audit[key] != binding[key]
            for key in ("text_sha256", "review_sha256", "evidence_versions")
        ):
            raise ValueError("professional_binding_invalid")
        if (
            report.get("status") != "PASS"
            or review.get("status") != "PASS"
            or audit.get("status") != "PASS"
            or review.get("verification_errors")
            or audit.get("verification_errors")
            or review.get("draft") != text
            or report.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest()
            or value["decision_statement"] not in text
        ):
            raise ValueError("professional_review_not_passed_for_exact_text")
        verify_receipt = _completed_rpc(analysis["progress"], "verify", packet, review)
        audit_receipt = _completed_rpc(analysis["progress"], "audit_support", packet, review, audit)
        if not verify_receipt or not audit_receipt:
            raise ValueError("professional_completed_receipt_missing")
        if not _checked_statement(value["decision_statement"], review, audit):
            raise ValueError("risk_decision_statement_not_verified")
        claims = {claim["id"]: claim for claim in result["claims"]}
        evidence = {item["key"]: item for item in analysis["progress"].get("evidence", [])}
        packet_evidence = {item["id"]: item for item in packet["evidence"]}
        roles, statements = [], []
        for field in ("supporting_claim_ids", "scope_control_claim_ids"):
            role_links, role_statements = set(), set()
            for identifier in value[field]:
                claim = claims[identifier]
                links = _source_links(claim, evidence)
                for key in {link[0] for link in links}:
                    original = packet_evidence.get(key)
                    current = evidence[key]
                    if (
                        original is None
                        or packet.get("evidence_map", {}).get(key, {}).get("record_ref")
                        != current["record_ref"]
                        or original["text"] != current["text"]
                    ):
                        raise ValueError("risk_basis_fixed_version_mismatch")
                if not links or not _checked_statement(
                    claim["text"], review, audit, required_sources={link[0] for link in links}
                ):
                    raise ValueError("risk_basis_not_source_bound_and_audited")
                role_links.update(links)
                role_statements.add(re.sub(r"[\W_]+", "", claim["text"].casefold()))
            roles.append(role_links)
            statements.append(role_statements)
        # Radar links preserve whole fixed passages, so two verified arguments may
        # share one passage. Repeating one statement under two IDs is not two arguments.
        if not (statements[0] - statements[1] and statements[1] - statements[0]):
            raise ValueError("risk_and_scope_require_distinct_checked_statements")
        receipt.update(
            status="accepted",
            decision=value["decision"],
            reassessment=value,
            report_text_sha256=report["text_sha256"],
            packet_sha256=saved["packet_sha256"],
            professional_receipts={"verify": verify_receipt, "audit_support": audit_receipt},
            basis_source_ids=sorted(
                {evidence[key]["source_id"] for key in {link[0] for links in roles for link in links}}
            ),
            basis_refs=[
                copy.deepcopy(evidence[key]["record_ref"])
                for key in sorted({link[0] for links in roles for link in links})
            ],
        )
    except (DomainError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        # Stable reason codes only; never expose provider text or restricted material.
        reason = (
            exc.code
            if isinstance(exc, DomainError)
            else str(exc)
            if type(exc) is ValueError and str(exc).replace("_", "").isalnum()
            else "risk_reassessment_binding_invalid"
        )
        receipt["errors"] = [reason]
    return receipt
