"""The shared HTTP contract for manual investigation records."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, RootModel

from bioradar.research.engine.schemas import RuntimeModel
from bioradar.research.mining.models import Candidate, EvidenceSupport, ProviderRef

InvestigationStatus = Literal["open", "paused", "closed"]
QuestionStatus = Literal["pending", "checking", "resolved", "blocked"]
MaterialStance = Literal["support", "oppose", "context", "uncertain"]
JudgmentAssessment = Literal["provisional", "unable_to_determine"]
ShortId = Annotated[str, Field(min_length=1, max_length=36)]


class InvestigationOriginCreate(RuntimeModel):
    run_id: ShortId
    result_hash: str = Field(min_length=64, max_length=64)
    candidate_key: str = Field(min_length=1, max_length=200)


class InvestigationCreate(RuntimeModel):
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    questions: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=32
    )
    origin: InvestigationOriginCreate | None = None
    is_demo: bool = False


class InvestigationQuestion(RuntimeModel):
    question_id: str
    text: str
    status: QuestionStatus = "pending"


class InvestigationQuestionUpdate(RuntimeModel):
    question_id: ShortId
    status: QuestionStatus


class InvestigationNoteCreate(RuntimeModel):
    expected_revision: int = Field(ge=1)
    kind: Literal["note"]
    text: str = Field(min_length=1, max_length=8000)


class InvestigationMaterialCreate(RuntimeModel):
    expected_revision: int = Field(ge=1)
    kind: Literal["material"]
    snapshot_id: ShortId
    record_ref: ProviderRef
    quote: str | None = Field(default=None, min_length=1, max_length=4000)
    stance: MaterialStance
    text: str = Field(min_length=1, max_length=8000)


class InvestigationJudgmentCreate(RuntimeModel):
    expected_revision: int = Field(ge=1)
    kind: Literal["judgment"]
    text: str = Field(min_length=1, max_length=8000)
    conditions: str = Field(default="", max_length=4000)
    assessment: JudgmentAssessment
    basis_entry_ids: list[ShortId] = Field(default_factory=list, max_length=128)


class InvestigationStatusCreate(RuntimeModel):
    expected_revision: int = Field(ge=1)
    kind: Literal["status"]
    status: InvestigationStatus
    text: str = Field(min_length=1, max_length=8000)
    question_updates: list[InvestigationQuestionUpdate] = Field(default_factory=list, max_length=32)


EntryRequest = Annotated[
    InvestigationNoteCreate
    | InvestigationMaterialCreate
    | InvestigationJudgmentCreate
    | InvestigationStatusCreate,
    Field(discriminator="kind"),
]


class InvestigationEntryCreate(RootModel[EntryRequest]):
    """Named schema with a flat, discriminated JSON request body."""


class InvestigationAutomation(RuntimeModel):
    source: Literal["monitor"] = "monitor"
    batch_id: str = Field(min_length=1, max_length=36)
    lead_key: str = Field(min_length=64, max_length=64)


class InvestigationNote(RuntimeModel):
    automation: InvestigationAutomation | None = None
    kind: Literal["note"] = "note"
    text: str


class InvestigationMaterial(RuntimeModel):
    automation: InvestigationAutomation | None = None
    kind: Literal["material"] = "material"
    text: str
    stance: MaterialStance
    snapshot_id: str
    snapshot_hash: str
    support: EvidenceSupport


class InvestigationJudgment(RuntimeModel):
    kind: Literal["judgment"] = "judgment"
    text: str
    conditions: str
    assessment: JudgmentAssessment
    basis_entry_ids: list[str]
    based_on_revision: int
    parent_judgment_entry_id: str | None


class InvestigationStatusChange(RuntimeModel):
    kind: Literal["status"] = "status"
    text: str
    status: InvestigationStatus
    question_updates: list[InvestigationQuestionUpdate]


class InvestigationEntry(RuntimeModel):
    id: str
    investigation_id: str
    revision: int
    kind: Literal["note", "material", "judgment", "status"]
    created_by: str
    created_at: datetime
    payload: Annotated[
        InvestigationNote | InvestigationMaterial | InvestigationJudgment | InvestigationStatusChange,
        Field(discriminator="kind"),
    ]


class InvestigationOrigin(RuntimeModel):
    run_id: str
    result_hash: str
    candidate_key: str
    snapshot_id: str
    snapshot_hash: str
    candidate: Candidate


class InvestigationSummary(RuntimeModel):
    id: str
    title: str
    question: str
    status: InvestigationStatus
    revision: int
    is_demo: bool
    created_by: str
    created_at: datetime
    updated_at: datetime
    needs_review: bool
    material_count: int
    judgment_count: int


class InvestigationView(InvestigationSummary):
    origin: InvestigationOrigin | None
    questions: list[InvestigationQuestion]
    entries: list[InvestigationEntry]
    latest_judgment_entry_id: str | None
    professional_status: Literal["not_reviewed"] = "not_reviewed"
    # This legacy field describes the shared WeKnora retrieval path.
    retrieval_status: Literal["not_connected"] = "not_connected"
    data_fabric_retrieval: Literal["bounded_cli_available"] = "bounded_cli_available"


class InvestigationList(RuntimeModel):
    items: list[InvestigationSummary]
    next_cursor: str | None = None
    unavailable_count: int = Field(default=0, ge=0)
    availability_note: str | None = None
