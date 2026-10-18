"""Auditable baselines. Pure functions: no clock, network, database or language model."""

from calendar import monthrange
from collections import defaultdict
from datetime import datetime

from bioradar.research.history.models import (
    HistoryMetrics,
    HistoryObservation,
    HistoryOutcome,
    HistoryPrediction,
    HistoryStudyCreate,
    aware,
)


def horizon_end(cutoff: datetime, months: int) -> datetime:
    aware(cutoff)
    year, month = divmod(cutoff.year * 12 + cutoff.month - 1 + months, 12)
    return cutoff.replace(year=year, month=month + 1, day=min(cutoff.day, monthrange(year, month + 1)[1]))


def _event_index(observation):
    index = defaultdict(list)
    for event in observation.events:
        index[(event.unit_kind, event.unit_id, event.event_type)].append(event)
    return index


def outcome_for(
    prediction: HistoryPrediction, observation: HistoryObservation, *, events=None
) -> HistoryOutcome:
    unit = prediction.unit
    if not unit.eligible:
        return HistoryOutcome(unit_id=unit.unit_id, status="not_eligible", reasons=unit.reasons)
    evidence = [
        e
        for e in (observation.events if events is None else events)
        if e.unit_id == unit.unit_id
        and e.unit_kind == unit.unit_kind
        and e.event_type == prediction.target
        and e.public_available_at <= observation.cutoff
    ]
    positive = sorted(
        (
            e
            for e in evidence
            if e.event_observed is True
            and prediction.cutoff < e.public_available_at <= prediction.horizon_end
        ),
        key=lambda e: (e.public_available_at, e.evidence_ref.version_key),
    )
    event = positive[0] if positive else None
    # Fixed-H scores never select only the early positives from an immature cohort.
    if observation.cutoff < prediction.horizon_end:
        return HistoryOutcome(
            unit_id=unit.unit_id,
            status="censored",
            observed_event_at=event.public_available_at if event else None,
            event_refs=(event.evidence_ref,) if event else (),
            reasons=("horizon_not_mature_observed_events_are_not_fixed_horizon_scores",),
        )
    negatives = [
        e
        for e in evidence
        if e.event_observed is False
        and e.observation_start is not None
        and e.observation_end is not None
        and e.observation_start <= prediction.cutoff
        and e.observation_end >= prediction.horizon_end
        and e.observation_end <= min(observation.cutoff, e.public_available_at)
    ]
    if positive and negatives:
        return HistoryOutcome(
            unit_id=unit.unit_id,
            status="unknown",
            event_refs=tuple(e.evidence_ref for e in (*positive, *negatives)),
            reasons=("conflicting_event_and_explicit_no_event_evidence",),
        )
    if event is not None:
        return HistoryOutcome(
            unit_id=unit.unit_id,
            status="event",
            label=1,
            observed_event_at=event.public_available_at,
            event_refs=tuple(e.evidence_ref for e in positive),
            reasons=("public_disclosure_target_not_therapeutic_success",)
            + (("historical_event_disclosed_later_not_new_occurrence",) if event.is_backfill else ()),
        )
    if negatives:
        return HistoryOutcome(
            unit_id=unit.unit_id,
            status="no_event",
            label=0,
            event_refs=tuple(e.evidence_ref for e in negatives),
            reasons=("explicit_covered_window_no_event_not_permanent_failure",),
        )
    return HistoryOutcome(
        unit_id=unit.unit_id,
        status="unknown",
        reasons=("no_explicit_outcome_or_complete_negative_observation_for_this_unit",),
    )


def make_predictions(
    request: HistoryStudyCreate,
    cohort: HistoryObservation,
    training: list[tuple[HistoryObservation, HistoryObservation]],
) -> tuple[tuple[HistoryPrediction, ...], tuple[str, ...]]:
    if cohort.cutoff != request.cutoff:
        raise ValueError("cohort cutoff differs from frozen request")

    def at_risk(unit, observation, index):
        reasons = list(unit.reasons)
        if unit.unit_kind != request.unit_kind:
            reasons.append("unit_kind_not_target")
        if any(
            e.unit_id == unit.unit_id
            and e.unit_kind == unit.unit_kind
            and e.event_type == request.target
            and e.event_observed is True
            and e.public_available_at <= observation.cutoff
            for e in index[(unit.unit_kind, unit.unit_id, request.target)]
        ):
            reasons.append("target_already_public_at_feature_cutoff")
        return unit.model_copy(
            update={"eligible": unit.eligible and not reasons, "reasons": tuple(dict.fromkeys(reasons))}
        )

    cohort_index = _event_index(cohort)
    risk_units = [at_risk(u, cohort, cohort_index) for u in cohort.units]
    test_lineages = {u.lineage_id for u in risk_units if u.eligible}
    candidates = {}
    gaps = []
    for features, labels in sorted(training, key=lambda pair: pair[0].cutoff):
        end = horizon_end(features.cutoff, request.horizon_months)
        if end > request.cutoff or labels.cutoff > request.cutoff:
            raise ValueError("training outcome window or labels leak beyond feature cutoff")
        if (
            features.source_id != cohort.source_id
            or labels.source_id != cohort.source_id
            or features.is_demo != cohort.is_demo
            or labels.is_demo != cohort.is_demo
        ):
            raise ValueError("training source or demo qualification differs")
        if not features.coverage_complete:
            gaps.append("incomplete_training_scope_excluded")
            continue
        feature_index, label_index = _event_index(features), _event_index(labels)
        for original in features.units:
            unit = at_risk(original, features, feature_index)
            if unit.unit_kind != request.unit_kind or unit.lineage_id in test_lineages:
                continue
            if not unit.eligible:
                continue
            fixed = HistoryPrediction(
                unit=unit,
                target=request.target,
                cutoff=features.cutoff,
                horizon_end=end,
                baseline="unsupported",
            )
            label = outcome_for(
                fixed, labels, events=label_index[(unit.unit_kind, unit.unit_id, request.target)]
            )
            if label.label is not None:
                # Latest mature landmark per lineage; repeated months do not grow N.
                candidates[unit.lineage_id] = (unit, label)
    samples = list(candidates.values())
    by_phase = defaultdict(list)
    for unit, label in samples:
        by_phase[unit.phase].append((unit, label))
    results, fitted = [], {}
    for unit in sorted(risk_units, key=lambda u: (u.unit_kind, u.unit_id)):
        reasons = list(unit.reasons)
        stratum = by_phase[unit.phase]
        baseline = "phase_historical_rate"
        if len(stratum) < request.minimum_training_units:
            stratum = samples
            baseline = "pooled_historical_rate"
            reasons.append("phase_stratum_too_small_used_pooled_training_rate")
        usable = (
            unit.eligible
            and unit.unit_kind == request.unit_kind
            and cohort.coverage_complete
            and request.mode != "current"
            and len(stratum) >= request.minimum_training_units
        )
        if request.mode == "current":
            reasons.append("current_view_is_not_a_historical_feature_replay")
        if not cohort.coverage_complete:
            reasons.append("cohort_scope_incomplete_no_probability")
        if len(stratum) < request.minimum_training_units:
            reasons.append("insufficient_disjoint_mature_training_lineages")
        basis_key = (baseline, unit.phase if baseline == "phase_historical_rate" else None)
        if basis_key not in fitted:
            positives = sum(label.label == 1 for _, label in stratum)
            refs = {}
            for train_unit, label in stratum:
                for ref in (train_unit.evidence_ref, *label.event_refs):
                    refs[ref.version_key] = ref
            fitted[basis_key] = (positives, tuple(refs[key] for key in sorted(refs)[:8]), len(refs) > 8)
        positives, preview, truncated = fitted[basis_key]
        results.append(
            HistoryPrediction(
                unit=unit,
                target=request.target,
                cutoff=request.cutoff,
                horizon_end=horizon_end(request.cutoff, request.horizon_months),
                probability=positives / len(stratum) if usable else None,
                baseline=baseline if usable else "unsupported",
                training_units=len(stratum),
                training_events=positives,
                training_refs=preview,
                training_refs_truncated=truncated,
                reasons=tuple(reasons),
            )
        )
    return tuple(results), tuple(sorted(set(gaps)))


def score_predictions(
    predictions: tuple[HistoryPrediction, ...],
    observation: HistoryObservation,
    review_budget: int,
) -> tuple[tuple[HistoryOutcome, ...], HistoryMetrics]:
    if review_budget < 1:
        raise ValueError("review budget must be positive")
    event_index = _event_index(observation)
    outcomes = tuple(
        outcome_for(p, observation, events=event_index[(p.unit.unit_kind, p.unit.unit_id, p.target)])
        for p in predictions
    )
    pairs = [(p, o) for p, o in zip(predictions, outcomes) if p.probability is not None]
    scored = [(p, o) for p, o in pairs if o.label is not None]
    ranked = sorted(pairs, key=lambda pair: (-pair[0].probability, pair[0].unit.unit_id))
    selected = ranked[:review_budget]
    known_selected = [(p, o) for p, o in selected if o.label is not None]
    selected_positive = sum(o.label == 1 for _, o in known_selected)
    selected_ids = {p.unit.unit_id for p, _ in selected}
    positives = sum(o.label == 1 for _, o in scored)
    missing_selected = len(selected) - len(known_selected)
    complete = bool(pairs) and len(scored) == len(pairs)
    calibration = []
    for bin_index in range(5):
        low, high = bin_index / 5, (bin_index + 1) / 5
        group = [
            (p, o) for p, o in scored if low <= p.probability and (p.probability < high or bin_index == 4)
        ]
        if group:
            calibration.append(
                {
                    "lower": low,
                    "upper": high,
                    "count": len(group),
                    "mean_prediction": sum(p.probability for p, _ in group) / len(group),
                    "observed_fraction": sum(o.label for _, o in group) / len(group),
                }
            )
    limitations = [
        "descriptive_historical_baseline_not_clinical_efficacy_or_investment_return",
        "fixed_horizon_complete_case_scores_not_ipcw_or_competing_risk_model",
        "calibration_on_small_groups_has_sampling_uncertainty",
        "lineage_and_source_coverage_limit_generalization",
    ]
    if not complete:
        limitations.append("evaluable_subset_only_selective_missingness_can_bias_scores")
    if not observation.coverage_complete:
        limitations.append("outcome_source_scope_incomplete_no_full_cohort_claim")
    brier = sum((p.probability - o.label) ** 2 for p, o in scored) / len(scored) if scored else None
    metrics = HistoryMetrics(
        cohort_count=len(predictions),
        predicted_count=len(pairs),
        evaluable_count=len(scored),
        events=positives,
        non_events=sum(o.label == 0 for _, o in scored),
        censored=sum(o.status == "censored" for o in outcomes),
        unknown=sum(o.status == "unknown" for o in outcomes),
        evaluation_coverage=len(scored) / len(pairs) if pairs else 0,
        brier_score=brier,
        selected_count=len(selected),
        selected_evaluable=len(known_selected),
        precision_at_budget=(
            selected_positive / len(selected) if selected and not missing_selected else None
        ),
        precision_lower=selected_positive / len(selected) if selected else None,
        precision_upper=(selected_positive + missing_selected) / len(selected) if selected else None,
        recall_at_budget=selected_positive / positives if complete and positives else None,
        missed_observed_events=sum(o.label == 1 and p.unit.unit_id not in selected_ids for p, o in scored),
        false_positive_count=sum(o.label == 0 for _, o in known_selected),
        calibration=tuple(calibration),
        lead_time_days=tuple(
            (o.observed_event_at - p.cutoff).total_seconds() / 86400
            for p, o in scored
            if o.label == 1 and o.observed_event_at is not None
        ),
        limitations=tuple(limitations),
    )
    return outcomes, metrics
