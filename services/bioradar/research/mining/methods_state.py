"""Confirmed combinations and evidence-preserving state timeline comparisons."""

from __future__ import annotations

import unicodedata
from collections import defaultdict

from bioradar.research.mining.common import candidate, result, unsupported
from bioradar.research.mining.models import canonical_hash
from bioradar.research.mining.prepare import (
    PreparedSnapshot,
    confirmed_ref,
    ref_key,
    section_visible,
    time_interval,
)
from bioradar.research.mining.temporal import exposure_metrics


def entity_combinations(prepared: PreparedSnapshot):
    method = "entity_combinations"
    grouped = defaultdict(list)
    excluded = 0
    for item in prepared.records:
        record = item.record
        labels = record.labels
        refs = (*labels.target_refs, *labels.disease_refs)
        if (
            {"deleted", "retracted"} & set(record.record_flags)
            or not labels.technology_form
            or not labels.disease_refs
            or labels.label_provider_ref is None
        ):
            continue
        if labels.provenance is not None:
            if not section_visible(labels.provenance, record, prepared.policy):
                excluded += 1
                continue
        elif prepared.policy.time_mode == "availability_replay":
            excluded += 1
            continue
        if not all(confirmed_ref(record, ref, prepared.policy) for ref in refs):
            excluded += 1
            continue
        key = (
            tuple(sorted({ref_key(ref) for ref in labels.target_refs})),
            tuple(sorted({ref_key(ref) for ref in labels.disease_refs})),
            tuple(sorted(set(labels.technology_form))),
        )
        grouped[key].append(item)
    if not grouped:
        return unsupported(method, prepared, "confirmed_disease_and_technology_labels_required")
    eligible = [item for items in grouped.values() for item in items]
    current_total = len({i.unit_key for i in eligible if i.window == "current"})
    baseline_total = len({i.unit_key for i in eligible if i.window == "baseline"})
    candidates = []
    rows = []
    for key, items in sorted(grouped.items()):
        current = len({i.unit_key for i in items if i.window == "current"})
        baseline = len({i.unit_key for i in items if i.window == "baseline"})
        counts = exposure_metrics(current, baseline, current_total, baseline_total)
        confirmed_studies = len({i.unit_key for i in items if i.confirmed_study})
        families = len({i.record.source_family for i in items if i.confirmed_source})
        strict_r2 = (
            bool(key[0])
            and current >= 3
            and families >= 2
            and counts["r2_growth"] >= 2
            and all(i.growth_eligible for i in items if i.window in {"current", "baseline"})
        )
        counts.update(
            {
                "target_keys": list(key[0]) if key[0] else None,
                "target_dimension": "observed" if key[0] else "unknown_optional_enrichment",
                "disease_keys": list(key[1]),
                "technology_forms": list(key[2]),
                "confirmed_study_count": confirmed_studies,
                "confirmed_source_family_count": families,
                "r2_threshold_met": strict_r2,
                "scope": "adopted_snapshot",
            }
        )
        rows.append(counts)
        if prepared.policy.discovery_mode == "incremental" and current < prepared.policy.minimum_support:
            continue
        candidates.append(
            candidate(
                method,
                key,
                f"规范实体组合：{' / '.join(key[2])}",
                items,
                metrics=counts,
                score=counts["r2_growth"] if current else len({i.unit_key for i in items}),
                reasons=(
                    "r2_threshold_met" if strict_r2 else "combination_exploration",
                    "not_a_mechanism_confirmation",
                ),
                data_gaps=tuple({reason for item in items for reason in item.reasons}),
                questions=("该实体组合在相同研究条件下是否有独立机制支持？",),
                requirements=("S04机制证据评价", "S03支持与反证原文"),
            )
        )
    return result(
        method,
        prepared,
        candidates,
        readiness="ready"
        if all(i.growth_eligible for i in eligible) and prepared.coverage_complete
        else "exploratory_only",
        statistics={"combination_rows": rows, "unresolved_identity_rows": excluded},
        limitations=("stable_identity_not_adopted_version_is_the_grouping_key",),
    )


def _normalized_value(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).split())
    return value


def state_transitions(prepared: PreparedSnapshot):
    method = "state_transitions"
    grouped = defaultdict(list)
    unknown_identity = 0
    for item in prepared.history:
        if "deleted" in item.record.record_flags:
            continue
        for assertion in item.record.state_assertions:
            if not assertion.entity_ref or not confirmed_ref(
                item.record, assertion.entity_ref, prepared.policy
            ):
                unknown_identity += 1
                continue
            qualifiers = sorted(
                ({"key": q.key, "value": _normalized_value(q.value)} for q in assertion.qualifiers),
                key=canonical_hash,
            )
            key = (ref_key(assertion.entity_ref), assertion.property, canonical_hash(qualifiers))
            grouped[key].append((item, assertion, time_interval(assertion.effective_time)))
    if not grouped:
        return unsupported(method, prepared, "confirmed_entities_and_state_assertions_required")
    candidates = []
    format_only = 0
    missing_time = 0
    future_plans = 0
    compared = 0
    for key, entries in sorted(grouped.items()):
        distinct = {}
        for item, assertion, interval in entries:
            existing = distinct.get(assertion.assertion_ref.version_key)
            if existing is not None and existing[1] != assertion:
                raise ValueError("conflicting payloads for an adopted assertion version")
            distinct.setdefault(assertion.assertion_ref.version_key, (item, assertion, interval))
        timed = []
        for entry in distinct.values():
            item, assertion, interval = entry
            if interval is None:
                missing_time += 1
            elif interval[0] > prepared.policy.as_of:
                future_plans += 1
                candidates.append(
                    candidate(
                        method,
                        {"state": key, "planned": assertion.value, "time": interval[0].isoformat()},
                        f"未来计划断言：{assertion.property}",
                        [item],
                        kind="hypothesis",
                        reasons=("future_effective_assertion_is_not_observed_progress",),
                        metrics={
                            "planned_value": assertion.value,
                            "effective_time": assertion.effective_time.model_dump(mode="json"),
                            "is_recent": False,
                        },
                        questions=("计划是否已实际执行，何时有可采用的新状态依据？",),
                        requirements=("实际执行后有源状态断言",),
                    )
                )
            elif interval[1] > prepared.policy.as_of:
                missing_time += 1
            else:
                timed.append(entry)
        timed.sort(
            key=lambda row: (
                row[2],
                row[0].record.available_at or row[0].record.observed_at,
                row[1].assertion_ref.version_key,
            )
        )
        if len(timed) == 1:
            item, assertion, _ = timed[0]
            candidates.append(
                candidate(
                    method,
                    {"state": key, "initial": _normalized_value(assertion.value)},
                    f"初始状态断言：{assertion.property}",
                    [item],
                    kind="observed_change",
                    reasons=("initial_observation_not_state_progress",),
                    metrics={
                        "before": None,
                        "after": assertion.value,
                        "assertion_ref": assertion.assertion_ref.model_dump(mode="json"),
                    },
                    questions=("是否存在可采用的旧状态断言？",),
                    requirements=("同对象旧版本与有效时间",),
                )
            )
        for before, after in zip(timed, timed[1:]):
            old_item, old, old_time = before
            new_item, new, new_time = after
            compared += 1
            old_value, new_value = _normalized_value(old.value), _normalized_value(new.value)
            if canonical_hash(old_value) == canonical_hash(new_value):
                format_only += 1
                continue
            overlap = old_time[1] > new_time[0] or old_time[0] == new_time[0]
            source_revision = old.assertion_ref.identity_key == new.assertion_ref.identity_key
            kind = "observed_change" if source_revision or not overlap else "data_quality"
            reason = (
                "source_revision_not_new_effective_event"
                if source_revision
                else ("conflicting_assertions" if overlap else "asserted_state_change_requires_verification")
            )
            metrics = {
                "before": old.value,
                "after": new.value,
                "property": new.property,
                "before_assertion_ref": old.assertion_ref.model_dump(mode="json"),
                "after_assertion_ref": new.assertion_ref.model_dump(mode="json"),
                "before_effective_time": old.effective_time.model_dump(mode="json"),
                "after_effective_time": new.effective_time.model_dump(mode="json"),
                "overlapping_time": overlap,
                "is_recent": new_item.window == "current"
                and "historical_backfill" not in new_item.record.record_flags,
            }
            candidates.append(
                candidate(
                    method,
                    {"state": key, "before": old_value, "after": new_value, "reason": reason},
                    f"{'状态断言冲突' if kind == 'data_quality' else '状态断言变化'}：{new.property}",
                    [old_item, new_item],
                    kind=kind,
                    reasons=(reason,),
                    metrics=metrics,
                    score=1,
                    questions=("值变化是事实迁移、资料修订还是不同来源冲突？",),
                    requirements=("S01来源修订说明", "同对象时态断言核实"),
                )
            )
    return result(
        method,
        prepared,
        candidates,
        readiness="ready" if not unknown_identity and not missing_time else "exploratory_only",
        statistics={
            "state_groups": len(grouped),
            "comparisons": compared,
            "format_only": format_only,
            "unknown_identity": unknown_identity,
            "missing_effective_time": missing_time,
            "future_plans": future_plans,
        },
        limitations=("an_assertion_change_does_not_resolve_which_source_is_true",),
    )
