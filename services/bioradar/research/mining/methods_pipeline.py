"""Conditioned landscape counts, reviewed maturity and censored project cohorts."""

from __future__ import annotations

from collections import Counter, defaultdict

from bioradar.research.mining.common import candidate, result, unsupported
from bioradar.research.mining.models import EngineRecord, MiningPolicy, StudyContext, canonical_hash
from bioradar.research.mining.prepare import (
    PreparedSnapshot,
    confirmed_ref,
    ref_key,
    section_visible,
    time_interval,
)


def context_key(
    context: StudyContext | None, record: EngineRecord | None = None, policy: MiningPolicy | None = None
) -> str | None:
    if context is None or not context.disease_refs or not context.population or not context.endpoint:
        return None
    if record is not None and policy is not None:
        if not context.provenance or not section_visible(context.provenance, record, policy):
            return None
    value = context.model_dump(mode="json", exclude={"disease_refs", "provenance"})
    value["diseases"] = sorted({ref_key(ref) for ref in context.disease_refs})
    return canonical_hash(value)


def pipeline_landscape(prepared: PreparedSnapshot):
    method = "pipeline_landscape"
    projects = {}
    unresolved = 0
    for item in prepared.records:
        record = item.record
        if not record.project_ref or {"deleted", "retracted"} & set(record.record_flags):
            continue
        if not confirmed_ref(record, record.project_ref, prepared.policy):
            unresolved += 1
            continue
        key = ref_key(record.project_ref)
        prior = projects.get(key)
        if prior is None or (record.available_at or record.observed_at, record.record_ref.version_key) > (
            prior.record.available_at or prior.record.observed_at,
            prior.record.record_ref.version_key,
        ):
            projects[key] = item
    if not projects:
        return unsupported(method, prepared, "confirmed_project_identity_required")
    grouped = defaultdict(list)
    for key, item in projects.items():
        conditioned = context_key(item.record.context, item.record, prepared.policy)
        grouped[conditioned or "unconditioned_project_inventory"].append((key, item))
    rows, candidates = [], []
    assert prepared.policy.current_start and prepared.policy.current_end
    for cohort, entries in sorted(grouped.items()):
        companies = Counter()
        stages = Counter()
        entered, exited = [], []
        missing_stages = 0
        missing_exit_reasons = 0
        for key, item in entries:
            record = item.record
            stages[record.project_stage or "unknown"] += 1
            missing_stages += record.project_stage is None
            owners = sorted(
                {ref_key(ref) for ref in record.company_refs if confirmed_ref(record, ref, prepared.policy)}
            )
            if len(owners) != len({ref_key(ref) for ref in record.company_refs}):
                owners = []  # Fractional shares cannot be precise when an owner identity is unresolved.
            if owners:
                for owner in owners:
                    companies[owner] += 1 / len(owners)
            else:
                companies["ownership_unknown"] += 1
            if item.window != "current" or "historical_backfill" in record.record_flags:
                continue
            for expression, selected in ((record.entry_time, entered), (record.exit_time, exited)):
                interval = time_interval(expression) if expression else None
                if (
                    interval
                    and prepared.policy.current_start <= interval[0]
                    and interval[1] <= (prepared.policy.current_end)
                ):
                    selected.append(key)
            if record.exit_time and not record.exit_reason:
                missing_exit_reasons += 1
        denominator = len(entries)
        shares = {key: count / denominator for key, count in sorted(companies.items())}
        # Unknown ownership is displayed separately; it is not a fictitious company.
        known_shares = {key: value for key, value in shares.items() if key != "ownership_unknown"}
        row = {
            "cohort": cohort,
            "unit": "project",
            "denominator": denominator,
            "project_keys": sorted(key for key, _ in entries),
            "stage_counts": dict(sorted(stages.items())),
            "company_fractional_shares": known_shares,
            "unknown_ownership_share": shares.get("ownership_unknown", 0),
            "hhi_known_ownership_on_all_projects": round(sum(v * v for v in known_shares.values()), 8),
            "ownership_counting_rule": "equal_fraction_per_project_owner",
            "entered_project_keys": sorted(entered),
            "exited_project_keys": sorted(exited),
            "missing_stage_count": missing_stages,
            "missing_exit_reason_count": missing_exit_reasons,
            "competitive_comparability": "undetermined"
            if cohort == "unconditioned_project_inventory"
            else ("conditioned_inventory_requires_domain_review"),
        }
        rows.append(row)
        candidates.append(
            candidate(
                method,
                {"cohort": cohort},
                "项目管线观察清单",
                [i for _, i in entries],
                kind="observed_change" if entered or exited else "association",
                metrics=row,
                score=len(entered) + len(exited) or denominator,
                reasons=("fixed_project_denominator", "quantity_is_not_value_or_clinical_success"),
                data_gaps=("competitive_context_unconfirmed",)
                if cohort == "unconditioned_project_inventory"
                else (),
                questions=(
                    "项目在人群、治疗线、地区和时间条件上是否互相竞争？",
                    "退出原因是商业变化还是研究结果？",
                ),
                requirements=("S02项目/药物/试验身份", "S03项目条件与退出原因原文"),
            )
        )
    return result(
        method,
        prepared,
        candidates,
        statistics={"cohorts": rows, "unresolved_projects": unresolved},
        limitations=("inventory_is_not_a_competition_or_investment_recommendation",),
    )


def maturity_shift(prepared: PreparedSnapshot):
    method = "maturity_shift"
    grouped = defaultdict(list)
    excluded = Counter()
    ranks = {stage: rank for rank, stage in enumerate(prepared.policy.maturity_stage_order)}
    seen_assessments = {}
    for item in prepared.records:
        for assessment in item.record.assessments:
            assessment_key = assessment.assessment_ref.version_key
            if assessment_key in seen_assessments:
                if seen_assessments[assessment_key] != assessment:
                    raise ValueError("conflicting adopted assessment payloads")
                excluded["duplicate_assessment_version"] += 1
                continue
            seen_assessments[assessment_key] = assessment
            if not section_visible(assessment.provenance, item.record, prepared.policy):
                excluded["assessment_not_visible"] += 1
                continue
            if (
                assessment.review_status != "reviewed"
                or not assessment.assessor
                or not assessment.evidence_refs
            ):
                excluded["signed_review_and_evidence_required"] += 1
                continue
            key = context_key(assessment.context, item.record, prepared.policy)
            if not key or assessment.stage not in ranks:
                excluded["comparable_context_and_known_evidence_stage_required"] += 1
                continue
            grouped[key].append((item, assessment))
    if not grouped:
        return unsupported(method, prepared, "reviewed_assessment_with_stage_and_study_conditions_required")
    candidates, rows = [], []
    for context, entries in sorted(grouped.items()):
        baseline = [a for item, a in entries if item.window == "baseline" and a.stance == "supportive"]
        current = [a for item, a in entries if item.window == "current" and a.stance == "supportive"]
        before = max((ranks[a.stage] for a in baseline), default=None)
        after = max((ranks[a.stage] for a in current), default=None)
        negative = [a for _, a in entries if a.stance in {"negative", "mixed"}]
        change = before is not None and after is not None and after > before
        row = {
            "context_key": context,
            "baseline_supported_stage": prepared.policy.maturity_stage_order[before]
            if before is not None
            else None,
            "current_supported_stage": prepared.policy.maturity_stage_order[after]
            if after is not None
            else None,
            "stage_migration_observed": change,
            "stage_counts": dict(sorted(Counter(a.stage for _, a in entries).items())),
            "study_count": len({ref_key(a.study_cluster_ref) for _, a in entries}),
            "negative_or_mixed_assessment_refs": [a.assessment_ref.model_dump(mode="json") for a in negative],
            "assessment_refs": [a.assessment_ref.model_dump(mode="json") for _, a in entries],
        }
        rows.append(row)
        candidates.append(
            candidate(
                method,
                {"context": context},
                "同条件证据阶段迁移" if change else "已评价证据阶段观察",
                [i for i, _ in entries],
                kind="observed_change" if change else "association",
                metrics=row,
                score=(after or 0) + len(negative),
                reasons=("reviewed_evidence_stage_not_registered_trial_phase",),
                questions=("新增结果是否改变成熟度判断，现存反证和适用条件是否已保留？",),
                requirements=("S04按相同条件的专业评价及反证",),
            )
        )
    return result(
        method,
        prepared,
        candidates,
        readiness="ready",
        statistics={"contexts": rows, "excluded": dict(excluded)},
        limitations=("ordered_stages_do_not_form_a_success_probability",),
    )


def cohort_event_table(observations: list[tuple[float, str]]) -> list[dict]:
    """KM for any recorded exit and Aalen-Johansen CIFs for competing exits.

    Administrative censoring is not an event. Coverage-loss/unknown exits must
    be removed and reported before invoking this estimator.
    """
    kinds = ("stage_advance", "clinical_failure", "commercial_exit")
    survival, incidence, table = 1.0, {kind: 0.0 for kind in kinds}, []
    for time in sorted({time for time, _ in observations}):
        at_risk = sum(duration >= time for duration, _ in observations)
        events = Counter(kind for duration, kind in observations if duration == time and kind in kinds)
        censored = sum(duration == time and kind == "no_event" for duration, kind in observations)
        old_survival = survival
        for kind in kinds:
            incidence[kind] += old_survival * events[kind] / at_risk
        survival *= 1 - sum(events.values()) / at_risk
        table.append(
            {
                "day": round(time, 8),
                "at_risk": at_risk,
                "events": dict(events),
                "censored": censored,
                "no_recorded_exit_survival": round(survival, 8),
                "competing_event_cumulative_incidence": {k: round(v, 8) for k, v in incidence.items()},
            }
        )
    return table


def translation_dwell(prepared: PreparedSnapshot):
    method = "translation_dwell"
    grouped = defaultdict(list)
    missing = Counter()
    for item in prepared.records:
        timeline = item.record.timeline
        if not timeline:
            continue
        if (
            not section_visible(timeline.provenance, item.record, prepared.policy)
            or not confirmed_ref(item.record, timeline.project_ref, prepared.policy)
            or not timeline.evidence_refs
        ):
            missing["timeline_identity_provenance_or_evidence_missing"] += 1
            continue
        entry = time_interval(timeline.entered_at)
        context = context_key(timeline.context, item.record, prepared.policy)
        if not entry or entry[0] != entry[1] or not context:
            missing["exact_stage_entry_time_and_context_required"] += 1
            continue
        if timeline.observation_end.tzinfo is None or timeline.observation_end < entry[0]:
            missing["invalid_observation_end"] += 1
            continue
        end = min(timeline.observation_end, prepared.policy.as_of)
        if end < entry[0]:
            missing["future_stage_entry"] += 1
            continue
        kind = "no_event"
        event = time_interval(timeline.event_time) if timeline.event_time else None
        if timeline.event_kind != "no_event":
            if not event or event[0] != event[1] or event[0] < entry[0]:
                missing["exact_nonnegative_event_time_required"] += 1
                continue
            if event[0] <= end:
                end, kind = event[0], timeline.event_kind
        duration = (end - entry[0]).total_seconds() / 86400
        grouped[(context, timeline.stage)].append((item, timeline, duration, kind))
    if not grouped:
        return unsupported(method, prepared, "evidence_backed_exact_stage_timeline_required")
    candidates, cohorts = [], []
    for key, rows in sorted(grouped.items()):
        # A project contributes once to a stage cohort even if it appears in many sources.
        by_project = {}
        for row in rows:
            identity = ref_key(row[1].project_ref)
            prior = by_project.get(identity)
            chronology = (row[1].observation_end, row[1].provenance.available_at or row[0].record.observed_at)
            if prior is not None:
                previous = (
                    prior[1].observation_end,
                    prior[1].provenance.available_at or prior[0].record.observed_at,
                )
                if chronology == previous and prior[1] != row[1]:
                    raise ValueError("conflicting same-time project timelines")
                if chronology <= previous:
                    continue
            by_project[identity] = row
        rows = [by_project[k] for k in sorted(by_project)]
        estimable = [
            (duration, kind)
            for _, timeline, duration, kind in rows
            if timeline.observation_coverage == "continuous" and kind not in {"coverage_loss", "unknown_exit"}
        ]
        event_table = cohort_event_table(estimable) if estimable else []
        observations = [
            {
                "project_ref": timeline.project_ref.model_dump(mode="json"),
                "observed_days": round(duration, 8),
                "event_kind": kind,
                "right_censored": kind == "no_event",
                "observation_coverage": timeline.observation_coverage,
            }
            for _, timeline, duration, kind in rows
        ]
        cohort = {
            "context_key": key[0],
            "stage": key[1],
            "observations": observations,
            "event_table": event_table,
            "excluded_from_estimator": len(rows) - len(estimable),
            "estimator_scope": "adopted_cohort_descriptive_not_population_success_rate",
            "median_time_to_any_recorded_exit_days": next(
                (r["day"] for r in event_table if r["no_recorded_exit_survival"] <= 0.5), None
            ),
        }
        cohorts.append(cohort)
        waits = [
            i
            for i, _, duration, kind in rows
            if duration >= prepared.policy.dwell_threshold_days and kind == "no_event"
        ]
        candidates.append(
            candidate(
                method,
                {"cohort": key},
                "阶段等待与删失队列观察",
                [i for i, _, _, _ in rows],
                metrics=cohort,
                score=len(waits),
                reasons=("lack_of_observed_advance_is_not_failure",),
                data_gaps=("observation_or_exit_type_incomplete",) if len(estimable) != len(rows) else (),
                questions=("长等待是否来自转化障碍、未公开进展还是观察覆盖不足？",),
                requirements=("S01连续观察与阶段转换事件", "领域专家核实等待原因"),
            )
        )
    return result(
        method,
        prepared,
        candidates,
        statistics={"cohorts": cohorts, "excluded": dict(missing)},
        limitations=("noninformative_censoring_and_cohort_selection_require_review",),
    )
