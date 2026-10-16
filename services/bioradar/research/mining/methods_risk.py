"""Bounded typed exposure paths and strictly comparable regional metrics."""

from __future__ import annotations

from collections import defaultdict, deque

from bioradar.research.mining.common import candidate, result, unsupported
from bioradar.research.mining.methods_network import eligible_relations
from bioradar.research.mining.prepare import PreparedSnapshot, ref_key, section_visible, time_interval


def event_exposure(prepared: PreparedSnapshot):
    method = "event_exposure"
    events = []
    for item in prepared.records:
        for event in item.record.events:
            if not section_visible(event.provenance, item.record, prepared.policy) or not event.evidence_refs:
                continue
            interval = time_interval(event.effective_time)
            if not interval or interval[1] > prepared.policy.as_of or not event.object_refs:
                continue
            events.append((item, event))
    if not events:
        return unsupported(
            method, prepared, "visible_evidence_backed_effective_event_and_direct_objects_required"
        )
    relations, excluded, truncated = eligible_relations(prepared, prepared.policy.exposure_relations)
    adjacency = defaultdict(list)
    for item, relation, _ in relations:
        if not relation.directed:
            continue  # An undirected association has no justified exposure direction.
        subject, target = ref_key(relation.subject_ref), ref_key(relation.object_ref)
        if relation.relation_type in prepared.policy.exposure_reverse_relations:
            subject, target = target, subject
        adjacency[subject].append((target, item, relation))
    for edges in adjacency.values():
        edges.sort(key=lambda edge: (edge[0], edge[2].relation_ref.version_key))
    candidates, explored_paths = [], 0
    partial = False
    pending_budget_drops = 0
    for event_item, event in sorted(events, key=lambda row: row[1].event_ref.version_key):
        roots = sorted(event.object_refs, key=lambda ref: ref.identity_key)
        allowed_roots = max(0, prepared.policy.limits.max_paths - explored_paths)
        if len(roots) > allowed_roots:
            partial = True
            pending_budget_drops += len(roots) - allowed_roots
        queue = deque((ref_key(ref), [ref_key(ref)], [], [event_item]) for ref in roots[:allowed_roots])
        while queue:
            if explored_paths >= prepared.policy.limits.max_paths:
                partial = True
                break
            node, nodes, edges, items = queue.popleft()
            explored_paths += 1
            effective = time_interval(event.effective_time)
            assert prepared.policy.current_start and prepared.policy.current_end and effective
            urgent = not edges and event.source_status == "supported"
            urgent = (
                urgent
                and prepared.policy.current_start <= effective[0]
                and (effective[1] <= prepared.policy.current_end)
            )
            urgent = urgent and event.event_type in prepared.policy.urgent_event_types
            candidates.append(
                candidate(
                    method,
                    {"event": event.event_ref.identity_key, "path": nodes},
                    "直接事件观察" if not edges else "条件化暴露路径",
                    items,
                    kind="observed_change"
                    if not edges and event.source_status == "supported"
                    else "hypothesis",
                    score=1 / len(nodes),
                    urgency="immediate" if urgent else "routine",
                    importance="high" if urgent else "undetermined",
                    reasons=(
                        "direct_source_event" if not edges else "typed_directional_exposure_hypothesis",
                        "reachability_is_not_causality_or_risk_probability",
                    ),
                    metrics={
                        "event_ref": event.event_ref.model_dump(mode="json"),
                        "event_type": event.event_type,
                        "jurisdiction": event.jurisdiction,
                        "nodes": nodes,
                        "relation_refs": [
                            relation.relation_ref.model_dump(mode="json") for relation in edges
                        ],
                        "relation_types": [relation.relation_type for relation in edges],
                        "hop_count": len(edges),
                        "path_direction_policy": "typed_forward_with_explicit_reverse_types",
                    },
                    data_gaps=() if not edges else ("exposure_strength_and_causal_conditions_unverified",),
                    questions=("事件与对象是否属于同一法域和适用期？", "每跳关系能否支持这类风险暴露？"),
                    requirements=("S03监管/供应/专利条款原文", "风险专家逐路径条件核实"),
                )
            )
            if len(edges) >= prepared.policy.limits.max_graph_hops:
                if any(target not in nodes for target, _, _ in adjacency[node]):
                    partial = True
                continue
            for target, relation_item, relation in adjacency[node]:
                if target not in nodes:
                    if explored_paths + len(queue) >= prepared.policy.limits.max_paths:
                        partial = True
                        pending_budget_drops += 1
                        continue
                    queue.append((target, [*nodes, target], [*edges, relation], [*items, relation_item]))
        if partial and explored_paths >= prepared.policy.limits.max_paths:
            break
    return result(
        method,
        prepared,
        candidates,
        partial=partial,
        truncated=truncated,
        statistics={
            "event_count": len(events),
            "paths_evaluated": explored_paths,
            "eligible_edges": len(relations),
            "excluded_edges": excluded,
            "pending_path_budget_drops": pending_budget_drops,
            "cross_object_variant_status": "success" if relations else "unsupported",
        },
        limitations=("law_jurisdiction_and_event_specific_propagation_require_domain_review",),
    )


def regional_comparison(prepared: PreparedSnapshot):
    method = "regional_comparison"
    grouped = defaultdict(list)
    for item in prepared.records:
        for indicator in item.record.regional_indicators:
            if not indicator.evidence_refs or not section_visible(
                indicator.provenance, item.record, prepared.policy
            ):
                continue
            if not all(
                (
                    indicator.indicator,
                    indicator.region,
                    indicator.period,
                    indicator.unit,
                    indicator.population_scope,
                    indicator.jurisdiction,
                )
            ):
                continue
            key = (
                indicator.indicator,
                indicator.period,
                indicator.unit,
                indicator.population_scope,
                indicator.jurisdiction,
                indicator.denominator is not None,
            )
            grouped[key].append((item, indicator))
    usable = {key: rows for key, rows in grouped.items() if len({i.region for _, i in rows}) >= 2}
    if not usable:
        return unsupported(
            method, prepared, "at_least_two_regions_with_same_indicator_unit_period_scope_required"
        )
    candidates, all_rows = [], []
    for key, entries in sorted(usable.items()):
        by_region = {}
        conflicts = set()
        for item, indicator in entries:
            by_region.setdefault(indicator.region, []).append((item, indicator))
        for region, evidence in sorted(by_region.items()):
            substantive = {(indicator.value, indicator.denominator) for _, indicator in evidence}
            if len(substantive) > 1:
                conflicts.add(region)
                candidates.append(
                    candidate(
                        method,
                        {"indicator_conflict": key, "region": region},
                        "同口径区域指标来源冲突",
                        [item for item, _ in evidence],
                        kind="data_quality",
                        score=1,
                        reasons=("conflicting_values_are_not_zero_or_resolved_by_source_order",),
                        metrics={
                            "region": region,
                            "observed_values": [
                                {
                                    "value": indicator.value,
                                    "denominator": indicator.denominator,
                                    "provider_ref": indicator.provenance.provider_ref.model_dump(mode="json"),
                                }
                                for _, indicator in evidence
                            ],
                        },
                        questions=("哪个指标版本适用，口径或数值为什么不同？",),
                        requirements=("S01指标修订与口径核实",),
                    )
                )
        rows = []
        for region, evidence in sorted(by_region.items()):
            if region in conflicts:
                continue
            indicator = evidence[0][1]
            normalized = (
                indicator.value / indicator.denominator
                if indicator.denominator is not None
                else (indicator.value)
            )
            rows.append(
                {
                    "region": region,
                    "value": indicator.value,
                    "denominator": indicator.denominator,
                    "comparable_value": normalized,
                    "support_record_count": len(evidence),
                    "normalization": "per_declared_denominator"
                    if indicator.denominator is not None
                    else "same_unit_raw",
                }
            )
        if len(rows) < 2:
            continue
        values = [r["comparable_value"] for r in rows]
        metric = {
            "indicator": key[0],
            "period": key[1],
            "unit": key[2],
            "population_scope": key[3],
            "jurisdiction": key[4],
            "rows": rows,
            "difference_max_min": max(values) - min(values),
            "excluded_conflicting_regions": sorted(conflicts),
        }
        all_rows.append(metric)
        candidates.append(
            candidate(
                method,
                {"indicator_group": key},
                f"同口径区域指标差异：{key[0]}",
                [
                    item
                    for region, evidence in by_region.items()
                    if region not in conflicts
                    for item, _ in evidence
                ],
                metrics=metric,
                score=abs(max(values) - min(values)),
                reasons=("standardized_descriptive_difference_not_policy_causation",),
                questions=("差异是否受到覆盖、人口构成和统计口径影响？",),
                requirements=("S01指标口径与分母", "B09政策选项与敏感性审查"),
            )
        )
    return result(
        method,
        prepared,
        candidates,
        statistics={"comparison_groups": all_rows},
        limitations=("regional_difference_does_not_establish_policy_effect",),
    )
