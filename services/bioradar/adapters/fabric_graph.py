"""Read a fixed graph version and its bounded projection through the history API."""

from contextlib import contextmanager

import httpx
from pydantic import ValidationError

from bioradar.adapters.fabric_history import FabricHistoryReader, history_error, time_upper
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.investigations.graph import GraphBundle, GraphNeighborhood, validate_neighborhood
from bioradar.research.mining.models import (
    MiningEntity,
    Relation,
    SectionProvenance,
    SnapshotCoverage,
    canonical_hash,
)


@contextmanager
def graph_reader(settings):
    if settings.data_fabric_url is None or settings.data_fabric_history_read_token is None:
        raise DomainError("graph_not_connected", "尚未配置图谱版本读取服务和只读许可", 503)
    with httpx.Client(
        base_url=settings.data_fabric_url,
        trust_env=False,
        headers={"Authorization": "Bearer " + settings.data_fabric_history_read_token.get_secret_value()},
    ) as client:
        yield FabricHistoryReader(
            client,
            authorization_ref=DevelopmentAccess(settings).actor().grant_ref,
            max_records=24,
            max_bytes=8_000_000,
            max_seconds=60,
        )


def read_graph_version(reader, request, cutoff, *, version_id=None):
    record = reader.read_version(
        {
            "source_id": request.source_id,
            "source_record_id": request.source_record_id,
            "version_id": version_id or request.graph_version_id,
            "is_demo": request.is_demo,
        },
        mode=request.mode,
        cutoff=cutoff,
    )
    try:
        if record["record_kind"] != "graph":
            raise ValueError("not a graph package")
        bundle = GraphBundle.model_validate_json(record["raw_content"])
        if bundle.is_demo is not request.is_demo:
            raise ValueError("graph data class differs from its adopted version")
    except (ValueError, ValidationError):
        raise history_error() from None
    return record, bundle


def read_neighborhood(reader, record, bundle, request, cutoff):
    params = {
        "source_id": request.source_id,
        "source_record_id": request.source_record_id,
        "is_demo": str(request.is_demo).lower(),
        "mode": request.mode,
        "cutoff": cutoff.isoformat(),
        "seed_ids": list(request.seed_ids),
        "relation_types": list(request.relation_types),
        "direction": request.direction,
        "max_depth": request.max_depth,
        "max_nodes": request.max_nodes,
        "max_edges": request.max_edges,
    }
    value = reader._get("/api/history/graph/" + request.graph_version_id + "/neighborhood", params)
    try:
        return validate_neighborhood(bundle, GraphNeighborhood.model_validate(value), record, request)
    except (ValueError, ValidationError, KeyError, TypeError):
        raise history_error() from None


def read_graph_evidence(reader, reference, *, is_demo, mode, cutoff):
    record = reader.read_version(
        {**reference.model_dump(mode="json"), "is_demo": is_demo},
        mode=mode,
        cutoff=cutoff,
    )
    if record["raw_sha256"] != reference.raw_sha256:
        raise history_error()
    if hasattr(reference, "locator"):
        locator = reference.locator
        text = record["clean_text"]
        if locator.end > len(text) or text[locator.start : locator.end] != reference.quote:
            raise DomainError("graph_quote_mismatch", "图关系引用与所选原件版本字符范围不一致", 502)
    return record


def adopted_graph_snapshot(reader, graph_record, bundle, *, neighborhood=None, documents=()):
    """Use existing typed relation fields only after exact evidence reads.

    The fixed graph remains the source. Unreviewed identities, background links,
    unsupported relationship meanings and untimed edges never become qualified
    graph input merely because they were displayed in a neighborhood.
    """
    documents = tuple(documents)
    all_records = (graph_record, *documents)
    collection = {
        "items": list(all_records),
        "source_id": graph_record["source_id"],
        "is_demo": graph_record["is_demo"],
        "coverage": {"scope_description": bundle.scope_description},
    }
    snapshot = reader.snapshot(collection)
    graph_engine_record = snapshot.records[0]
    relation_rows, entities, gaps = [], [], []
    graph_ref = reader.ref(graph_record)
    available = time_upper(graph_record["first_seen_at"])
    nodes = {node.key: node for node in (neighborhood.nodes if neighborhood else ())}
    document_versions = {(d["source_id"], d["source_record_id"], d["version_id"]): d for d in documents}

    def node_ref(node):
        return graph_ref.model_copy(
            update={
                "object_type": "graph_entity",
                "object_id": canonical_hash(list(node.key)),
            }
        )

    for node in nodes.values():
        entities.append(
            MiningEntity(
                canonical_entity_ref=node_ref(node) if node.identity_status == "confirmed" else None,
                temporary_entity_ref=node_ref(node).model_copy(update={"object_type": "graph_mention"}),
                entity_type=node.entity_type,
                mapping_ref=graph_ref if node.identity_status == "confirmed" else None,
                mapping_version=graph_record["version_id"] if node.identity_status == "confirmed" else None,
                mapping_status="confirmed" if node.identity_status == "confirmed" else "unresolved",
                mapping_available_at=available,
            )
        )
    for edge in neighborhood.edges if neighborhood else ():
        endpoints = (nodes[edge.source.key], nodes[edge.target.key])
        eligible = (
            bundle.graph_kind == "reviewed"
            and edge.binding_status == "bound"
            and edge.review.status == "approved"
            and edge.relation_type
            in {"coauthored", "collaborated", "supplies", "depends_on", "licensed_to", "manufactures"}
            and all(
                n.identity_status == "confirmed" and n.identity_review_basis and n.review.status == "approved"
                for n in endpoints
            )
            and bool(edge.evidence)
            and all(e.key in document_versions for e in edge.evidence)
            and edge.valid_from is not None
        )
        if not eligible:
            gaps.append("edge_not_qualified_for_existing_graph_kernels")
            continue
        # Each quote is checked again against the selected source version at the
        # projection boundary; a caller cannot mark a merely read record as bound.
        for evidence in edge.evidence:
            document = document_versions[evidence.key]
            if (
                document["raw_sha256"] != evidence.raw_sha256
                or evidence.locator.end > len(document["clean_text"])
                or document["clean_text"][evidence.locator.start : evidence.locator.end] != evidence.quote
            ):
                raise history_error()
        relation_rows.append(
            Relation(
                relation_ref=graph_ref.model_copy(
                    update={
                        "object_type": "graph_relation",
                        "object_id": canonical_hash([bundle.namespace, edge.id]),
                    }
                ),
                subject_ref=node_ref(endpoints[0]),
                object_ref=node_ref(endpoints[1]),
                relation_type=edge.relation_type,
                directed=True,
                identity_status="confirmed",
                effective_time=edge.valid_from.expression(),
                valid_until=edge.valid_to.expression() if edge.valid_to else None,
                evidence_refs=tuple(reader.ref(document_versions[e.key]) for e in edge.evidence),
                provenance=SectionProvenance(
                    schema_version="datafabric.graph.bundle.v1",
                    provider_ref=graph_ref,
                    available_at=available,
                ),
            )
        )
    graph_engine_record = graph_engine_record.model_copy(
        update={
            "canonical_entity_refs": tuple(entities),
            "relations": tuple(relation_rows),
            "evidence_refs": tuple(reader.ref(d) for d in documents),
            "missing_fields": tuple(sorted(set((*graph_engine_record.missing_fields, *gaps)))),
        }
    )
    records = (graph_engine_record, *snapshot.records[1:])
    counts = {}
    for record in records:
        counts[record.source_id] = counts.get(record.source_id, 0) + 1
    coverage = tuple(
        SnapshotCoverage(
            source_instance=snapshot.source_instance,
            source_id=source_id,
            mode="bounded_sample",
            scope_description=bundle.scope_description,
            record_count=count,
            reasons=("derived_graph_is_not_an_independent_study", "published_subgraph_not_global_coverage"),
        )
        for source_id, count in sorted(counts.items())
    )
    return snapshot.model_copy(
        update={
            "snapshot_id": "graph-"
            + canonical_hash(
                {
                    "graph": graph_record["version_id"],
                    "nodes": [node.key for node in nodes.values()],
                    "edges": [relation.relation_ref.object_id for relation in relation_rows],
                    "documents": sorted(document_versions),
                }
            )[:24],
            "records": records,
            "coverage": coverage,
            "metadata_gaps": ("derived_graph_is_context_not_an_independent_study", *tuple(sorted(set(gaps)))),
        }
    )


@contextmanager
def graph_clinical_reader(settings, actor):
    """Optional current-title reader; it does not make historical evidence."""
    from bioradar.adapters.data_fabric_research import DataFabricResearchReader

    url = settings.data_fabric_research_url or settings.data_fabric_url
    allowed = set(settings.engine_source_id_allowlist.get("datafabric-test", []))
    if url is None or "clinicaltrials" not in allowed:
        yield None
        return
    with httpx.Client(base_url=url, trust_env=False) as client:
        yield DataFabricResearchReader(
            client,
            source_instance="datafabric-test",
            authorization_ref=actor.grant_ref,
            allowed_source_sites=allowed,
            provider="data_fabric_graph_research",
        )


def resolve_graph_names(reader, record, bundle, request, cutoff):
    from bioradar.research.investigations.graph import GraphResolution

    results, identities = [], []
    original = {node.key: node for node in bundle.nodes}
    for name in request.seed_names:
        value = reader._get(
            "/api/history/graph/" + record["version_id"] + "/resolve",
            {
                "source_id": request.source_id,
                "source_record_id": request.source_record_id,
                "is_demo": str(request.is_demo).lower(),
                "mode": request.mode,
                "cutoff": cutoff.isoformat(),
                "name": name,
                "limit": 20,
            },
        )
        try:
            resolved = GraphResolution.model_validate(value)
            if (
                resolved.graph_ref.version_id != record["version_id"]
                or resolved.graph_ref.raw_sha256 != record["raw_sha256"]
                or resolved.graph_ref.source_id != record["source_id"]
                or resolved.graph_ref.source_record_id != record["source_record_id"]
                or resolved.graph_ref.is_demo is not record["is_demo"]
                or resolved.query.get("name") != name
                or any(original.get(node.key) != node for node in resolved.candidates)
                or (resolved.status == "resolved" and (len(resolved.candidates) != 1 or resolved.truncated))
                or (resolved.status == "not_found" and resolved.candidates)
            ):
                raise ValueError("seed candidates differ from the selected graph")
        except (ValueError, ValidationError):
            raise history_error() from None
        results.append(resolved)
        if resolved.status == "resolved" and not resolved.truncated:
            candidate = resolved.candidates[0]
            identities.append(candidate.wire)
    ready = all(result.status == "resolved" and not result.truncated for result in results)
    return tuple(dict.fromkeys(identities)) if ready else (), results


def list_graph_packages(reader, *, source_id, is_demo, cutoff, limit=20, cursor=None):
    from bioradar.adapters.fabric_history import validate_record
    from bioradar.research.investigations.graph import (
        GraphPackageList,
        GraphPackageSummary,
        GraphPackageUnavailable,
    )

    params = {
        "source_id": source_id,
        "is_demo": str(is_demo).lower(),
        "mode": "current",
        "cutoff": cutoff.isoformat(),
        "limit": limit,
        "record_kind": "graph",
    }
    if cursor:
        params["cursor"] = cursor
    response = reader._get("/api/history/records", params)
    try:
        if (
            response["schema_version"] != "datafabric.history.collection.v1"
            or not isinstance(response["items"], list)
            or len(response["items"]) > limit
            or (response.get("next_cursor") is not None and not isinstance(response["next_cursor"], str))
        ):
            raise ValueError("invalid graph package list")
        items, unavailable = [], []
        for value in response["items"]:
            # A malformed cross-source envelope is a response error, not a
            # partially selectable catalog that might leak another source.
            if (
                not isinstance(value, dict)
                or value.get("source_id") != source_id
                or value.get("is_demo") is not is_demo
                or not isinstance(value.get("source_record_id"), str)
                or not isinstance(value.get("version_id"), str)
            ):
                raise ValueError("graph package source mismatch")
            try:
                record = validate_record(value, source_id, is_demo, "current", cutoff)
                if record["record_kind"] != "graph":
                    raise ValueError("non-graph record returned for graph catalog")
                bundle = GraphBundle.model_validate_json(record["raw_content"])
            except (ValueError, DomainError):
                # Immutable old versions are not silently rewritten. Incompatible
                # projections are explicit and cannot poison the healthy page.
                unavailable.append(
                    GraphPackageUnavailable(
                        source_id=source_id,
                        source_record_id=value["source_record_id"][:400],
                        version_id=value["version_id"][:64],
                        reason="graph_projection_incompatible_original_version_preserved",
                    )
                )
                continue
            items.append(
                GraphPackageSummary(
                    source_id=source_id,
                    source_record_id=record["source_record_id"],
                    version_id=record["version_id"],
                    is_demo=is_demo,
                    title=bundle.title,
                    namespace=bundle.namespace,
                    graph_kind=bundle.graph_kind,
                    scope_description=bundle.scope_description,
                    selection_truncated=bundle.selection_truncated,
                )
            )
        limitations = [
            "Finite page of authorized published graph packages; not the complete shared graph.",
            "Background links do not establish identity, document evidence or past visibility.",
        ]
        if unavailable:
            limitations.append(
                "Some immutable packages have incompatible projections and are not selectable."
            )
        return GraphPackageList(
            items=items,
            unavailable_packages=unavailable,
            next_cursor=response.get("next_cursor"),
            as_of=cutoff,
            limitations=limitations,
        )
    except (KeyError, TypeError, ValueError):
        raise history_error() from None
