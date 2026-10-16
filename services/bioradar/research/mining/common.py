"""Small result/provenance helpers; all scores remain descriptive."""

from __future__ import annotations

from typing import Any

from bioradar.research.mining.models import Candidate, EvidenceSupport, MethodResult, canonical_hash
from bioradar.research.mining.prepare import PreparedRecord, PreparedSnapshot
from bioradar.research.mining.registry import METHODS


def support(item: PreparedRecord, quote: str | None = None, start: int | None = None) -> EvidenceSupport:
    record = item.record
    if quote is not None:
        text = record.normalized_text or ""
        offset = text.find(quote) if start is None else start
        if offset < 0 or text[offset : offset + len(quote)] != quote:
            raise ValueError("candidate quote does not match the frozen normalized text")
    else:
        offset = None
    return EvidenceSupport(
        record_ref=record.record_ref,
        document_hash=record.document_hash,
        locator=record.locator,
        evidence_refs=record.evidence_refs,
        quote=quote,
        normalized_start=offset,
        normalized_end=offset + len(quote) if offset is not None and quote is not None else None,
        normalized_text_hash=record.normalized_text_hash,
        source_url=record.source_url,
        quote_status="normalized_only" if quote is not None else "unavailable",
    )


def candidate(
    method_id: str,
    group: Any,
    title: str,
    items: list[PreparedRecord],
    *,
    kind: str = "association",
    metrics: dict | None = None,
    score: float = 0.0,
    reasons: tuple[str, ...] = (),
    data_gaps: tuple[str, ...] = (),
    questions: tuple[str, ...] = (),
    requirements: tuple[str, ...] = (),
    quotes: dict[str, str] | None = None,
    urgency: str = "routine",
    importance: str = "undetermined",
) -> Candidate:
    key = canonical_hash({"method": method_id, "version": METHODS[method_id].version, "group": group})
    by_ref = {item.record.record_ref.version_key: item for item in items}
    supports = tuple(
        support(item, (quotes or {}).get(item.record.document_hash)) for _, item in sorted(by_ref.items())
    )
    return Candidate(
        candidate_key=key,
        method_id=method_id,
        method_version=METHODS[method_id].version,
        group_key=canonical_hash(group),
        kind=kind,
        title=title,
        reasons=reasons,
        supports=supports,
        metrics=metrics or {},
        score=round(float(score), 8),
        data_gaps=tuple(sorted(set(data_gaps))),
        next_questions=questions,
        next_data_requirements=requirements,
        evidence_status="partial" if items and all(i.confirmed_study for i in items) else "unverified",
        urgency=urgency,
        importance=importance,
    )


def result(
    method_id: str,
    prepared: PreparedSnapshot,
    candidates: list[Candidate],
    *,
    readiness: str = "exploratory_only",
    reasons: tuple[str, ...] = (),
    statistics: dict | None = None,
    partial: bool = False,
    limitations: tuple[str, ...] = (),
    truncated: int = 0,
) -> MethodResult:
    candidates.sort(key=lambda c: (-c.score, c.candidate_key))
    limit = prepared.policy.limits.max_candidates_per_method
    extra = max(0, len(candidates) - limit)
    return MethodResult(
        method_id=method_id,
        method_version=METHODS[method_id].version,
        readiness=readiness,
        reasons=reasons,
        execution_status="partial"
        if partial or prepared.truncated_count or truncated or extra
        else "success",
        candidates=tuple(candidates[:limit]),
        statistics={
            **(statistics or {}),
            "candidate_limit_omitted": extra,
            "record_budget_omitted": prepared.truncated_count,
            "truncation_is_resource_diagnostic_not_evidence_count": True,
        },
        excluded_records=prepared.excluded_records,
        truncated_count=truncated + extra + prepared.truncated_count,
        limitations=tuple(sorted(set((*prepared.limitations, *limitations)))),
    )


def unsupported(method_id: str, prepared: PreparedSnapshot, *reasons: str) -> MethodResult:
    return MethodResult(
        method_id=method_id,
        method_version=METHODS[method_id].version,
        readiness="unsupported",
        execution_status="unsupported",
        reasons=tuple(reasons),
        excluded_records=prepared.excluded_records,
        statistics={"eligible_records": len(prepared.records)},
        limitations=prepared.limitations,
    )
