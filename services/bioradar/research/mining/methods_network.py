"""Small adopted graph projections; no writes to the shared entity graph."""

from __future__ import annotations

from collections import Counter, defaultdict

import networkx as nx

from bioradar.research.mining.common import candidate, result, unsupported
from bioradar.research.mining.prepare import PreparedSnapshot, ref_key, section_visible, time_interval


def eligible_relations(prepared: PreparedSnapshot, allowed: tuple[str, ...]):
    selected, excluded, seen = [], Counter(), {}
    for item in prepared.records:
        for relation in item.record.relations:
            if relation.relation_type not in allowed:
                continue
            if relation.identity_status != "confirmed" or not relation.evidence_refs:
                excluded["identity_or_edge_evidence_missing"] += 1
                continue
            if not section_visible(relation.provenance, item.record, prepared.policy):
                excluded["edge_version_not_visible"] += 1
                continue
            interval = time_interval(relation.effective_time)
            if not interval or interval[1] > prepared.policy.as_of:
                excluded["edge_effective_time_unknown_or_future"] += 1
                continue
            until = time_interval(relation.valid_until) if relation.valid_until else None
            if relation.valid_until and (not until or until[0] <= prepared.policy.as_of):
                excluded["edge_expired_or_expiry_ambiguous"] += 1
                continue
            key = relation.relation_ref.version_key
            if key in seen:
                if seen[key] != relation:
                    raise ValueError("conflicting versions of an adopted relation")
                continue
            seen[key] = relation
            selected.append((item, relation, interval))
    selected.sort(key=lambda row: row[1].relation_ref.version_key)
    limit = prepared.policy.limits.max_graph_edges
    return selected[:limit], dict(excluded), max(0, len(selected) - limit)


def research_network(prepared: PreparedSnapshot):
    method = "research_network"
    relations, excluded, truncated = eligible_relations(prepared, prepared.policy.cooperation_relations)
    if not relations:
        return unsupported(method, prepared, "confirmed_evidence_backed_timed_cooperation_relations_required")
    baseline, complete = nx.Graph(), nx.Graph()
    current_edges = []
    paper_members = defaultdict(set)
    for _, relation, _ in relations:
        paper = ref_key(relation.study_cluster_ref or relation.relation_ref)
        paper_members[paper].update((ref_key(relation.subject_ref), ref_key(relation.object_ref)))
    seen_contributions = set()
    assert prepared.policy.current_start
    for item, relation, interval in relations:
        a, b = sorted((ref_key(relation.subject_ref), ref_key(relation.object_ref)))
        if a == b:
            continue
        paper = ref_key(relation.study_cluster_ref or relation.relation_ref)
        contribution = (paper, a, b)
        if contribution in seen_contributions:
            continue
        seen_contributions.add(contribution)
        weight = 1 / max(1, len(paper_members[paper]) - 1)
        old_weight = complete.get_edge_data(a, b, {}).get("weight", 0)
        complete.add_edge(a, b, weight=old_weight + weight)
        if interval[1] < prepared.policy.current_start:
            baseline.add_edge(a, b, weight=baseline.get_edge_data(a, b, {}).get("weight", 0) + weight)
        elif item.window == "current" and "historical_backfill" not in item.record.record_flags:
            current_edges.append((a, b, item, relation))
    components = sorted((sorted(group) for group in nx.connected_components(baseline)))
    component_id = {node: index for index, group in enumerate(components) for node in group}
    candidates = []
    for a, b, item, relation in current_edges:
        if baseline.has_edge(a, b):
            continue
        bridge = a in component_id and b in component_id and component_id[a] != component_id[b]
        candidates.append(
            candidate(
                method,
                {"new_edge": (a, b)},
                "研究网络新增桥接" if bridge else "新增合作关系观察",
                [item],
                kind="observed_change",
                score=2 if bridge else 1,
                reasons=("joins_baseline_components" if bridge else "new_in_adopted_projection",),
                metrics={
                    "endpoints": [a, b],
                    "baseline_component_ids": [component_id.get(a), component_id.get(b)],
                    "is_bridge": bridge,
                    "relation_ref": relation.relation_ref.model_dump(mode="json"),
                },
                questions=("合作变化是否真实，还是来源扩展或消歧修订？",),
                requirements=("S02同名消歧与合作事件核实",),
            )
        )
    pair_set = set()
    over_limit = False
    for a in sorted(baseline.nodes):
        for neighbor in sorted(baseline.neighbors(a)):
            for b in sorted(baseline.neighbors(neighbor)):
                if a < b and not baseline.has_edge(a, b):
                    pair_set.add((a, b))
                    if len(pair_set) >= prepared.policy.limits.max_link_pairs:
                        over_limit = True
                        break
            if over_limit:
                break
        if over_limit:
            break
    node_items = defaultdict(list)
    for item, relation, _ in relations:
        node_items[ref_key(relation.subject_ref)].append(item)
        node_items[ref_key(relation.object_ref)].append(item)
    for a, b, score in nx.adamic_adar_index(baseline, sorted(pair_set)):
        if score <= 0:
            continue
        shared = sorted(set(baseline[a]) & set(baseline[b]))
        candidates.append(
            candidate(
                method,
                {"link_hypothesis": (a, b)},
                "合作连接假说，尚未观察到关系",
                node_items[a] + node_items[b],
                kind="hypothesis",
                score=score,
                reasons=("adamic_adar_prediction_is_not_a_real_edge",),
                metrics={
                    "endpoints": [a, b],
                    "common_neighbors": shared,
                    "adamic_adar_index": round(score, 8),
                },
                questions=("这条连接假说是否有独立合作证据？",),
                requirements=("S03合作原文检索",),
            )
        )
    return result(
        method,
        prepared,
        candidates,
        statistics={
            "nodes": complete.number_of_nodes(),
            "edges": complete.number_of_edges(),
            "baseline_components": components,
            "weighted_degrees": dict(sorted(complete.degree(weight="weight"))),
            "excluded_edges": excluded,
            "link_pairs_evaluated": len(pair_set),
        },
        partial=over_limit,
        truncated=truncated,
        limitations=(
            "adopted_projection_does_not_prove_global_network_absence",
            "network_links_are_not_causal_evidence",
        ),
    )
