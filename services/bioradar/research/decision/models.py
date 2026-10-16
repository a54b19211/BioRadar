"""Shared decision boundary. Declarations are not verified expert approval."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from bioradar.research.mining.models import FrozenModel, ProviderRef

DECISION_COMPUTE_VERSION = "bioradar-decision-compute-v0.1"
GateStatus = Literal["pass", "fail", "uncertain", "unknown"]


def aware(value: datetime | None) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError("decision timestamps require timezone")


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


class NumericInterval(FrozenModel):
    lower: Decimal = Field(max_digits=24, decimal_places=8)
    upper: Decimal = Field(max_digits=24, decimal_places=8)

    @field_validator("lower", "upper", mode="before", json_schema_input_type=str | int)
    @classmethod
    def reject_rounded_floats(cls, value: object) -> object:
        if isinstance(value, (float, bool)):
            raise ValueError("decimal bounds require strings or exact integers, not binary floats")
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> NumericInterval:
        if not self.lower.is_finite() or not self.upper.is_finite() or self.lower > self.upper:
            raise ValueError("interval requires ordered finite decimal values")
        return self

    @field_serializer("lower", "upper")
    def serialize_decimal(self, value: Decimal) -> str:
        return decimal_text(value)


class DecisionEvidenceLink(FrozenModel):
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_ref: ProviderRef
    evidence_ref: ProviderRef | None = None
    quote: str | None = Field(default=None, min_length=1, max_length=8000)
    field_path: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def has_support(self) -> DecisionEvidenceLink:
        if self.quote is None and self.field_path is None:
            raise ValueError("evidence requires an exact quote or supported typed field path")
        return self


class InputBasis(FrozenModel):
    kind: Literal["observed", "declared_assumption", "supplied_model_result"]
    claimed_author: str = Field(min_length=1, max_length=200)
    authored_at: datetime
    rationale: str = Field(min_length=1, max_length=4000)
    evidence: tuple[DecisionEvidenceLink, ...] = Field(default=(), max_length=16)
    model_description: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_basis(self) -> InputBasis:
        aware(self.authored_at)
        if self.kind == "observed" and not self.evidence:
            raise ValueError("observed basis requires bound source evidence")
        if self.kind == "supplied_model_result" and not self.model_description:
            raise ValueError("model result requires model description")
        return self


class DeclaredStatement(FrozenModel):
    text: str = Field(min_length=1, max_length=4000)
    basis: InputBasis


class InterestDisclosure(FrozenModel):
    status: Literal["unknown", "declared_none", "declared_interest"] = "unknown"
    statement: DeclaredStatement | None = None

    @model_validator(mode="after")
    def declared_statement(self) -> InterestDisclosure:
        if self.status != "unknown" and self.statement is None:
            raise ValueError("declared interests require an attributed statement")
        return self


class InterestDeclarations(FrozenModel):
    commissioner: InterestDisclosure = Field(default_factory=InterestDisclosure)
    funder: InterestDisclosure = Field(default_factory=InterestDisclosure)
    author: InterestDisclosure = Field(default_factory=InterestDisclosure)
    reviewer: InterestDisclosure = Field(default_factory=InterestDisclosure)


class ReviewPlan(FrozenModel):
    review_at: datetime | None = None
    trigger_indicators: tuple[DeclaredStatement, ...] = Field(default=(), max_length=16)
    change_conditions: tuple[DeclaredStatement, ...] = Field(default=(), max_length=16)
    basis: InputBasis

    @model_validator(mode="after")
    def validate_time(self) -> ReviewPlan:
        aware(self.review_at)
        return self


class DecisionBrief(FrozenModel):
    title: str = Field(min_length=1, max_length=240)
    audience: Literal["central", "provincial", "university", "enterprise"]
    decision_question: str = Field(min_length=1, max_length=4000)
    mandate: str = Field(min_length=1, max_length=4000)
    as_of: datetime
    decision_deadline: datetime
    horizon_start: date
    horizon_end: date
    baseline_option_id: str = Field(min_length=1, max_length=100)
    scope_description: str = Field(min_length=1, max_length=4000)
    why_now: DeclaredStatement | None = None
    interest_declarations: InterestDeclarations = Field(default_factory=InterestDeclarations)
    review_plan: ReviewPlan | None = None

    @model_validator(mode="after")
    def validate_times(self) -> DecisionBrief:
        aware(self.as_of)
        aware(self.decision_deadline)
        if self.decision_deadline < self.as_of or self.horizon_end < self.horizon_start:
            raise ValueError("decision deadline and horizon must be ordered")
        if self.review_plan and self.review_plan.review_at and self.review_plan.review_at < self.as_of:
            raise ValueError("review date cannot precede appraisal cutoff")
        return self


class MetricSpec(FrozenModel):
    metric_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    direction: Literal["maximize", "minimize"]
    unit: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1, max_length=2000)
    population: str = Field(min_length=1, max_length=1000)
    geography: str = Field(min_length=1, max_length=1000)
    jurisdiction: str = Field(min_length=1, max_length=1000)
    period_start: date
    period_end: date
    price_base: str | None = Field(default=None, min_length=1, max_length=200)
    denominator: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    comparability_basis: str = Field(min_length=1, max_length=2000)

    @field_validator("denominator", mode="before", json_schema_input_type=str | int | None)
    @classmethod
    def exact_denominator(cls, value: object) -> object:
        return NumericInterval.reject_rounded_floats(value)

    @field_serializer("denominator")
    def serialize_denominator(self, value: Decimal | None) -> str | None:
        return decimal_text(value) if value is not None else None

    @model_validator(mode="after")
    def ordered_period(self) -> MetricSpec:
        if self.period_end < self.period_start:
            raise ValueError("metric period must be ordered")
        return self


class DecisionOption(FrozenModel):
    option_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    implementation_owner: DeclaredStatement | None = None
    implementation_steps: tuple[DeclaredStatement, ...] = Field(default=(), max_length=16)
    beneficiaries: tuple[DeclaredStatement, ...] = Field(default=(), max_length=16)
    adversely_affected: tuple[DeclaredStatement, ...] = Field(default=(), max_length=16)
    irreversible_commitments: tuple[DeclaredStatement, ...] = Field(default=(), max_length=16)


class DecisionScenario(FrozenModel):
    scenario_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    basis: InputBasis


class OutcomeCell(FrozenModel):
    option_id: str
    scenario_id: str
    metric_id: str
    value: NumericInterval | None = None
    unknown_reason: str | None = Field(default=None, min_length=1, max_length=2000)
    basis: InputBasis

    @model_validator(mode="after")
    def explicit_unknown(self) -> OutcomeCell:
        if (self.value is None) != (self.unknown_reason is not None):
            raise ValueError("missing outcome requires a reason; known outcome cannot claim unknown")
        if self.value and max(abs(self.value.lower), abs(self.value.upper)) > Decimal("1e14"):
            raise ValueError("declared numeric magnitude exceeds bounded appraisal limits")
        return self


class HardConstraint(FrozenModel):
    constraint_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    metric_id: str
    operator: Literal["le", "ge"]
    threshold: NumericInterval | None = None
    unknown_reason: str | None = Field(default=None, min_length=1, max_length=2000)
    basis: InputBasis

    @model_validator(mode="after")
    def explicit_unknown(self) -> HardConstraint:
        if (self.threshold is None) != (self.unknown_reason is not None):
            raise ValueError("missing threshold requires reason; known threshold cannot claim unknown")
        if self.threshold and max(abs(self.threshold.lower), abs(self.threshold.upper)) > Decimal("1e14"):
            raise ValueError("declared numeric magnitude exceeds bounded appraisal limits")
        return self


class DecisionInput(FrozenModel):
    schema_version: Literal["bioradar.decision.input.v1"] = "bioradar.decision.input.v1"
    input_id: str = Field(min_length=1, max_length=200)
    is_demo: bool = False
    brief: DecisionBrief
    metrics: tuple[MetricSpec, ...] = Field(min_length=1, max_length=16)
    options: tuple[DecisionOption, ...] = Field(min_length=1, max_length=24)
    scenarios: tuple[DecisionScenario, ...] = Field(min_length=1, max_length=32)
    outcomes: tuple[OutcomeCell, ...] = Field(default=(), max_length=12288)
    constraints: tuple[HardConstraint, ...] = Field(default=(), max_length=32)
    evidence_snapshot_hashes: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_keys(self) -> DecisionInput:
        collections = (
            (tuple(m.metric_id for m in self.metrics), "metric"),
            (tuple(o.option_id for o in self.options), "option"),
            (tuple(s.scenario_id for s in self.scenarios), "scenario"),
            (tuple(c.constraint_id for c in self.constraints), "constraint"),
            (self.evidence_snapshot_hashes, "snapshot"),
            (tuple((c.option_id, c.scenario_id, c.metric_id) for c in self.outcomes), "cell"),
        )
        for keys, label in collections:
            if len(set(keys)) != len(keys):
                raise ValueError(f"duplicate {label} keys")
        metrics = {m.metric_id: m for m in self.metrics}
        options = {o.option_id for o in self.options}
        scenarios = {s.scenario_id for s in self.scenarios}
        if self.brief.baseline_option_id not in options:
            raise ValueError("baseline option must be retained in input")
        for cell in self.outcomes:
            if (
                cell.option_id not in options
                or cell.scenario_id not in scenarios
                or cell.metric_id not in metrics
            ):
                raise ValueError("outcome references undeclared option, scenario or metric")
            if cell.basis.kind == "observed" and metrics[cell.metric_id].period_end > self.brief.as_of.date():
                raise ValueError("future outcome cannot be an observed fact")
        if any(c.metric_id not in metrics for c in self.constraints):
            raise ValueError("constraint references undeclared metric")
        return self


class DecisionPolicy(FrozenModel):
    policy_version: Literal["bioradar.decision.policy.v1"] = "bioradar.decision.policy.v1"
    time_mode: Literal["current_appraisal"] = "current_appraisal"
    pareto_metric_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    regret_metric_id: str | None = None
    max_options: int = Field(default=24, ge=1, le=24)
    max_scenarios: int = Field(default=32, ge=1, le=32)
    max_metrics: int = Field(default=16, ge=1, le=16)
    max_constraints: int = Field(default=32, ge=0, le=32)
    max_questions: int = Field(default=48, ge=1, le=200)

    @model_validator(mode="after")
    def unique_metrics(self) -> DecisionPolicy:
        if len(set(self.pareto_metric_ids)) != len(self.pareto_metric_ids):
            raise ValueError("duplicate Pareto metric IDs")
        return self


class ConstraintCheck(FrozenModel):
    option_id: str
    scenario_id: str
    constraint_id: str
    metric_id: str
    status: GateStatus
    value: NumericInterval | None
    threshold: NumericInterval | None
    shortfall: NumericInterval | None = None
    reason: str


class OptionFeasibility(FrozenModel):
    option_id: str
    status: GateStatus
    checks: tuple[ConstraintCheck, ...] = ()
    reasons: tuple[str, ...] = ()


class BaselineDifference(FrozenModel):
    option_id: str
    baseline_option_id: str
    scenario_id: str
    metric_id: str
    difference: NumericInterval | None
    status: Literal["bounded", "exact", "unknown"]


class ParetoComparison(FrozenModel):
    option_a: str
    option_b: str
    scenario_id: str | None = None
    status: Literal["a_dominates", "b_dominates", "tied", "incomparable", "unknown"]
    metric_ids: tuple[str, ...]
    reason: str


class RegretCase(FrozenModel):
    option_id: str
    scenario_id: str
    value: NumericInterval


class RegretSummary(FrozenModel):
    status: Literal["not_requested", "unsupported", "partial", "complete"]
    metric_id: str | None = None
    unit: str | None = None
    interpretation: Literal["exact_on_declared_scenarios", "conservative_outer_bounds"] | None = None
    conditional_comparison_set: tuple[str, ...] = ()
    excluded_options: tuple[str, ...] = ()
    exclusion_reasons: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    cases: tuple[RegretCase, ...] = ()
    worst_case_bounds: dict[str, NumericInterval] = Field(default_factory=dict)
    least_upper_bound_options: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class InvestigationQuestion(FrozenModel):
    question_id: str
    category: Literal["hard_gate", "missing_comparison", "overlapping_comparison", "governance", "source"]
    option_ids: tuple[str, ...] = ()
    scenario_id: str | None = None
    metric_id: str | None = None
    constraint_id: str | None = None
    question: str
    reason: str


class DecisionResult(FrozenModel):
    schema_version: Literal["bioradar.decision.result.v1"] = "bioradar.decision.result.v1"
    compute_version: Literal["bioradar-decision-compute-v0.1"] = DECISION_COMPUTE_VERSION
    input_hash: str
    policy_hash: str
    evidence_basis_hash: str
    result_hash: str = ""
    is_demo: bool
    execution_status: Literal["success", "partial", "unsupported"]
    feasibility: tuple[OptionFeasibility, ...] = ()
    conditional_comparison_set: tuple[str, ...] = ()
    excluded_from_comparison: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    baseline_differences: tuple[BaselineDifference, ...] = ()
    pareto_comparisons: tuple[ParetoComparison, ...] = ()
    regret: RegretSummary
    investigation_questions: tuple[InvestigationQuestion, ...] = ()
    questions_omitted: int = Field(default=0, ge=0)
    data_gaps: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
