"""Three narrow runtime tables; analysis_runs remains mining/decision only."""

from datetime import datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bioradar.data.database import Base
from bioradar.research.engine.schemas import RuntimeModel

MONITOR_VERSION = "bioradar-monitor-v0.1"
BatchState = Literal["accepted", "queued", "investigating", "completed", "blocked"]
LeadState = Literal["pending", "investigating", "investigated", "deferred", "blocked"]


class MonitorState(Base):
    __tablename__ = "monitor_state"
    workspace_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    paused: Mapped[bool] = mapped_column(nullable=False, default=False)
    control_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    # Per-source cursors and a bounded current feature window, not a second corpus.
    source_progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    next_source: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))


class MonitorBatch(Base):
    __tablename__ = "monitor_batches"
    __table_args__ = (UniqueConstraint("workspace_id", "batch_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    batch_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    is_demo: Mapped[bool] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    # Frozen source page, manifest, projection, policy and subsequent receipts.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("engine_snapshots.id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_runs.id"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MonitorLead(Base):
    __tablename__ = "monitor_leads"
    __table_args__ = (UniqueConstraint("workspace_id", "lead_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    lead_key: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("monitor_batches.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), nullable=False)
    investigation_id: Mapped[str | None] = mapped_column(ForeignKey("investigations.id"))
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    support_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MonitorControl(RuntimeModel):
    expected_revision: int = Field(ge=1)
    paused: bool


class MonitorSourceStatus(RuntimeModel):
    source_id: str
    is_demo: bool
    acked_after: int = 0
    scan_max_seq: int | None = None
    active_batch_id: str | None = None
    state: str = "idle"
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    error_code: str | None = None
    retained_records: int = 0


class MonitorPublicationStatus(RuntimeModel):
    source_id: str
    publisher_state: Literal["running", "idle", "resource_blocked", "unavailable", "not_started", "stale"]
    last_heartbeat: datetime | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    disk_paused: bool | None = None
    error_code: str | None = None


class MonitorStatus(RuntimeModel):
    enabled: bool
    paused: bool
    control_revision: int
    process_status: Literal["disabled", "paused", "starting", "running", "stale", "not_connected"]
    last_heartbeat: datetime | None = None
    limits: dict[str, int]
    sources: list[MonitorSourceStatus]
    publication_sources: list[MonitorPublicationStatus] = Field(default_factory=list)
    counts: dict[str, int]
    limitations: list[str]


class MonitorSelectionSummary(RuntimeModel):
    schema_version: Literal["bioradar.monitor-selection.v1"] = "bioradar.monitor-selection.v1"
    scope: Literal["this_batch_manifest_and_frozen_computation_window"]
    new_records: int = Field(ge=0)
    evaluated: int = Field(ge=0)
    selected: int = Field(ge=0)
    no_candidate: int = Field(ge=0)
    unknown: int = Field(ge=0)
    methods: list[dict]
    record_outcomes: list[dict]
    runtime_configuration: dict[str, int | None] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    limitations: list[str]


class MonitorBatchView(RuntimeModel):
    id: str
    source_id: str
    is_demo: bool
    state: BatchState
    created_at: datetime
    updated_at: datetime
    run_id: str | None
    snapshot_id: str | None
    from_after: int
    next_after: int
    max_seq: int
    changed_versions: int
    window_records: int
    scope_complete: bool = False
    selection_summary: MonitorSelectionSummary | None = None
    investigation_ids: list[str]
    error_code: str | None
    limitations: list[str]


class MonitorLeadView(RuntimeModel):
    id: str
    lead_key: str
    title: str
    method_id: str
    state: LeadState
    run_id: str
    investigation_id: str | None
    batch_id: str
    new_to_radar: bool
    needs_investigation: bool
    importance: Literal["undetermined", "low", "medium", "high"] | None = None
    importance_reason: str | None = None
    urgency_reason: str | None = None
    urgency: Literal["undetermined", "routine", "soon", "immediate"]
    changed_support_versions: int
    error_code: str | None = None
    data_gaps: list[str]
    created_at: datetime
    updated_at: datetime


class MonitorBatchList(RuntimeModel):
    items: list[MonitorBatchView]
    next_cursor: str | None = None
    unavailable_count: int = 0


class MonitorLeadList(RuntimeModel):
    items: list[MonitorLeadView]
    next_cursor: str | None = None
    unavailable_count: int = 0
