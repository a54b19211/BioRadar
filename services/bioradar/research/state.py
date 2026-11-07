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

from typing import Annotated, Any, Literal, TypedDict, cast

Basis = Literal["question", "prior_report", "exploratory"]


class Shown:
    """Marks a field the external views (analysis detail, list and export; analyses.view) may show. A field
    without it - every field added later, until someone decides - is left out of those views; app.redact
    then withholds, in the shown fields, what a source that cannot be read now makes unreadable."""

    def __repr__(self):
        return "SHOWN"


SHOWN = Shown()


class Note(TypedDict):
    code: Annotated[str, SHOWN]
    detail: Annotated[str, SHOWN]
    stage: Annotated[str, SHOWN]


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
    id: Annotated[str, SHOWN]  # Q1 ...
    text: Annotated[str, SHOWN]
    required: Annotated[bool, SHOWN]
    importance: Annotated[float, SHOWN]
    status: Annotated[Literal["open", "answered", "conflict"], SHOWN]
    evidence: Annotated[list[str], SHOWN]  # refs E1 ...
    objects: Annotated[list[str], SHOWN]  # names of the question's objects this check is about
    exploratory: Annotated[list[str], SHOWN]  # names not from the question that the check text mentions


class Query(TypedDict, total=False):
    terms: Annotated[list[str], SHOWN]
    checks: Annotated[list[str], SHOWN]
    origin: Annotated[str, SHOWN]  # plan | object:E3 | read:E3 | "" (older runs, retries)
    value: Annotated[int, SHOWN]  # 3 found object, 2 decisive record, 1 plan, 0 background record


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
    terms: Annotated[list[str], SHOWN]
    page: Annotated[int, SHOWN]
    record_id: Annotated[str, SHOWN]
    version_id: Annotated[str, SHOWN]
    browse: Annotated[str, SHOWN]


class Action(TypedDict, total=False):
    kind: Annotated[Literal["search", "fetch", "next_page", "read"], SHOWN]
    source: Annotated[str, SHOWN]
    args: Annotated[ActionArgs, SHOWN]
    target_checks: Annotated[list[str], SHOWN]
    gain: Annotated[float, SHOWN]
    status: Annotated[Literal["completed", "failed"], SHOWN]
    origin: Annotated[str, SHOWN]
    found: Annotated[int, SHOWN]
    new: Annotated[int, SHOWN]
    ref: Annotated[str, SHOWN]
    evidence_gain: Annotated[float, SHOWN]
    error: Annotated[str, SHOWN]


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
    id: Annotated[str, SHOWN]  # F1 ...
    ref: Annotated[str, SHOWN]
    text: Annotated[str, SHOWN]
    quote: Annotated[str, SHOWN]
    checks: Annotated[list[str], SHOWN]
    stance: Annotated[Literal["supports", "refutes", "limits", "context"], SHOWN]
    object: Annotated[str, SHOWN]
    hypothesis: Annotated[str, SHOWN]
    hypothesis_stance: Annotated[Literal["supports", "refutes", "none"], SHOWN]


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
    decision: Annotated[str, SHOWN]
    reason: Annotated[str, SHOWN]


class ComputedOperand(TypedDict):
    fact: Annotated[str, SHOWN]
    value: Annotated[float, SHOWN]
    ref: Annotated[str, SHOWN]
    quote: Annotated[str, SHOWN]


class Computation(TypedDict):
    id: Annotated[str, SHOWN]  # A1 ...
    label: Annotated[str, SHOWN]
    value: Annotated[Any, SHOWN]
    expression: Annotated[str, SHOWN]
    unit: Annotated[str, SHOWN]
    period: Annotated[str, SHOWN]
    population: Annotated[str, SHOWN]
    denominator: Annotated[str, SHOWN]
    operands: Annotated[list[ComputedOperand], SHOWN]


class MechanismResult(TypedDict, total=False):
    status: Literal["completed", "failed", "not_run"]
    reason: str
    # status completed: the rest is MechEvidence's evaluation as returned (overall, rationale, gaps, ...)


class ReportVersion(TypedDict, total=False):
    step: Annotated[str, SHOWN]  # generate | revise_1 ...
    draft: Annotated[str, SHOWN]
    status: Annotated[str, SHOWN]  # unchecked until its check answered, then BioCheck's status
    claims: Annotated[list[dict[str, Any]], SHOWN]
    error: Annotated[str, SHOWN]


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
    checks: Annotated[list[Check], SHOWN]
    queries: Annotated[list[Query], SHOWN]
    judgments: list[str]
    needs_calculation: bool
    mechanism: MechanismTarget | None
    graph: list[GraphEntry]
    # investigate
    actions: Annotated[list[Action], SHOWN]
    hits: list[Hit]
    facts: Annotated[list[Fact], SHOWN]
    relations: list[Relation]
    pages: dict[str, Page]  # "source|term|term" -> paging of that search
    refs: dict[str, str]  # E1 -> "source|record_id"
    retries: int
    browse_at: dict[str, str]  # source -> time every page of its fixed material set is read at
    browsed: dict[str, list[int]]  # source -> pages of the fixed material set done
    hit_positions: dict[str, list[HitPosition]]  # "source|record_id" -> verified search-hit passages
    weknora: list[dict[str, Any]]  # each semantic search: its terms and Fabric's summary
    found_objects: list[FoundObject]
    stop: Annotated[Stop, SHOWN]
    # compute, mechanism, report, assess
    computations: list[Computation]
    mechanism_result: MechanismResult | None
    report: Report
    assessment: dict[str, Any] | None  # schemas.Assessment as dumped, with each item's BioCheck `check`
    # bookkeeping
    notes: Annotated[list[Note], SHOWN]
    fabric_requests: dict[str, RequestCount]  # Fabric endpoint -> requests sent by all processes so far
    # which receipt of Fabric's source capabilities each execution planned with (audit, never a permission)
    capability_snapshots: list[dict[str, Any]]


class ResultReview(TypedDict, total=False):
    claims: Annotated[list[dict[str, Any]] | None, SHOWN]  # BioCheck's sentence checks of the last check
    coverage: Annotated[Any, SHOWN]
    read_requests: Annotated[Any, SHOWN]
    reason: Annotated[str | None, SHOWN]
    revised: Annotated[bool | None, SHOWN]
    versions: Annotated[list[ReportVersion] | None, SHOWN]


class Result(TypedDict, total=False):
    """What a finished run stores in `analyses.result` (Run.result(), plus what analyses.execute adds)."""

    report_status: Annotated[str, SHOWN]
    publishable: Annotated[bool, SHOWN]  # the publication gate (views.publication): only a PASS report
    hold: Annotated[str | None, SHOWN]  # pending_verification while not publishable
    hold_reason: Annotated[str | None, SHOWN]
    draft: Annotated[str, SHOWN]
    review: Annotated[ResultReview, SHOWN]
    assessment: Annotated[dict[str, Any] | None, SHOWN]
    checks: Annotated[list[Check], SHOWN]
    plan_basis: Annotated[dict[str, Basis], SHOWN]
    computations: Annotated[list[Computation], SHOWN]
    mechanism: Annotated[dict[str, Any] | None, SHOWN]  # MechanismResult with MechEvidence's evaluation
    stop: Annotated[Stop | None, SHOWN]
    reads: Annotated[int, SHOWN]
    notes: Annotated[list[Note], SHOWN]
    usage: Annotated[dict[str, int], SHOWN]
    source_updates: Annotated[Any, SHOWN]  # corrections that arrived during the run (analyses.execute)
    trace: Annotated[str, SHOWN]  # an unexpected failure's trace (analyses.execute)


def load(progress: dict | None) -> RunState:
    """The run state of a stored progress, as stored: a copy of the top level, nothing added or renamed."""
    return cast(RunState, dict(progress or {}))
