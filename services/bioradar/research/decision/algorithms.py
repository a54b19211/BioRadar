"""Exact interval arithmetic over a finite, explicitly declared comparison set."""

from __future__ import annotations

from decimal import Decimal
from itertools import combinations

from bioradar.research.decision.models import (
    BaselineDifference,
    ConstraintCheck,
    DecisionInput,
    DecisionPolicy,
    NumericInterval,
    OptionFeasibility,
    OutcomeCell,
    ParetoComparison,
    RegretCase,
    RegretSummary,
)

ZERO = Decimal(0)


def cell_index(decision_input: DecisionInput) -> dict[tuple[str, str, str], OutcomeCell]:
    return {(c.option_id, c.scenario_id, c.metric_id): c for c in decision_input.outcomes}


def oriented(value: NumericInterval, direction: str) -> tuple[Decimal, Decimal]:
    return (value.lower, value.upper) if direction == "maximize" else (-value.upper, -value.lower)


def check_constraints(decision_input: DecisionInput) -> tuple[OptionFeasibility, ...]:
    cells = cell_index(decision_input)
    results = []
    for option in sorted(decision_input.options, key=lambda o: o.option_id):
        checks = []
        for scenario in sorted(decision_input.scenarios, key=lambda s: s.scenario_id):
            for constraint in sorted(decision_input.constraints, key=lambda c: c.constraint_id):
                cell = cells.get((option.option_id, scenario.scenario_id, constraint.metric_id))
                value, threshold = cell.value if cell else None, constraint.threshold
                status = "unknown"
                shortfall = None
                reason = "missing_outcome_or_threshold"
                if value is not None and threshold is not None:
                    if constraint.operator == "le":
                        low, high = value.lower - threshold.upper, value.upper - threshold.lower
                    else:
                        low, high = threshold.lower - value.upper, threshold.upper - value.lower
                    shortfall = NumericInterval(lower=max(ZERO, low), upper=max(ZERO, high))
                    if high <= ZERO:
                        status, reason = "pass", "all_declared_bounds_meet_threshold"
                    elif low > ZERO:
                        status, reason = "fail", "all_declared_bounds_violate_threshold"
                    else:
                        status, reason = "uncertain", "bounds_cross_threshold_no_joint_dependence_model"
                checks.append(
                    ConstraintCheck(
                        option_id=option.option_id,
                        scenario_id=scenario.scenario_id,
                        constraint_id=constraint.constraint_id,
                        metric_id=constraint.metric_id,
                        status=status,
                        value=value,
                        threshold=threshold,
                        shortfall=shortfall,
                        reason=reason,
                    )
                )
        statuses = {c.status for c in checks}
        status = next((s for s in ("fail", "unknown", "uncertain") if s in statuses), "pass")
        reasons = tuple(sorted({c.reason for c in checks if c.status != "pass"}))
        if not checks:
            reasons = ("no_hard_constraints_declared_no_authority_or_capacity_inferred",)
        results.append(
            OptionFeasibility(
                option_id=option.option_id, status=status, checks=tuple(checks), reasons=reasons
            )
        )
    return tuple(results)


def baseline_differences(decision_input: DecisionInput) -> tuple[BaselineDifference, ...]:
    cells = cell_index(decision_input)
    results = []
    baseline = decision_input.brief.baseline_option_id
    for option in sorted(decision_input.options, key=lambda o: o.option_id):
        for scenario in sorted(decision_input.scenarios, key=lambda s: s.scenario_id):
            for metric in sorted(decision_input.metrics, key=lambda m: m.metric_id):
                cell = cells.get((option.option_id, scenario.scenario_id, metric.metric_id))
                base = cells.get((baseline, scenario.scenario_id, metric.metric_id))
                value, base_value = cell.value if cell else None, base.value if base else None
                difference, status = None, "unknown"
                if value is not None and base_value is not None:
                    if option.option_id == baseline:
                        difference, status = NumericInterval(lower=ZERO, upper=ZERO), "exact"
                    else:
                        difference = NumericInterval(
                            lower=value.lower - base_value.upper, upper=value.upper - base_value.lower
                        )
                        status = "exact" if difference.lower == difference.upper else "bounded"
                results.append(
                    BaselineDifference(
                        option_id=option.option_id,
                        baseline_option_id=baseline,
                        scenario_id=scenario.scenario_id,
                        metric_id=metric.metric_id,
                        difference=difference,
                        status=status,
                    )
                )
    return tuple(results)


def _relation(pairs: list[tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]] | None]) -> tuple[str, str]:
    if not pairs or any(pair is None for pair in pairs):
        return "unknown", "missing_comparable_outcome_no_imputation"
    known = [pair for pair in pairs if pair is not None]
    if all(a[0] == a[1] == b[0] == b[1] for a, b in known):
        return "tied", "all_selected_point_outcomes_equal"
    if all(a[0] >= b[1] for a, b in known) and any(a[0] > b[1] for a, b in known):
        return "a_dominates", "guaranteed_interval_dominance_on_declared_metrics"
    if all(b[0] >= a[1] for a, b in known) and any(b[0] > a[1] for a, b in known):
        return "b_dominates", "guaranteed_interval_dominance_on_declared_metrics"
    return "incomparable", "tradeoff_or_overlapping_bounds_no_default_weights"


def compare_pareto(
    decision_input: DecisionInput, policy: DecisionPolicy, comparison_set: tuple[str, ...]
) -> tuple[ParetoComparison, ...]:
    cells = cell_index(decision_input)
    metrics = {m.metric_id: m for m in decision_input.metrics}
    ids = tuple(sorted(policy.pareto_metric_ids))
    scenarios = tuple(sorted(s.scenario_id for s in decision_input.scenarios))
    results = []
    for a, b in combinations(sorted(comparison_set), 2):
        combined = []
        for scenario in scenarios:
            pairs = []
            for metric_id in ids:
                left, right = cells.get((a, scenario, metric_id)), cells.get((b, scenario, metric_id))
                pair = None
                if left and right and left.value is not None and right.value is not None:
                    direction = metrics[metric_id].direction
                    pair = oriented(left.value, direction), oriented(right.value, direction)
                pairs.append(pair)
            status, reason = _relation(pairs)
            combined.extend(pairs)
            results.append(
                ParetoComparison(
                    option_a=a, option_b=b, scenario_id=scenario, status=status, metric_ids=ids, reason=reason
                )
            )
        status, reason = _relation(combined)
        results.append(ParetoComparison(option_a=a, option_b=b, status=status, metric_ids=ids, reason=reason))
    return tuple(results)


def compare_regret(
    decision_input: DecisionInput,
    policy: DecisionPolicy,
    comparison_set: tuple[str, ...],
    excluded: dict[str, tuple[str, ...]],
    feasibility: tuple[OptionFeasibility, ...],
) -> RegretSummary:
    metric_id = policy.regret_metric_id
    common = {
        "conditional_comparison_set": comparison_set,
        "excluded_options": tuple(sorted(excluded)),
        "exclusion_reasons": excluded,
    }
    if metric_id is None:
        return RegretSummary(status="not_requested", **common)
    metric = next(m for m in decision_input.metrics if m.metric_id == metric_id)
    limitations = (
        "only_explicit_all_scenario_gate_pass_options_compared_not_global_optimum",
        "scenario_set_is_declared_not_exhaustive_and_has_no_probabilities",
        "interval_endpoints_are_outer_bounds_not_asserted_jointly_attainable",
    )
    if len(comparison_set) == 1:
        limitations = (*limitations, "only_one_comparable_option_no_between_option_choice_established")
    if not comparison_set:
        return RegretSummary(
            status="unsupported", metric_id=metric_id, unit=metric.unit, limitations=limitations, **common
        )
    cells = cell_index(decision_input)
    scenarios = tuple(sorted(s.scenario_id for s in decision_input.scenarios))
    if any(
        (cell := cells.get((option, scenario, metric_id))) is None or cell.value is None
        for option in comparison_set
        for scenario in scenarios
    ):
        return RegretSummary(
            status="unsupported",
            metric_id=metric_id,
            unit=metric.unit,
            limitations=(*limitations, "missing_competitor_outcome_blocks_entire_regret_comparison"),
            **common,
        )
    cases = []
    worst = {}
    point_only = True
    for option in comparison_set:
        option_cases = []
        for scenario in scenarios:
            own = cells[(option, scenario, metric_id)].value
            assert own is not None
            low, high = oriented(own, metric.direction)
            point_only = point_only and low == high
            # A strategy compared with itself has exactly zero regret, even if
            # its outcome interval is wide. Competitors use a rectangular envelope.
            others = [
                oriented(cells[(competitor, scenario, metric_id)].value, metric.direction)
                for competitor in comparison_set
                if competitor != option
            ]
            value = NumericInterval(
                lower=max((ZERO, *(lo - high for lo, _ in others))),
                upper=max((ZERO, *(hi - low for _, hi in others))),
            )
            option_cases.append(value)
            cases.append(RegretCase(option_id=option, scenario_id=scenario, value=value))
        worst[option] = NumericInterval(
            lower=max(value.lower for value in option_cases), upper=max(value.upper for value in option_cases)
        )
    best_bound = min(value.upper for value in worst.values())
    partial = any(f.status in {"uncertain", "unknown"} for f in feasibility)
    return RegretSummary(
        status="partial" if partial else "complete",
        metric_id=metric_id,
        unit=metric.unit,
        interpretation="exact_on_declared_scenarios" if point_only else "conservative_outer_bounds",
        cases=tuple(cases),
        worst_case_bounds=worst,
        least_upper_bound_options=tuple(
            option for option in comparison_set if worst[option].upper == best_bound
        ),
        limitations=limitations,
        **common,
    )
