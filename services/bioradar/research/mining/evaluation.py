"""Descriptive value admission followed by separately recorded queue capacity."""

from collections import Counter

from bioradar.research.mining.models import Candidate, KddPolicy
from bioradar.research.mining.runner import candidate_order, select_candidates


def evaluate_patterns(candidates: list[Candidate], policy: KddPolicy, *, dataset=None):
    from bioradar.research.mining.incremental import period_priorities

    incremental = period_priorities(dataset, policy, candidates)

    def review_order(candidate):
        base = candidate_order(candidate)
        new_period = incremental.get(candidate.candidate_key, {}).get("classification") == "new_period"
        return (base[0], -int(new_period), *base[1:])

    evaluations = []
    eligible = []
    thresholds = policy.value_policy
    for candidate in sorted(candidates, key=lambda value: value.candidate_key):
        metrics = candidate.metrics
        units = int(metrics.get("analysis_unit_count", metrics.get("support_units", len(candidate.supports))))
        novelty = metrics.get("cosine_distance")
        effect = metrics.get("effect")
        reasons = []
        accepted = True
        exploratory_topic = (
            units == 1
            and candidate.kind == "hypothesis"
            and metrics.get("representation") == "tfidf_nmf_control"
            and metrics.get("mode") == "stock_exploration"
            and thresholds.allow_singleton_exploration
        )
        if metrics.get("comparable") is False:
            reasons.append("conditions_or_periods_not_comparable")
            accepted = False
        if metrics.get("conflicting_values"):
            reasons.append("conflicting_source_values_require_resolution")
            accepted = False
        if units < thresholds.minimum_pattern_support:
            singleton_novelty = (
                units == 1
                and novelty is not None
                and novelty >= thresholds.novelty_distance_threshold
                and thresholds.allow_singleton_exploration
            )
            native_notice = metrics.get("native_notice_event") is True
            graph_hint = metrics.get("graph_relation_hint") is True
            if native_notice:
                reasons.append("single_official_notice_for_verification_not_outbreak_detection")
            elif graph_hint:
                reasons.append(
                    "single_fixed_graph_hypothesis_for_original_verification_not_confirmed_relation"
                )
            elif singleton_novelty:
                reasons.append("single_unit_novelty_for_followup_not_independent_replication")
            elif exploratory_topic:
                reasons.append("single_document_topic_for_content_followup_not_supported_pattern")
            else:
                accepted = False
                reasons.append("below_frozen_minimum_pattern_support")
        if effect is not None:
            low, high = effect.get("lower"), effect.get("upper")
            if low is None or high is None:
                accepted = False
                reasons.append("effect_unknown")
            elif low <= 0 <= high:
                accepted = False
                reasons.append("no_directional_change_or_overlapping_value_intervals")
            else:
                minimum = min(abs(low), abs(high))
                relative = effect.get("relative_minimum")
                if minimum < thresholds.absolute_change_threshold:
                    accepted = False
                    reasons.append("below_frozen_absolute_change_threshold")
                elif relative is not None and relative < thresholds.relative_change_threshold:
                    accepted = False
                    reasons.append("below_frozen_relative_change_threshold")
                elif relative is None and thresholds.absolute_change_threshold <= 0:
                    accepted = False
                    reasons.append("relative_change_undefined_requires_explicit_absolute_threshold")
                else:
                    reasons.append("comparable_directional_change_meets_frozen_thresholds")
        elif novelty is not None:
            if novelty < thresholds.novelty_distance_threshold:
                accepted = False
                reasons.append("below_frozen_novelty_threshold")
            else:
                reasons.append("descriptive_novelty_requires_content_review")
        elif candidate.kind == "data_quality":
            accepted = False
            reasons.append("data_quality_gap_not_business_pattern")
        elif metrics.get("unchanged"):
            accepted = False
            reasons.append("unchanged_inventory_retained_as_negative_control")
        else:
            reasons.append("bounded_pattern_for_expert_value_review")
        score = None
        if accepted:
            if not exploratory_topic:
                score = float(
                    novelty if novelty is not None else (effect or {}).get("relative_minimum") or 1.0
                )
            eligible.append(candidate)
        evaluations.append(
            {
                "candidate_key": candidate.candidate_key,
                "eligible": accepted,
                "value_score": score,
                "value_reasons": reasons,
                "novelty": {"kind": "descriptive_cosine_distance", "distance": novelty}
                if novelty is not None
                else None,
                "effect": effect,
                "uncertainty": {
                    "statistical_significance": "not_established",
                    "data_gaps": list(candidate.data_gaps),
                },
                "importance": candidate.importance,
                "urgency": candidate.urgency,
                "required_evidence": list(candidate.next_data_requirements),
                "eligibility_kind": "exploratory_content_followup"
                if exploratory_topic
                else "descriptive_pattern",
                "qualification": (
                    "exploratory_content_followup_not_supported_pattern"
                    if exploratory_topic
                    else "descriptive_attention_rule_not_validated_business_value_or_scientific_approval"
                ),
            }
        )
        if incremental:
            evaluations[-1]["incremental_context"] = incremental[candidate.candidate_key]
    # Apply the method-specific candidate bound here, after every generated
    # candidate has been saved and evaluated. It is a resource limit, not value.
    pools = {}
    capacity_deferred = set()
    new_order = any(
        incremental.get(candidate.candidate_key, {}).get("classification") == "new_period"
        for candidate in eligible
    )
    for candidate in sorted(eligible, key=review_order if new_order else candidate_order):
        pool = pools.setdefault(candidate.method_id, [])
        if len(pool) >= policy.limits.max_candidates_per_method:
            capacity_deferred.add(candidate.candidate_key)
        else:
            pool.append(candidate)
    admitted = [candidate for pool in pools.values() for candidate in pool]
    selected = select_candidates(
        admitted,
        policy,
        sort_key=review_order if new_order else None,
        bucket_dimensions=3 if new_order else 2,
    )
    selected_keys = {candidate.candidate_key for candidate in selected}
    selected_supports = Counter(
        (
            candidate.method_id,
            tuple(sorted(support.record_ref.identity_key for support in candidate.supports)),
        )
        for candidate in selected
    )
    eligibility = {evaluation["candidate_key"]: evaluation["eligible"] for evaluation in evaluations}
    decisions = []
    for candidate in sorted(candidates, key=lambda value: value.candidate_key):
        key = candidate.candidate_key
        signature = candidate.method_id, tuple(sorted(s.record_ref.identity_key for s in candidate.supports))
        if not eligibility[key]:
            decision, reasons = "value_excluded", ["frozen_value_policy_not_met"]
        elif key in selected_keys:
            exploratory = next(
                item["eligibility_kind"] == "exploratory_content_followup"
                for item in evaluations
                if item["candidate_key"] == key
            )
            decision, reasons = (
                "selected",
                [
                    "exploratory_content_followup_within_review_capacity"
                    if exploratory
                    else "value_eligible_and_within_review_capacity"
                ],
            )
        elif key in capacity_deferred:
            decision, reasons = "capacity_deferred", ["method_candidate_capacity"]
        elif (
            candidate.urgency != "immediate"
            and selected_supports[signature] >= policy.limits.max_candidates_per_group
        ):
            decision, reasons = "redundancy_deferred", ["same_support_review_capacity"]
        else:
            decision, reasons = "capacity_deferred", ["total_review_capacity"]
        if new_order and eligibility[key] and incremental.get(key, {}).get("classification") == "new_period":
            reasons.append("new_fixed_period_review_priority_after_value_admission")
        decisions.append({"candidate_key": key, "decision": decision, "reasons": reasons})
    counts = Counter(item["decision"] for item in decisions)
    summary = {
        "generated_count": len(candidates),
        "value_eligible_count": len(eligible),
        "exploratory_followup_eligible_count": sum(
            item["eligible"] and item["eligibility_kind"] == "exploratory_content_followup"
            for item in evaluations
        ),
        "descriptive_pattern_eligible_count": sum(
            item["eligible"] and item["eligibility_kind"] == "descriptive_pattern" for item in evaluations
        ),
        "selected_count": len(selected),
        "value_excluded_count": counts["value_excluded"],
        "capacity_deferred_count": counts["capacity_deferred"],
        "redundancy_deferred_count": counts["redundancy_deferred"],
        "review_capacity": policy.limits.review_capacity,
        "method_candidate_capacity": policy.limits.max_candidates_per_method,
        "all_generated_candidates_retained": True,
        "deferred_candidate_keys": [
            item["candidate_key"] for item in decisions if item["decision"].endswith("deferred")
        ],
    }
    if incremental:
        summary["incremental_review"] = {
            "version": "qualified-prior-fixed-period-priority-v1",
            "changed_version_count": len(policy.monitor_changed_refs or ()),
            "priority_applied": new_order,
            "classifications": dict(Counter(c["classification"] for c in incremental.values())),
            "new_period_candidate_keys": sorted(
                k for k, c in incremental.items() if c["classification"] == "new_period"
            ),
            "qualification": (
                "Priority only after unchanged value thresholds; historical and missing-baseline "
                "candidates remain in full method results."
            ),
        }
    return selected, tuple(evaluations), tuple(decisions), summary
