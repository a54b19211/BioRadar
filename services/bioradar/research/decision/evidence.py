"""Validate adopted versions, local visibility, literal citations and typed facts.

The runtime must authorize each snapshot against its current server grant first.
No client claim of authorship, permission or a past date authenticates that claim.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from decimal import Decimal
from typing import Iterator

from bioradar.research.decision.models import DecisionInput, InputBasis, MetricSpec, NumericInterval
from bioradar.research.mining.models import NormalizedSnapshot, canonical_hash
from bioradar.research.mining.prepare import normalized_records, publication_interval, snapshot_hash


def input_bases(
    decision_input: DecisionInput,
) -> Iterator[tuple[str, InputBasis, NumericInterval | None, str | None]]:
    def key(*parts: str | int) -> str:
        return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))

    for scenario in sorted(decision_input.scenarios, key=lambda s: s.scenario_id):
        yield key("scenario", scenario.scenario_id), scenario.basis, None, None
    for cell in sorted(decision_input.outcomes, key=lambda c: (c.option_id, c.scenario_id, c.metric_id)):
        yield (
            key("outcome", cell.option_id, cell.scenario_id, cell.metric_id),
            cell.basis,
            cell.value,
            cell.metric_id,
        )
    for constraint in sorted(decision_input.constraints, key=lambda c: c.constraint_id):
        yield (
            key("constraint", constraint.constraint_id),
            constraint.basis,
            constraint.threshold,
            constraint.metric_id,
        )
    for option in sorted(decision_input.options, key=lambda o: o.option_id):
        if option.implementation_owner:
            yield key("owner", option.option_id), option.implementation_owner.basis, None, None
        for field in (
            "implementation_steps",
            "beneficiaries",
            "adversely_affected",
            "irreversible_commitments",
        ):
            for index, statement in enumerate(getattr(option, field)):
                yield key(field, option.option_id, index), statement.basis, None, None
    interests = decision_input.brief.interest_declarations
    for role in ("commissioner", "funder", "author", "reviewer"):
        disclosure = getattr(interests, role)
        if disclosure.statement:
            yield key("interest", role), disclosure.statement.basis, None, None
    if decision_input.brief.why_now:
        yield key("why_now"), decision_input.brief.why_now.basis, None, None
    review = decision_input.brief.review_plan
    if review:
        yield key("review_plan"), review.basis, None, None
        for field in ("trigger_indicators", "change_conditions"):
            for index, statement in enumerate(getattr(review, field)):
                yield key("review", field, index), statement.basis, None, None


def _periods(metric: MetricSpec) -> set[str]:
    start, end = metric.period_start, metric.period_end
    periods = {f"{start.isoformat()}/{end.isoformat()}"}
    if start == end:
        periods.add(start.isoformat())
    if start == date(start.year, 1, 1) and end == date(start.year, 12, 31):
        periods.add(str(start.year))
    if start.day == 1 and end == date(
        start.year, start.month, calendar.monthrange(start.year, start.month)[1]
    ):
        periods.add(f"{start.year:04d}-{start.month:02d}")
    return periods


def validate_evidence(
    decision_input: DecisionInput, snapshots: tuple[NormalizedSnapshot, ...] = ()
) -> tuple[str, ...]:
    """Raise for invalid evidence; return explicit limits of valid current citations."""
    if len(snapshots) > 8 or sum(len(s.records) for s in snapshots) > 20000:
        raise ValueError("decision_evidence_budget_exceeded")
    hashes = [snapshot_hash(snapshot) for snapshot in snapshots]
    if len(set(hashes)) != len(hashes):
        raise ValueError("duplicate_decision_evidence_snapshots")
    if set(hashes) != set(decision_input.evidence_snapshot_hashes):
        raise ValueError("decision_evidence_snapshot_binding_mismatch")
    demo_states = {s.is_demo for s in snapshots}
    if len(demo_states) > 1 or (True in demo_states and not decision_input.is_demo):
        raise ValueError("decision_demo_and_source_status_mismatch")
    by_hash = dict(zip(hashes, snapshots, strict=True))
    record_indices = {
        key: {canonical_hash(record.record_ref): record for record in normalized_records(snapshot)}
        for key, snapshot in by_hash.items()
    }
    metrics = {m.metric_id: m for m in decision_input.metrics}
    gaps: set[str] = set()
    for path, basis, value, metric_id in input_bases(decision_input):
        if basis.kind != "observed":
            gaps.add("assumptions_and_model_results_are_current_declarations_not_historical_knowledge")
        numeric_support = False
        for link in sorted(basis.evidence, key=canonical_hash):
            snapshot = by_hash.get(link.snapshot_hash)
            if snapshot is None:
                raise ValueError("decision_reference_to_unbound_snapshot")
            record = record_indices[link.snapshot_hash].get(canonical_hash(link.record_ref))
            if record is None:
                raise ValueError("decision_reference_not_exact_adopted_record_version")
            if (
                snapshot.observed_at > decision_input.brief.as_of
                or record.observed_at > decision_input.brief.as_of
            ):
                raise ValueError("decision_evidence_not_locally_observed_by_cutoff")
            if record.available_at is not None and record.available_at > decision_input.brief.as_of:
                raise ValueError("decision_evidence_available_after_cutoff")
            interval = publication_interval(record)
            if interval and interval[0] > decision_input.brief.as_of:
                raise ValueError("decision_evidence_future_publication")
            if record.available_at is None:
                gaps.add("unknown_source_availability_uses_current_frozen_export_observation_only")
            if link.evidence_ref and canonical_hash(link.evidence_ref) not in {
                canonical_hash(ref) for ref in record.evidence_refs
            }:
                raise ValueError("decision_reference_not_exact_adopted_evidence_version")
            if basis.kind == "observed" and {"retracted", "deleted"}.intersection(record.record_flags):
                raise ValueError("decision_observation_uses_retracted_or_deleted_record")
            if basis.kind != "observed" and {"retracted", "deleted"}.intersection(record.record_flags):
                gaps.add(f"declared_input_cites_inactive_source:{canonical_hash(record.record_ref)}")
            if link.quote is not None and (
                record.text_status != "inline_authorized"
                or not record.normalized_text
                or link.quote not in record.normalized_text
            ):
                raise ValueError("decision_quote_not_literal_in_fixed_authorized_text")
            if link.field_path is not None:
                match = re.fullmatch(r"/regional_indicators/(0|[1-9]\d*)/value", link.field_path)
                if not match or int(match[1]) >= len(record.regional_indicators):
                    raise ValueError("decision_typed_field_path_not_supported")
                indicator = record.regional_indicators[int(match[1])]
                if (
                    indicator.provenance.available_at
                    and indicator.provenance.available_at > decision_input.brief.as_of
                ):
                    raise ValueError("decision_typed_facet_available_after_cutoff")
                if indicator.provenance.available_at is None:
                    gaps.add("typed_facet_availability_unknown_current_export_only")
                if basis.kind == "observed" and value is not None:
                    if metric_id is None:
                        raise ValueError("decision_numeric_fact_requires_metric_scope")
                    metric = metrics[metric_id]
                    if (
                        metric.unit != indicator.unit
                        or metric.geography != indicator.region
                        or metric.population != indicator.population_scope
                        or metric.jurisdiction != indicator.jurisdiction
                        or indicator.period not in _periods(metric)
                        or metric.definition != indicator.indicator
                        or metric.denominator
                        != (
                            Decimal(str(indicator.denominator)) if indicator.denominator is not None else None
                        )
                    ):
                        raise ValueError("decision_numeric_fact_scope_or_unit_mismatch")
                    source_value = Decimal(str(indicator.value))
                    if value.lower != source_value or value.upper != source_value:
                        raise ValueError("decision_numeric_fact_differs_from_adopted_value")
                    numeric_support = True
                    if metric.price_base is not None:
                        gaps.add(f"price_base_is_declared_not_verified_in_source_facet:{metric_id}")
        if basis.kind == "observed" and value is not None and not numeric_support:
            raise ValueError("decision_observed_numeric_requires_bound_typed_value")
        if basis.kind == "observed" and value is None:
            gaps.add(f"source_bound_statement_semantics_not_independently_verified:{path}")
    return tuple(sorted(gaps))


def evidence_basis_hash(decision_input: DecisionInput, snapshots: tuple[NormalizedSnapshot, ...] = ()) -> str:
    validate_evidence(decision_input, snapshots)
    # This is the adopted source manifest. Selected refs/assumptions/scope and
    # qualification diagnostics are bound by decision_input_hash/result_hash.
    return canonical_hash({"snapshot_hashes": sorted(snapshot_hash(s) for s in snapshots)})
