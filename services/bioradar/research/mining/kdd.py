"""Four-direction KDD computation over fixed analysis units, without I/O."""

import re
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from html import unescape

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from bioradar.research.datasets.build import build_dataset, unit_records
from bioradar.research.mining.common import support
from bioradar.research.mining.evaluation import evaluate_patterns
from bioradar.research.mining.methods_text import _analyzer
from bioradar.research.mining.models import (
    Candidate,
    ExcludedRecord,
    KddPolicy,
    MethodResult,
    MiningResult,
    NormalizedSnapshot,
    ProviderRef,
    canonical_hash,
)
from bioradar.research.mining.prepare import ref_key, snapshot_hash

KDD_COMPUTE_VERSION = "bioradar-mining-compute-v0.3"
VERSIONS = {
    "research_tfidf_topics": "conditioned-tfidf-nmf-v2",
    "research_embedding_clusters": "fixed-embedding-unit-clusters-v1",
    "research_evidence_balance": "conditioned-independent-evidence-v1",
    "conditional_pipeline_landscape": "project-condition-observation-v2",
    "pipeline_graph_hints": "fixed-graph-verification-hints-v1",
    "business_metric_change": "source-bound-period-change-v2",
    "public_health_metric_change": "source-bound-period-change-v2",
    "public_health_notifications": "native-notice-publication-v1",
}
DIRECTION_METHODS = {
    "research": ("research_tfidf_topics", "research_embedding_clusters", "research_evidence_balance"),
    "pipeline": ("conditional_pipeline_landscape", "pipeline_graph_hints"),
    "business": ("business_metric_change",),
    "public_health": ("public_health_metric_change", "public_health_notifications"),
}
MAX_CLUSTER_UNITS = 1000


def kdd_versions(policy: KddPolicy) -> dict[str, str]:
    return {"__engine_compute__": KDD_COMPUTE_VERSION} | {
        method: VERSIONS[method] for method in DIRECTION_METHODS[policy.kdd_direction]
    }


def _candidate(dataset, method, key, title, units, *, metrics=None, kind="association", gaps=(), score=0.0):
    records = unit_records(dataset, units)
    independent_records = unit_records(dataset, [unit for unit in units if unit["independent_study"]])
    studies = {ref_key(item.record.study_cluster_ref) for item in independent_records if item.confirmed_study}
    return Candidate(
        candidate_key=canonical_hash([method, VERSIONS[method], key]),
        method_id=method,
        method_version=VERSIONS[method],
        group_key=canonical_hash(key),
        kind=kind,
        title=title,
        reasons=(
            "fixed_adopted_analysis_units",
            "descriptive_pattern_requires_business_and_scientific_review",
        ),
        supports=tuple(support(item) for item in records),
        metrics={
            "analysis_unit_count": len(units),
            "independent_study_count": len(studies),
            **(metrics or {}),
        },
        score=round(float(score), 8),
        data_gaps=tuple(sorted(set(gaps))),
        next_questions=("该模式是否改变当前研究问题的决策条件，是否存在重要反例或口径变化？",),
        next_data_requirements=tuple(sorted(set(gaps))),
        evidence_status="partial"
        if units and all(unit["independent_study"] for unit in units)
        else "unverified",
    )


def _result(
    dataset, method, candidates, *, statistics=None, reasons=(), partial=False, ready=True, truncated=0
):
    return MethodResult(
        method_id=method,
        method_version=VERSIONS[method],
        readiness="ready" if ready else ("unsupported" if reasons and not candidates else "exploratory_only"),
        execution_status="unsupported"
        if reasons and not candidates
        else ("partial" if partial or dataset.prepared.truncated_count or truncated else "success"),
        candidates=tuple(sorted(candidates, key=lambda item: item.candidate_key)),
        reasons=tuple(reasons),
        statistics={"analysis_units": len(dataset.units), **(statistics or {})},
        excluded_records=tuple(
            ExcludedRecord(record_ref=ProviderRef.model_validate(row["record_ref"]), reason=row["reason"])
            for row in dataset.exclusions
        ),
        truncated_count=dataset.prepared.truncated_count + truncated,
        limitations=tuple(
            sorted(set(dataset.gaps) | {"patterns_are_not_expert_approved_or_statistically_significant"})
        ),
    )


def _research_analyzer(text):
    # Lexical features see visible narrative words; the fixed source text and
    # vector binding retain their original bytes and hashes.
    return _analyzer(re.sub(r"</?[A-Za-z][^<>]*>", " ", unescape(text)))


def _research_tfidf(dataset):
    method = "research_tfidf_topics"
    units = [unit for unit in dataset.units if unit["features"].get("text")]
    if not units:
        return _result(dataset, method, [], ready=False, reasons=("authorized_text_units_required",))
    baseline = [i for i, unit in enumerate(units) if unit["features"]["window"] == "baseline"]
    current = [i for i, unit in enumerate(units) if unit["features"]["window"] == "current"]
    temporal = bool(baseline and current)
    training = baseline if temporal else list(range(len(units)))
    vectorizer = TfidfVectorizer(
        analyzer=_research_analyzer, max_features=dataset.prepared.policy.limits.max_features
    )
    texts = [unit["features"]["text"] for unit in units]
    try:
        trained = vectorizer.fit_transform([texts[i] for i in training])
        matrix = vectorizer.transform(texts)
    except ValueError:
        return _result(dataset, method, [], ready=False, reasons=("no_usable_text_features",))
    count = min(dataset.prepared.policy.topic_components, trained.shape[0], trained.shape[1])
    model = NMF(
        n_components=count, init="nndsvda", random_state=dataset.prepared.policy.random_seed, max_iter=300
    )
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        model.fit(trained)
        weights = model.transform(matrix)
    if not np.isfinite(weights).all() or not np.isfinite(model.components_).all():
        raise ValueError("nonfinite topic factors")
    candidates = []
    vocabulary = vectorizer.get_feature_names_out()
    model_hash = canonical_hash(model.components_.round(8).tolist())
    for component in range(count):
        indices = [
            i for i, topic in enumerate(weights.argmax(axis=1)) if topic == component and weights[i].sum() > 0
        ]
        if not indices:
            continue
        terms = [
            str(vocabulary[i])
            for i in np.argsort(-model.components_[component], kind="stable")[:6]
            if model.components_[component, i] > 0
        ]
        members = [units[i] for i in indices]
        candidates.append(
            _candidate(
                dataset,
                method,
                ["topic", model_hash, terms],
                "科研主题候选：" + " / ".join(terms),
                members,
                metrics={
                    "terms": terms,
                    "representation": "tfidf_nmf_control",
                    "model_hash": model_hash,
                    "mode": "frozen_baseline" if temporal else "stock_exploration",
                    "cosine_distance": None,
                    "novelty_status": "topic_inventory_not_a_temporal_novelty_estimate",
                    "unit_weights": "one_per_analysis_unit",
                    "comparable": True,
                },
                kind="association" if len(members) >= 2 else "hypothesis",
                gaps=("topic_content_and_independent_replication_require_review",),
                score=len(members),
            )
        )
    if temporal:
        for i in current:
            distance = (
                1.0 if not matrix[i].nnz else float(1 - cosine_similarity(matrix[i], matrix[baseline]).max())
            )
            candidates.append(
                _candidate(
                    dataset,
                    method,
                    ["lexical_novelty", units[i]["unit_id"], model_hash],
                    "科研词汇新颖性候选",
                    [units[i]],
                    kind="hypothesis",
                    metrics={
                        "cosine_distance": round(distance, 8),
                        "representation": "tfidf_nmf_control",
                        "model_hash": model_hash,
                        "comparable": True,
                    },
                    gaps=("novelty_is_not_scientific_importance",),
                    score=distance,
                )
            )
    return _result(
        dataset,
        method,
        candidates,
        ready=False,
        statistics={
            "representation": "tfidf_nmf_control",
            "model_hash": model_hash,
            "processed_unit_ids": [unit["unit_id"] for unit in units],
            "truncated_text_unit_ids": [
                unit["unit_id"] for unit in units if unit["features"]["text_truncated"]
            ],
            "vocabulary_hash": canonical_hash(vocabulary.tolist()),
            "fit_unit_ids": [units[i]["unit_id"] for i in training],
            "fit_scope": "baseline_only" if temporal else "stock_exploration_not_historical_prediction",
            "lexical_preprocessing": "html_entities_decoded_and_tags_removed_fixed_source_unchanged_v1",
            "novelty_reference_scope": "baseline_only_for_current_units"
            if temporal
            else "none_stock_exploration_has_no_temporal_baseline",
            "solver_warnings": sorted({type(w.message).__name__ for w in emitted}),
        },
        partial=any(unit["features"]["text_truncated"] for unit in units),
    )


def _research_embeddings(dataset):
    method = "research_embedding_clusters"
    spaces, rejected = defaultdict(list), []
    for unit in dataset.units:
        values = unit["features"].get("embeddings", [])
        signatures = {(value["provider"], value["model_version"], value["dimension"]) for value in values}
        by_text = defaultdict(list)
        for value in values:
            by_text[value["text_hash"]].append(value)
        conflicting = any(
            len({canonical_hash(value["vector"]) for value in members}) > 1 for members in by_text.values()
        )
        if len(signatures) != 1 or len(by_text) != unit["features"]["text_member_count"] or conflicting:
            rejected.append(unit["unit_id"])
            continue
        vectors = np.array([members[0]["vector"] for _, members in sorted(by_text.items())], dtype=float)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        vector = vectors.mean(axis=0)
        if not np.isfinite(vector).all() or not np.linalg.norm(vector):
            rejected.append(unit["unit_id"])
            continue
        spaces[next(iter(signatures))].append((unit, vector / np.linalg.norm(vector)))
    if not spaces:
        return _result(
            dataset,
            method,
            [],
            ready=False,
            reasons=("complete_same_model_version_embedding_units_required",),
            statistics={"representation": "embedding", "rejected_unit_ids": rejected},
        )
    candidates, spaces_summary, all_omitted = [], [], []
    for signature, rows in sorted(spaces.items()):
        omitted = [unit["unit_id"] for unit, _ in rows[MAX_CLUSTER_UNITS:]]
        all_omitted += omitted
        rows = rows[:MAX_CLUSTER_UNITS]
        matrix = np.array([vector for _, vector in rows])
        assignments = DBSCAN(
            eps=max(1e-6, 1 - dataset.prepared.policy.topic_similarity),
            min_samples=max(2, dataset.prepared.policy.minimum_support),
            metric="cosine",
        ).fit_predict(matrix)
        for label in sorted(set(assignments) - {-1}):
            members = [rows[i][0] for i, value in enumerate(assignments) if value == label]
            candidates.append(
                _candidate(
                    dataset,
                    method,
                    ["cluster", sorted(unit["unit_id"] for unit in members), signature],
                    "固定向量的多材料主题候选",
                    members,
                    metrics={
                        "representation": "embedding",
                        "model_signature": list(signature),
                        "comparable": True,
                    },
                    gaps=("semantic_cluster_content_and_value_unreviewed",),
                    score=len(members),
                )
            )
        baseline = [i for i, (unit, _) in enumerate(rows) if unit["features"]["window"] == "baseline"]
        current = [i for i, (unit, _) in enumerate(rows) if unit["features"]["window"] == "current"]
        if baseline:
            for i in current:
                distances = 1 - cosine_similarity(matrix[i : i + 1], matrix[baseline])[0]
                nearest = int(distances.argmin())
                distance = float(distances[nearest])
                candidates.append(
                    _candidate(
                        dataset,
                        method,
                        ["novelty", rows[i][0]["unit_id"], signature],
                        "固定向量语义新颖性候选",
                        [rows[i][0]],
                        kind="hypothesis",
                        metrics={
                            "cosine_distance": round(distance, 8),
                            "representation": "embedding",
                            "model_signature": list(signature),
                            "nearest_baseline_unit": rows[baseline[nearest]][0]["unit_id"],
                            "comparable": True,
                        },
                        gaps=("embedding_distance_does_not_establish_usefulness",),
                        score=distance,
                    )
                )
        spaces_summary.append(
            {
                "model_signature": list(signature),
                "processed_unit_ids": [unit["unit_id"] for unit, _ in rows],
                "noise_unit_ids": [
                    rows[i][0]["unit_id"] for i, value in enumerate(assignments) if value == -1
                ],
                "algorithm_input_deferred_unit_ids": omitted,
            }
        )
    return _result(
        dataset,
        method,
        candidates,
        ready=False,
        partial=bool(all_omitted),
        truncated=len(all_omitted),
        statistics={
            "representation": "embedding",
            "model_spaces": spaces_summary,
            "rejected_unit_ids": rejected,
            "algorithm_input_deferred_unit_ids": all_omitted,
            "algorithm_input_limit_per_model": MAX_CLUSTER_UNITS,
            "aggregation": "equal_unique_fixed_text_l2_vectors_mean_then_l2_normalize",
            "tfidf_control_method": "research_tfidf_topics",
            "different_model_spaces_never_compared": True,
        },
    )


def _research_balance(dataset):
    method = "research_evidence_balance"
    inventory = []
    for unit in dataset.units:
        if unit["independent_study"] and unit["features"].get("assessments"):
            inventory.append(
                {
                    "unit_id": unit["unit_id"],
                    "condition_key": unit["condition_key"],
                    "assessment_refs": [
                        assessment["assessment_ref"] for assessment in unit["features"]["assessments"]
                    ],
                    "assessment_stances": sorted(
                        {assessment["stance"] for assessment in unit["features"]["assessments"]}
                    ),
                }
            )
    # Assessment currently has a study/condition but no shared claim or
    # intervention target. Opposite stances across unrelated drugs are not
    # counterevidence. Keep the individual inventory without inventing a balance.
    return _result(
        dataset,
        method,
        [],
        ready=False,
        reasons=("explicit_shared_claim_or_intervention_scope_required",),
        statistics={
            "assessment_inventory": inventory,
            "cross_study_stance_aggregation": "not_permitted_without_shared_target",
        },
    )


def _pipeline(dataset):
    method = "conditional_pipeline_landscape"
    if not dataset.units:
        return _result(
            dataset, method, [], ready=False, reasons=("project_or_native_registration_identity_required",)
        )
    groups = defaultdict(list)
    for unit in dataset.units:
        if unit["unit_kind"] == "graph_relation_hint":
            continue
        condition = unit["condition_key"]
        if condition is None:
            partial = {
                key: value
                for key, value in unit["dimensions"].items()
                if key not in {"project_ref", "known_at"}
            }
            partial["disease_refs"] = sorted(
                ref_key(ProviderRef.model_validate(value)) for value in partial.get("disease_refs", [])
            )
            # Incomplete conditions remain distinct; missing population is not
            # permission to turn different indications into repeated observations.
            condition = "partial_conditions:" + canonical_hash(partial)
        groups[condition].append(unit)
    if not groups:
        return _result(
            dataset, method, [], ready=False, reasons=("project_or_native_registration_identity_required",)
        )
    candidates = []
    for condition, units in sorted(groups.items()):
        projects = defaultdict(list)
        for unit in units:
            project = ProviderRef.model_validate(unit["dimensions"]["project_ref"])
            projects[ref_key(project)].append(unit)
        latest, transitions = [], []
        observed_comparisons = 0
        version_counts = {}
        ambiguous_order = 0
        for key, points in sorted(projects.items()):
            versions = {}
            for unit in sorted(points, key=lambda value: value["unit_id"]):
                fixed = tuple(
                    sorted(
                        ProviderRef.model_validate(member["record_ref"]).version_key
                        for member in unit["members"]
                    )
                )
                versions.setdefault(fixed, unit)
            points = sorted(
                versions.values(), key=lambda unit: (unit["dimensions"]["known_at"], unit["unit_id"])
            )
            version_counts[key] = len({version for fixed in versions for version in fixed})
            ambiguous_order += sum(max(0, len(fixed) - 1) for fixed in versions)
            latest.append(points[-1])
            for before, after in zip(points, points[1:]):
                if before["dimensions"]["known_at"] >= after["dimensions"]["known_at"]:
                    ambiguous_order += 1
                    continue
                observed_comparisons += 1
                state_keys = ("stages", "statuses", "exit_reasons")
                if any(before["features"][key] != after["features"][key] for key in state_keys):
                    transitions.append(
                        {
                            "project_key": key,
                            "before_unit_id": before["unit_id"],
                            "after_unit_id": after["unit_id"],
                            "time_kind": "source_state_observed_not_clinical_event",
                            "historical_backfill": after["features"]["historical_backfill"],
                        }
                    )
        stage_counts = Counter()
        for unit in latest:
            stages = unit["features"]["stages"]
            stage_counts[stages[0] if len(stages) == 1 else "unknown_or_conflicting"] += 1
        candidates.append(
            _candidate(
                dataset,
                method,
                condition,
                "同条件项目或原生登记格局",
                units,
                kind="observed_change" if transitions else "association",
                metrics={
                    "analysis_unit_count": len(projects),
                    "latest_unit_ids": [unit["unit_id"] for unit in latest],
                    "project_count": len(projects),
                    "stage_counts": dict(sorted(stage_counts.items())),
                    "transitions": transitions,
                    "unchanged": bool(observed_comparisons) and not transitions,
                    "observed_comparisons": observed_comparisons,
                    "fixed_version_counts_by_project": version_counts,
                    "ambiguous_version_order_comparisons": ambiguous_order,
                    "history_status": "distinct_ordered_fixed_versions_observed"
                    if observed_comparisons
                    else "insufficient_distinct_ordered_fixed_versions",
                    "temporal_comparison_unit": "entity_x_condition_x_fixed_source_version",
                    "comparable": not condition.startswith("partial_conditions:"),
                    "identity_kinds": sorted({unit["unit_kind"] for unit in units}),
                    "drug_program_count": sum(unit["unit_kind"] == "confirmed_project" for unit in latest),
                },
                gaps=("registration_is_not_drug_program_or_failure_reason",)
                + (
                    ()
                    if not condition.startswith("partial_conditions:")
                    else ("indication_and_competitive_conditions_required",)
                )
                + (() if observed_comparisons else ("distinct_ordered_version_history_required",))
                + (() if not ambiguous_order else ("fixed_version_observation_order_ambiguous",)),
                score=len(transitions),
            )
        )
    return _result(dataset, method, candidates, ready=bool(candidates))


def _pipeline_graph_hints(dataset):
    method = "pipeline_graph_hints"
    units = [unit for unit in dataset.units if unit["unit_kind"] == "graph_relation_hint"]
    if not units:
        return _result(dataset, method, [], ready=False, reasons=("fixed_graph_hypotheses_not_supplied",))
    candidates = []
    for unit in units:
        hint = unit["features"]["hint"]
        candidates.append(
            _candidate(
                dataset,
                method,
                [hint["hint_ref"][key] for key in ("provider", "object_type", "object_id")],
                "图背景关系待核："
                + hint["subject_label"][:80]
                + " → "
                + hint["relation_type"]
                + " → "
                + hint["object_label"][:80],
                [unit],
                kind="hypothesis",
                metrics={
                    "graph_relation_hint": True,
                    "comparable": True,
                    "query_terms": hint["query_terms"],
                    "relation_type": hint["relation_type"],
                    "identity_statuses": hint["identity_statuses"],
                    "review_status": hint["review_status"],
                    "declared_binding_status": hint["declared_binding_status"],
                    "originals_validated": hint["originals_validated"],
                    "graph_role": "background_for_verification",
                    "project_count": None,
                    "population_denominator_included": False,
                    "clinical_effect_support_permitted": False,
                },
                gaps=("graph_relation_requires_original_evidence_verification",),
            )
        )
    return _result(
        dataset,
        method,
        candidates,
        ready=False,
        statistics={
            "graph_hypothesis_count": len(units),
            "project_denominator_contribution": 0,
            "confirmation_not_inferred_from_graph_presence": True,
        },
    )


def _periods_comparable(left, right):
    a = datetime.fromisoformat(left["period_start"])
    b = datetime.fromisoformat(left["period_end"])
    c = datetime.fromisoformat(right["period_start"])
    d = datetime.fromisoformat(right["period_end"])

    def months(start, end):
        return (end.year - start.year) * 12 + end.month - start.month

    calendar_equal = a.day == b.day == c.day == d.day == 1 and months(a, b) == months(c, d) > 0
    return b <= c and ((b - a) == (d - c) or calendar_equal)


def _period_basis(dimensions):
    start = datetime.fromisoformat(dimensions["period_start"])
    end = datetime.fromisoformat(dimensions["period_end"])
    months = (end.year - start.year) * 12 + end.month - start.month
    if (
        start.day == end.day == 1
        and start.hour == end.hour == start.minute == end.minute == 0
        and start.second == end.second == start.microsecond == end.microsecond == 0
        and months > 0
    ):
        return {
            "kind": "explicit_calendar_month_interval",
            "months": months,
            "start_month_phase": (start.month - 1) % min(months, 12),
            "qualification": "boundary_derived_not_source_fiscal_year_or_period_label",
        }
    return {
        "kind": "exact_duration_interval",
        "seconds": (end - start).total_seconds(),
        "qualification": "noncalendar_period_definition_and_annual_position_unresolved",
    }


def _metrics(dataset):
    method = DIRECTION_METHODS[dataset.direction][0]
    if not dataset.units:
        return _result(
            dataset, method, [], ready=False, reasons=("qualified_source_bound_metric_periods_required",)
        )
    groups = defaultdict(list)
    for unit in dataset.units:
        if unit["unit_kind"] == "native_notice_publication":
            continue
        dimensions = unit["dimensions"]
        series = {
            key: value
            for key, value in dimensions.items()
            if key not in {"period_start", "period_end", "subject_ref"}
        }
        series["period_basis"] = _period_basis(dimensions)
        groups[canonical_hash(series)].append(unit)
    if not groups:
        return _result(
            dataset, method, [], ready=False, reasons=("qualified_source_bound_metric_periods_required",)
        )
    candidates = []
    deferred = []
    for series, units in sorted(groups.items()):
        units.sort(key=lambda unit: (unit["dimensions"]["period_start"], unit["unit_id"]))
        limit = dataset.prepared.policy.limits.max_temporal_bins
        deferred.extend(unit["unit_id"] for unit in units[:-limit])
        units = units[-limit:]
        period_basis = _period_basis(units[0]["dimensions"])
        if len(units) == 1:
            candidates.append(
                _candidate(
                    dataset,
                    method,
                    [series, "one_period"],
                    "指标尚无可比前期",
                    units,
                    kind="data_quality",
                    metrics={"comparable": False, "period_basis": period_basis},
                    gaps=("at_least_two_comparable_periods_required",),
                )
            )
        for before, after in zip(units, units[1:]):
            previous, current = before["features"]["observations"][0], after["features"]["observations"][0]
            comparable = _periods_comparable(before["dimensions"], after["dimensions"])
            conflict = before["features"]["conflicting_values"] or after["features"]["conflicting_values"]
            effect = None
            qualification = "source_bound_reported_value_change_not_causal"
            with localcontext() as arithmetic:
                arithmetic.prec = 256
                low = Decimal(current["value_lower"]) - Decimal(previous["value_upper"])
                high = Decimal(current["value_upper"]) - Decimal(previous["value_lower"])
                maximum_baseline = max(
                    abs(Decimal(previous["value_lower"])), abs(Decimal(previous["value_upper"]))
                )
                relative = (
                    min(abs(low), abs(high)) / maximum_baseline
                    if maximum_baseline and not low <= 0 <= high
                    else None
                )
                if comparable and not conflict:
                    effect = {
                        "lower": float(low),
                        "upper": float(high),
                        "relative_minimum": float(relative) if relative is not None else None,
                        "unit": current["unit"],
                        "kind": "reported_value_interval_difference",
                    }
            if dataset.direction == "public_health":
                qualification = "notification_or_measurement_change_not_incidence_or_outbreak"
            candidates.append(
                _candidate(
                    dataset,
                    method,
                    [series, before["unit_id"], after["unit_id"]],
                    "同口径经营指标变化" if dataset.direction == "business" else "同地区同人群监测指标变化",
                    [before, after],
                    kind="observed_change" if comparable and not conflict else "data_quality",
                    metrics={
                        "comparable": comparable,
                        "conflicting_values": conflict,
                        "effect": effect,
                        "qualification": qualification,
                        "period_unit_ids": [before["unit_id"], after["unit_id"]],
                        "period_basis": period_basis,
                        "seasonal_adjustment": "not_performed",
                        "denominator_known": previous["denominator"] is not None
                        and current["denominator"] is not None,
                    },
                    gaps=(
                        "source_population_and_observation_coverage_require_review",
                        "seasonality_and_consolidation_changes_not_adjusted",
                    )
                    + (() if comparable else ("nonoverlapping_comparable_periods_required",))
                    + (
                        ("noncalendar_period_definition_and_annual_position_unresolved",)
                        if period_basis["kind"] == "exact_duration_interval"
                        else ()
                    ),
                    score=(effect or {}).get("relative_minimum") or 0.0,
                )
            )
    return _result(
        dataset,
        method,
        candidates,
        ready=any(candidate.metrics.get("effect") is not None for candidate in candidates),
        partial=bool(deferred),
        truncated=len(deferred),
        statistics={
            "series_count": len(groups),
            "period_partition": "explicit_date_interval_basis_before_adjacency_v2",
            "source_fy_fp_labels_used_for_comparability": False,
            "algorithm_input_deferred_unit_ids": deferred,
            "algorithm_input_limit_per_series": dataset.prepared.policy.limits.max_temporal_bins,
            "rates_not_inferred_from_missing_denominators": True,
        },
    )


def _public_notifications(dataset):
    method = "public_health_notifications"
    units = [unit for unit in dataset.units if unit["unit_kind"] == "native_notice_publication"]
    if not units:
        return _result(
            dataset, method, [], ready=False, reasons=("native_notice_publication_units_required",)
        )
    candidates, periods = [], defaultdict(list)
    unknown_month = []
    for unit in units:
        public_time = unit["dimensions"]["publication_start"]
        stock = unit["features"]["historical_backfill"] or unit["features"]["window"] != "current"
        candidates.append(
            _candidate(
                dataset,
                method,
                ["notice", unit["unit_id"]],
                "WHO原生通报待核",
                [unit],
                kind="association" if stock else "observed_change",
                metrics={
                    "native_notice_event": True,
                    "qualification": "notification_descriptive_not_incidence_or_outbreak_onset",
                    "publication_start": public_time,
                    "publication_end": unit["dimensions"]["publication_end"],
                    "freshness": "stock_or_backfilled_notice"
                    if stock
                    else "recent_publication_not_outbreak_onset",
                    "new_epidemic_claim_permitted": False,
                    "regional_or_population_rate_permitted": False,
                    "region": None,
                    "disease": None,
                    "comparable": True,
                },
                gaps=(
                    "notification_sampling_coverage_unknown",
                    "case_counts_population_and_outbreak_onset_not_supplied",
                ),
                score=1.0,
            )
        )
        if public_time:
            low = datetime.fromisoformat(public_time)
            high = datetime.fromisoformat(unit["dimensions"]["publication_end"])
            last = high - timedelta(microseconds=1) if high > low else high
            if (low.year, low.month) == (last.year, last.month):
                periods[low.strftime("%Y-%m")].append(unit)
            else:
                unknown_month.append(unit["unit_id"])
        else:
            unknown_month.append(unit["unit_id"])
    for month, members in sorted(periods.items()):
        if len(members) < 2:
            continue
        candidates.append(
            _candidate(
                dataset,
                method,
                ["publication_month", month],
                "已采用WHO通报的发布期次描述",
                members,
                metrics={
                    "publication_month": month,
                    "notification_count": len(members),
                    "qualification": "adopted_notice_publication_count_not_case_or_outbreak_count",
                    "collection_coverage": "bounded_adopted_scope_not_full_who_feed",
                    "complete_time_bucket": False,
                    "new_epidemic_claim_permitted": False,
                    "comparable": True,
                },
                gaps=("notification_sampling_coverage_unknown",),
                score=len(members),
            )
        )
    return _result(
        dataset,
        method,
        candidates,
        ready=True,
        statistics={
            "analysis_mode": "notification_descriptive",
            "notification_count": len(units),
            "publication_period_counts": {month: len(members) for month, members in sorted(periods.items())},
            "unknown_publication_month_unit_ids": unknown_month,
            "unknown_publication_time_unit_ids": [
                unit["unit_id"] for unit in units if not unit["dimensions"]["publication_start"]
            ],
            "collection_coverage": dataset.prepared.snapshot.coverage_mode,
            "population_or_incidence_inference_permitted": False,
        },
    )


def run_kdd(snapshot: NormalizedSnapshot, policy: KddPolicy) -> MiningResult:
    dataset = build_dataset(snapshot, policy)
    functions = {
        "research_tfidf_topics": _research_tfidf,
        "research_embedding_clusters": _research_embeddings,
        "research_evidence_balance": _research_balance,
        "conditional_pipeline_landscape": _pipeline,
        "pipeline_graph_hints": _pipeline_graph_hints,
        "business_metric_change": _metrics,
        "public_health_metric_change": _metrics,
        "public_health_notifications": _public_notifications,
    }
    outcomes = []
    for method in DIRECTION_METHODS[policy.kdd_direction]:
        try:
            outcome = functions[method](dataset)
        except (ValueError, TypeError, ArithmeticError) as error:
            outcome = _result(
                dataset, method, [], ready=False, reasons=("calculation_failed:" + type(error).__name__,)
            )
            outcome = outcome.model_copy(update={"execution_status": "failed"})
        outcomes.append(outcome)
    selected, evaluations, decisions, summary = evaluate_patterns(
        [candidate for outcome in outcomes for candidate in outcome.candidates], policy, dataset=dataset
    )
    preparation = dataset.manifest()
    preparation["selection_summary"] = summary
    preparation["dataset_hash_scope"] = (
        "data_preparation_without_dataset_hash_selection_summary_or_dataset_hash_scope"
    )
    payload = {
        "schema_version": "bioradar.engine.result.v1",
        "compute_version": KDD_COMPUTE_VERSION,
        "snapshot_hash": snapshot_hash(snapshot),
        "policy_hash": canonical_hash(policy),
        "method_results": [outcome.model_dump(mode="json") for outcome in outcomes],
        "candidates": [candidate.model_dump(mode="json") for candidate in selected],
        "data_preparation": preparation,
        "pattern_evaluations": list(evaluations),
        "selection_decisions": list(decisions),
        "limitations": sorted(
            set(dataset.gaps)
            | {"descriptive_patterns_not_statistical_significance_or_validated_business_value"}
            | ({"fixture_not_real_intelligence"} if snapshot.is_demo else set())
        ),
    }
    result = MiningResult(**payload, result_hash="pending")
    return result.model_copy(
        update={"result_hash": canonical_hash(result.model_dump(mode="json", exclude={"result_hash"}))}
    )
