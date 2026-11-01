"""Bounded machine analysis is a research draft, never a human judgment."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field

from bioradar.research.engine.schemas import RuntimeModel
from bioradar.research.mining.models import AssessmentAxes, ProviderRef
from bioradar.topics.risk import TopicRiskReassessment

ANALYSIS_VERSION = "bioradar-investigation-analysis-v0.5"
PROMPT_VERSION = "bioradar-investigation-reading-v0.5"
Text = Annotated[str, Field(min_length=1, max_length=2000)]
LocalId = Annotated[str, Field(min_length=1, max_length=80)]
State = Literal["queued", "running", "completed", "needs_review", "blocked", "failed"]


class AnalysisBudget(RuntimeModel):
    max_calls: int | None = Field(default=None, ge=1, le=100)
    max_tokens: int | None = Field(default=None, ge=128, le=10_000_000)
    max_seconds: int | None = Field(default=None, ge=5, le=86400)
    max_queries: int | None = Field(default=None, ge=0, le=32)
    max_tool_calls: int | None = Field(default=None, ge=1, le=256)


class InitialQuery(RuntimeModel):
    source_id: str = Field(min_length=1, max_length=200)
    terms: list[Annotated[str, Field(min_length=2, max_length=120)]] = Field(min_length=1, max_length=3)
    reason: Text = "研究问题的初始查找"


class QuestionPlan(RuntimeModel):
    queries: list[InitialQuery] = Field(max_length=6)
    limitations: list[Text] = Field(default_factory=list, max_length=8)


class QuestionSpan(RuntimeModel):
    start: int = Field(ge=0, description="研究问题原始文本的Unicode起点，不是字节位置。")
    end: int = Field(ge=1, description="研究问题原始文本的Unicode排他终点，由宿主核对精确名称。")


class NamedQuestionComponent(RuntimeModel):
    id: LocalId
    name: str = Field(min_length=1, max_length=200)
    question_span: QuestionSpan


class NamedQuestionObject(RuntimeModel):
    id: LocalId
    name: str = Field(min_length=1, max_length=200)
    question_span: QuestionSpan
    components: list[NamedQuestionComponent] = Field(
        default_factory=list, max_length=4, exclude_if=lambda value: not value
    )


class ResearchQuestionPlan(QuestionPlan):
    named_objects: list[NamedQuestionObject] = Field(default_factory=list, max_length=16)


class HistoricalAnalysisScope(RuntimeModel):
    mode: Literal["public_history_reconstruction", "operational_replay"]
    cutoff: AwareDatetime


class QuestionAsk(RuntimeModel):
    question: str = Field(min_length=1, max_length=4000)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    is_demo: bool = False
    source_ids: list[str] = Field(default_factory=list, max_length=8)
    initial_queries: list[InitialQuery] = Field(default_factory=list, max_length=6)
    budget: AnalysisBudget | None = None
    historical_scope: HistoricalAnalysisScope | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    query_policy: Literal["baseline", "query_planner"] | None = None
    kdd_direction: Literal["research", "pipeline", "business", "public_health"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )




class AnalysisCreate(RuntimeModel):
    expected_revision: int = Field(ge=1)
    question: str | None = Field(default=None, min_length=1, max_length=4000)
    auto_retrieve: bool = False
    source_ids: list[str] = Field(default_factory=list, max_length=8)
    initial_queries: list[InitialQuery] = Field(default_factory=list, max_length=6)
    budget: AnalysisBudget | None = None
    historical_scope: HistoricalAnalysisScope | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    query_policy: Literal["baseline", "query_planner"] | None = None
    kdd_direction: Literal["research", "pipeline", "business", "public_health"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    retry_of_analysis_id: str | None = Field(default=None, min_length=1, max_length=36)
    parent_analysis_id: str | None = Field(default=None, min_length=1, max_length=36)
    material_entry_ids: list[str] = Field(default_factory=list, max_length=10)


class ModelEvidenceLink(RuntimeModel):
    evidence_key: LocalId
    passage_id: LocalId
    role: Literal["supports", "refutes", "limits", "context"]
    reason: Text


class ModelClaim(RuntimeModel):
    id: LocalId
    text: Text
    kind: Literal["source_statement", "inference"]
    scope: Text
    conditions: list[Text] = Field(max_length=8)
    status: Literal["supported", "contradicted", "mixed", "insufficient"]
    evidence_links: list[ModelEvidenceLink] = Field(max_length=2)


class Explanation(RuntimeModel):
    id: LocalId
    text: Text
    for_claim_ids: list[LocalId] = Field(max_length=8)
    against_claim_ids: list[LocalId] = Field(max_length=8)
    discriminating_evidence_needed: Text


class EvidenceGap(RuntimeModel):
    id: LocalId
    question: Text
    why_it_matters: Text


class PlannedQuery(RuntimeModel):
    gap_id: LocalId
    target_claim_id: LocalId
    source_id: str = Field(min_length=1, max_length=200)
    terms: list[Annotated[str, Field(min_length=2, max_length=120)]] = Field(min_length=1, max_length=3)
    anchoring_evidence_keys: list[LocalId] = Field(min_length=1, max_length=4)
    reason: Text
    expected_discrimination: Text


class PolicyPlannedQuery(PlannedQuery):
    coverage_check_ids: list[LocalId] = Field(default_factory=list, max_length=8)


class ConditionalAnswer(RuntimeModel):
    answer: str = Field(min_length=1, max_length=4000)
    conditions: list[Text] = Field(max_length=12)
    unknowns: list[Text] = Field(max_length=12)


class CoverageCheck(RuntimeModel):
    id: LocalId
    status: Literal["passed", "failed", "unknown"] = "unknown"
    evidence_ids: list[LocalId] = Field(default_factory=list, max_length=32)
    reason: str = Field(default="", max_length=2000)


class WarningAssessment(RuntimeModel):
    decision: Literal["verify_now", "alert", "watch"]
    supporting_claim_ids: list[LocalId] = Field(default_factory=list, max_length=6)
    opposing_claim_ids: list[LocalId] = Field(default_factory=list, max_length=6)
    critical_unknowns: list[Text] = Field(default_factory=list, max_length=8)
    next_actions: list[Text] = Field(default_factory=list, max_length=6)
    reason: Text


class CompetingHypothesis(RuntimeModel):
    id: LocalId
    explanation: Text
    supporting_claim_ids: list[LocalId] = Field(default_factory=list, max_length=6)
    opposing_claim_ids: list[LocalId] = Field(default_factory=list, max_length=6)
    discriminating_evidence_needed: Text
    untested_conditions: list[Text] = Field(default_factory=list, max_length=8)


class HypothesisJudgment(RuntimeModel):
    least_refuted_hypothesis_id: LocalId
    reason: Text
    next_discriminating_evidence: Text
    qualification: Literal["这是模型判断，未证实"] = "这是模型判断，未证实"


class ModelReading(RuntimeModel):
    mechanism_candidate: Text | None = None
    claims: list[ModelClaim] = Field(min_length=1, max_length=6)
    explanations: list[Explanation] = Field(max_length=2)
    gaps: list[EvidenceGap] = Field(max_length=4)
    next_queries: list[PlannedQuery] = Field(max_length=2)
    conditional_answer: ConditionalAnswer


class PolicyReading(ModelReading):
    current_judgment: HypothesisJudgment | None = Field(default=None, exclude_if=lambda value: value is None)
    coverage_checks: list[CoverageCheck] = Field(default_factory=list, max_length=64)
    next_queries: list[PolicyPlannedQuery] = Field(max_length=2)
    warning_assessment: WarningAssessment | None = Field(default=None, exclude_if=lambda value: value is None)
    competing_hypotheses: list[CompetingHypothesis] | None = Field(
        default=None, max_length=4, exclude_if=lambda value: value is None
    )


class ClaimUpdate(RuntimeModel):
    claim_id: LocalId
    previous_claim_id: LocalId | None
    reason: Text
    evidence_keys: list[LocalId] = Field(max_length=8)


class AnalysisUpdate(RuntimeModel):
    stance: Literal["initial", "maintained", "revised", "unable_to_compare"]
    changed_claims: list[ClaimUpdate] = Field(max_length=8)
    reasons: list[Text] = Field(min_length=1, max_length=8)
    decisive_evidence_keys: list[LocalId] = Field(max_length=12)


class ModelSynthesis(RuntimeModel):
    claims: list[ModelClaim] = Field(min_length=1, max_length=6)
    explanations: list[Explanation] = Field(max_length=2)
    gaps: list[EvidenceGap] = Field(max_length=4)
    conditional_answer: ConditionalAnswer
    update: AnalysisUpdate


class FinalResearchSynthesis(ModelSynthesis):
    """Future-only synthesis: all assessment references share this output's claims."""

    warning_assessment: WarningAssessment | None = Field(
        description="本轮问题的总体行动建议；可同时列出定向核实动作。不适用时显式null，不是真实性概率。"
    )
    competing_hypotheses: list[CompetingHypothesis] = Field(
        max_length=4, description="只引用本次claims已有id；无合理竞争解释时显式空列表，不猜旧编号映射。"
    )
    topic_risk_reassessment: TopicRiskReassessment | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    current_judgment: HypothesisJudgment | None = Field(default=None, exclude_if=lambda value: value is None)


class ClaimReview(RuntimeModel):
    claim_id: LocalId
    verdict: Literal["grounded", "overstated", "unsupported", "uncertain"]
    issue: str = Field(max_length=2000)
    evidence_keys: list[LocalId] = Field(max_length=8)


REVIEW_SECTIONS = ("claims", "explanations", "gaps", "conditional_answer", "update")
ReviewSection = Literal["claims", "explanations", "gaps", "conditional_answer", "update"]


class DraftFinding(RuntimeModel):
    # Historical results retain their original pointer; new model output uses a section.
    path: str | None = Field(default=None, min_length=2, max_length=200)
    section: ReviewSection | None = None
    claim_id: LocalId | None = None
    issue: Text
    evidence_keys: list[LocalId] = Field(max_length=8)


class GenericReview(RuntimeModel):
    provider: str | None = None
    status: Literal["passed", "needs_review"]
    claims: list[ClaimReview] = Field(min_length=0, max_length=8)
    overall_limitations: list[Text] = Field(max_length=8)
    findings: list[DraftFinding] = Field(default_factory=list, max_length=8)
    checked_sections: list[ReviewSection] = Field(default_factory=list, max_length=5)


class ModelFinding(RuntimeModel):
    section: Literal["claims", "explanations", "gaps", "conditional_answer", "update"]
    claim_id: LocalId | None = None
    issue: Text
    evidence_keys: list[LocalId] = Field(default_factory=list, max_length=8)


class ModelReview(RuntimeModel):
    status: Literal["passed", "needs_review"]
    claims: list[ClaimReview] = Field(min_length=1, max_length=8)
    overall_limitations: list[Text] = Field(max_length=8)
    findings: list[ModelFinding] = Field(max_length=8)


class AnalysisPassage(RuntimeModel):
    id: LocalId
    text: str = Field(min_length=1, max_length=500)
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class AnalysisMaterialContext(RuntimeModel):
    entry_id: str
    stance: Literal["support", "oppose", "context"]
    note: str = Field(max_length=8000)
    origin: Literal["user_supplied", "machine_proposed"]
    qualification: str


class AnalysisEvidence(RuntimeModel):
    key: str
    material_entry_id: str | None
    material_context: AnalysisMaterialContext | None = None
    evidence_role: Literal["source_material", "graph_background"] = "source_material"
    text_basis: Literal["snapshot_normalized", "source_clean_text", "source_raw_content"] = (
        "snapshot_normalized"
    )
    history_binding: dict | None = None
    source_context: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    facts: dict = Field(default_factory=dict)
    field_locators: dict = Field(default_factory=dict)
    snapshot_id: str
    record_ref: ProviderRef
    source_id: str
    title: str
    text: str
    start: int
    end: int
    text_sha256: str
    source_length: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    passages: list[AnalysisPassage] = Field(default_factory=list)
    selection_hash: str | None = None
    publication_time: str | None = None
    source_url: str | None = None
    record_flags: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class VerifiedEvidenceLink(RuntimeModel):
    evidence_key: LocalId
    quote: str = Field(min_length=1, max_length=600)
    role: Literal["supports", "refutes", "limits", "context"]
    reason: Text
    start: int
    end: int
    passage_id: LocalId | None = None


class VerifiedClaim(ModelClaim):
    evidence_links: list[VerifiedEvidenceLink] = Field(max_length=2)


class AnalysisResult(RuntimeModel):
    assessment_axes: AssessmentAxes | None = Field(default=None, exclude_if=lambda value: value is None)
    version: Literal[
        "bioradar-investigation-analysis-v0.1",
        "bioradar-investigation-analysis-v0.2",
        "bioradar-investigation-analysis-v0.3",
        "bioradar-investigation-analysis-v0.4",
        "bioradar-investigation-analysis-v0.4.1",
        "bioradar-investigation-analysis-v0.4.2",
        "bioradar-investigation-analysis-v0.4.3",
        "bioradar-investigation-analysis-v0.4.4",
        "bioradar-investigation-analysis-v0.4.5",
        "bioradar-investigation-analysis-v0.4.6",
        "bioradar-investigation-analysis-v0.4.7",
        "bioradar-investigation-analysis-v0.5",
    ] = ANALYSIS_VERSION
    claims: list[VerifiedClaim]
    explanations: list[Explanation]
    gaps: list[EvidenceGap]
    conditional_answer: ConditionalAnswer
    update: AnalysisUpdate
    review: GenericReview
    professional_status: Literal["not_reviewed"] = "not_reviewed"
    report: dict | None = None
    mechanism_evaluation: dict | None = None
    query_policy: dict | None = None
    warning_assessment: WarningAssessment | None = Field(default=None, exclude_if=lambda value: value is None)
    competing_hypotheses: list[CompetingHypothesis] | None = Field(
        default=None, max_length=4, exclude_if=lambda value: value is None
    )
    assessment_binding: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    topic_risk_reassessment: TopicRiskReassessment | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    current_judgment: HypothesisJudgment | None = Field(default=None, exclude_if=lambda value: value is None)
    limitations: list[str]


class QueryPagination(RuntimeModel):
    has_more: bool
    next_cursor: str | None = None
    max_seq: int = Field(ge=0)
    page_number: int = Field(ge=1)


class ReturnedSourceVersion(RuntimeModel):
    source_id: Text
    source_record_id: Text
    version_id: Text


class AnalysisQueryReceipt(RuntimeModel):
    index: int
    phase: Literal["initial", "followup"] = "followup"
    provider: Literal["fabric", "weknora", "graph", "fabric_graph", "radar", "professional"] = "fabric"
    kind: str = "document_search"
    action_id: str | None = None
    semantic_key: str | None = None
    decision_step: int | None = None
    cost: dict | None = None
    graph_context: dict | list | None = None
    artifact_refs: list[dict] = Field(default_factory=list, exclude_if=lambda value: not value)
    check_updates: list[dict] = Field(default_factory=list, exclude_if=lambda value: not value)
    plan: PolicyPlannedQuery | PlannedQuery | InitialQuery | dict
    relaxed_from: PolicyPlannedQuery | PlannedQuery | InitialQuery | None = None
    relaxation_reason: Text | None = None
    status: Literal["prepared", "sent", "read", "empty", "failed", "unavailable", "unconfirmed"]
    scope: str = "fabric_current_adopted_versions_not_the_web"
    returned: int = 0
    returned_versions: list[ReturnedSourceVersion] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    new_versions: int = 0
    has_more: bool = False
    pagination: QueryPagination | None = Field(default=None, exclude_if=lambda value: value is None)
    error_code: str | None = None
    evidence_keys: list[str] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
    usage: dict | None = None


class AnalysisSummary(RuntimeModel):
    id: str
    investigation_id: str
    parent_analysis_id: str | None
    state: State
    trigger: Literal["manual", "monitor", "topic", "material_update", "history"]
    created_at: datetime
    completed_at: datetime | None
    question: str
    based_on_revision: int
    version: str
    evidence_count: int
    error_code: str | None
    result_summary: str | None
    professional_status: Literal["not_reviewed"] = "not_reviewed"


class AnalysisView(AnalysisSummary):
    evidence_time: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    human_review_context: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)
    is_demo: bool
    input_hash: str
    prompt_version: str
    search_scope: list[str]
    evidence: list[AnalysisEvidence]
    queries: list[AnalysisQueryReceipt]
    calls: list[dict]
    result: AnalysisResult | None
    initial_draft: AnalysisResult | None = None
    limitations: list[str]
    note_entry_id: str | None
    progress: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)
    providers: dict = Field(default_factory=dict)
    query_policy: dict | None = None


class AnalysisList(RuntimeModel):
    enabled: bool
    configured: bool
    unavailable_reason: str | None
    limits: dict[str, int]
    retrieval_sources: list[str]
    items: list[AnalysisSummary]
    next_cursor: str | None = None
    unavailable_count: int = 0


class AnalysisEvidenceRead(RuntimeModel):
    evidence: AnalysisEvidence
    normalized_text: str
    source_url: str | None
    warning: str = "固定来源文本；机器证据关系仍须人工核对。"
