"""Pure current appraisal; no external calls, hidden utility or historical claim."""

from __future__ import annotations

import json
from decimal import Decimal, localcontext
from itertools import combinations

from bioradar.research.decision.algorithms import (
    baseline_differences,
    cell_index,
    check_constraints,
    compare_pareto,
    compare_regret,
    oriented,
)
from bioradar.research.decision.evidence import evidence_basis_hash, validate_evidence
from bioradar.research.decision.hashing import decision_input_hash, decision_policy_hash
from bioradar.research.decision.models import (
    DecisionInput,
    DecisionPolicy,
    DecisionResult,
    InvestigationQuestion,
    OptionFeasibility,
    ParetoComparison,
    RegretSummary,
)
from bioradar.research.mining.models import NormalizedSnapshot, canonical_hash

BASE_LIMITATIONS = (
    "current_appraisal_only_not_historical_decision_replay",
    "claimed_authorship_and_interest_declarations_are_not_verified_expert_approval",
    "calculation_is_conditional_on_declared_options_metrics_scopes_assumptions_and_constraints",
    "no_default_weights_no_probabilities_no_policy_causality_or_investment_return_inferred",
    "no_automatic_portfolio_addition_shared_cost_or_synergy_model",
    "differences_and_interval_comparisons_use_conservative_bounds_not_joint_attainability",
    "review_and_implementation_statements_are_plans_not_executed_tasks",
)


def _key(*parts: str) -> str:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _questions(
    decision_input: DecisionInput,
    policy: DecisionPolicy,
    feasibility: tuple[OptionFeasibility, ...],
    comparisons: tuple[ParetoComparison, ...],
    source_gaps: tuple[str, ...],
) -> tuple[tuple[InvestigationQuestion, ...], int, tuple[str, ...]]:
    questions: dict[str, InvestigationQuestion] = {}
    gaps = set(source_gaps)
    metrics = {m.metric_id: m for m in decision_input.metrics}
    options = {o.option_id: o.label for o in decision_input.options}
    scenes = {s.scenario_id: s.label for s in decision_input.scenarios}
    constraints = {c.constraint_id: c.label for c in decision_input.constraints}
    cells = cell_index(decision_input)

    def add(category: str, question: str, reason: str, **kwargs: object) -> None:
        value = {"category": category, "question": question, "reason": reason, **kwargs}
        identity = canonical_hash(value)
        questions[identity] = InvestigationQuestion(question_id=identity, **value)

    for option in feasibility:
        for check in option.checks:
            if check.status in {"unknown", "uncertain"}:
                add(
                    "hard_gate",
                    f"核实“{options[check.option_id]}”在“{scenes[check.scenario_id]}”下的“{constraints[check.constraint_id]}”。",
                    "blocks_all_scenario_feasibility"
                    if check.status == "unknown"
                    else "bounds_cross_hard_threshold",
                    option_ids=(check.option_id,),
                    scenario_id=check.scenario_id,
                    metric_id=check.metric_id,
                    constraint_id=check.constraint_id,
                )
                gaps.add(
                    _key("hard_gate", check.option_id, check.scenario_id, check.constraint_id, check.status)
                )
    required = set(policy.pareto_metric_ids) | {c.metric_id for c in decision_input.constraints}
    if policy.regret_metric_id:
        required.add(policy.regret_metric_id)
    for option in sorted(decision_input.options, key=lambda o: o.option_id):
        for scenario in sorted(decision_input.scenarios, key=lambda s: s.scenario_id):
            for metric_id in sorted(required):
                cell = cells.get((option.option_id, scenario.scenario_id, metric_id))
                if cell is None or cell.value is None:
                    gaps.add(_key("missing_outcome", option.option_id, scenario.scenario_id, metric_id))
                    add(
                        "missing_comparison",
                        f"取得“{option.label}”在“{scenario.label}”下的“{metrics[metric_id].label}”及同口径依据。",
                        "outcome_unknown_not_zero_or_midpoint",
                        option_ids=(option.option_id,),
                        scenario_id=scenario.scenario_id,
                        metric_id=metric_id,
                    )
    # Only genuine uncertain pairwise directions generate precision questions.
    # Exact tradeoffs require an explicit decision objective, not more citations.
    for comparison in comparisons:
        if comparison.scenario_id is None or comparison.status != "incomparable":
            continue
        scene = comparison.scenario_id
        uncertain = False
        for metric_id in comparison.metric_ids:
            a = cells[(comparison.option_a, scene, metric_id)].value
            b = cells[(comparison.option_b, scene, metric_id)].value
            assert a is not None and b is not None
            alo, ahi = oriented(a, metrics[metric_id].direction)
            blo, bhi = oriented(b, metrics[metric_id].direction)
            if alo - bhi < Decimal(0) < ahi - blo:
                uncertain = True
                add(
                    "overlapping_comparison",
                    f"核实“{scenes[scene]}”下“{options[comparison.option_a]}”与“{options[comparison.option_b]}”的"
                    f"“{metrics[metric_id].label}”区间。",
                    "pairwise_direction_can_reverse_within_bounds_not_probability_or_information_value",
                    option_ids=(comparison.option_a, comparison.option_b),
                    scenario_id=scene,
                    metric_id=metric_id,
                )
        if not uncertain:
            add(
                "governance",
                f"明确“{scenes[scene]}”下“{options[comparison.option_a]}”与“{options[comparison.option_b]}”的目标取舍。",
                "non_dominance_does_not_authorize_implicit_weights_or_a_preferred_policy",
                option_ids=(comparison.option_a, comparison.option_b),
                scenario_id=scene,
            )
    if policy.regret_metric_id and policy.regret_metric_id not in policy.pareto_metric_ids:
        metric_id = policy.regret_metric_id
        eligible = sorted(item.option_id for item in feasibility if item.status == "pass")
        for a, b in combinations(eligible, 2):
            for scene in sorted(scenes):
                left, right = cells.get((a, scene, metric_id)), cells.get((b, scene, metric_id))
                if left is None or right is None or left.value is None or right.value is None:
                    continue
                alo, ahi = oriented(left.value, metrics[metric_id].direction)
                blo, bhi = oriented(right.value, metrics[metric_id].direction)
                if alo - bhi < Decimal(0) < ahi - blo:
                    add(
                        "overlapping_comparison",
                        f"核实“{scenes[scene]}”下“{options[a]}”与“{options[b]}”的"
                        f"后悔目标“{metrics[metric_id].label}”区间。",
                        "regret_objective_pairwise_direction_unresolved_not_global_information_value",
                        option_ids=(a, b),
                        scenario_id=scene,
                        metric_id=metric_id,
                    )
    for role in ("commissioner", "funder", "author", "reviewer"):
        disclosure = getattr(decision_input.brief.interest_declarations, role)
        if disclosure.status == "unknown":
            gaps.add(_key("interest_declaration_unknown", role))
            label = {"commissioner": "委托方", "funder": "资助方", "author": "作者", "reviewer": "审阅者"}[
                role
            ]
            add("governance", f"取得{label}的利益声明并核实责任。", "missing_disclosure_is_not_no_conflict")
    if not decision_input.constraints:
        gaps.add("no_hard_constraints_declared")
        add(
            "governance",
            "明确实施权限、预算、人员和最低绩效门槛。",
            "empty_constraint_set_is_not_capacity_or_permission",
        )
    if decision_input.brief.why_now is None:
        gaps.add("why_now_not_declared")
    review = decision_input.brief.review_plan
    if (
        review is None
        or review.review_at is None
        or not review.trigger_indicators
        or not review.change_conditions
    ):
        gaps.add("review_time_triggers_or_change_conditions_unknown")
        add(
            "governance",
            "明确复查日期、触发指标和改变结论的条件。",
            "a_review_plan_is_not_an_executed_monitor",
        )
    for option in sorted(decision_input.options, key=lambda o: o.option_id):
        for field in (
            "implementation_owner",
            "implementation_steps",
            "beneficiaries",
            "adversely_affected",
            "irreversible_commitments",
        ):
            if not getattr(option, field):
                gaps.add(_key("option_statement_unknown", option.option_id, field))
        if not option.adversely_affected or not option.irreversible_commitments:
            add(
                "governance",
                f"核实“{option.label}”的受损群体与不可逆承诺。",
                "missing_distribution_or_commitment_statement_is_not_none",
                option_ids=(option.option_id,),
            )
    for gap in source_gaps:
        if "inactive_source" in gap:
            add(
                "source",
                "复核已撤回或删除的来源；为当前假设补充有效依据，或明确只作为反证保留。",
                gap,
            )
        elif "unknown" in gap or "not_verified" in gap:
            add("source", "核实所用材料的可见时间或专业解释。", gap)
    priority = {
        "hard_gate": 0,
        "missing_comparison": 1,
        "overlapping_comparison": 2,
        "source": 3,
        "governance": 4,
    }
    ordered = sorted(questions.values(), key=lambda q: (priority[q.category], q.question_id))
    return (
        tuple(ordered[: policy.max_questions]),
        max(0, len(ordered) - policy.max_questions),
        tuple(sorted(gaps)),
    )


def evaluate_decision(
    decision_input: DecisionInput,
    policy: DecisionPolicy,
    snapshots: tuple[NormalizedSnapshot, ...] = (),
) -> DecisionResult:
    """Compute declared alternatives; invalid source binding always raises."""
    source_gaps = validate_evidence(decision_input, snapshots)
    metric_ids = {m.metric_id for m in decision_input.metrics}
    if not set(policy.pareto_metric_ids) <= metric_ids or (
        policy.regret_metric_id is not None and policy.regret_metric_id not in metric_ids
    ):
        raise ValueError("decision_policy_references_undeclared_metric")
    common = {
        "input_hash": decision_input_hash(decision_input),
        "policy_hash": decision_policy_hash(policy),
        "evidence_basis_hash": evidence_basis_hash(decision_input, snapshots),
        "is_demo": decision_input.is_demo,
    }
    exceeded = [
        name
        for name, count, budget in (
            ("options", len(decision_input.options), policy.max_options),
            ("scenarios", len(decision_input.scenarios), policy.max_scenarios),
            ("metrics", len(decision_input.metrics), policy.max_metrics),
            ("constraints", len(decision_input.constraints), policy.max_constraints),
        )
        if count > budget
    ]
    if exceeded:
        result = DecisionResult(
            **common,
            execution_status="unsupported",
            regret=RegretSummary(status="unsupported"),
            data_gaps=tuple(f"policy_budget_exceeded:{name}" for name in exceeded),
            limitations=BASE_LIMITATIONS,
        )
    else:
        # Do not inherit a caller's Decimal rounding context for budget decisions.
        with localcontext() as context:
            context.prec = 64
            feasibility = check_constraints(decision_input)
            comparison_set = tuple(f.option_id for f in feasibility if f.status == "pass")
            excluded = {
                f.option_id: (f"all_scenario_feasibility_{f.status}", *f.reasons)
                for f in feasibility
                if f.status != "pass"
            }
            differences = baseline_differences(decision_input)
            pareto = compare_pareto(decision_input, policy, comparison_set)
            regret = compare_regret(decision_input, policy, comparison_set, excluded, feasibility)
            questions, omitted, gaps = _questions(decision_input, policy, feasibility, pareto, source_gaps)
        partial = (
            omitted > 0
            or any(f.status in {"uncertain", "unknown"} for f in feasibility)
            or any(d.status == "unknown" for d in differences)
            or any(p.status == "unknown" for p in pareto)
            or regret.status in {"unsupported", "partial"}
            or not comparison_set
        )
        result = DecisionResult(
            **common,
            execution_status="partial" if partial else "success",
            feasibility=feasibility,
            conditional_comparison_set=comparison_set,
            excluded_from_comparison=excluded,
            baseline_differences=differences,
            pareto_comparisons=pareto,
            regret=regret,
            investigation_questions=questions,
            questions_omitted=omitted,
            data_gaps=gaps,
            limitations=BASE_LIMITATIONS,
        )
    return result.model_copy(
        update={"result_hash": canonical_hash(result.model_dump(mode="json", exclude={"result_hash"}))}
    )
