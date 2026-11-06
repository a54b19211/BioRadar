"""The typed state of one analysis run: what `analyses.progress` holds, key by key.

The run keeps its state as the JSON dict stored in `analyses.progress` and saves it after every stage and
investigation action; a restarted worker reads it back and goes on. The types below are TypedDicts over that
same dict, not a class that is converted to and from it, because the stored format is the contract:

- rows written by earlier versions must resume as they are, and a key or field that is absent there means
  something different from one present with a default (`used_previous` missing counts as "used"; an action
  without `evidence_gain` predates it), so every type is total=False and nothing fills defaults in;
- what is saved must stay byte-identical (tests/golden), so no layer may add, rename or reorder keys.

Every key the run writes is declared here; tests/test_run_state.py fails when a stored progress holds one that
is not. Shapes owned by another service's contract (BioCheck claims and coverage, MechEvidence's evaluation,
Fabric's search summaries) are typed as plain dicts here and documented by that contract."""

from typing import Any, Literal, TypedDict, cast

Basis = Literal["question", "prior_report", "exploratory"]


class Note(TypedDict):
    code: str
    detail: str
    stage: str


class RequestCount(TypedDict):
    n: int
    errors: int


class Hypothesis(TypedDict, total=False):
    id: str  # H1 ...
    statement: str
    alternatives: list[str]
    confirm: list[str]
    refute: list[str]


class Check(TypedDict, total=False):
    id: str  # Q1 ...
    text: str
    required: bool
    importance: float
    status: Literal["open", "answered", "conflict"]
    evidence: list[str]  # refs E1 ...
    objects: list[str]  # names of the question's objects this check is about
    exploratory: list[str]  # names not from the question that the check text mentions


class Query(TypedDict, total=False):
    terms: list[str]
    checks: list[str]
    origin: str  # plan | object:E3 | read:E3 | "" (older runs, retries)
    value: int  # 3 found object, 2 decisive record, 1 plan, 0 background record


class RemovedName(TypedDict):
    name: str
    basis: Basis
    checks: list[str]


class MechanismTarget(TypedDict):
    statement: str
    species: str
    system: str
    disease: str


class GraphCandidate(TypedDict):
    id: str | None
    name: str | None
    identity: str | None


class GraphRef(TypedDict):
    source: str
    record_id: str
    version_id: str
    basis: Literal["node", "bundle"]


class GraphEntry(TypedDict):
    object: str
    graph_records: list[str]
    candidates: list[GraphCandidate]
    refs: list[GraphRef]


class ActionArgs(TypedDict, total=False):
    terms: list[str]
    page: int
    record_id: str
    version_id: str
    browse: str


class Action(TypedDict, total=False):
    kind: Literal["search", "fetch", "next_page", "read"]
    source: str
    args: ActionArgs
    target_checks: list[str]
    gain: float
    status: Literal["completed", "failed"]
    origin: str
    found: int
    new: int
    ref: str
    evidence_gain: float
    error: str


class Sketch(TypedDict, total=False):
    matched: list[str]
    when: str | None
    duplicate_of: str


class Hit(TypedDict, total=False):
    source: str
    record_id: str
    title: str
    checks: list[str]
    critical: bool
    read: bool
    found_by: int
    version_id: str  # a hit pinned to one fixed version (a correction notice)
    sketch: Sketch


class Fact(TypedDict, total=False):
    id: str  # F1 ...
    ref: str
    text: str
    quote: str
    checks: list[str]
    stance: Literal["supports", "refutes", "limits", "context"]
    object: str
    hypothesis: str
    hypothesis_stance: Literal["supports", "refutes", "none"]


# "from" is a keyword, so this one is declared functionally
Relation = TypedDict(
    "Relation",
    {"from": str, "source": str, "to": str, "to_source": str, "to_version": str | None, "kind": str},
)


class Page(TypedDict, total=False):
    pages: int
    found: int
    checks: list[str]
    cursor: str | None
    new: bool | int
    at: str  # current mode: the time every later page of this search is read at


class HitPosition(TypedDict, total=False):
    version_id: str
    processor_version: str | None
    text_sha256: str | None
    range: list[int] | None
    text: str  # only when the hit came without a range


class FoundObject(TypedDict):
    name: str
    ref: str
    quote: str


class Stop(TypedDict):
    decision: str
    reason: str


class ComputedOperand(TypedDict):
    fact: str
    value: float
    ref: str
    quote: str


class Computation(TypedDict):
    id: str  # A1 ...
    label: str
    value: Any
    expression: str
    unit: str
    period: str
    population: str
    denominator: str
    operands: list[ComputedOperand]


class MechanismResult(TypedDict, total=False):
    status: Literal["completed", "failed", "not_run"]
    reason: str
    # status completed: the rest is MechEvidence's evaluation as returned (overall, rationale, gaps, ...)


class ReportVersion(TypedDict, total=False):
    step: str  # generate | revise_1 ...
    draft: str
    status: str  # unchecked until its check answered, then BioCheck's status
    claims: list[dict[str, Any]]
    error: str


class Report(TypedDict, total=False):
    status: str  # NOT_RUN | BioCheck's status of the last check
    draft: str
    revised: bool
    versions: list[ReportVersion]
    reason: str
    changed: Any
    claims: list[dict[str, Any]]  # BioCheck's sentence checks
    coverage: Any
    read_requests: list[Any]


class RunState(TypedDict, total=False):
    # plan
    used_previous: bool
    hypotheses: list[Hypothesis]
    objects: list[str]
    plan_basis: dict[str, Basis]
    plan_removed: list[RemovedName]
    checks: list[Check]
    queries: list[Query]
    judgments: list[str]
    needs_calculation: bool
    mechanism: MechanismTarget | None
    graph: list[GraphEntry]
    # investigate
    actions: list[Action]
    hits: list[Hit]
    facts: list[Fact]
    relations: list[Relation]
    pages: dict[str, Page]  # "source|term|term" -> paging of that search
    refs: dict[str, str]  # E1 -> "source|record_id"
    retries: int
    browse_at: dict[str, str]  # source -> time every page of its fixed material set is read at
    browsed: dict[str, list[int]]  # source -> pages of the fixed material set done
    hit_positions: dict[str, list[HitPosition]]  # "source|record_id" -> verified search-hit passages
    weknora: list[dict[str, Any]]  # each semantic search: its terms and Fabric's summary
    found_objects: list[FoundObject]
    stop: Stop
    # compute, mechanism, report, assess
    computations: list[Computation]
    mechanism_result: MechanismResult | None
    report: Report
    assessment: dict[str, Any] | None  # schemas.Assessment as dumped, with each item's BioCheck `check`
    # bookkeeping
    notes: list[Note]
    fabric_requests: dict[str, RequestCount]  # Fabric endpoint -> requests sent by all processes so far


def load(progress: dict | None) -> RunState:
    """The run state of a stored progress, as stored: a copy of the top level, nothing added or renamed."""
    return cast(RunState, dict(progress or {}))
