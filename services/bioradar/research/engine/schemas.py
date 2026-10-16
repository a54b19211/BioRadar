"""Runtime HTTP models that embed the single mining input/output contract."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

from bioradar.research.decision.models import DecisionInput, DecisionPolicy, DecisionResult
from bioradar.research.mining.models import MethodResult, MiningPolicy, MiningResult, NormalizedSnapshot


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionLogin(RuntimeModel):
    credential: SecretStr = Field(min_length=1, max_length=1000)


class SessionView(RuntimeModel):
    enabled: bool
    authenticated: bool
    actor_id: str | None = None
    workspace_id: str | None = None


class MethodInfo(RuntimeModel):
    method_id: str
    version: str
    label: str
    input_groups: list[str]
    goals: list[str]


class SnapshotSummary(RuntimeModel):
    id: str
    snapshot_hash: str
    is_demo: bool
    source_instance: str
    coverage_mode: str
    record_count: int
    observed_at: datetime
    created_at: datetime
    metadata_gaps: list[str]


class SnapshotDetail(RuntimeModel):
    id: str
    snapshot_hash: str
    snapshot: NormalizedSnapshot


class SnapshotList(RuntimeModel):
    items: list[SnapshotSummary]
    next_cursor: str | None = None


class RunCreate(RuntimeModel):
    snapshot_id: str = Field(min_length=1, max_length=36)
    policy: MiningPolicy


class RunAction(RuntimeModel):
    action: Literal["retry_failed"]
    expected_revision: int = Field(ge=1)


class RunError(RuntimeModel):
    code: str
    message: str


class FeedbackCreate(RuntimeModel):
    candidate_key: str = Field(min_length=1, max_length=200)
    result_hash: str = Field(min_length=64, max_length=64)
    decision: Literal["relevant", "irrelevant", "duplicate", "needs_check"]
    reason: str = Field(min_length=1, max_length=2000)


class FeedbackView(RuntimeModel):
    id: str
    run_id: str
    actor_id: str
    candidate_key: str
    result_hash: str
    decision: str
    reason: str
    created_at: datetime


class AttemptDiagnostic(RuntimeModel):
    attempt: int
    state: str
    error_code: str | None
    completed_at: datetime | None
    result_hash: str | None
    method_results: list[MethodResult] = Field(default_factory=list)


class RunView(RuntimeModel):
    id: str
    snapshot_id: str
    input_hash: str
    original_policy: dict[str, JsonValue]
    policy: MiningPolicy | None = Field(
        description="Current schema view only; original_policy is the frozen input"
    )
    policy_view_status: Literal["current", "historical_requires_new_run"]
    state: Literal["queued", "running", "completed", "partial", "failed"]
    revision: int
    attempt: int
    dispatch_pending: bool
    dependency_note: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    error: RunError | None = None
    result: MiningResult | None = None
    candidate_count: int | None = Field(default=None, ge=0)
    feedback: list[FeedbackView] = Field(default_factory=list)
    attempt_diagnostics: list[AttemptDiagnostic] = Field(default_factory=list)


class RunList(RuntimeModel):
    items: list[RunView]
    next_cursor: str | None = None


class DecisionRunCreate(RuntimeModel):
    snapshot_ids: list[Annotated[str, Field(min_length=1, max_length=36)]] = Field(
        default_factory=list, max_length=8
    )
    input: DecisionInput
    policy: DecisionPolicy


class DecisionSnapshotBinding(RuntimeModel):
    id: str
    snapshot_hash: str
    source_instance: str
    is_demo: bool


class DecisionAttemptDiagnostic(RuntimeModel):
    attempt: int
    state: str
    error_code: str | None
    completed_at: datetime | None
    result_hash: str | None
    decision_result: DecisionResult | None = None


class DecisionRunView(RuntimeModel):
    id: str
    run_kind: Literal["decision"] = "decision"
    title: str
    audience: str
    is_demo: bool
    input_hash: str
    evidence_basis_hash: str
    source_snapshots: list[DecisionSnapshotBinding]
    submitted_by: str
    accepted_at: datetime
    submitted_request_hash: str
    decision_input: DecisionInput | None
    original_input: dict[str, JsonValue] | None
    input_view_status: Literal["current", "historical_requires_new_run"]
    policy: DecisionPolicy | None
    original_policy: dict[str, JsonValue]
    policy_view_status: Literal["current", "historical_requires_new_run"]
    state: Literal["queued", "running", "completed", "partial", "failed"]
    revision: int
    attempt: int
    dispatch_pending: bool
    dependency_note: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    error: RunError | None = None
    result: DecisionResult | None = None
    attempt_diagnostics: list[DecisionAttemptDiagnostic] = Field(default_factory=list)


class DecisionRunList(RuntimeModel):
    items: list[DecisionRunView]
    next_cursor: str | None = None
