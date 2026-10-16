"""The single typed boundary shared by adapters, pure methods and the local runtime.

V1 provider fields retain their meanings. Optional extensions are evidence-backed
adopted sections, not a replacement entity/graph/document authority.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Readiness = Literal["ready", "exploratory_only", "unsupported"]
CoverageMode = Literal["complete", "bounded_sample", "manual_package", "unknown"]
TimePrecision = Literal["instant", "day", "month", "year", "interval", "unknown"]
METHOD_IDS = (
    "lexical_emergence",
    "text_topics",
    "entity_combinations",
    "state_transitions",
    "pipeline_landscape",
    "research_network",
    "maturity_shift",
    "counterevidence_impact",
    "translation_dwell",
    "event_exposure",
    "regional_comparison",
)
ENGINE_COMPUTE_VERSION = "bioradar-mining-compute-v0.2"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ProviderRef(FrozenModel):
    provider: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    adopted_version: str = Field(min_length=1)
    version_source: Literal["provider", "export_hash"]
    content_hash: str = Field(min_length=1)
    authorization_ref: str = Field(min_length=1)

    @property
    def identity_key(self) -> tuple[str, str, str]:
        return self.provider, self.object_type, self.object_id

    @property
    def version_key(self) -> tuple[str, str, str, str]:
        return (*self.identity_key, self.adopted_version)


class Locator(FrozenModel):
    type: Literal["page", "paragraph", "offset", "unavailable"] = "unavailable"
    page: int | None = Field(default=None, ge=1)
    paragraph_id: str | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def check_position(self) -> Locator:
        if self.type == "offset" and (self.start is None or self.end is None or self.end < self.start):
            raise ValueError("offset locator requires an ordered start and end")
        if self.type == "page" and self.page is None:
            raise ValueError("page locator requires page")
        if self.type == "paragraph" and not self.paragraph_id:
            raise ValueError("paragraph locator requires paragraph_id")
        return self


class TimeExpression(FrozenModel):
    raw: str | None = None
    precision: TimePrecision = "unknown"
    instant: datetime | None = None

    @model_validator(mode="after")
    def validate_time(self) -> TimeExpression:
        if self.instant is not None and self.instant.tzinfo is None:
            raise ValueError("time expression requires timezone")
        if self.precision in {"month", "year", "interval", "unknown"} and self.instant is not None:
            raise ValueError("coarse time cannot become an instant")
        return self


class MiningEntity(FrozenModel):
    canonical_entity_ref: ProviderRef | None = None
    temporary_entity_ref: ProviderRef
    entity_type: str
    mapping_ref: ProviderRef | None = None
    mapping_version: str | None = None
    mapping_status: Literal["confirmed", "candidate", "unresolved", "new_entity"] = "unresolved"
    mapping_available_at: datetime | None = None


class MiningLabels(FrozenModel):
    technology_form: tuple[str, ...] = ()
    target_refs: tuple[ProviderRef, ...] = ()
    disease_refs: tuple[ProviderRef, ...] = ()
    label_provider_ref: ProviderRef | None = None
    provenance: SectionProvenance | None = None


class StateAssertion(FrozenModel):
    assertion_ref: ProviderRef
    entity_ref: ProviderRef | None = None
    property: str = Field(min_length=1)
    value: str | int | float | bool | None = None
    qualifiers: tuple[Qualifier, ...] = ()
    effective_time: TimeExpression = Field(default_factory=TimeExpression)
    source_ref: ProviderRef


class Qualifier(FrozenModel):
    key: str
    value: str | int | float | bool | None = None


class RuleReadiness(FrozenModel):
    rule: Literal["R1", "R2", "R3", "R4"]
    state: Readiness
    reasons: tuple[str, ...] = ()


class SectionProvenance(FrozenModel):
    schema_version: str = Field(min_length=1)
    provider_ref: ProviderRef
    available_at: datetime | None = None

    @model_validator(mode="after")
    def validate_time(self) -> SectionProvenance:
        if self.available_at is not None and self.available_at.tzinfo is None:
            raise ValueError("section available_at requires timezone")
        return self


class StudyContext(FrozenModel):
    disease_refs: tuple[ProviderRef, ...] = ()
    population: str | None = None
    treatment_line: str | None = None
    endpoint: str | None = None
    geography: str | None = None
    comparator: str | None = None
    study_design: str | None = None
    provenance: SectionProvenance | None = None


class Embedding(FrozenModel):
    provider: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    dimension: int = Field(ge=1)
    vector: tuple[float, ...]
    text_hash: str
    provenance: SectionProvenance

    @model_validator(mode="after")
    def check_vector(self) -> Embedding:
        if len(self.vector) != self.dimension or not all(math.isfinite(x) for x in self.vector):
            raise ValueError("embedding dimension or numeric values are invalid")
        if not any(self.vector):
            raise ValueError("embedding must be nonzero")
        return self


class Relation(FrozenModel):
    relation_ref: ProviderRef
    subject_ref: ProviderRef
    object_ref: ProviderRef
    relation_type: str
    directed: bool = True
    identity_status: Literal["confirmed", "unresolved"] = "unresolved"
    effective_time: TimeExpression = Field(default_factory=TimeExpression)
    valid_until: TimeExpression | None = None
    evidence_refs: tuple[ProviderRef, ...] = ()
    study_cluster_ref: ProviderRef | None = None
    provenance: SectionProvenance


class Assessment(FrozenModel):
    assessment_ref: ProviderRef
    study_cluster_ref: ProviderRef
    stage: str
    review_status: Literal["reviewed", "unreviewed", "inconclusive"] = "unreviewed"
    stance: Literal["supportive", "negative", "mixed", "undetermined"] = "undetermined"
    assessor: str | None = None
    context: StudyContext
    evidence_refs: tuple[ProviderRef, ...] = ()
    provenance: SectionProvenance


class ClaimDependency(FrozenModel):
    judgment_id: str
    claim_id: str
    claim_text: str
    entity_refs: tuple[ProviderRef, ...] = ()
    context: StudyContext | None = None
    evidence_refs: tuple[ProviderRef, ...] = ()
    provenance: SectionProvenance


class Event(FrozenModel):
    event_ref: ProviderRef
    event_type: str
    object_refs: tuple[ProviderRef, ...] = ()
    jurisdiction: str | None = None
    effective_time: TimeExpression = Field(default_factory=TimeExpression)
    evidence_refs: tuple[ProviderRef, ...] = ()
    source_status: Literal["supported", "unverified"] = "unverified"
    provenance: SectionProvenance


class RegionalIndicator(FrozenModel):
    indicator: str
    region: str
    period: str
    unit: str
    value: float
    denominator: float | None = Field(default=None, gt=0)
    population_scope: str
    jurisdiction: str
    evidence_refs: tuple[ProviderRef, ...] = ()
    provenance: SectionProvenance


class ProjectTimeline(FrozenModel):
    project_ref: ProviderRef
    stage: str
    entered_at: TimeExpression
    observation_end: datetime
    event_time: TimeExpression | None = None
    event_kind: Literal[
        "stage_advance", "clinical_failure", "commercial_exit", "unknown_exit", "no_event", "coverage_loss"
    ] = "no_event"
    observation_coverage: Literal["continuous", "gaps", "unknown"] = "unknown"
    context: StudyContext
    evidence_refs: tuple[ProviderRef, ...] = ()
    provenance: SectionProvenance


class EngineRecord(FrozenModel):
    record_ref: ProviderRef
    evidence_refs: tuple[ProviderRef, ...] = ()
    document_hash: str = Field(min_length=1)
    source_instance: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    observed_at: datetime
    source_family: str | None = None
    source_independence_status: Literal["confirmed", "unresolved"] = "unresolved"
    study_cluster_ref: ProviderRef | None = None
    study_independence_status: Literal["confirmed", "unresolved", "not_applicable"] = "unresolved"
    published_at: datetime | None = None
    published_time_raw: str | None = None
    time_precision: TimePrecision = "unknown"
    publication_time_verified: bool = False
    first_seen_at: datetime | None = None
    available_at: datetime | None = None
    source_url: str | None = None
    ingestion_mode: Literal["live_delta", "historical_backfill", "sample_export", "fixture"] = "sample_export"
    text_status: Literal["inline_authorized", "reference_authorized", "unavailable"] = "unavailable"
    normalized_text: str | None = None
    normalized_text_hash: str | None = None
    normalization_version: str | None = None
    authorized_text_ref: ProviderRef | None = None
    locator: Locator = Field(default_factory=Locator)
    canonical_entity_refs: tuple[MiningEntity, ...] = ()
    labels: MiningLabels = Field(default_factory=MiningLabels)
    project_ref: ProviderRef | None = None
    project_stage: str | None = None
    company_refs: tuple[ProviderRef, ...] = ()
    drug_ref: ProviderRef | None = None
    trial_refs: tuple[ProviderRef, ...] = ()
    project_status: str | None = None
    entry_time: TimeExpression | None = None
    exit_time: TimeExpression | None = None
    exit_reason: str | None = None
    state_assertions: tuple[StateAssertion, ...] = ()
    record_flags: tuple[
        Literal["revised", "deleted", "retracted", "historical_backfill", "new_source"], ...
    ] = ()
    quality_refs: tuple[ProviderRef, ...] = ()
    lineage_refs: tuple[ProviderRef, ...] = ()
    missing_fields: tuple[str, ...] = ()
    available_fields: tuple[str, ...] = ()
    rule_readiness: tuple[RuleReadiness, ...] = ()
    upstream_status: dict[str, str | None] = Field(default_factory=dict)
    context: StudyContext | None = None
    embedding: Embedding | None = None
    relations: tuple[Relation, ...] = ()
    assessments: tuple[Assessment, ...] = ()
    dependencies: tuple[ClaimDependency, ...] = ()
    events: tuple[Event, ...] = ()
    regional_indicators: tuple[RegionalIndicator, ...] = ()
    timeline: ProjectTimeline | None = None

    @model_validator(mode="after")
    def check_evidence(self) -> EngineRecord:
        timestamps = (self.observed_at, self.published_at, self.available_at, self.first_seen_at)
        if any(t is not None and t.tzinfo is None for t in timestamps):
            raise ValueError("record timestamps must have a timezone")
        if self.time_precision in {"month", "year", "interval", "unknown"} and self.published_at:
            raise ValueError("coarse or unknown publication time cannot become an instant")
        if self.text_status == "inline_authorized":
            if not self.normalized_text or not self.normalization_version:
                raise ValueError("inline text requires text and normalization_version")
            actual = hashlib.sha256(self.normalized_text.encode()).hexdigest()
            if self.normalized_text_hash is not None and self.normalized_text_hash != actual:
                raise ValueError("normalized text hash does not match text")
            object.__setattr__(self, "normalized_text_hash", actual)
        elif self.text_status == "reference_authorized" and self.authorized_text_ref is None:
            raise ValueError("referenced text requires authorized_text_ref")
        elif self.text_status == "unavailable" and (self.normalized_text or self.authorized_text_ref):
            raise ValueError("unavailable text cannot contain usable text")
        if self.embedding and self.embedding.text_hash != self.normalized_text_hash:
            raise ValueError("embedding belongs to a different text version")
        if self.study_independence_status == "confirmed" and not self.study_cluster_ref:
            raise ValueError("confirmed study independence requires study_cluster_ref")
        if self.source_independence_status == "confirmed" and not self.source_family:
            raise ValueError("confirmed source independence requires source_family")
        return self


class SnapshotCoverage(FrozenModel):
    source_instance: str
    source_id: str
    mode: CoverageMode = "unknown"
    scope_description: str
    record_count: int = Field(ge=0)
    total_reported: int | None = Field(default=None, ge=0)
    is_consistent_snapshot: bool = False
    reasons: tuple[str, ...] = ()


class NormalizedSnapshot(FrozenModel):
    schema_version: Literal["bioradar.engine.snapshot.v1"] = "bioradar.engine.snapshot.v1"
    snapshot_id: str
    is_demo: bool = False
    source_instance: str
    coverage_mode: CoverageMode = "unknown"
    observed_at: datetime
    records: tuple[EngineRecord, ...]
    coverage: tuple[SnapshotCoverage, ...] = ()
    metadata_gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check_snapshot(self) -> NormalizedSnapshot:
        if self.observed_at.tzinfo is None:
            raise ValueError("snapshot observed_at requires timezone")
        if not self.is_demo and any(r.ingestion_mode == "fixture" for r in self.records):
            raise ValueError("fixture records require is_demo")
        if any(r.source_instance != self.source_instance for r in self.records):
            raise ValueError("a snapshot must contain a single explicitly named source instance")
        coverage_keys = [(c.source_instance, c.source_id) for c in self.coverage]
        if len(set(coverage_keys)) != len(coverage_keys):
            raise ValueError("duplicate coverage source keys are not allowed")
        if any(c.source_instance != self.source_instance for c in self.coverage):
            raise ValueError("coverage must belong to the named snapshot instance")
        return self


class ResourceLimits(FrozenModel):
    max_records: int = Field(default=2000, ge=1, le=100000)
    max_text_characters: int = Field(default=2000000, ge=100, le=50000000)
    max_features: int = Field(default=15000, ge=10, le=100000)
    max_candidates_per_method: int = Field(default=100, ge=1, le=1000)
    review_capacity: int = Field(default=20, ge=1, le=1000)
    max_candidates_per_group: int = Field(default=2, ge=1, le=20)
    max_graph_edges: int = Field(default=10000, ge=1, le=100000)
    max_graph_hops: int = Field(default=3, ge=1, le=6)
    max_paths: int = Field(default=50, ge=1, le=1000)
    max_link_pairs: int = Field(default=1000, ge=1, le=20000)
    max_temporal_bins: int = Field(default=128, ge=2, le=2048)


class MiningPolicy(FrozenModel):
    policy_version: Literal["bioradar.mining.policy.v1"] = "bioradar.mining.policy.v1"
    as_of: datetime
    time_mode: Literal["publication_retrospective", "availability_replay"] = "publication_retrospective"
    discovery_mode: Literal["stock_and_incremental", "incremental"] = "stock_and_incremental"
    baseline_start: datetime | None = None
    baseline_end: datetime | None = None
    current_start: datetime | None = None
    current_end: datetime | None = None
    method_ids: tuple[str, ...] = Field(default=METHOD_IDS, max_length=len(METHOD_IDS))
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    scope_label: str | None = Field(
        default=None, description="Annotation only; records are not filtered by this label"
    )
    minimum_support: int = Field(default=2, ge=1)
    minimum_growth: float = Field(default=2.0, ge=1)
    exploration_fraction: float = Field(default=0.2, ge=0, le=1)
    known_vocabulary: tuple[str, ...] = Field(default=(), max_length=50000)
    vocabulary_version: str | None = None
    stoplist: tuple[str, ...] = Field(default=(), max_length=2000)
    topic_components: int = Field(default=8, ge=1, le=100)
    topic_similarity: float = Field(default=0.55, ge=0, le=1)
    novelty_distance: float = Field(default=0.45, ge=0, le=2)
    random_seed: int = 0
    burst_scale: float = Field(default=2, gt=1)
    burst_penalty: float = Field(default=1, ge=0)
    burst_states: int = Field(default=3, ge=2, le=10)
    dwell_threshold_days: int = Field(default=365, ge=1)
    maturity_stage_order: tuple[str, ...] = (
        "hypothesis",
        "in_vitro",
        "animal",
        "human_early_result",
        "human_late_result",
        "regulatory",
    )
    cooperation_relations: tuple[str, ...] = ("coauthored", "collaborated")
    exposure_relations: tuple[str, ...] = ("supplies", "depends_on", "licensed_to", "manufactures")
    exposure_reverse_relations: tuple[str, ...] = ("depends_on",)
    urgent_event_types: tuple[str, ...] = ("regulatory_safety_action", "product_recall")

    @model_validator(mode="after")
    def freeze_windows(self) -> MiningPolicy:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of requires timezone")
        end = self.current_end or self.as_of
        start = self.current_start or (end - timedelta(days=30))
        baseline_end = self.baseline_end or start
        baseline_start = self.baseline_start or (baseline_end - timedelta(days=365))
        times = (baseline_start, baseline_end, start, end)
        if any(t.tzinfo is None for t in times):
            raise ValueError("window bounds require timezone")
        if not baseline_start < baseline_end <= start < end <= self.as_of:
            raise ValueError("windows must be ordered, disjoint and end by as_of")
        if (end - baseline_start).days // 7 + 1 > self.limits.max_temporal_bins:
            raise ValueError("window span exceeds max_temporal_bins")
        if any(len(word) > 256 for word in (*self.known_vocabulary, *self.stoplist)):
            raise ValueError("vocabulary and stoplist entries must not exceed 256 characters")
        if len(set(self.method_ids)) != len(self.method_ids) or set(self.method_ids) - set(METHOD_IDS):
            raise ValueError("method_ids must be unique supported identifiers")
        for name, value in zip(("baseline_start", "baseline_end", "current_start", "current_end"), times):
            object.__setattr__(self, name, value.astimezone(UTC))
        return self


class EvidenceSupport(FrozenModel):
    record_ref: ProviderRef
    document_hash: str
    locator: Locator
    evidence_refs: tuple[ProviderRef, ...] = ()
    quote: str | None = None
    normalized_start: int | None = Field(default=None, ge=0)
    normalized_end: int | None = Field(default=None, ge=0)
    normalized_text_hash: str | None = None
    quote_status: Literal["normalized_only", "source_verified", "unavailable"] = "unavailable"
    source_url: str | None = None


class Candidate(FrozenModel):
    candidate_key: str
    method_id: str
    method_version: str
    group_key: str
    kind: Literal["observed_change", "association", "hypothesis", "data_quality"]
    title: str
    reasons: tuple[str, ...]
    supports: tuple[EvidenceSupport, ...]
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    score: float = 0.0
    evidence_status: Literal["unverified", "partial"] = "unverified"
    importance: Literal["undetermined", "low", "medium", "high"] = "undetermined"
    urgency: Literal["undetermined", "routine", "soon", "immediate"] = "routine"
    data_gaps: tuple[str, ...] = ()
    next_questions: tuple[str, ...] = ()
    next_data_requirements: tuple[str, ...] = ()


class ExcludedRecord(FrozenModel):
    record_ref: ProviderRef
    reason: str


class MethodResult(FrozenModel):
    method_id: str
    method_version: str
    readiness: Readiness
    reasons: tuple[str, ...] = ()
    execution_status: Literal["success", "partial", "failed", "unsupported"]
    candidates: tuple[Candidate, ...] = ()
    statistics: dict[str, JsonValue] = Field(default_factory=dict)
    excluded_records: tuple[ExcludedRecord, ...] = ()
    truncated_count: int = Field(default=0, ge=0)
    limitations: tuple[str, ...] = ()


class MiningResult(FrozenModel):
    schema_version: Literal["bioradar.engine.result.v1"] = "bioradar.engine.result.v1"
    compute_version: Literal["bioradar-mining-compute-v1", "bioradar-mining-compute-v0.2"] = (
        ENGINE_COMPUTE_VERSION
    )
    snapshot_hash: str
    policy_hash: str
    method_results: tuple[MethodResult, ...]
    candidates: tuple[Candidate, ...]
    result_hash: str
    limitations: tuple[str, ...] = ()


def canonical_hash(value: Any) -> str:
    """Hash canonical JSON; caller selects identity/content fields deliberately."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


StateAssertion.model_rebuild()
MiningLabels.model_rebuild()
