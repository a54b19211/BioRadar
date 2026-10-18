"""Small, explicit contracts for historical research; no new analysis run kind."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from bioradar.research.mining.models import FrozenModel, ProviderRef

HISTORY_COMPUTE_VERSION = "bioradar-history-baseline-v0.2"
HistoryMode = Literal["current", "operational_replay", "public_history_reconstruction"]
HistoryTarget = Literal["trial_stoppage", "regulatory_approval", "next_phase_disclosure"]


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("historical timestamps require timezone")
    return value


class HistoryTrainingRun(FrozenModel):
    cutoff: datetime
    run_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def timestamp(self):
        aware(self.cutoff)
        return self


class HistoryStudyCreate(FrozenModel):
    schema_version: Literal["bioradar.history.request.v1"] = "bioradar.history.request.v1"
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    source_id: str = Field(min_length=1, max_length=200)
    is_demo: bool = False
    mode: HistoryMode = "public_history_reconstruction"
    cutoff: datetime
    target: HistoryTarget
    horizon_months: Literal[12, 24] = 24
    training_cutoffs: tuple[datetime, ...] = Field(default=(), max_length=6)
    training_runs: tuple[HistoryTrainingRun, ...] = Field(default=(), max_length=6)
    run_id: str | None = Field(default=None, max_length=64)
    cohort_terms: tuple[str, ...] = Field(default=(), max_length=3)
    max_records: int = Field(default=5000, ge=1, le=20000)
    max_bytes: int = Field(default=8_000_000, ge=1024, le=32_000_000)
    max_seconds: int = Field(default=90, ge=5, le=120)
    review_budget: int = Field(default=10, ge=1, le=1000)
    followup_queries: int = Field(default=2, ge=0, le=2)
    minimum_training_units: int = Field(default=3, ge=2, le=100)

    @model_validator(mode="after")
    def chronological(self):
        aware(self.cutoff)
        landmarks = (*self.training_cutoffs, *(r.cutoff for r in self.training_runs))
        if any(aware(t) >= self.cutoff for t in landmarks):
            raise ValueError("training landmarks must precede feature cutoff")
        if len(set(landmarks)) != len(landmarks) or len(landmarks) > 6:
            raise ValueError("training landmarks must be unique")
        if any(not 2 <= len(term.strip()) <= 120 for term in self.cohort_terms):
            raise ValueError("cohort terms must be bounded nonempty literals")
        return self

    @property
    def unit_kind(self) -> Literal["trial", "program"]:
        return "trial" if self.target == "trial_stoppage" else "program"


class HistoryEvaluationCreate(FrozenModel):
    cutoff: datetime

    @model_validator(mode="after")
    def timestamp(self):
        aware(self.cutoff)
        return self


class HistoricalUnit(FrozenModel):
    unit_id: str
    unit_kind: Literal["trial", "program"]
    lineage_id: str
    phase: str = "unknown"
    label: str
    indication: str | None = None
    jurisdiction: str | None = None
    evidence_ref: ProviderRef
    eligible: bool = True
    reasons: tuple[str, ...] = ()


class HistoricalEvent(FrozenModel):
    unit_id: str
    unit_kind: Literal["trial", "program"]
    event_type: HistoryTarget
    public_available_at: datetime
    evidence_ref: ProviderRef
    event_at: datetime | None = None
    is_backfill: bool = False
    event_observed: bool | None = None
    observation_start: datetime | None = None
    observation_end: datetime | None = None

    @model_validator(mode="after")
    def timestamps(self):
        aware(self.public_available_at)
        for value in (self.event_at, self.observation_start, self.observation_end):
            if value is not None:
                aware(value)
        if self.observation_start and self.observation_end and self.observation_end < self.observation_start:
            raise ValueError("outcome observation window is reversed")
        if self.event_observed is True and (
            self.event_at is None or self.event_at > self.public_available_at
        ):
            raise ValueError("observed event requires event time no later than disclosure")
        if self.event_observed is False and (
            self.observation_start is None
            or self.observation_end is None
            or self.observation_end > self.public_available_at
        ):
            raise ValueError("negative observation must cover an already observed window")
        return self


class HistoryObservation(FrozenModel):
    """One actually adopted source view, never a synthetic complete world."""

    cutoff: datetime
    source_id: str
    is_demo: bool
    snapshot_id: str | None
    snapshot_hash: str | None
    units: tuple[HistoricalUnit, ...] = ()
    events: tuple[HistoricalEvent, ...] = ()
    coverage_complete: bool = False
    coverage_end: datetime | None = None
    scope_description: str
    record_count: int = Field(ge=0)
    gaps: tuple[str, ...] = ()


class HistoryPrediction(FrozenModel):
    unit: HistoricalUnit
    target: HistoryTarget
    cutoff: datetime
    horizon_end: datetime
    probability: float | None = Field(default=None, ge=0, le=1)
    baseline: Literal["phase_historical_rate", "pooled_historical_rate", "unsupported"]
    training_units: int = Field(default=0, ge=0)
    training_events: int = Field(default=0, ge=0)
    training_refs: tuple[ProviderRef, ...] = Field(
        default=(),
        description="Reference preview; full training versions remain in frozen snapshots/evidence",
    )
    training_refs_truncated: bool = False
    reasons: tuple[str, ...] = ()


class HistoryOutcome(FrozenModel):
    unit_id: str
    status: Literal["event", "no_event", "censored", "unknown", "not_eligible"]
    label: Literal[0, 1] | None = None
    observed_event_at: datetime | None = None
    event_refs: tuple[ProviderRef, ...] = ()
    reasons: tuple[str, ...] = ()


class HistoryMetrics(FrozenModel):
    cohort_count: int = Field(ge=0)
    predicted_count: int = Field(ge=0)
    evaluable_count: int = Field(ge=0)
    events: int = Field(ge=0)
    non_events: int = Field(ge=0)
    censored: int = Field(ge=0)
    unknown: int = Field(ge=0)
    evaluation_coverage: float = Field(ge=0, le=1)
    brier_score: float | None = Field(default=None, ge=0, le=1)
    selected_count: int = Field(ge=0)
    selected_evaluable: int = Field(ge=0)
    precision_at_budget: float | None = Field(default=None, ge=0, le=1)
    precision_lower: float | None = Field(default=None, ge=0, le=1)
    precision_upper: float | None = Field(default=None, ge=0, le=1)
    recall_at_budget: float | None = Field(default=None, ge=0, le=1)
    missed_observed_events: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    calibration: tuple[dict, ...] = ()
    lead_time_days: tuple[float, ...] = ()
    limitations: tuple[str, ...] = ()


class HistoryStudySummary(FrozenModel):
    id: str
    title: str
    source_id: str
    is_demo: bool
    state: Literal["collecting", "predicted", "partial", "unsupported", "failed"]
    created_at: datetime
    target: HistoryTarget
    cutoff: datetime
    prediction_hash: str | None
    investigation_id: str | None


class HistoryStudyView(HistoryStudySummary):
    request: HistoryStudyCreate
    compute_version: str = HISTORY_COMPUTE_VERSION
    predictions: tuple[HistoryPrediction, ...] = ()
    cohort: HistoryObservation | None = None
    training: tuple[dict, ...] = ()
    sources: tuple[dict, ...] = ()
    queries: tuple[dict, ...] = ()
    mining: dict | None = None
    fact_summary: dict | None = None
    next_questions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluations: tuple[dict, ...] = ()
    professional_status: Literal["not_reviewed"] = "not_reviewed"
    model_regime: Literal["deterministic_historical_reconstruction"] = (
        "deterministic_historical_reconstruction"
    )
    weknora_status: Literal["not_connected"] = "not_connected"
