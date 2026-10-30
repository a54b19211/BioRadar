"""Persistent topic scopes, lightweight contributions and immutable evaluations."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bioradar.data.database import Base
from bioradar.research.engine.schemas import RuntimeModel
from bioradar.research.mining.models import ProviderRef

TopicDomain = Literal[
    "pipeline", "research", "business", "public_health", "policy", "regulatory", "supply", "regional", "other"
]
TopicProfile = Literal["text_exposure", "registered_state", "typed_metric", "source_event"]
ShortText = Annotated[str, Field(min_length=1, max_length=128)]


class TopicDefinition(RuntimeModel):
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    domain: TopicDomain
    source_instance: str = Field(default="datafabric-test", min_length=1, max_length=200)
    source_ids: list[ShortText] = Field(min_length=1, max_length=16)
    source_sites: list[ShortText] = Field(default_factory=list, max_length=16)
    terms_all: list[ShortText] = Field(default_factory=list, max_length=16)
    terms_any: list[ShortText] = Field(default_factory=list, max_length=16)
    profiles: list[TopicProfile] = Field(min_length=1, max_length=4)
    metric_ids: list[ShortText] = Field(default_factory=list, max_length=16)
    bucket: Literal["hour", "day", "week", "month"] = "month"
    lookback_buckets: int = Field(default=24, ge=3, le=120)
    cadence_hours: int = Field(default=24, ge=1, le=720)
    minimum_support: int = Field(default=2, ge=2, le=100)

    @field_validator("source_ids", "source_sites", "terms_all", "terms_any", "profiles", "metric_ids")
    @classmethod
    def unique_values(cls, values):
        if len(set(v.casefold() for v in values)) != len(values):
            raise ValueError("duplicate scope values")
        if any(not v.strip() or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("scope values must be nonempty text")
        return values


class TopicCreate(RuntimeModel):
    is_demo: bool = False
    definition: TopicDefinition


class TopicUpdate(TopicCreate):
    expected_revision: int = Field(ge=1)


class TopicControl(RuntimeModel):
    expected_revision: int = Field(ge=1)
    paused: bool
    lifecycle: Literal["closed", "warming"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reason: str | None = Field(default=None, max_length=2000, exclude_if=lambda value: value is None)


class TopicSourceProgress(RuntimeModel):
    source_id: str
    after: int = 0
    scan_max_seq: int | None = None
    seed_complete: bool = False
    last_success_at: datetime | None = None
    error_code: str | None = None
    upstream_health: dict | None = None


class TopicView(RuntimeModel):
    id: str
    is_demo: bool
    definition: TopicDefinition
    scope_revision: int
    control_revision: int
    observation_revision: int
    paused: bool
    state: Literal["paused", "seeding", "catching_up", "current", "source_unavailable"]
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    next_evaluation_at: datetime | None = None
    sources: list[TopicSourceProgress]
    observation_count: int
    latest_evaluation_id: str | None = None
    data_gaps: list[str]
    tracking: dict | None = Field(default=None, exclude_if=lambda value: value is None)


class TopicRiskReviewView(RuntimeModel):
    topic: TopicView
    assessment: dict


class TopicList(RuntimeModel):
    items: list[TopicView]
    next_cursor: str | None = None
    unavailable_count: int = 0


class TopicTemplate(RuntimeModel):
    id: TopicDomain
    title: str
    description: str
    definition: TopicDefinition
    qualification: str
    missing_sources: list[str]


class TopicTemplates(RuntimeModel):
    items: list[TopicTemplate]
    limitations: list[str]


class TopicBucket(RuntimeModel):
    start: str
    end: str
    complete_time_bucket: bool
    exposures: int
    confirmed_study_units: int
    unresolved_units: int
    source_counts: dict[str, int]
    feature_counts: dict[str, int]
    qualification: Literal["adopted_exposure_only", "comparable_study_exposure"] = "adopted_exposure_only"


class TopicSignal(RuntimeModel):
    key: str
    kind: Literal[
        "text_pattern",
        "state_change",
        "milestone_check",
        "metric_change",
        "source_event",
        "evidence_revision",
        "source_claim",
    ]
    title: str
    reason: str
    support_units: int = 0
    qualification: str
    details: dict = Field(default_factory=dict)
    evidence_keys: list[str] = Field(default_factory=list)
    next_questions: list[str] = Field(default_factory=list)


class TopicRegisteredState(RuntimeModel):
    unit_id: str
    status: str | None = None
    phase: list[str] | None = None
    first_observed_at: str
    last_observed_at: str
    source_updated_at: dict | None = None
    primary_completion_date: dict | str | None = None
    completion_date: dict | str | None = None
    evidence_key: str
    qualification: str = "last_known_registration_not_current_clinical_truth"


class TopicMetricPoint(RuntimeModel):
    period_start: str
    period_end: str
    lower: str | None
    upper: str | None
    measurement_basis: str
    denominator: dict | None = None
    evidence_key: str
    qualified: bool


class TopicMetricSeries(RuntimeModel):
    key: str
    subject: dict
    metric_id: str
    metric_version: str
    unit: str
    population: str | None
    region: str | None
    denominator_unit: str | None = None
    points: list[TopicMetricPoint]
    descriptive_slope: str | None = None
    qualification: str


class TopicEvidence(RuntimeModel):
    key: str
    record_ref: ProviderRef
    source_id: str
    source_record_id: str
    version_id: str
    raw_sha256: str
    source_site: str | None = None
    field_locators: dict = Field(default_factory=dict)


class TopicCoverage(RuntimeModel):
    scope: Literal["committed_adoptions"] = "committed_adoptions"
    seed_complete: bool
    source_watermarks: dict[str, int]
    source_errors: dict[str, str]
    observed_source_sites: list[str]
    missing_source_sites: list[str]
    matched_records: int
    active_records: int
    withdrawn_records: int
    undated_records: int
    limited_feature_records: int
    evaluated_records: int = 0
    omitted_records: int = 0
    registered_status_counts: dict[str, int] = Field(default_factory=dict)
    original_source_coverage_known: bool = False
    limitations: list[str]


class TopicEvaluation(RuntimeModel):
    id: str
    topic_id: str
    scope_revision: int
    compute_version: str
    definition: TopicDefinition
    observation_revision: int
    as_of: datetime
    created_at: datetime
    state: Literal["descriptive", "insufficient_data"]
    summary: str
    buckets: list[TopicBucket]
    signals: list[TopicSignal]
    registered_states: list[TopicRegisteredState]
    metric_series: list[TopicMetricSeries]
    coverage: TopicCoverage
    evidence: list[TopicEvidence]
    evidence_truncated: bool = False
    data_gaps: list[str]
    next_questions: list[str]
    draft: str
    investigation_id: str | None = None
    automation_status: Literal[
        "not_needed", "deferred", "saved", "needs_review", "blocked", "waiting_budget"
    ] = "not_needed"
    tracking: dict | None = Field(default=None, exclude_if=lambda value: value is None)


class TopicEvaluationList(RuntimeModel):
    items: list[TopicEvaluation]
    next_cursor: str | None = None
    unavailable_count: int = 0


class TopicScopeRecord(Base):
    __tablename__ = "topic_scopes"
    __table_args__ = (UniqueConstraint("workspace_id", "topic_id", "scope_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    scope_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    control_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(nullable=False)
    is_demo: Mapped[bool] = mapped_column(nullable=False)
    paused: Mapped[bool] = mapped_column(nullable=False, default=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Independent seed/catch-up positions; the monitor's global ACK is unrelated.
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    observation_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_source: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_evaluation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TopicObservationRecord(Base):
    __tablename__ = "topic_observations"
    __table_args__ = (UniqueConstraint("scope_id", "source_id", "source_record_id", "version_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_id: Mapped[str] = mapped_column(ForeignKey("topic_scopes.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(1000), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    matched: Mapped[bool] = mapped_column(nullable=False)
    withdrawn: Mapped[bool] = mapped_column(nullable=False)
    source_site: Mapped[str] = mapped_column(String(200), nullable=False)
    publication_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metric_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trial_id: Mapped[str | None] = mapped_column(String(100))
    registration_status: Mapped[str | None] = mapped_column(String(100))
    dependency_source_id: Mapped[str | None] = mapped_column(String(200))
    supersedes_ref: Mapped[dict | None] = mapped_column(JSONB)
    # Selection intervals describe our adopted head, never a medical valid-time claim.
    selected_from: Mapped[int | None] = mapped_column(Integer)
    selected_to: Mapped[int | None] = mapped_column(Integer)
    # Delivery bookkeeping is separate from the immutable contribution, so old source facts never change.
    attention_pending: Mapped[bool] = mapped_column(nullable=False, default=False)
    contribution: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TopicEvaluationRecord(Base):
    __tablename__ = "topic_evaluations"
    __table_args__ = (UniqueConstraint("scope_id", "evaluation_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_id: Mapped[str] = mapped_column(ForeignKey("topic_scopes.id"), nullable=False, index=True)
    evaluation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Frozen assessment never changes. Only separate append receipts may progress.
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    followup: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
