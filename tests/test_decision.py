"""Finite appraisal arithmetic and the boundaries that prevent false advice."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import localcontext
from pathlib import Path

import pytest
from bioradar.research.decision.evidence import evidence_basis_hash, validate_evidence
from bioradar.research.decision.hashing import decision_input_hash, decision_policy_hash
from bioradar.research.decision.models import (
    DecisionBrief,
    DecisionEvidenceLink,
    DecisionInput,
    DecisionOption,
    DecisionPolicy,
    DecisionScenario,
    DeclaredStatement,
    HardConstraint,
    InputBasis,
    MetricSpec,
    NumericInterval,
    OutcomeCell,
    ReviewPlan,
)
from bioradar.research.decision.runner import evaluate_decision
from bioradar.research.mining.models import (
    EngineRecord,
    NormalizedSnapshot,
    ProviderRef,
    RegionalIndicator,
    SectionProvenance,
    canonical_hash,
)
from bioradar.research.mining.prepare import snapshot_hash
from pydantic import ValidationError

pytestmark = pytest.mark.ENGINE
AS_OF = datetime(2026, 10, 13, tzinfo=UTC)


def interval(lower: str | int, upper: str | int | None = None) -> NumericInterval:
    return NumericInterval(lower=lower, upper=lower if upper is None else upper)


def assumption() -> InputBasis:
    return InputBasis(
        kind="declared_assumption",
        claimed_author="Synthetic fixture analyst",
        authored_at=datetime(2026, 10, 1, tzinfo=UTC),
        rationale=(
            "Synthetic arithmetic demonstration; no biomedical, policy or investment effect established."
        ),
    )


def full_decision_fixture() -> tuple[DecisionInput, DecisionPolicy, tuple[NormalizedSnapshot, ...]]:
    ref = ProviderRef(
        provider="fixture",
        object_type="source_record",
        object_id="K-capacity",
        adopted_version="v1",
        version_source="provider",
        content_hash=canonical_hash({"synthetic_capacity": 80}),
        authorization_ref="development:development:sources",
    )
    facet = SectionProvenance(
        schema_version="fixture.capacity.v1", provider_ref=ref, available_at=datetime(2026, 10, 5, tzinfo=UTC)
    )
    source = EngineRecord(
        record_ref=ref,
        document_hash=ref.content_hash,
        source_instance="fixture",
        source_id="fixture-source",
        observed_at=datetime(2026, 10, 10, tzinfo=UTC),
        available_at=datetime(2026, 10, 5, tzinfo=UTC),
        ingestion_mode="fixture",
        text_status="inline_authorized",
        normalization_version="fixture.text.v1",
        normalized_text=(
            "Synthetic fixture: K has 80 available assay slots/day in R on 2026-10-01. Not clinical efficacy."
        ),
        regional_indicators=(
            RegionalIndicator(
                indicator="available_assay_slots",
                region="R",
                period="2026-10-01",
                unit="slot/day",
                value=80,
                population_scope="authorized_K_service",
                jurisdiction="test-only",
                provenance=facet,
            ),
        ),
    )
    snapshot = NormalizedSnapshot(
        snapshot_id="decision-fixture-source-v1",
        is_demo=True,
        source_instance="fixture",
        observed_at=source.observed_at,
        coverage_mode="manual_package",
        records=(source,),
        metadata_gaps=("synthetic_resource_inventory_not_real_capacity",),
    )
    source_hash = snapshot_hash(snapshot)
    observed = InputBasis(
        kind="observed",
        claimed_author="Synthetic source curator",
        authored_at=source.observed_at,
        rationale=(
            "Exact adopted synthetic capacity value; an engineered fixture, not a professional finding."
        ),
        evidence=(
            DecisionEvidenceLink(
                snapshot_hash=source_hash, record_ref=ref, field_path="/regional_indicators/0/value"
            ),
        ),
    )
    basis = assumption()

    def statement(text: str) -> DeclaredStatement:
        return DeclaredStatement(text=text, basis=basis)

    brief = DecisionBrief(
        title="技术K服务配置：合成决策研究夹具",
        audience="provincial",
        decision_question="在声明预算和最低服务要求下，比较维持现状、试点、合作、扩大与待补证方案。",
        mandate="仅演示同口径方案比较；不具有真实预算、临床、产业、监管或采购决定效力。",
        as_of=AS_OF,
        decision_deadline=datetime(2026, 12, 1, tzinfo=UTC),
        horizon_start=date(2027, 1, 1),
        horizon_end=date(2027, 12, 31),
        baseline_option_id="baseline",
        scope_description="技术K、地域R、authorized_K_service；所有未来结果为明确合成假设。",
        why_now=statement("演示一项有截止时间的配置问题；不表示实际新技术突破。"),
        review_plan=ReviewPlan(
            review_at=datetime(2026, 11, 15, tzinfo=UTC),
            basis=basis,
            trigger_indicators=(statement("核实未来服务量、预算和外部合作承诺。"),),
            change_conditions=(statement("预算或最低服务约束得到不同证据时重新比较；不执行自动投资。"),),
        ),
    )
    common = {
        "population": "authorized_K_service",
        "geography": "R",
        "jurisdiction": "test-only",
        "period_start": date(2027, 1, 1),
        "period_end": date(2027, 12, 31),
        "comparability_basis": (
            "All scenarios/options use the identical declared definition, population and period."
        ),
    }
    metrics = (
        MetricSpec(
            metric_id="cost",
            label="合成年度总费用",
            direction="minimize",
            unit="synthetic_budget_unit",
            definition="declared_total_annual_cost",
            price_base="synthetic_constant_2026",
            **common,
        ),
        MetricSpec(
            metric_id="service",
            label="合成服务量",
            direction="maximize",
            unit="synthetic_service_unit",
            definition="declared_annual_service_volume",
            **common,
        ),
        MetricSpec(
            metric_id="current_slots",
            label="合成当前容量观察",
            direction="maximize",
            unit="slot/day",
            definition="available_assay_slots",
            **{
                **common,
                "period_start": date(2026, 10, 1),
                "period_end": date(2026, 10, 1),
            },
        ),
    )
    options = tuple(
        DecisionOption(
            option_id=key,
            label=label,
            description="Declared independent option totals; no addition of project benefits.",
            implementation_owner=statement("合成责任主体；实施权限尚须另行核实。"),
            implementation_steps=(statement("先核实预算、容量与专业适用性，再由有权主体决定。"),),
            beneficiaries=(statement("目标服务群体为假设受益者，真实效果未知。"),),
            adversely_affected=(statement("其他用途可能承担机会成本，分配效果尚未核实。"),),
            irreversible_commitments=(statement("扩大投入可能形成沉没成本；数值未作真实估计。"),),
        )
        for key, label in (
            ("baseline", "维持现状"),
            ("pilot", "有限试点"),
            ("partner", "外部合作"),
            ("expand", "扩大配置"),
            ("unknown", "待补证方案"),
        )
    )
    scenarios = tuple(
        DecisionScenario(
            scenario_id=key,
            label=label,
            description="Synthetic future; no assigned probability.",
            basis=basis,
        )
        for key, label in (("normal", "常态假设"), ("stress", "供需压力假设"))
    )
    costs = {
        "baseline": (25, 30),
        "pilot": (65, 80),
        "partner": (45, 60),
        "expand": (95, 120),
        "unknown": (15, 20),
    }
    service = {
        ("baseline", "normal"): (20, 30),
        ("baseline", "stress"): (10, 15),
        ("pilot", "normal"): (50, 65),
        ("pilot", "stress"): (30, 45),
        ("partner", "normal"): (35, 55),
        ("partner", "stress"): (25, 40),
        ("expand", "normal"): (70, 90),
        ("expand", "stress"): (45, 70),
    }
    outcomes = []
    for option in options:
        for scenario in scenarios:
            outcomes.append(
                OutcomeCell(
                    option_id=option.option_id,
                    scenario_id=scenario.scenario_id,
                    metric_id="cost",
                    value=interval(*costs[option.option_id]),
                    basis=basis,
                )
            )
            bounds = service.get((option.option_id, scenario.scenario_id))
            outcomes.append(
                OutcomeCell(
                    option_id=option.option_id,
                    scenario_id=scenario.scenario_id,
                    metric_id="service",
                    value=interval(*bounds) if bounds else None,
                    unknown_reason="Synthetic option has no defensible effect estimate."
                    if bounds is None
                    else None,
                    basis=basis,
                )
            )
            outcomes.append(
                OutcomeCell(
                    option_id=option.option_id,
                    scenario_id=scenario.scenario_id,
                    metric_id="current_slots",
                    value=interval(80),
                    basis=observed,
                )
            )
    decision_input = DecisionInput(
        input_id="decision-fixture-v1",
        is_demo=True,
        brief=brief,
        metrics=metrics,
        options=options,
        scenarios=scenarios,
        outcomes=tuple(outcomes),
        evidence_snapshot_hashes=(source_hash,),
        constraints=(
            HardConstraint(
                constraint_id="budget",
                label="声明预算上限",
                metric_id="cost",
                operator="le",
                threshold=interval(100),
                basis=basis,
            ),
            HardConstraint(
                constraint_id="minimum_service",
                label="声明最低服务",
                metric_id="service",
                operator="ge",
                threshold=interval(20),
                basis=basis,
            ),
        ),
    )
    policy = DecisionPolicy(pareto_metric_ids=("cost", "service"), regret_metric_id="service")
    return decision_input, policy, (snapshot,)


def commissioned_fixture(
    audience: str,
) -> tuple[DecisionInput, DecisionPolicy, tuple[NormalizedSnapshot, ...]]:
    """Author explicit different briefs; the evaluator never branches by audience."""
    base, base_policy, snapshots = full_decision_fixture()
    payload = base.model_dump(mode="json")
    payload["input_id"] = f"decision-{audience}-synthetic-v1"
    payload["brief"]["audience"] = audience
    briefs = {
        "central": (
            "中央委托合成例：未满足服务群体的有限试点",
            "在声明职责和预算内，比较地域R服务支持选项对合成未满足群体的覆盖。",
            "只研究中央层面支持工具；审批、支付及实施授权不由本例决定。",
        ),
        "provincial": (
            "省级委托合成例：最低服务要求下的年度费用",
            "在地域R最低服务量25与预算100的声明条件下，比较自建和共建的年度费用。",
            "只研究假设省级可控制资源配置；地方实际资金和能力仍须授权核实。",
        ),
        "university": (
            "学校委托合成例：人员门槛下的公共平台",
            "在校级人员需求不超过35人月的假设条件下，比较自建与合作服务的可行性。",
            "以学校公共平台使命为研究范围；院系、附属医院、伦理与产权权利不从数量推定。",
        ),
        "enterprise": (
            "企业委托合成例：自研与合作的净现金情景",
            "在声明研发预算和最低交付要求内，比较自主验证与合作许可的合成净现金区间。",
            "只比较企业可控制的行动及署名现金估计；不由注册阶段推临床或商业成功率。",
        ),
    }
    title, question, mandate = briefs[audience]
    payload["brief"].update(title=title, decision_question=question, mandate=mandate)
    payload["brief"]["scope_description"] = (
        "同一合成地域R与当前容量80slots事实；未来需求、费用、人员、群体和现金效果均是独立署名假设。"
    )
    if audience == "central":
        payload["metrics"][1].update(
            label="合成未满足群体服务量",
            definition="declared_underserved_group_service_volume",
            population="synthetic_underserved_group",
            unit="synthetic_underserved_service_unit",
        )
        policy = base_policy
    elif audience == "provincial":
        payload["constraints"][1]["threshold"] = interval(25).model_dump(mode="json")
        payload["constraints"][1]["label"] = "声明省级最低服务25"
        policy = base_policy.model_copy(update={"regret_metric_id": "cost"})
    elif audience == "university":
        staff = {
            **payload["metrics"][1],
            "metric_id": "staff_load",
            "label": "合成人员需求",
            "direction": "minimize",
            "unit": "synthetic_person_month",
            "definition": "declared_staff_required_for_complete_option",
            "population": "synthetic_university_staff",
        }
        payload["metrics"].append(staff)
        demand = {"baseline": (15, 20), "pilot": (30, 40), "partner": (20, 30), "expand": (50, 70)}
        for option in base.options:
            for scene in base.scenarios:
                value = demand.get(option.option_id)
                cell = OutcomeCell(
                    option_id=option.option_id,
                    scenario_id=scene.scenario_id,
                    metric_id="staff_load",
                    value=interval(*value) if value else None,
                    unknown_reason="Future staff need unknown" if value is None else None,
                    basis=assumption(),
                )
                payload["outcomes"].append(cell.model_dump(mode="json"))
        payload["constraints"].append(
            HardConstraint(
                constraint_id="staff_limit",
                label="未来可承担人员需求35人月（假设）",
                metric_id="staff_load",
                operator="le",
                threshold=interval(35),
                basis=assumption(),
            ).model_dump(mode="json")
        )
        policy = base_policy.model_copy(update={"pareto_metric_ids": ("service", "staff_load")})
    else:
        cash = {
            **payload["metrics"][0],
            "metric_id": "net_cash",
            "label": "合成年度净现金",
            "direction": "maximize",
            "definition": "declared_enterprise_annual_net_cash",
            "population": "synthetic_enterprise",
            "unit": "synthetic_cash_unit",
        }
        payload["metrics"].append(cash)
        forecasts = {"baseline": (-15, -5), "pilot": (-20, 20), "partner": (5, 30), "expand": (-60, 90)}
        for option in base.options:
            for scene in base.scenarios:
                value = forecasts.get(option.option_id)
                payload["outcomes"].append(
                    OutcomeCell(
                        option_id=option.option_id,
                        scenario_id=scene.scenario_id,
                        metric_id="net_cash",
                        value=interval(*value) if value else None,
                        unknown_reason="Enterprise cash estimate unknown" if value is None else None,
                        basis=assumption(),
                    ).model_dump(mode="json")
                )
        policy = base_policy.model_copy(
            update={"pareto_metric_ids": ("cost", "net_cash"), "regret_metric_id": "net_cash"}
        )
    return DecisionInput.model_validate(payload), policy, snapshots


def replace_cell(
    decision_input: DecisionInput, option: str, scene: str, metric: str, value: NumericInterval | None
) -> DecisionInput:
    payload = decision_input.model_dump(mode="json")
    for cell in payload["outcomes"]:
        if (cell["option_id"], cell["scenario_id"], cell["metric_id"]) == (option, scene, metric):
            cell["value"] = value.model_dump(mode="json") if value else None
            cell["unknown_reason"] = "missing" if value is None else None
    return DecisionInput.model_validate(payload)


def minimal_input(
    values: dict[str, dict[str, NumericInterval | None]], direction: str = "maximize"
) -> DecisionInput:
    base, _, _ = full_decision_fixture()
    basis = assumption()
    metric = base.metrics[1].model_copy(update={"metric_id": "goal", "direction": direction})
    scenes = sorted({scene for outcomes in values.values() for scene in outcomes})
    return DecisionInput(
        input_id="minimal",
        is_demo=True,
        brief=base.brief.model_copy(update={"baseline_option_id": next(iter(values))}),
        options=tuple(
            DecisionOption(option_id=key, label=key, description="Synthetic option") for key in values
        ),
        metrics=(metric,),
        scenarios=tuple(
            DecisionScenario(scenario_id=key, label=key, description="Synthetic", basis=basis)
            for key in scenes
        ),
        outcomes=tuple(
            OutcomeCell(
                option_id=option,
                scenario_id=scene,
                metric_id="goal",
                value=value,
                unknown_reason="missing" if value is None else None,
                basis=basis,
            )
            for option, outcomes in values.items()
            for scene, value in outcomes.items()
        ),
    )


def test_fixture_runs_real_arithmetic_and_retains_infeasible_baseline():
    decision_input, policy, snapshots = full_decision_fixture()
    result = evaluate_decision(decision_input, policy, snapshots)
    assert {f.option_id: f.status for f in result.feasibility} == {
        "baseline": "fail",
        "expand": "uncertain",
        "partner": "pass",
        "pilot": "pass",
        "unknown": "unknown",
    }
    assert result.conditional_comparison_set == ("partner", "pilot")
    assert "baseline" in result.excluded_from_comparison
    assert len(result.baseline_differences) == 30
    assert len(result.regret.cases) == 4
    assert result.regret.worst_case_bounds["pilot"] == interval(0, 10)
    assert result.regret.worst_case_bounds["partner"] == interval(0, 30)
    assert result.regret.least_upper_bound_options == ("pilot",)
    assert result.regret.status == "partial" and result.execution_status == "partial"
    assert result.investigation_questions[0].category == "hard_gate"
    assert result.is_demo


def test_four_audiences_preserve_facts_and_arithmetic_without_hidden_weights():
    decision_input, policy, snapshots = full_decision_fixture()
    baseline = evaluate_decision(decision_input, policy, snapshots)
    for audience in ("central", "provincial", "university", "enterprise"):
        changed = decision_input.model_copy(
            update={"brief": decision_input.brief.model_copy(update={"audience": audience})}
        )
        result = evaluate_decision(changed, policy, snapshots)
        assert result.evidence_basis_hash == baseline.evidence_basis_hash
        assert result.feasibility == baseline.feasibility
        assert result.pareto_comparisons == baseline.pareto_comparisons
        assert result.regret == baseline.regret


def test_maximize_and_minimize_exact_scenario_regret():
    policy = DecisionPolicy(pareto_metric_ids=("goal",), regret_metric_id="goal")
    decision_input = minimal_input(
        {"a": {"s1": interval(100), "s2": interval(20)}, "b": {"s1": interval(70), "s2": interval(60)}}
    )
    result = evaluate_decision(decision_input, policy)
    assert result.regret.worst_case_bounds == {"a": interval(40), "b": interval(30)}
    assert result.regret.least_upper_bound_options == ("b",)
    assert result.regret.interpretation == "exact_on_declared_scenarios"
    minimization = DecisionInput.model_validate(
        {
            **decision_input.model_dump(mode="json"),
            "metrics": [{**decision_input.metrics[0].model_dump(mode="json"), "direction": "minimize"}],
        }
    )
    assert evaluate_decision(minimization, policy).regret.worst_case_bounds == {
        "a": interval(30),
        "b": interval(40),
    }


def test_self_interval_comparison_is_zero_not_its_own_width():
    decision_input = minimal_input({"a": {"s": interval(0, 100)}})
    result = evaluate_decision(
        decision_input, DecisionPolicy(pareto_metric_ids=("goal",), regret_metric_id="goal")
    )
    assert result.regret.cases[0].value == interval(0)
    assert result.baseline_differences[0].difference == interval(0)
    assert "only_one_comparable_option_no_between_option_choice_established" in result.regret.limitations


def test_interval_regret_envelope_excludes_self_and_claims_no_joint_realization():
    decision_input = minimal_input({"a": {"s": interval(10, 100)}, "b": {"s": interval(20, 30)}})
    result = evaluate_decision(
        decision_input, DecisionPolicy(pareto_metric_ids=("goal",), regret_metric_id="goal")
    )
    assert result.regret.worst_case_bounds["a"] == interval(0, 20)
    assert result.regret.worst_case_bounds["b"] == interval(0, 80)
    assert result.regret.interpretation == "conservative_outer_bounds"


def test_missing_competitor_is_not_dropped_or_imputed():
    decision_input = minimal_input({"cheap": {"s": interval(0)}, "unknown": {"s": None}})
    result = evaluate_decision(
        decision_input, DecisionPolicy(pareto_metric_ids=("goal",), regret_metric_id="goal")
    )
    assert result.regret.status == "unsupported"
    assert result.regret.conditional_comparison_set == ("cheap", "unknown")
    assert result.regret.least_upper_bound_options == ()
    assert result.pareto_comparisons[0].status == "unknown"


def test_interval_pareto_guarantees_not_midpoints_and_has_no_weighted_tradeoff():
    decision_input = minimal_input({"a": {"s": interval(5, 10)}, "b": {"s": interval(7, 8)}})
    policy = DecisionPolicy(pareto_metric_ids=("goal",))
    result = evaluate_decision(decision_input, policy)
    assert result.pareto_comparisons[0].status == "incomparable"
    assert any(q.category == "overlapping_comparison" for q in result.investigation_questions)
    guaranteed = replace_cell(decision_input, "a", "s", "goal", interval(9, 10))
    assert evaluate_decision(guaranteed, policy).pareto_comparisons[0].status == "a_dominates"


def test_uncertain_threshold_is_not_pass_and_missing_threshold_is_unknown():
    decision_input = minimal_input({"a": {"s": interval(5, 7)}})
    constraint = HardConstraint(
        constraint_id="limit",
        label="limit",
        metric_id="goal",
        operator="le",
        threshold=interval(6, 8),
        basis=assumption(),
    )
    policy = DecisionPolicy(pareto_metric_ids=("goal",), regret_metric_id="goal")
    result = evaluate_decision(decision_input.model_copy(update={"constraints": (constraint,)}), policy)
    assert result.feasibility[0].status == "uncertain"
    assert result.feasibility[0].checks[0].shortfall == interval(0, 1)
    assert not result.conditional_comparison_set
    missing = constraint.model_copy(update={"threshold": None, "unknown_reason": "budget unknown"})
    assert (
        evaluate_decision(decision_input.model_copy(update={"constraints": (missing,)}), policy)
        .feasibility[0]
        .status
        == "unknown"
    )


def test_safe_extreme_decimal_subtraction_and_caller_context_independence():
    decision_input = minimal_input(
        {"a": {"s": interval("-99999999999999.12345678")}, "b": {"s": interval("99999999999999.12345678")}}
    )
    policy = DecisionPolicy(pareto_metric_ids=("goal",), regret_metric_id="goal")
    expected = evaluate_decision(decision_input, policy)
    assert expected.regret.worst_case_bounds["a"] == interval("199999999999998.24691356")
    with localcontext() as context:
        context.prec = 6
        assert evaluate_decision(decision_input, policy) == expected
    # The declared magnitude limit is deliberate; use legal negative bounds.
    with pytest.raises(ValidationError):
        minimal_input({"a": {"s": interval("-100000000000000.12345678")}})


@pytest.mark.parametrize("value", [1.5, True, "NaN", "Infinity", "1e1000", "0.123456789"])
def test_invalid_or_already_rounded_bounds_are_rejected(value):
    with pytest.raises(ValidationError):
        NumericInterval(lower=value, upper=value)


def test_source_ref_hash_quote_numeric_and_scope_are_strict():
    decision_input, policy, snapshots = full_decision_fixture()
    validate_evidence(decision_input, snapshots)
    value = replace_cell(decision_input, "pilot", "normal", "current_slots", interval(999))
    with pytest.raises(ValueError, match="differs_from_adopted"):
        evaluate_decision(value, policy, snapshots)
    payload = decision_input.model_dump(mode="json")
    payload["metrics"][2]["unit"] = "different-unit"
    with pytest.raises(ValueError, match="scope_or_unit"):
        validate_evidence(DecisionInput.model_validate(payload), snapshots)
    payload = decision_input.model_dump(mode="json")
    payload["outcomes"][2]["basis"]["evidence"][0]["record_ref"]["content_hash"] = "fake"
    with pytest.raises(ValueError, match="exact_adopted_record"):
        validate_evidence(DecisionInput.model_validate(payload), snapshots)


def test_observed_numbers_require_typed_source_not_arbitrary_quote():
    decision_input, _, snapshots = full_decision_fixture()
    payload = decision_input.model_dump(mode="json")
    link = payload["outcomes"][2]["basis"]["evidence"][0]
    link["field_path"] = None
    link["quote"] = "K has 80"
    with pytest.raises(ValueError, match="requires_bound_typed"):
        validate_evidence(DecisionInput.model_validate(payload), snapshots)
    payload["outcomes"][2]["basis"]["kind"] = "declared_assumption"
    validate_evidence(DecisionInput.model_validate(payload), snapshots)
    link["quote"] = "not present"
    with pytest.raises(ValueError, match="quote_not_literal"):
        validate_evidence(DecisionInput.model_validate(payload), snapshots)


def test_future_effect_cannot_be_observed_even_with_a_citation():
    decision_input, _, _ = full_decision_fixture()
    payload = decision_input.model_dump(mode="json")
    payload["outcomes"][0]["basis"] = payload["outcomes"][2]["basis"]
    with pytest.raises(ValidationError, match="future outcome"):
        DecisionInput.model_validate(payload)


def test_evidence_must_be_locally_observed_not_backdated_by_claimed_author():
    decision_input, _, snapshots = full_decision_fixture()
    payload = decision_input.model_dump(mode="json")
    payload["brief"]["as_of"] = "2026-10-06T00:00:00Z"
    with pytest.raises(ValueError, match="locally_observed"):
        validate_evidence(DecisionInput.model_validate(payload), snapshots)
    with pytest.raises(ValidationError):
        DecisionPolicy(pareto_metric_ids=("cost",), time_mode="availability_replay")


def test_demo_real_mix_is_rejected_but_real_source_demo_is_permitted():
    decision_input, _, snapshots = full_decision_fixture()
    with pytest.raises(ValueError, match="demo_and_source"):
        validate_evidence(decision_input.model_copy(update={"is_demo": False}), snapshots)
    real_record = snapshots[0].records[0].model_copy(update={"ingestion_mode": "sample_export"})
    real = snapshots[0].model_copy(update={"is_demo": False, "records": (real_record,)})
    payload = decision_input.model_dump(mode="json")
    old_hash, new_hash = snapshot_hash(snapshots[0]), snapshot_hash(real)
    changed = DecisionInput.model_validate_json(json.dumps(payload).replace(old_hash, new_hash))
    validate_evidence(changed, (real,))
    with pytest.raises(ValueError, match="demo_and_source"):
        validate_evidence(
            changed.model_copy(update={"evidence_snapshot_hashes": (old_hash, new_hash)}),
            (snapshots[0], real),
        )


def test_order_input_id_decimal_format_and_author_declarations_hash_correctly():
    decision_input, policy, snapshots = full_decision_fixture()
    shuffled = decision_input.model_copy(
        update={
            "input_id": "different-import-id",
            "options": tuple(reversed(decision_input.options)),
            "metrics": tuple(reversed(decision_input.metrics)),
            "scenarios": tuple(reversed(decision_input.scenarios)),
            "outcomes": tuple(reversed(decision_input.outcomes)),
            "constraints": tuple(reversed(decision_input.constraints)),
        }
    )
    assert decision_input_hash(decision_input) == decision_input_hash(shuffled)
    assert evaluate_decision(decision_input, policy, snapshots) == evaluate_decision(
        shuffled, policy, snapshots
    )
    assert canonical_hash(interval("1.00")) == canonical_hash(interval(1))
    changed = replace_cell(decision_input, "pilot", "normal", "service", interval(52, 65))
    assert decision_input_hash(changed) != decision_input_hash(decision_input)
    assert evidence_basis_hash(changed, snapshots) == evidence_basis_hash(decision_input, snapshots)
    assert decision_policy_hash(policy) == decision_policy_hash(
        policy.model_copy(update={"pareto_metric_ids": ("service", "cost")})
    )


def test_unknown_governance_is_not_no_conflict_and_question_budget_is_explicit():
    decision_input, policy, snapshots = full_decision_fixture()
    result = evaluate_decision(decision_input, policy.model_copy(update={"max_questions": 1}), snapshots)
    assert result.questions_omitted > 0 and result.execution_status == "partial"
    assert result.investigation_questions[0].category == "hard_gate"
    assert any("interest_declaration_unknown" in gap for gap in result.data_gaps)


def test_duplicate_ids_cells_and_undeclared_targets_are_rejected():
    decision_input, policy, snapshots = full_decision_fixture()
    for field in ("metrics", "options", "scenarios", "outcomes", "constraints"):
        payload = decision_input.model_dump(mode="json")
        payload[field].append(payload[field][0])
        with pytest.raises(ValidationError, match="duplicate"):
            DecisionInput.model_validate(payload)
    with pytest.raises(ValidationError):
        DecisionPolicy(pareto_metric_ids=("cost", "cost"))
    with pytest.raises(ValueError, match="undeclared_metric"):
        evaluate_decision(decision_input, policy.model_copy(update={"regret_metric_id": "absent"}), snapshots)


def test_resource_budget_is_unsupported_not_silently_shortlisted():
    decision_input, policy, snapshots = full_decision_fixture()
    result = evaluate_decision(decision_input, policy.model_copy(update={"max_options": 1}), snapshots)
    assert result.execution_status == "unsupported"
    assert result.regret.least_upper_bound_options == ()
    assert result.feasibility == ()


def test_fixture_files_reuse_actual_test_data():
    decision_input, policy, snapshots = full_decision_fixture()
    examples = Path(__file__).resolve().parents[1] / "contracts" / "examples"
    if not (examples / "decision-fixture.request.json").exists():
        pytest.fail("decision example has not been exported")
    request = json.loads((examples / "decision-fixture.request.json").read_text())
    assert DecisionInput.model_validate(request["input"]) == decision_input
    assert DecisionPolicy.model_validate(request["policy"]) == policy
    assert (
        NormalizedSnapshot.model_validate_json((examples / "decision-fixture.snapshot.json").read_text())
        == snapshots[0]
    )


def test_explicit_commissions_share_facts_but_have_different_declared_decisions():
    expected = {
        "central": {"partner": interval(0, 30), "pilot": interval(0, 10)},
        "provincial": {"partner": interval(0), "pilot": interval(5, 35)},
        "university": {"partner": interval(0)},
        "enterprise": {"partner": interval(0, 15), "pilot": interval(0, 50)},
    }
    facts = set()
    for audience in expected:
        decision_input, policy, snapshots = commissioned_fixture(audience)
        result = evaluate_decision(decision_input, policy, snapshots)
        facts.add(result.evidence_basis_hash)
        assert result.regret.worst_case_bounds == expected[audience]
        if audience == "university":
            gates = {item.option_id: item.status for item in result.feasibility}
            assert gates["pilot"] == "uncertain" and gates["expand"] == "fail"
        for outcome in decision_input.outcomes:
            if outcome.metric_id == "current_slots":
                assert outcome.value == interval(80) and outcome.basis.kind == "observed"
            else:
                assert outcome.basis.kind == "declared_assumption"
    assert len(facts) == 1


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "object_type",
        "object_id",
        "adopted_version",
        "version_source",
        "content_hash",
        "authorization_ref",
    ],
)
def test_all_seven_reference_fields_bind_the_actual_source(field):
    decision_input, _, snapshots = full_decision_fixture()
    payload = decision_input.model_dump(mode="json")
    ref = payload["outcomes"][2]["basis"]["evidence"][0]["record_ref"]
    ref[field] = "export_hash" if field == "version_source" else "changed"
    with pytest.raises(ValueError, match="exact_adopted_record"):
        validate_evidence(DecisionInput.model_validate(payload), snapshots)


@pytest.mark.parametrize(
    "path", ["/events/0/value", "/regional_indicators/-1/value", "/regional_indicators/99/value"]
)
def test_typed_paths_have_a_bounded_explicit_whitelist(path):
    decision_input, _, snapshots = full_decision_fixture()
    payload = decision_input.model_dump(mode="json")
    payload["outcomes"][2]["basis"]["evidence"][0]["field_path"] = path
    with pytest.raises(ValueError, match="field_path_not_supported"):
        validate_evidence(DecisionInput.model_validate(payload), snapshots)


def rebind_snapshot(
    decision_input: DecisionInput, prior: NormalizedSnapshot, new: NormalizedSnapshot
) -> DecisionInput:
    return DecisionInput.model_validate_json(
        decision_input.model_dump_json().replace(snapshot_hash(prior), snapshot_hash(new))
    )


def test_future_facet_or_available_version_cannot_borrow_old_source_time():
    decision_input, _, snapshots = full_decision_fixture()
    source = snapshots[0].records[0]
    future = datetime(2027, 1, 1, tzinfo=UTC)
    indicator = source.regional_indicators[0].model_copy(
        update={
            "provenance": source.regional_indicators[0].provenance.model_copy(
                update={"available_at": future}
            ),
        }
    )
    changed = snapshots[0].model_copy(
        update={"records": (source.model_copy(update={"regional_indicators": (indicator,)}),)}
    )
    with pytest.raises(ValueError, match="facet_available_after"):
        validate_evidence(rebind_snapshot(decision_input, snapshots[0], changed), (changed,))
    changed = snapshots[0].model_copy(
        update={"records": (source.model_copy(update={"available_at": future}),)}
    )
    with pytest.raises(ValueError, match="available_after"):
        validate_evidence(rebind_snapshot(decision_input, snapshots[0], changed), (changed,))


def test_unknown_availability_is_current_export_only_and_not_historical_proof():
    decision_input, policy, snapshots = full_decision_fixture()
    source = snapshots[0].records[0]
    changed = snapshots[0].model_copy(update={"records": (source.model_copy(update={"available_at": None}),)})
    result = evaluate_decision(rebind_snapshot(decision_input, snapshots[0], changed), policy, (changed,))
    assert "unknown_source_availability_uses_current_frozen_export_observation_only" in result.data_gaps
    assert "current_appraisal_only_not_historical_decision_replay" in result.limitations


def test_source_denominator_conflict_is_not_ignored():
    decision_input, _, snapshots = full_decision_fixture()
    source = snapshots[0].records[0]
    indicator = source.regional_indicators[0].model_copy(update={"denominator": 100.0})
    changed = snapshots[0].model_copy(
        update={"records": (source.model_copy(update={"regional_indicators": (indicator,)}),)}
    )
    bound_input = rebind_snapshot(decision_input, snapshots[0], changed)
    with pytest.raises(ValueError, match="scope_or_unit"):
        validate_evidence(bound_input, (changed,))
    payload = bound_input.model_dump(mode="json")
    payload["metrics"][2]["denominator"] = "100"
    validate_evidence(DecisionInput.model_validate(payload), (changed,))


def test_retracted_observation_and_conflicting_versions_are_not_valid_facts():
    decision_input, _, snapshots = full_decision_fixture()
    source = snapshots[0].records[0]
    changed = snapshots[0].model_copy(
        update={"records": (source.model_copy(update={"record_flags": ("retracted",)}),)}
    )
    with pytest.raises(ValueError, match="retracted_or_deleted"):
        validate_evidence(rebind_snapshot(decision_input, snapshots[0], changed), (changed,))
    conflict = source.model_copy(update={"source_family": "different-payload"})
    with pytest.raises(ValueError, match="conflicting payloads"):
        snapshot_hash(snapshots[0].model_copy(update={"records": (source, conflict)}))


def test_inactive_source_can_support_a_declared_hypothesis_but_remains_visible():
    decision_input, policy, snapshots = full_decision_fixture()
    source = snapshots[0].records[0].model_copy(update={"record_flags": ("deleted",)})
    changed = snapshots[0].model_copy(update={"records": (source,)})
    payload = rebind_snapshot(decision_input, snapshots[0], changed).model_dump(mode="json")
    for cell in payload["outcomes"]:
        if cell["metric_id"] == "current_slots":
            cell["basis"]["kind"] = "declared_assumption"
    result = evaluate_decision(DecisionInput.model_validate(payload), policy, (changed,))
    assert any(gap.startswith("declared_input_cites_inactive_source:") for gap in result.data_gaps)
    assert any(
        item.category == "source" and "撤回" in item.question for item in result.investigation_questions
    )


def test_missing_public_effect_does_not_make_lowest_cost_a_winner():
    decision_input, policy, snapshots = full_decision_fixture()
    for option in decision_input.options:
        for scene in decision_input.scenarios:
            decision_input = replace_cell(
                decision_input, option.option_id, scene.scenario_id, "service", None
            )
    decision_input = decision_input.model_copy(update={"constraints": (decision_input.constraints[0],)})
    result = evaluate_decision(decision_input, policy, snapshots)
    assert result.regret.status == "unsupported"
    assert not result.regret.least_upper_bound_options
    assert all(item.status == "unknown" for item in result.pareto_comparisons)


def test_time_extra_weights_probabilities_and_fake_receipts_are_rejected():
    decision_input, _, _ = full_decision_fixture()
    payload = decision_input.model_dump(mode="json")
    payload["brief"]["as_of"] = "2026-10-13T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        DecisionInput.model_validate(payload)
    for extra in ({"weights": {"cost": 0.5}}, {"accepted_at": AS_OF}, {"submitted_by": "Expert"}):
        with pytest.raises(ValidationError):
            DecisionInput.model_validate({**decision_input.model_dump(mode="json"), **extra})
    payload = decision_input.model_dump(mode="json")
    payload["scenarios"][0]["probability"] = 0.5
    with pytest.raises(ValidationError):
        DecisionInput.model_validate(payload)


def test_numeric_contract_advertises_strings_and_integers_not_float():
    schema = NumericInterval.model_json_schema()
    assert {item["type"] for item in schema["properties"]["lower"]["anyOf"]} == {"string", "integer"}


def test_regret_objective_unknown_order_is_reported_even_outside_pareto_metrics():
    decision_input, policy, snapshots = full_decision_fixture()
    policy = policy.model_copy(update={"pareto_metric_ids": ("cost",)})
    result = evaluate_decision(decision_input, policy, snapshots)
    assert any(
        item.category == "overlapping_comparison"
        and item.metric_id == "service"
        and item.reason == "regret_objective_pairwise_direction_unresolved_not_global_information_value"
        for item in result.investigation_questions
    )


def test_structural_diagnostic_paths_do_not_collide_on_colon_ids():
    from bioradar.research.decision.evidence import input_bases

    decision_input = minimal_input({"a:b": {"c": interval(1)}, "a": {"b:c": interval(2)}})
    paths = [path for path, _, _, _ in input_bases(decision_input)]
    assert len(paths) == len(set(paths))
