"""Review priority from fixed prior periods; this never changes value eligibility."""

from collections import defaultdict
from datetime import datetime

from bioradar.research.mining.models import ProviderRef, canonical_hash


def period_priorities(dataset, policy, candidates):
    if policy.monitor_changed_refs is None:
        return {}
    if dataset is None:
        return {c.candidate_key: {"classification": "missing_analysis_dataset"} for c in candidates}
    from bioradar.research.datasets.build import build_dataset
    from bioradar.research.mining.kdd import _period_basis

    changed = {ref.version_key for ref in policy.monitor_changed_refs}
    identities = {ref.identity_key for ref in policy.monitor_changed_refs}
    history = [item.record for item in dataset.prepared.history]
    changed_times = defaultdict(list)
    for record in history:
        if record.record_ref.version_key in changed:
            changed_times[record.record_ref.identity_key].append(record.available_at or record.observed_at)
    prior = [
        record
        for record in history
        if record.record_ref.identity_key in identities
        and record.record_ref.version_key not in changed
        and any(
            (record.available_at or record.observed_at) < timestamp
            for timestamp in changed_times[record.record_ref.identity_key]
        )
    ]
    # Numeric datasets retain current heads; their prior fixed versions are in
    # prepared.history. Reuse the same source/metric/time qualification on those
    # actual frozen originals, rather than treating current-file history as new.
    prior_units = ()
    if prior:
        prior_snapshot = dataset.prepared.snapshot.model_copy(update={"records": tuple(prior)})
        prior_units = build_dataset(prior_snapshot, policy).units

    def series(unit):
        dimensions = unit["dimensions"]
        values = {
            key: value
            for key, value in dimensions.items()
            if key not in {"period_start", "period_end", "subject_ref"}
        }
        values["period_basis"] = _period_basis(dimensions)
        return canonical_hash(values)

    baseline = defaultdict(list)
    for unit in prior_units:
        if not unit["dimensions"].get("period_start"):
            continue
        refs = [ProviderRef.model_validate(m["record_ref"]) for m in unit["members"]]
        for identity in {ref.identity_key for ref in refs}:
            baseline[identity, series(unit)].append((unit, refs))
    units = {unit["unit_id"]: unit for unit in dataset.units}
    result = {}
    for candidate in candidates:
        ids = candidate.metrics.get("period_unit_ids") or []
        if len(ids) != 2 or ids[-1] not in units:
            result[candidate.candidate_key] = {"classification": "no_qualified_numeric_period_mapping"}
            continue
        current = units[ids[-1]]
        dimensions = current["dimensions"]
        refs = [ProviderRef.model_validate(m["record_ref"]) for m in current["members"]]
        matching = [ref for ref in refs if ref.version_key in changed]
        context = {
            "classification": "historical_stock",
            "period_unit_ids": list(ids),
            "current_period_start": dimensions["period_start"],
            "current_period_end": dimensions["period_end"],
            "basis": (
                "qualified_earlier_fixed_version_same_native_record_subject_metric_unit_and_period_basis"
            ),
        }
        if matching:
            old = {
                unit["unit_id"]: (unit, refs)
                for ref in matching
                for unit, refs in baseline.get((ref.identity_key, series(current)), [])
            }
            context["changed_ref_keys"] = [list(ref.version_key) for ref in matching]
            if not old:
                context["classification"] = "missing_prior_fixed_version_baseline"
            else:
                latest = max(
                    datetime.fromisoformat(unit["dimensions"]["period_end"]) for unit, _ in old.values()
                )
                start, end = (
                    datetime.fromisoformat(dimensions[key]) for key in ("period_start", "period_end")
                )
                context["baseline_latest_period_end"] = latest.isoformat()
                context["baseline_period_count"] = len(old)
                context["baseline_unit_ids"] = sorted(
                    key
                    for key, (unit, _) in old.items()
                    if datetime.fromisoformat(unit["dimensions"]["period_end"]) == latest
                    or (
                        unit["dimensions"]["period_start"] == dimensions["period_start"]
                        and unit["dimensions"]["period_end"] == dimensions["period_end"]
                    )
                )
                context["baseline_ref_keys"] = [
                    list(key) for key in sorted({ref.version_key for _, refs in old.values() for ref in refs})
                ]
                if any(
                    unit["dimensions"]["period_start"] == dimensions["period_start"]
                    and unit["dimensions"]["period_end"] == dimensions["period_end"]
                    for unit, _ in old.values()
                ):
                    context["classification"] = "existing_fixed_period"
                elif end > latest and start >= latest:
                    context["classification"] = "new_period"
                elif end <= latest:
                    context["classification"] = "historical_period_backfill"
                else:
                    context["classification"] = "overlapping_or_revised_period"
        result[candidate.candidate_key] = context
    return result
