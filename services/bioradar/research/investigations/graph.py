"""Typed, bounded graph context for ordinary investigations.

Graph identities and relationships come from the provider. This module does
not resolve names, merge entities, infer a medical mechanism, or sign judgments.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from bioradar.research.engine.schemas import RuntimeModel
from bioradar.research.investigations.models import InvestigationView
from bioradar.research.mining.models import FrozenModel, ProviderRef, TimeExpression, canonical_hash
from bioradar.research.mining.prepare import time_interval

TextId = Annotated[str, Field(min_length=1, max_length=200)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class GraphIdentity(FrozenModel):
    namespace: str = Field(min_length=1, max_length=160)
    id: str = Field(min_length=1, max_length=400)

    @field_validator("namespace", "id")
    @classmethod
    def safe_identity(cls, value):
        if "|" in value or any(ord(character) < 33 for character in value):
            raise ValueError("graph identifiers cannot contain delimiters or controls")
        return value

    @property
    def key(self):
        return self.namespace, self.id

    @property
    def wire(self):
        return self.namespace + "|" + self.id


class GraphTime(FrozenModel):
    value: str = Field(min_length=4, max_length=40)
    precision: Literal["year", "month", "day", "instant"]
    meaning: str = Field(default="source_reported", max_length=80)

    @model_validator(mode="after")
    def valid_time(self):
        expression = self.expression()
        if time_interval(expression) is None:
            raise ValueError("invalid graph source time")
        return self

    def expression(self):
        return TimeExpression(
            raw=self.value,
            precision=self.precision,
            instant=datetime.fromisoformat(self.value.replace("Z", "+00:00"))
            if self.precision == "instant"
            else None,
        )


class GraphReview(FrozenModel):
    status: str = Field(min_length=1, max_length=100)
    reviewed_by: str | None = Field(default=None, max_length=200)
    reviewed_at: datetime | None = None

    @field_validator("reviewed_at")
    @classmethod
    def aware(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("review time requires a timezone")
        return value


class GraphDocumentRef(FrozenModel):
    source_id: str = Field(min_length=1, max_length=80)
    source_record_id: str = Field(min_length=1, max_length=400)
    version_id: Sha256
    raw_sha256: Sha256
    origin_doc_uuid: str | None = Field(default=None, max_length=400)

    @property
    def key(self):
        return self.source_id, self.source_record_id, self.version_id


class GraphLocator(FrozenModel):
    basis: Literal["clean_text"]
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("an evidence quotation must have a nonempty range")
        return self


class GraphEvidence(GraphDocumentRef):
    quote: str = Field(min_length=1, max_length=100_000)
    locator: GraphLocator


class GraphFieldEvidence(GraphDocumentRef):
    json_pointer: str = Field(min_length=1, max_length=2000, pattern=r"^/")
    value: Any


class GraphNode(GraphIdentity):
    entity_type: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=1000)
    aliases: tuple[str, ...] = ()
    external_ids: dict[str, Any] = Field(default_factory=dict)
    identity_status: Literal["confirmed", "rule_confirmed", "unresolved", "background"]
    identity_review_basis: str | None = None
    identity_rule: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)
    field_evidence: tuple[GraphFieldEvidence, ...] = Field(default=(), exclude_if=lambda value: not value)
    review: GraphReview
    mention_ids: tuple[str, ...] = ()
    evidence: tuple[GraphEvidence, ...] = ()


class GraphEdge(FrozenModel):
    id: str = Field(min_length=1, max_length=400)
    source: GraphIdentity
    target: GraphIdentity
    relation_type: str = Field(min_length=1, max_length=200)
    directed: Literal[True] = True
    relation_props: dict[str, Any] = Field(default_factory=dict)
    valid_from: GraphTime | None = None
    valid_to: GraphTime | None = None
    review: GraphReview
    binding_status: Literal["bound", "unbound", "background"]
    evidence: tuple[GraphEvidence, ...] = ()
    source_doc_uuid: str | None = None
    unbound_reason: str | None = None
    field_evidence: tuple[GraphFieldEvidence, ...] = Field(default=(), exclude_if=lambda value: not value)

    @field_validator("directed", mode="before")
    @classmethod
    def actual_boolean(cls, value):
        if value is not True:
            raise ValueError("the provider must preserve the asserted direction")
        return value


class GraphRedirect(FrozenModel):
    source: GraphIdentity
    target: GraphIdentity
    merge_log_id: str | int
    reviewed_by: str | None = None
    merged_at: datetime | None = None


class GraphBundle(FrozenModel):
    schema_version: Literal["datafabric.graph.bundle.v1"]
    graph_kind: Literal["reviewed", "background"]
    namespace: TextId
    is_demo: bool
    title: str = Field(min_length=1, max_length=1000)
    origin: Literal[
        "legacy_reviewed_export", "primekg_current_capture", "source_field_projection", "explicit_demo"
    ]
    scope_description: str = Field(min_length=1, max_length=4000)
    selected_document_uuids: tuple[str, ...] = ()
    scope_record_ids: tuple[str, ...] = ()
    scope_manifest_sha256: Sha256 | None = None
    scope_record_count: int | None = Field(default=None, ge=0)
    batch_offset: int = Field(default=0, ge=0)
    next_document_offset: int | None = None
    dependency_source_ids: tuple[str, ...] = ()
    document_refs: tuple[GraphDocumentRef, ...] = ()
    selection_truncated: bool = False
    nodes: tuple[GraphNode, ...] = Field(default=(), max_length=1000)
    edges: tuple[GraphEdge, ...] = Field(default=(), max_length=3000)
    redirects: tuple[GraphRedirect, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def coherent(self):
        GraphIdentity(namespace=self.namespace, id="namespace-check")
        nodes = {node.key: node for node in self.nodes}
        if len(nodes) != len(self.nodes) or len({edge.id for edge in self.edges}) != len(self.edges):
            raise ValueError("ambiguous graph identity")
        documents = {ref.key: ref for ref in self.document_refs}
        if len(documents) != len(self.document_refs):
            raise ValueError("duplicate graph document version")
        for node in self.nodes:
            if node.identity_status == "confirmed" and (
                node.review.status != "approved" or not node.identity_review_basis
            ):
                raise ValueError("confirmed graph identity requires the provider review basis")
            if node.identity_status == "rule_confirmed":
                rule = node.identity_rule or {}
                if (
                    self.origin != "source_field_projection"
                    or node.review.status != "rule_confirmed"
                    or not rule.get("method_version")
                    or rule.get("namespace") != node.namespace
                    or rule.get("value") != node.id
                    or not node.identity_review_basis
                ):
                    raise ValueError("rule-confirmed identity requires an explicit native-field rule")
                basis = GraphFieldEvidence.model_validate(rule.get("basis"))
                bound = documents.get(basis.key)
                if bound is None or bound.raw_sha256 != basis.raw_sha256 or basis.value != node.id:
                    raise ValueError("native identity rule is not bound to its fixed source value")
        for edge in self.edges:
            if edge.source.key not in nodes or edge.target.key not in nodes:
                raise ValueError("edge endpoint outside the fixed graph package")
            if edge.binding_status == "bound" and not edge.evidence and not edge.field_evidence:
                raise ValueError("bound relationship lacks a document quotation")
        for owner in (*self.nodes, *self.edges):
            for evidence in (*owner.evidence, *owner.field_evidence):
                bound = documents.get(evidence.key)
                if bound is None or bound.raw_sha256 != evidence.raw_sha256:
                    raise ValueError("relationship evidence is not a declared document version")
        if self.origin == "explicit_demo" and not self.is_demo:
            raise ValueError("explicit fixture must remain synthetic")
        if self.origin == "primekg_current_capture" and self.graph_kind != "background":
            raise ValueError("current PrimeKG is background only")
        if self.graph_kind == "background" and (
            self.document_refs
            or self.dependency_source_ids
            or any(node.identity_status != "background" for node in self.nodes)
            or any(edge.binding_status != "background" for edge in self.edges)
        ):
            raise ValueError("background graph cannot manufacture confirmed identities or evidence")
        return self


class GraphReference(GraphDocumentRef):
    is_demo: bool
    first_seen_at: datetime

    @field_validator("first_seen_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("server first observation requires a timezone")
        return value


class GraphCoverage(FrozenModel):
    scope: Literal["selected_published_subgraph"]
    truncated: bool
    selection_truncated: bool = False
    stop_reason: str | None = None
    returned_nodes: int = Field(ge=0, le=100)
    returned_edges: int = Field(ge=0, le=200)
    max_depth: int = Field(ge=0, le=2)


class GraphNeighborhood(FrozenModel):
    schema_version: Literal["datafabric.graph.neighborhood.v1"]
    graph_ref: GraphReference
    namespace: TextId
    nodes: tuple[GraphNode, ...] = Field(default=(), max_length=100)
    edges: tuple[GraphEdge, ...] = Field(default=(), max_length=200)
    redirects: tuple[GraphRedirect, ...] = ()
    seeds_found: tuple[GraphIdentity, ...] = ()
    seeds_missing: tuple[GraphIdentity, ...] = ()
    coverage: GraphCoverage
    limitations: tuple[str, ...] = ()


class GraphResolution(FrozenModel):
    schema_version: Literal["datafabric.graph.resolution.v1"]
    graph_ref: GraphReference
    query: dict[str, str | None]
    status: Literal["resolved", "ambiguous", "not_found"]
    candidates: tuple[GraphNode, ...] = Field(default=(), max_length=20)
    truncated: bool
    limitations: tuple[str, ...] = ()


class GraphPackageSummary(RuntimeModel):
    source_id: str
    source_record_id: str
    version_id: str
    is_demo: bool
    title: str
    namespace: str
    graph_kind: Literal["reviewed", "background"]
    scope_description: str
    selection_truncated: bool


class GraphPackageUnavailable(RuntimeModel):
    source_id: str
    source_record_id: str
    version_id: str
    reason: str


class GraphPackageList(RuntimeModel):
    items: list[GraphPackageSummary]
    unavailable_packages: list[GraphPackageUnavailable] = Field(default_factory=list)
    next_cursor: str | None
    as_of: datetime
    limitations: list[str]


class GraphInvestigationCreate(RuntimeModel):
    title: str = Field(default="图谱引导的有界调查", min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    source_id: str = Field(min_length=1, max_length=80)
    source_record_id: str = Field(min_length=1, max_length=400)
    graph_version_id: Sha256
    is_demo: bool = False
    seed_ids: tuple[str, ...] = Field(default=(), max_length=20)
    seed_names: tuple[Annotated[str, Field(min_length=1, max_length=120)], ...] = Field(
        default=(), max_length=3
    )
    relation_types: tuple[str, ...] = Field(default=(), max_length=16)
    direction: Literal["out", "in", "both"] = "both"
    max_depth: int = Field(default=1, ge=1, le=2)
    max_nodes: int = Field(default=40, ge=1, le=100)
    max_edges: int = Field(default=60, ge=1, le=200)
    max_followups: int = Field(default=2, ge=0, le=2)
    max_evidence_records: int = Field(default=4, ge=0, le=4)
    topic_terms: tuple[Annotated[str, Field(min_length=2, max_length=120)], ...] = Field(
        default=(), max_length=2
    )
    search_clinicaltrials: bool = True
    compare_version_id: Sha256 | None = None
    investigation_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_revision: int | None = Field(default=None, ge=1)
    mode: Literal["current", "operational_replay", "public_history_reconstruction"] = "current"
    cutoff: datetime | None = None

    @model_validator(mode="after")
    def qualifications(self):
        if bool(self.seed_ids) == bool(self.seed_names):
            raise ValueError("provide explicit IDs or provider-resolved names, not both")
        if any(not name.strip() for name in self.seed_names):
            raise ValueError("a source name cannot be blank")
        identities = [
            GraphIdentity(namespace=value.split("|", 1)[0], id=value.split("|", 1)[1])
            for value in self.seed_ids
            if "|" in value
        ]
        if len(identities) != len(self.seed_ids) or len({item.key for item in identities}) != len(identities):
            raise ValueError("explicit, unique namespace|id seeds are required")
        if len(set(self.relation_types)) != len(self.relation_types):
            raise ValueError("duplicate relation type")
        if any(not value.strip() or len(value) > 200 for value in self.relation_types):
            raise ValueError("invalid relation type")
        if (self.investigation_id is None) != (self.expected_revision is None):
            raise ValueError("existing investigation requires its current revision")
        if self.cutoff is not None and (self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None):
            raise ValueError("cutoff requires a timezone")
        if self.mode != "current" and self.cutoff is None:
            raise ValueError("historical graph reads require an explicit cutoff")
        return self


class GraphFollowUp(FrozenModel):
    edge_id: str
    source: GraphIdentity
    target: GraphIdentity
    relation_type: str
    neighbor: GraphIdentity
    neighbor_name: str
    provider_review_status: str
    binding_status: Literal["bound", "unbound", "background"]
    question: str
    search_terms: tuple[str, ...] = ()


class GraphInvestigationResult(RuntimeModel):
    schema_version: Literal["bioradar.graph-investigation.v1"] = "bioradar.graph-investigation.v1"
    execution_id: str
    investigation: InvestigationView
    completion_status: Literal[
        "bounded_context_saved", "partial", "read_failed", "empty_neighborhood", "awaiting_seed_selection"
    ]
    graph_snapshot_id: str | None = None
    mining_snapshot_id: str | None = None
    graph_ref: ProviderRef | None = None
    followups: list[GraphFollowUp] = Field(default_factory=list)
    reads: list[dict[str, Any]] = Field(default_factory=list)
    comparison: dict[str, Any] | None = None
    qualification_gaps: list[str] = Field(default_factory=list)
    seed_resolutions: list[GraphResolution] = Field(default_factory=list)


def validate_neighborhood(bundle, neighborhood, record, request):
    """The response must be a literal subset of the immutable adopted graph."""
    ref = neighborhood.graph_ref
    if (
        ref.source_id != record["source_id"]
        or ref.source_record_id != record["source_record_id"]
        or ref.version_id != record["version_id"]
        or ref.raw_sha256 != record["raw_sha256"]
        or ref.is_demo is not record["is_demo"]
        or neighborhood.namespace != bundle.namespace
        or neighborhood.coverage.returned_nodes != len(neighborhood.nodes)
        or neighborhood.coverage.returned_edges != len(neighborhood.edges)
        or len(neighborhood.nodes) > request.max_nodes
        or len(neighborhood.edges) > request.max_edges
        or neighborhood.coverage.max_depth > request.max_depth
    ):
        raise ValueError("graph projection does not match the selected immutable version")
    original_nodes = {node.key: node for node in bundle.nodes}
    original_edges = {edge.id: edge for edge in bundle.edges}
    returned_nodes = {node.key: node for node in neighborhood.nodes}
    if len(returned_nodes) != len(neighborhood.nodes) or len({e.id for e in neighborhood.edges}) != len(
        neighborhood.edges
    ):
        raise ValueError("duplicate neighborhood identity")
    if any(original_nodes.get(node.key) != node for node in neighborhood.nodes):
        raise ValueError("modified graph node")
    if any(original_edges.get(edge.id) != edge for edge in neighborhood.edges):
        raise ValueError("modified graph edge")
    if any(
        edge.source.key not in returned_nodes or edge.target.key not in returned_nodes
        for edge in neighborhood.edges
    ):
        raise ValueError("neighborhood omitted an edge endpoint")
    if any(redirect not in bundle.redirects for redirect in neighborhood.redirects):
        raise ValueError("modified identity redirect")
    requested = {tuple(value.split("|", 1)) for value in request.seed_ids}
    found = {identity.key for identity in neighborhood.seeds_found}
    missing = {identity.key for identity in neighborhood.seeds_missing}
    if found & missing or found | missing != requested or any(key not in returned_nodes for key in found):
        raise ValueError("seed coverage is not explicit")
    if bundle.selection_truncated and not neighborhood.coverage.truncated:
        raise ValueError("provider sampling cannot become complete neighborhood coverage")
    return neighborhood


def select_graph_followups(neighborhood, *, topic_terms=(), max_followups=2):
    """Ask about actual adjacent relationships, without inventing entity alignment."""
    if not 0 <= max_followups <= 2:
        raise ValueError("at most two graph-derived questions")
    if max_followups == 0:
        return []
    nodes = {node.key: node for node in neighborhood.nodes}
    seeds = {node.key for node in neighborhood.seeds_found}
    selected, used = [], set()

    def source_order(edge):
        other = edge.target if edge.source.key in seeds else edge.source
        tier = (
            0
            if edge.review.status == "approved" and edge.binding_status == "bound"
            else 1
            if edge.binding_status == "bound"
            else 2
        )
        return tier, other.namespace, other.id, edge.relation_type, edge.source.key, edge.target.key, edge.id

    # Source review/binding and identity are transparent tie-breakers, not an
    # estimate of scientific importance, clinical benefit or business priority.
    for edge in sorted(neighborhood.edges, key=source_order):
        if edge.source.key in seeds:
            other = nodes[edge.target.key]
        elif edge.target.key in seeds:
            other = nodes[edge.source.key]
        else:
            continue
        if other.key in used:
            continue
        source, target = nodes[edge.source.key], nodes[edge.target.key]
        terms = tuple(dict.fromkeys((*topic_terms, other.name)))
        if any(not 2 <= len(term.strip()) <= 120 for term in terms):
            terms = ()
        selected.append(
            GraphFollowUp(
                edge_id=edge.id,
                source=edge.source,
                target=edge.target,
                relation_type=edge.relation_type,
                neighbor=GraphIdentity(namespace=other.namespace, id=other.id),
                neighbor_name=other.name,
                provider_review_status=edge.review.status,
                binding_status=edge.binding_status,
                question=(
                    f"源图将“{source.name}”通过“{edge.relation_type}”有向关联到“{target.name}”。"
                    "所引原文具体说了什么，适用条件和相反材料有哪些？"
                    "图中关联、登记命中均不能直接证明疗效、机制或战略价值。"
                ),
                search_terms=terms,
            )
        )
        used.add(other.key)
        if len(selected) == max_followups:
            break
    return selected


def compare_graph_versions(before, after):
    """Compare two adopted graph packages, not real-world relation births/deaths."""
    if before.namespace != after.namespace or before.is_demo is not after.is_demo:
        raise ValueError("graph version comparison requires one namespace and data class")
    result = {}
    for label, old, new, key in (
        ("nodes", before.nodes, after.nodes, lambda item: item.key),
        ("edges", before.edges, after.edges, lambda item: item.id),
    ):
        a, b = {key(item): item for item in old}, {key(item): item for item in new}
        result[label] = {
            "added_to_this_package": len(b.keys() - a.keys()),
            "not_in_this_package": len(a.keys() - b.keys()),
            "changed": sum(a[k] != b[k] for k in a.keys() & b.keys()),
            "unchanged": sum(a[k] == b[k] for k in a.keys() & b.keys()),
        }
    result["identity_redirects_changed"] = canonical_hash(
        before.model_dump(mode="json")["redirects"]
    ) != canonical_hash(after.model_dump(mode="json")["redirects"])
    result["scope_changed"] = before.scope_description != after.scope_description
    result["limitation"] = (
        "Package differences are not newly occurring studies or relation deletions; "
        "provider coverage, identity corrections, and review changes require checking."
    )
    return result


def run_graph_investigation(engine, actor, reader, request, *, execution_id, clinical_reader=None):
    """Save a bounded graph-guided pass; writes are never caught and replayed.

    Only source reads are converted into explicit gaps. Database write failures
    propagate so a caller can inspect its durable execution receipt before retry.
    """
    import json
    from datetime import UTC, datetime
    from uuid import uuid4

    from bioradar.adapters.fabric_graph import (
        adopted_graph_snapshot,
        read_graph_evidence,
        read_graph_version,
        read_neighborhood,
        resolve_graph_names,
    )
    from bioradar.api.errors import DomainError
    from bioradar.research.history.facts import summarize_records
    from bioradar.research.investigations.digging import _quote, summarize_trials
    from bioradar.research.investigations.models import (
        InvestigationCreate,
        InvestigationEntryCreate,
    )
    from bioradar.research.investigations.service import InvestigationService

    if not execution_id or len(execution_id) > 160:
        raise ValueError("a bounded durable execution key is required")
    cutoff = request.cutoff or datetime.now(UTC)
    if cutoff > datetime.now(UTC):
        raise DomainError("graph_future_cutoff", "图谱调查截止时间不能在未来", 422)
    instance = (
        (engine.settings.data_fabric_demo_source_instance or "fixture")
        if request.is_demo
        else reader.source_instance
    )
    if (
        instance not in actor.source_allowlist
        or request.source_id not in engine.settings.engine_source_id_allowlist.get(instance, [])
    ):
        raise DomainError("source_not_authorized", "图谱来源不在当前采用许可范围", 403)
    # One request-scoped current catalog also protects old snapshots/materials.
    engine.authorize_history_source(request.source_id)
    runtime = InvestigationService(engine)
    if request.investigation_id is not None:
        current = runtime.get(actor, request.investigation_id)
        if current.revision != request.expected_revision:
            raise DomainError("revision_conflict", "调查已更新，请核对后使用当前版本", 409)
        if current.is_demo is not request.is_demo:
            raise DomainError("fixture_scope_mismatch", "真实与测试图谱材料不得混用", 422)
        view = current.model_dump(mode="json")
    else:
        _, view = runtime.create(
            actor,
            InvestigationCreate(
                title=request.title,
                question=request.question,
                questions=[
                    "核对固定图包范围、原始审核状态和身份依据",
                    "由实际关系提出问题，回读精确版本原文并记录未查清项",
                    "补充独立反面资料与专业核验，再由研究人员更新判断",
                ],
                is_demo=request.is_demo,
            ),
            "graph:" + canonical_hash(execution_id),
        )
        current = runtime.get(actor, view["id"])
        if current.revision > 1:
            raise DomainError(
                "graph_execution_already_started",
                "该执行已有调查记录；请核对，不会自动重跑图查询。",
                409,
            )
        view = current.model_dump(mode="json")

    report = {
        "execution_id": execution_id,
        "investigation": view,
        "completion_status": "read_failed",
        "graph_snapshot_id": None,
        "mining_snapshot_id": None,
        "graph_ref": None,
        "followups": [],
        "reads": [],
        "comparison": None,
        "qualification_gaps": [
            "graph_context_is_not_a_new_independent_study_or_professional_assessment",
            "no_automatic_support_oppose_or_human_judgment",
            "weknora_and_professional_evaluation_not_connected",
            "followup_order_is_source_review_binding_then_source_identity_not_importance",
        ],
    }

    def append(kind, **payload):
        nonlocal view
        _, view = runtime.append(
            actor,
            view["id"],
            InvestigationEntryCreate.model_validate(
                {"kind": kind, "expected_revision": view["revision"], **payload}
            ),
            str(uuid4()),
        )

    def note(text):
        if len(text) > 8000:
            text = text[:7900] + "\n[本条展示达到保存上限；完整采用原件与版本引用保留。]"
        append("note", text=text)

    def read_failure(exc):
        return exc.code if isinstance(exc, DomainError) else "graph_source_read_failed"

    def finish(status):
        report["completion_status"] = status
        report["qualification_gaps"] = list(dict.fromkeys(report["qualification_gaps"]))
        note(
            "程序图谱调查收据（不是专业判断）：\n"
            + json.dumps(
                {
                    "execution_id": execution_id,
                    "status": status,
                    "graph_snapshot_id": report["graph_snapshot_id"],
                    "mining_snapshot_id": report["mining_snapshot_id"],
                    "read_statuses": [
                        {k: value for k, value in row.items() if k not in {"summary", "source_quote"}}
                        for row in report["reads"]
                    ],
                    "qualification_gaps": report["qualification_gaps"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        append(
            "status",
            status="paused",
            text="本次有界图谱查证已停止，状态："
            + status
            + "。保留原图、材料和旧判断，等待人工核查；未自动生成判断。",
        )
        report["investigation"] = InvestigationView.model_validate(view)
        return GraphInvestigationResult.model_validate(report)

    append(
        "note",
        text="程序开始有界图谱追查。执行标识："
        + execution_id
        + "\n只采用指定图版本；最多2个派生问题、4份绑定原件、每次标题查证最多3份资料。"
        + "查不到、不可用、读取失败和未核实分别记录，未知写入结果不自动重发。",
    )
    record = bundle = neighborhood = None
    error = None
    try:
        record, bundle = read_graph_version(reader, request, cutoff)
        if request.seed_names:
            resolved_ids, resolutions = resolve_graph_names(reader, record, bundle, request, cutoff)
            report["seed_resolutions"] = [value.model_dump(mode="json") for value in resolutions]
            if resolved_ids:
                request = request.model_copy(update={"seed_ids": resolved_ids, "seed_names": ()})
            else:
                neighborhood = None
        if not request.seed_names:
            neighborhood = read_neighborhood(reader, record, bundle, request, cutoff)
    except Exception as exc:
        error = read_failure(exc)
    if error:
        report["reads"].append({"kind": "graph", "status": "failed", "error_code": error})
        report["qualification_gaps"].append("graph_version_or_neighborhood_not_read")
        return finish("read_failed")

    if neighborhood is None:
        snapshot = adopted_graph_snapshot(reader, record, bundle)
        _, adopted = engine.adopt_snapshot(actor, snapshot, str(uuid4()))
        report["graph_snapshot_id"] = adopted["id"]
        report["graph_ref"] = reader.ref(record)
        append(
            "material",
            snapshot_id=adopted["id"],
            record_ref=reader.ref(record).model_dump(mode="json"),
            quote=None,
            stance="context",
            text="已采用固定图包，但名称未唯一定位；没有自动选择首项或执行邻域追查。\n"
            + bundle.title
            + "\n版本："
            + record["version_id"],
        )
        note(
            "源系统名称候选，须研究人员明确选择：\n"
            + json.dumps(report["seed_resolutions"], ensure_ascii=False, indent=2)
        )
        report["qualification_gaps"].append("graph_seed_ambiguous_or_not_found_select_explicit_identity")
        return finish("awaiting_seed_selection")

    snapshot = adopted_graph_snapshot(reader, record, bundle)
    _, adopted = engine.adopt_snapshot(actor, snapshot, str(uuid4()))
    report["graph_snapshot_id"] = adopted["id"]
    report["graph_ref"] = reader.ref(record)
    node_names = {node.key: node.name for node in neighborhood.nodes}
    lines = [
        "程序图谱背景材料；不是医学、机制或投资判断。",
        f"固定来源：{record['source_id']} / {record['source_record_id']}",
        f"采用版本：{record['version_id']}",
        f"范围：{bundle.scope_description}",
        f"图类型：{bundle.graph_kind}；实际读取节点{len(neighborhood.nodes)}，物理边{len(neighborhood.edges)}。",
        "正反向物理边和多个节点都不是独立研究数量，箭头不自动表示因果。",
        "源审核状态和身份资格：",
    ]
    lines.extend(
        f"- {node.name[:160]} [{node.namespace}|{node.id}]："
        f"review={node.review.status}；identity={node.identity_status}；"
        f"basis={node.identity_review_basis or 'unknown'}"
        for node in neighborhood.nodes[:15]
    )
    lines.append("实际关系（更多条目以固定原件为准）：")
    lines.extend(
        f"- {node_names[edge.source.key][:100]} --{edge.relation_type}--> "
        f"{node_names[edge.target.key][:100]}；review={edge.review.status}；"
        f"binding={edge.binding_status}；"
        f"未绑定原因={edge.unbound_reason or 'none supplied'}"
        for edge in neighborhood.edges[:12]
    )
    lines.append("投影是否截断：" + str(neighborhood.coverage.truncated))
    append(
        "material",
        snapshot_id=adopted["id"],
        record_ref=reader.ref(record).model_dump(mode="json"),
        quote=None,
        stance="context",
        text="\n".join(lines)[:8000],
    )
    report["reads"].append(
        {
            "kind": "graph",
            "status": "adopted",
            "source_id": record["source_id"],
            "version_id": record["version_id"],
            "node_count": len(neighborhood.nodes),
            "physical_edge_count": len(neighborhood.edges),
            "coverage": neighborhood.coverage.model_dump(mode="json"),
        }
    )
    report["qualification_gaps"].extend((*bundle.limitations, *neighborhood.limitations))
    if bundle.graph_kind == "background":
        report["qualification_gaps"].append(
            "background_graph_is_received_context_not_external_past_proof_or_document_evidence"
        )
    if any(node.identity_status != "confirmed" for node in neighborhood.nodes):
        report["qualification_gaps"].append("unconfirmed_or_background_graph_identities")
    if any(edge.binding_status != "bound" for edge in neighborhood.edges):
        report["qualification_gaps"].append("relationships_without_source_document_binding")
    if neighborhood.seeds_missing:
        report["qualification_gaps"].append("some_explicit_seed_identities_missing")
    partial = neighborhood.coverage.truncated

    if request.compare_version_id:
        error = None
        try:
            _, prior = read_graph_version(reader, request, cutoff, version_id=request.compare_version_id)
            comparison = compare_graph_versions(prior, bundle)
        except Exception as exc:
            error = read_failure(exc)
        if error:
            report["reads"].append({"kind": "comparison", "status": "failed", "error_code": error})
            partial = True
        else:
            report["comparison"] = comparison
            note(
                "程序固定图版本比较；差异不代表新研究或真实关系删除：\n"
                + json.dumps(comparison, ensure_ascii=False, indent=2)
            )

    plans = select_graph_followups(
        neighborhood, topic_terms=request.topic_terms, max_followups=request.max_followups
    )
    report["followups"] = plans
    for plan in plans:
        note(
            "由已采用图关系触发的后续问题（原文绑定/来源审核优先，其余按来源身份排序，不是重要性评分）：\n"
            + plan.question
            + "\n固定图版本："
            + record["version_id"]
            + "\n原关系ID："
            + plan.edge_id
            + "\n原始方向："
            + plan.source.wire
            + " -> "
            + plan.target.wire
            + "\n原审核状态："
            + plan.provider_review_status
            + "\n绑定状态："
            + plan.binding_status
            + "\n待查标题词："
            + json.dumps(plan.search_terms, ensure_ascii=False)
        )
    selected_edges = {plan.edge_id for plan in plans}
    references = [
        evidence for edge in neighborhood.edges if edge.id in selected_edges for evidence in edge.evidence
    ]
    references.extend(evidence for node in neighborhood.nodes for evidence in node.evidence)
    references.extend(bundle.document_refs)
    groups = {}
    for reference in references:
        groups.setdefault(reference.key, []).append(reference)
    documents = []
    for key, refs in list(groups.items())[: request.max_evidence_records]:
        source_id, native_id, version_id = key
        error = None
        try:
            document = read_graph_evidence(
                reader, refs[0], is_demo=request.is_demo, mode=request.mode, cutoff=cutoff
            )
            for reference in refs[1:]:
                if hasattr(reference, "locator"):
                    loc = reference.locator
                    if (
                        document["raw_sha256"] != reference.raw_sha256
                        or loc.end > len(document["clean_text"])
                        or document["clean_text"][loc.start : loc.end] != reference.quote
                    ):
                        raise DomainError("graph_quote_mismatch", "图引用原文不一致", 502)
        except Exception as exc:
            error = read_failure(exc)
        receipt = {
            "kind": "graph_document",
            "source_id": source_id,
            "source_record_id": native_id,
            "version_id": version_id,
        }
        if error:
            receipt.update(status="failed", error_code=error)
            report["reads"].append(receipt)
            partial = True
            continue
        document_snapshot = reader.snapshot(
            {
                "items": [document],
                "source_id": source_id,
                "is_demo": request.is_demo,
                "coverage": {"scope_description": "Exact graph-linked source version; no global coverage."},
            }
        )
        _, adopted_document = engine.adopt_snapshot(actor, document_snapshot, str(uuid4()))
        quote = next((ref.quote for ref in refs if hasattr(ref, "quote") and len(ref.quote) <= 4000), None)
        summary = summarize_records([document], limit=1)
        if quote is None and document["clean_text"]:
            quote = document["clean_text"][:700]
        text = (
            "由固定图包回读的来源原件；不自动证明图中关系或对命题的支持/反面。\n"
            f"来源：{source_id} / {native_id}\n版本：{version_id}\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)
            + "\n原文节选（完整内容以采用原件为准）：\n"
            + document["clean_text"][:1000]
        )
        append(
            "material",
            snapshot_id=adopted_document["id"],
            record_ref=reader.ref(document).model_dump(mode="json"),
            quote=quote,
            stance="context",
            text=text[:8000],
        )
        documents.append(document)
        receipt.update(
            status="adopted",
            snapshot_id=adopted_document["id"],
            verified_quotation_count=sum(hasattr(ref, "quote") for ref in refs),
        )
        report["reads"].append(receipt)
    if len(groups) > request.max_evidence_records:
        report["qualification_gaps"].append("graph_evidence_record_budget_exhausted")
        partial = True

    if documents:
        projected = adopted_graph_snapshot(
            reader, record, bundle, neighborhood=neighborhood, documents=documents
        )
        if projected.records[0].relations:
            _, adopted_projection = engine.adopt_snapshot(actor, projected, str(uuid4()))
            report["mining_snapshot_id"] = adopted_projection["id"]
            note(
                "已生成可供既有挖掘方法检查的有限关系采用包："
                + adopted_projection["id"]
                + "。仅含身份、原文绑定、人工关系审核、关系类型与时间均合格的边；"
                "是否可执行及其含义仍由原方法门槛决定，未自动生成研究结论。"
            )

    if request.search_clinicaltrials and plans and request.mode == "current" and clinical_reader is not None:
        try:
            engine.authorize_history_source("clinicaltrials")
        except DomainError as exc:
            report["reads"].append(
                {"kind": "clinicaltrials_permission", "status": "unavailable", "error_code": exc.code}
            )
            clinical_reader = None
            partial = True
    if request.search_clinicaltrials and plans:
        if request.mode != "current":
            report["qualification_gaps"].append("current_title_search_not_used_as_historical_evidence")
        elif clinical_reader is None:
            report["qualification_gaps"].append("clinicaltrials_title_reader_not_connected_or_not_authorized")
        elif request.is_demo:
            report["qualification_gaps"].append("real_title_search_not_mixed_into_synthetic_investigation")
        else:
            # Share this pass's existing bounded deadline; there is no second job.
            clinical_reader.deadline = min(clinical_reader.deadline, reader.deadline)
            seen = set()
            for plan in plans:
                if not plan.search_terms:
                    report["qualification_gaps"].append("neighbor_name_outside_literal_title_query_budget")
                    continue
                error = None
                try:
                    found = clinical_reader.search(plan.search_terms, limit=3)
                except Exception as exc:
                    error = read_failure(exc)
                receipt = {
                    "kind": "clinicaltrials_title_query",
                    "edge_id": plan.edge_id,
                    "terms": list(plan.search_terms),
                    "scope": "title_literal_and_current_db",
                }
                if error:
                    receipt.update(status="failed", error_code=error)
                    report["reads"].append(receipt)
                    partial = True
                    continue
                receipt.update(
                    status="empty" if not found["items"] else "read",
                    returned=len(found["items"]),
                    has_more=found["has_more"],
                    adopted=0,
                    projections_unavailable=0,
                )
                if found["has_more"]:
                    partial = True
                for item in found["items"]:
                    if item["uuid"] in seen:
                        continue
                    seen.add(item["uuid"])
                    error = None
                    try:
                        document = clinical_reader.read_document(item["uuid"])
                        document_snapshot = (
                            clinical_reader.snapshot_from_documents([document])
                            if document["projection"]["status"] == "available"
                            else None
                        )
                    except Exception as exc:
                        error = read_failure(exc)
                    if error:
                        report["reads"].append(
                            {
                                "kind": "clinicaltrials_document",
                                "status": "failed",
                                "document_uuid": item["uuid"],
                                "error_code": error,
                            }
                        )
                        partial = True
                        continue
                    if document_snapshot is None:
                        receipt["projections_unavailable"] += 1
                        partial = True
                        report["qualification_gaps"].append("clinicaltrials_protocol_projection_unavailable")
                        report["reads"].append(
                            {
                                "kind": "clinicaltrials_document",
                                "status": "unavailable",
                                "document_uuid": item["uuid"],
                                "reason": document["projection"].get("reason"),
                            }
                        )
                        continue
                    _, adopted_document = engine.adopt_snapshot(actor, document_snapshot, str(uuid4()))
                    summary = summarize_trials([document])
                    append(
                        "material",
                        snapshot_id=adopted_document["id"],
                        record_ref=document_snapshot.records[0].record_ref.model_dump(mode="json"),
                        quote=_quote(document),
                        stance="context",
                        text=(
                            "由真实图邻居名称触发的有限标题查证；命中不确认图关系，"
                            "登记状态不代表疗效或安全性结论。\n"
                            + json.dumps(summary["rows"][0], ensure_ascii=False, indent=2)
                        )[:8000],
                    )
                    receipt["adopted"] += 1
                report["reads"].append(receipt)
                note("图关系触发的标题查证收据：\n" + json.dumps(receipt, ensure_ascii=False, indent=2))
            report["qualification_gaps"].append(
                "title_search_is_limited_current_registry_context_not_independent_evidence_or_negative_result"
            )
    return finish(
        "partial" if partial else "bounded_context_saved" if neighborhood.edges else "empty_neighborhood"
    )
