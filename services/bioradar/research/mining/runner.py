"""Pure method execution and deterministic review-capacity candidate selection."""

from __future__ import annotations

import math
from collections import defaultdict, deque

from bioradar.research.mining.methods_impact import counterevidence_impact
from bioradar.research.mining.methods_network import research_network
from bioradar.research.mining.methods_pipeline import maturity_shift, pipeline_landscape, translation_dwell
from bioradar.research.mining.methods_risk import event_exposure, regional_comparison
from bioradar.research.mining.methods_state import entity_combinations, state_transitions
from bioradar.research.mining.methods_text import lexical_emergence, text_topics
from bioradar.research.mining.models import (
    Candidate,
    MethodResult,
    MiningPolicy,
    MiningResult,
    NormalizedSnapshot,
    canonical_hash,
)
from bioradar.research.mining.prepare import prepare, snapshot_hash
from bioradar.research.mining.registry import ENGINE_COMPUTE_VERSION, METHODS

IMPLEMENTATIONS = {
    "lexical_emergence": lexical_emergence,
    "text_topics": text_topics,
    "entity_combinations": entity_combinations,
    "state_transitions": state_transitions,
    "pipeline_landscape": pipeline_landscape,
    "research_network": research_network,
    "maturity_shift": maturity_shift,
    "counterevidence_impact": counterevidence_impact,
    "translation_dwell": translation_dwell,
    "event_exposure": event_exposure,
    "regional_comparison": regional_comparison,
}
assert set(IMPLEMENTATIONS) == set(METHODS)


def candidate_order(candidate: Candidate) -> tuple:
    urgency = {"immediate": 0, "soon": 1, "routine": 2, "undetermined": 3}
    importance = {"high": 0, "medium": 1, "low": 2, "undetermined": 3}
    return (
        urgency[candidate.urgency],
        importance[candidate.importance],
        candidate.method_id,
        -candidate.metrics.get("confirmed_study_count", 0),
        -candidate.score,
        candidate.candidate_key,
    )


def select_candidates(
    candidates: list[Candidate], policy: MiningPolicy, *, sort_key=None, bucket_dimensions=2
) -> tuple[Candidate, ...]:
    rank_key = sort_key or candidate_order
    ordered = sorted(candidates, key=rank_key)
    selected: dict[str, Candidate] = {}
    support_groups = {}
    capacity = policy.limits.review_capacity

    def take(candidate: Candidate) -> bool:
        if candidate.candidate_key in selected or len(selected) >= capacity:
            return False
        # Same-support lexical variants cannot consume the whole review queue.
        signature = (
            candidate.method_id,
            tuple(sorted(s.record_ref.identity_key for s in candidate.supports)),
        )
        count = support_groups.get(signature, 0)
        if candidate.urgency != "immediate" and count >= policy.limits.max_candidates_per_group:
            return False
        selected[candidate.candidate_key] = candidate
        support_groups[signature] = count + 1
        return True

    for c in ordered:
        if c.urgency == "immediate":
            take(c)
    nonurgent_capacity = capacity - len(selected)
    reserve = math.ceil(nonurgent_capacity * policy.exploration_fraction)
    explorations = [
        c
        for c in ordered
        if c.urgency != "immediate"
        and (c.kind == "hypothesis" or c.data_gaps or c.metrics.get("mode") == "stock_exploration")
    ]
    for c in explorations:
        if reserve <= 0:
            break
        if take(c):
            reserve -= 1
    # All remaining slots use repeated fair rounds. Method scores have different
    # dimensions and are compared only inside that method's priority bucket.
    buckets = defaultdict(lambda: defaultdict(deque))
    for c in ordered:
        buckets[rank_key(c)[:bucket_dimensions]][c.method_id].append(c)
    for _, pools in sorted(buckets.items()):
        while any(pools.values()) and len(selected) < capacity:
            for method in sorted(pools):
                while pools[method]:
                    if take(pools[method].popleft()):
                        break
    return tuple(sorted(selected.values(), key=lambda c: rank_key(c)[:bucket_dimensions]))


def run_mining(snapshot: NormalizedSnapshot, policy: MiningPolicy) -> MiningResult:
    from bioradar.research.mining.models import DiscoveryPolicy, KddPolicy

    if isinstance(policy, DiscoveryPolicy):
        from bioradar.research.mining.kdd import run_discovery

        return run_discovery(snapshot, policy)
    if isinstance(policy, KddPolicy):
        from bioradar.research.mining.kdd import run_kdd

        return run_kdd(snapshot, policy)
    prepared = prepare(snapshot, policy)
    outcomes = []
    for identifier in policy.method_ids:
        try:
            outcome = IMPLEMENTATIONS[identifier](prepared)
        except Exception as exc:
            # Never serialize exception text: it may contain input evidence/secrets.
            outcome = MethodResult(
                method_id=identifier,
                method_version=METHODS[identifier].version,
                readiness="exploratory_only",
                execution_status="failed",
                reasons=(f"calculation_failed:{type(exc).__name__}",),
                excluded_records=prepared.excluded_records,
                limitations=prepared.limitations,
            )
        outcomes.append(outcome)
    candidates = select_candidates([c for result in outcomes for c in result.candidates], policy)
    limitations = set(prepared.limitations)
    limitations.add("candidate_review_and_scientific_validation_are_not_completed_by_mining")
    if snapshot.is_demo:
        limitations.add("fixture_not_real_intelligence")
    payload = {
        "schema_version": "bioradar.engine.result.v1",
        "compute_version": ENGINE_COMPUTE_VERSION,
        "snapshot_hash": snapshot_hash(snapshot),
        "policy_hash": canonical_hash(policy),
        "method_results": [r.model_dump(mode="json") for r in outcomes],
        "candidates": [c.model_dump(mode="json") for c in candidates],
        "limitations": sorted(limitations),
    }
    return MiningResult(**payload, result_hash=canonical_hash(payload))
