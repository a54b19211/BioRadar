"""Bounded graph adoption probes; all source responses here are explicitly synthetic."""

import copy
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
from bioradar.adapters.fabric_graph import (
    adopted_graph_snapshot,
    read_graph_evidence,
    read_graph_version,
    read_neighborhood,
)
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.api.app import create_app
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.graph import (
    GraphBundle,
    GraphInvestigationCreate,
    compare_graph_versions,
    run_graph_investigation,
    select_graph_followups,
)
from bioradar.research.investigations.models import InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.models import Event, MiningPolicy, SectionProvenance, TimeExpression
from bioradar.research.mining.runner import run_mining
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_engine_persistence import CREDENTIAL, GRANT
from test_engine_persistence import database as database  # noqa: F401

pytestmark = pytest.mark.ENGINE
AS_OF = datetime(2026, 10, 17, tzinfo=UTC)


def original_record(name, body, *, source="fixture-source", kind="document", demo=True):
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    facts = (
        {key: body[key] for key in ("graph_kind", "namespace", "origin", "scope_description")}
        if kind == "graph"
        else body
    )
    return {
        "schema_version": "datafabric.history.record.v1",
        "source_id": source,
        "source_record_id": name,
        "version_id": digest,
        "record_kind": kind,
        "is_demo": demo,
        "title": "SYNTHETIC " + name,
        "source_url": None,
        "raw_content": raw,
        "raw_media_type": "application/json",
        "raw_sha256": digest,
        "clean_text": raw if kind == "graph" else body.get("body", raw),
        "parser_version": "synthetic-graph-test.v1",
        "facts": facts,
        "field_locators": {key: {"json_pointer": "/" + key} for key in facts},
        "event_time": None,
        "source_published_at": None,
        "source_updated_at": None,
        "first_seen_at": "2026-10-10T00:00:00Z",
        "received_at": "2026-10-10T00:00:00Z",
        "availability_proof": None,
        "mapping_review": None,
        "published_seq": 1,
    }


def bundle(*, background=False, demo=True):
    doc = original_record(
        "source",
        {
            "body": "SYNTHETIC A collaborates with B; B collaborates with C; A supplies D. "
            "SYNTHETIC safety event on 2026-10-12 affects A."
        },
        demo=demo,
    )
    review = {
        "status": "unreviewed" if background else "approved",
        "reviewed_by": None if background else "synthetic-reviewer",
        "reviewed_at": None if background else "2026-10-09T00:00:00Z",
    }
    namespace = "synthetic-background" if background else "synthetic-tang-instance"

    def identity(name):
        return {"namespace": namespace, "id": name}

    ref = {key: doc[key] for key in ("source_id", "source_record_id", "version_id", "raw_sha256")}
    ref["origin_doc_uuid"] = None
    evidence = {
        **ref,
        "quote": doc["clean_text"],
        "locator": {"basis": "clean_text", "start": 0, "end": len(doc["clean_text"])},
    }
    nodes = [
        {
            **identity(name),
            "entity_type": "synthetic-entity",
            "name": "SYNTHETIC " + name,
            "aliases": [],
            "external_ids": {},
            "identity_status": "background" if background else "confirmed",
            "identity_review_basis": "background" if background else "explicit synthetic identity review",
            "review": review,
            "mention_ids": [],
            "evidence": [],
        }
        for name in "ABCD"
    ]
    edges = []
    for number, (a, b, kind) in enumerate(
        (("A", "B", "collaborated"), ("B", "C", "collaborated"), ("A", "D", "supplies"))
    ):
        edges.append(
            {
                "id": str(number),
                "source": identity(a),
                "target": identity(b),
                "relation_type": kind,
                "directed": True,
                "relation_props": {},
                "valid_from": None if background else {"value": "2025-01-01", "precision": "day"},
                "valid_to": None,
                "review": review,
                "binding_status": "background" if background else "bound",
                "evidence": [] if background else [evidence],
                "source_doc_uuid": None,
                "unbound_reason": None,
            }
        )
    value = {
        "schema_version": "datafabric.graph.bundle.v1",
        "graph_kind": "background" if background else "reviewed",
        "namespace": namespace,
        "is_demo": demo,
        "title": "SYNTHETIC graph: not intelligence",
        "origin": "explicit_demo" if demo else "primekg_current_capture",
        "scope_description": "Explicit synthetic selected subgraph",
        "selected_document_uuids": [],
        "scope_record_ids": ["SYNTHETIC"],
        "next_document_offset": None,
        "dependency_source_ids": [] if background else ["fixture-source"],
        "document_refs": [] if background else [ref],
        "selection_truncated": False,
        "nodes": nodes,
        "edges": edges,
        "redirects": [],
        "limitations": ["synthetic_test_only"],
    }
    return GraphBundle.model_validate(value), doc


def request(record, **changes):
    values = {
        "question": "SYNTHETIC question; not medical research",
        "source_id": record["source_id"],
        "source_record_id": record["source_record_id"],
        "graph_version_id": record["version_id"],
        "is_demo": record["is_demo"],
        "seed_ids": (json.loads(record["raw_content"])["namespace"] + "|B",),
        "max_depth": 2,
        "cutoff": AS_OF,
        "search_clinicaltrials": False,
    }
    return GraphInvestigationCreate(**(values | changes))


def source(bundle_value, doc=None, *, extra_records=(), mutate=None, resolution_status="resolved"):
    graph = original_record(
        "graph",
        bundle_value.model_dump(mode="json"),
        source="primekg_graph" if bundle_value.graph_kind == "background" else "reviewed_graph",
        kind="graph",
        demo=bundle_value.is_demo,
    )
    records = {r["version_id"]: r for r in (graph, *([doc] if doc else []), *extra_records)}
    calls = []

    def handler(req):
        calls.append(req)
        path = req.url.path
        if path.startswith("/api/history/versions/"):
            record = records.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"record": record}) if record else httpx.Response(404)
        ref = {
            key: graph[key]
            for key in (
                "source_id",
                "source_record_id",
                "version_id",
                "raw_sha256",
                "is_demo",
                "first_seen_at",
            )
        }
        if path.endswith("/resolve"):
            candidates = (
                list(bundle_value.nodes[:2]) if resolution_status == "ambiguous" else [bundle_value.nodes[1]]
            )
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.graph.resolution.v1",
                    "graph_ref": ref,
                    "query": {
                        "name": req.url.params["name"],
                        "external_namespace": None,
                        "external_id": None,
                    },
                    "status": resolution_status,
                    "candidates": [n.model_dump(mode="json") for n in candidates]
                    if resolution_status != "not_found"
                    else [],
                    "truncated": False,
                    "limitations": [],
                },
            )
        if path.endswith("/neighborhood"):
            seeds = req.url.params.get_list("seed_ids")
            found = [{"namespace": item.split("|", 1)[0], "id": item.split("|", 1)[1]} for item in seeds]
            value = {
                "schema_version": "datafabric.graph.neighborhood.v1",
                "graph_ref": ref,
                "namespace": bundle_value.namespace,
                "nodes": [n.model_dump(mode="json") for n in bundle_value.nodes],
                "edges": [e.model_dump(mode="json") for e in bundle_value.edges],
                "redirects": [],
                "seeds_found": found,
                "seeds_missing": [],
                "coverage": {
                    "scope": "selected_published_subgraph",
                    "truncated": False,
                    "selection_truncated": False,
                    "stop_reason": "selected_subgraph_exhausted",
                    "returned_nodes": len(bundle_value.nodes),
                    "returned_edges": len(bundle_value.edges),
                    "max_depth": 2,
                },
                "limitations": [],
            }
            if mutate:
                mutate(value)
            return httpx.Response(200, json=value)
        return httpx.Response(500)

    reader = FabricHistoryReader(
        httpx.Client(base_url="http://fabric.test", transport=httpx.MockTransport(handler)),
        authorization_ref=GRANT,
    )
    return reader, graph, calls


def test_background_directions_only_trigger_questions_and_reverse_pair_is_not_another_study():
    value, _ = bundle(background=True)
    reverse = value.edges[0].model_copy(
        update={"id": "reverse", "source": value.edges[0].target, "target": value.edges[0].source}
    )
    value = value.model_copy(update={"edges": (value.edges[0], reverse)})
    reader, graph, _ = source(value)
    req = request(graph, seed_ids=(value.nodes[0].wire,))
    record, adopted = read_graph_version(reader, req, AS_OF)
    neighborhood = read_neighborhood(reader, record, adopted, req, AS_OF)
    plans = select_graph_followups(neighborhood, topic_terms=("SYNTHETIC",))
    assert len(plans) == 1 and plans[0].neighbor.id == "B"
    assert plans[0].source.id == "A" and plans[0].binding_status == "background"
    projected = adopted_graph_snapshot(reader, graph, value, neighborhood=neighborhood)
    assert projected.records[0].relations == ()
    result = run_mining(
        projected, MiningPolicy(as_of=AS_OF, method_ids=("lexical_emergence", "research_network"))
    )
    assert all(item.execution_status == "unsupported" for item in result.method_results)
    assert select_graph_followups(neighborhood, max_followups=0) == []


@pytest.mark.parametrize("change", ["edge", "node", "reference", "coverage"])
def test_neighbor_projection_cannot_substitute_or_modify_frozen_original(change):
    value, doc = bundle()

    def tamper(body):
        if change == "edge":
            body["edges"][0]["relation_type"] = "INVENTED"
        elif change == "node":
            body["nodes"][0]["identity_status"] = "unresolved"
        elif change == "reference":
            body["graph_ref"]["version_id"] = "0" * 64
        else:
            body["coverage"]["returned_edges"] += 1

    reader, graph, _ = source(value, doc, mutate=tamper)
    req = request(graph)
    original, adopted = read_graph_version(reader, req, AS_OF)
    with pytest.raises(DomainError):
        read_neighborhood(reader, original, adopted, req, AS_OF)


def test_quote_binding_checks_exact_unicode_range_and_does_not_accept_just_a_hash():
    value, doc = bundle()
    reader, _, _ = source(value, doc)
    evidence = value.edges[0].evidence[0]
    assert read_graph_evidence(reader, evidence, is_demo=True, mode="current", cutoff=AS_OF) == doc
    bad = evidence.model_copy(update={"quote": "SYNTHETIC invented source"})
    with pytest.raises(DomainError):
        read_graph_evidence(reader, bad, is_demo=True, mode="current", cutoff=AS_OF)
    bad = evidence.model_copy(update={"locator": evidence.locator.model_copy(update={"end": 10000})})
    with pytest.raises(DomainError):
        read_graph_evidence(reader, bad, is_demo=True, mode="current", cutoff=AS_OF)


def test_exact_reviewed_timed_relations_enable_existing_network_and_directional_exposure_kernels():
    value, doc = bundle()
    reader, graph, _ = source(value, doc)
    req = request(graph)
    neighborhood = read_neighborhood(reader, graph, value, req, AS_OF)
    projected = adopted_graph_snapshot(reader, graph, value, neighborhood=neighborhood, documents=[doc])
    assert len(projected.records[0].relations) == 3
    supply = next(r for r in projected.records[0].relations if r.relation_type == "supplies")
    event = Event(
        event_ref=reader.ref(doc).model_copy(update={"object_type": "explicit_synthetic_safety_event"}),
        event_type="safety",
        object_refs=(supply.subject_ref,),
        evidence_refs=(reader.ref(doc),),
        effective_time=TimeExpression(raw="2026-10-12", precision="day"),
        source_status="supported",
        provenance=SectionProvenance(
            schema_version="explicit-demo-event.v1",
            provider_ref=reader.ref(doc),
            available_at=datetime(2026, 10, 10, tzinfo=UTC),
        ),
    )
    projected = projected.model_copy(
        update={
            "records": (projected.records[0].model_copy(update={"events": (event,)}), *projected.records[1:])
        }
    )
    result = run_mining(
        projected,
        MiningPolicy(
            as_of=AS_OF,
            method_ids=("research_network", "event_exposure"),
        ),
    )
    network, risk = result.method_results
    assert network.execution_status == "success" and network.candidates
    assert risk.execution_status == "success"
    assert any(c.metrics["hop_count"] == 1 for c in risk.candidates)
    assert all(c.kind == "hypothesis" for c in risk.candidates if c.metrics["hop_count"])
    # Provider approval is not enough without original evidence and event time.
    no_time = value.model_copy(
        update={"edges": tuple(edge.model_copy(update={"valid_from": None}) for edge in value.edges)}
    )
    no_time_reader, no_time_graph, _ = source(no_time, doc)
    no_time_neighborhood = read_neighborhood(
        no_time_reader, no_time_graph, no_time, request(no_time_graph), AS_OF
    )
    assert (
        not adopted_graph_snapshot(
            no_time_reader, no_time_graph, no_time, neighborhood=no_time_neighborhood, documents=[doc]
        )
        .records[0]
        .relations
    )


def test_source_identity_is_not_a_name_merge_or_auto_review_upgrade():
    value, _ = bundle()
    raw = value.model_dump(mode="json")
    raw["nodes"][0]["review"]["status"] = "auto_approved"
    with pytest.raises(ValidationError):
        GraphBundle.model_validate(raw)
    raw = value.model_dump(mode="json")
    raw["nodes"][0]["id"] = "A|collision"
    with pytest.raises(ValidationError):
        GraphBundle.model_validate(raw)
    identity = value.nodes[0].model_copy(update={"id": "different", "name": value.nodes[1].name})
    changed = value.model_copy(update={"nodes": (*value.nodes, identity)})
    assert compare_graph_versions(value, changed)["nodes"]["added_to_this_package"] == 1


def test_graph_version_comparison_preserves_original_and_labels_package_scope():
    value, _ = bundle()
    before = value.model_dump(mode="json")
    changed = value.model_copy(update={"edges": value.edges[:-1], "selection_truncated": True})
    comparison = compare_graph_versions(value, changed)
    assert comparison["edges"]["not_in_this_package"] == 1
    assert comparison["nodes"]["unchanged"] == 4
    assert value.model_dump(mode="json") == before


@pytest.fixture
def runtime(database):  # noqa: F811
    settings = Settings(
        environment="test",
        database_url=None,
        redis_url=None,
        engine_dev_credential=CREDENTIAL,
        engine_source_id_allowlist={
            "fixture": ["reviewed_graph", "fixture-source"],
            "datafabric-test": ["primekg_graph", "reviewed_graph", "clinicaltrials", "legacy_documents"],
        },
    )
    engine = EngineService(database, settings)
    engine.history_source_reader = lambda: [
        {"source_id": name, "read_enabled": True}
        for name in (
            "reviewed_graph",
            "primekg_graph",
            "fixture-source",
            "clinicaltrials",
            "legacy_documents",
        )
    ]
    return engine, DevelopmentAccess(settings).actor()


def test_postgres_saves_context_versions_and_later_graph_does_not_rewrite_human_judgment(runtime):
    engine, actor = runtime
    value, doc = bundle()
    reader, graph, calls = source(value, doc)
    first = run_graph_investigation(engine, actor, reader, request(graph), execution_id="first")
    assert first.investigation.judgment_count == 0 and first.investigation.material_count == 2
    assert first.mining_snapshot_id and first.investigation.status == "paused"
    runtime = InvestigationService(engine)
    material_ids = [entry.id for entry in first.investigation.entries if entry.kind == "material"]
    _, judged = runtime.append(
        actor,
        first.investigation.id,
        InvestigationEntryCreate.model_validate(
            {
                "kind": "judgment",
                "expected_revision": first.investigation.revision,
                "text": "SYNTHETIC human judgment; not a professional assessment",
                "conditions": "SYNTHETIC conditions",
                "assessment": "provisional",
                "basis_entry_ids": material_ids,
            }
        ),
        "human",
    )
    prior_judgment = copy.deepcopy(judged["entries"][-1])
    newer = value.model_copy(update={"title": "SYNTHETIC revised graph"})
    reader2, graph2, _ = source(newer, doc, extra_records=[graph])
    appended = run_graph_investigation(
        engine,
        actor,
        reader2,
        request(
            graph2,
            investigation_id=first.investigation.id,
            expected_revision=judged["revision"],
            compare_version_id=graph["version_id"],
        ),
        execution_id="second",
    )
    assert appended.investigation.needs_review
    assert appended.investigation.judgment_count == 1
    assert (
        next(
            entry.model_dump(mode="json")
            for entry in appended.investigation.entries
            if entry.id == prior_judgment["id"]
        )
        == prior_judgment
    )
    assert appended.comparison["edges"]["unchanged"] == 3
    count = len(calls)
    with pytest.raises(DomainError) as failure:
        run_graph_investigation(engine, actor, reader, request(graph), execution_id="first")
    assert failure.value.status_code == 409 and len(calls) == count


def test_name_ambiguity_is_saved_for_selection_without_neighborhood_or_evidence_reads(runtime):
    engine, actor = runtime
    value, doc = bundle()
    reader, graph, calls = source(value, doc, resolution_status="ambiguous")
    req = request(graph, seed_ids=(), seed_names=("SYNTHETIC ambiguous",))
    result = run_graph_investigation(engine, actor, reader, req, execution_id="ambiguous")
    assert result.completion_status == "awaiting_seed_selection"
    assert result.graph_snapshot_id and result.investigation.material_count == 1
    assert len(result.seed_resolutions[0].candidates) == 2
    assert not any(req.url.path.endswith("/neighborhood") for req in calls)
    assert all(doc["version_id"] not in str(req.url) for req in calls)


def test_unavailable_graph_is_a_saved_failed_read_not_a_fake_empty_neighborhood(runtime):
    engine, actor = runtime
    value, _ = bundle()
    _, graph, _ = source(value)
    reader = FabricHistoryReader(
        httpx.Client(
            base_url="http://fabric.test",
            transport=httpx.MockTransport(lambda req: httpx.Response(503)),
        ),
        authorization_ref=GRANT,
    )
    result = run_graph_investigation(engine, actor, reader, request(graph), execution_id="failed")
    assert result.completion_status == "read_failed" and result.graph_snapshot_id is None
    assert result.investigation.material_count == 0 and result.investigation.status == "paused"
    assert result.reads[0]["status"] == "failed"


@pytest.mark.parametrize("demo_namespace", [None, "explicit-simulation-namespace"])
def test_http_api_uses_current_permission_for_graph_and_legacy_snapshot_read(
    runtime, monkeypatch, demo_namespace
):
    engine, actor = runtime
    value, doc = bundle()
    reader, graph, _ = source(value, doc)
    allowed = {"reviewed_graph", "fixture-source"}
    if demo_namespace:
        engine.settings.data_fabric_demo_source_instance = demo_namespace
        engine.settings.engine_source_allowlist = [demo_namespace]
        engine.settings.engine_source_id_allowlist = {demo_namespace: sorted(allowed)}
        reader.demo_source_instance = demo_namespace

    @contextmanager
    def controlled_reader(_settings):
        yield reader

    @contextmanager
    def no_clinical(_settings, _actor):
        yield None

    monkeypatch.setattr("bioradar.adapters.fabric_graph.graph_reader", controlled_reader)
    monkeypatch.setattr("bioradar.adapters.fabric_graph.graph_clinical_reader", no_clinical)
    monkeypatch.setattr(
        "bioradar.adapters.fabric_history.current_history_sources",
        lambda settings: [{"source_id": name, "read_enabled": True} for name in allowed],
    )
    app = create_app(engine.settings)
    app.state.database = engine.database
    headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "actual-http-graph"}
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        app.state.database = engine.database
        response = client.post(
            "/api/engine/investigations/graph", json=request(graph).model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 201, response.text
        saved = response.json()
        snapshot_id, investigation_id = saved["graph_snapshot_id"], saved["investigation"]["id"]
        assert client.get("/api/engine/snapshots/" + snapshot_id, headers=headers).status_code == 200
        allowed.remove("reviewed_graph")
        assert client.get("/api/engine/snapshots/" + snapshot_id, headers=headers).status_code == 403
        assert (
            client.get("/api/engine/investigations/" + investigation_id, headers=headers).status_code == 403
        )


def test_request_rejects_implicit_identity_choice_and_naive_historical_time():
    value, _ = bundle()
    _, graph, _ = source(value)
    for changes in (
        {"seed_ids": ()},
        {"seed_names": ("SYNTHETIC",)},
        {"seed_ids": ("missing-delimiter",)},
        {"cutoff": datetime(2026, 10, 17)},
    ):
        with pytest.raises(ValidationError):
            request(graph, **changes)


def test_package_catalog_cannot_bypass_removed_source_instance_grant(runtime, monkeypatch):
    engine, _ = runtime
    settings = engine.settings.model_copy(update={"engine_source_allowlist": ["fixture"]})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not be touched after local instance permission is removed")

    monkeypatch.setattr("bioradar.adapters.fabric_graph.graph_reader", forbidden)
    monkeypatch.setattr("bioradar.adapters.fabric_history.current_history_sources", forbidden)
    app = create_app(settings)
    app.state.database = engine.database
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        response = client.get(
            "/api/engine/graph/packages?source_id=primekg_graph&is_demo=false",
            headers={"Authorization": "Bearer " + CREDENTIAL},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "source_not_authorized"


def test_unknown_database_write_is_not_caught_as_source_failure_or_retried(runtime, monkeypatch):
    engine, actor = runtime
    value, doc = bundle()
    reader, graph, calls = source(value, doc)
    attempted = []

    def uncertain(*_args, **_kwargs):
        attempted.append("write")
        raise RuntimeError("synthetic uncertain transaction outcome")

    monkeypatch.setattr(engine, "adopt_snapshot", uncertain)
    with pytest.raises(RuntimeError, match="uncertain transaction"):
        run_graph_investigation(engine, actor, reader, request(graph), execution_id="write-uncertain")
    assert attempted == ["write"]
    assert len(calls) == 2  # One version and one neighborhood; no document read or retry.


def test_bad_old_package_is_explicitly_unavailable_without_hiding_healthy_page():
    from bioradar.adapters.fabric_graph import list_graph_packages

    value, _ = bundle(background=True)
    _, graph, _ = source(value)
    bad = copy.deepcopy(graph)
    bad["source_record_id"] = "incompatible-old-projection"
    bad["field_locators"] = {key: {"kind": "json_pointer", "path": "/" + key} for key in bad["facts"]}
    with httpx.Client(
        base_url="http://fabric.test",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.history.collection.v1",
                    "items": [bad, graph],
                    "next_cursor": "next-page",
                },
            )
        ),
    ) as client:
        reader = FabricHistoryReader(client, authorization_ref=GRANT)
        result = list_graph_packages(reader, source_id="primekg_graph", is_demo=True, cutoff=AS_OF)
    assert len(result.items) == 1 and result.items[0].version_id == graph["version_id"]
    assert len(result.unavailable_packages) == 1 and result.next_cursor == "next-page"
    assert result.unavailable_packages[0].source_record_id == "incompatible-old-projection"


def test_graph_research_provider_marker_is_revocable_without_breaking_old_text_provider(runtime):
    from bioradar.research.mining.models import EngineRecord, NormalizedSnapshot, ProviderRef

    engine, actor = runtime

    def snapshot(provider):
        return NormalizedSnapshot(
            snapshot_id="synthetic-current-source",
            source_instance="datafabric-test",
            observed_at=AS_OF,
            is_demo=False,
            records=(
                EngineRecord(
                    record_ref=ProviderRef(
                        provider=provider,
                        object_type="document",
                        object_id="synthetic-only",
                        adopted_version="synthetic-v1",
                        version_source="provider",
                        content_hash="synthetic-only",
                        authorization_ref=GRANT,
                    ),
                    source_instance="datafabric-test",
                    source_id="clinicaltrials",
                    observed_at=AS_OF,
                    document_hash="synthetic-only",
                    normalized_text="SYNTHETIC source text",
                    text_status="inline_authorized",
                    normalization_version="synthetic-v1",
                ),
            ),
        )

    _, adopted = engine.adopt_snapshot(actor, snapshot("data_fabric_graph_research"), "new-marker")
    revoked = EngineService(engine.database, engine.settings)
    revoked.history_source_reader = lambda: []
    with pytest.raises(DomainError) as failure:
        revoked.get_snapshot(actor, adopted["id"])
    assert failure.value.status_code == 403

    def disconnected():
        raise AssertionError("old text provider must not acquire a graph dependency")

    old = EngineService(engine.database, engine.settings)
    old.history_source_reader = disconnected
    _, retained = old.adopt_snapshot(actor, snapshot("data_fabric:datafabric-test"), "old-text")
    assert (
        old.get_snapshot(actor, retained["id"]).snapshot.records[0].normalized_text == "SYNTHETIC source text"
    )


def test_followup_order_prefers_reviewed_bound_sources_then_stable_neighbor_identity():
    value, doc = bundle()
    edges = list(value.edges)
    edges[0] = edges[0].model_copy(
        update={
            "binding_status": "unbound",
            "evidence": (),
            "review": edges[0].review.model_copy(update={"status": "auto_approved"}),
        }
    )
    value = value.model_copy(update={"edges": tuple(edges)})
    reader, graph, _ = source(value, doc)
    neighborhood = read_neighborhood(reader, graph, value, request(graph), AS_OF)
    plans = select_graph_followups(neighborhood, max_followups=2)
    assert [plan.neighbor.id for plan in plans] == ["C", "A"]
    reordered = neighborhood.model_copy(update={"edges": tuple(reversed(neighborhood.edges))})
    assert select_graph_followups(reordered, max_followups=2) == plans


def test_clinical_projection_unavailable_is_partial_and_not_an_empty_success(runtime):
    from bioradar.adapters.data_fabric_research import DataFabricResearchReader
    from test_digging import document as protocol_document

    engine, actor = runtime
    value, _ = bundle(background=True, demo=False)
    reader, graph, _ = source(value)
    document = protocol_document("graph-unavailable")
    document["projection"] = {
        "version": "clinicaltrials.protocol.v1",
        "status": "unavailable",
        "reason": "synthetic malformed source",
        "text": None,
        "locator": None,
        "facts": None,
        "field_paths": None,
    }

    def respond(req):
        if req.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.research.search.v1",
                    "source_site": "clinicaltrials",
                    "scope": "title_literal_and_current_db",
                    "terms": req.url.params.get_list("terms"),
                    "limit": 3,
                    "has_more": False,
                    "items": [
                        {
                            "uuid": document["uuid"],
                            "title": document["title"],
                            "source_site": "clinicaltrials",
                        }
                    ],
                },
            )
        return httpx.Response(200, json=document)

    with httpx.Client(
        base_url="http://fabric-current.test", transport=httpx.MockTransport(respond)
    ) as client:
        current = DataFabricResearchReader(
            client,
            source_instance="datafabric-test",
            authorization_ref=GRANT,
            allowed_source_sites={"clinicaltrials"},
            provider="data_fabric_graph_research",
        )
        result = run_graph_investigation(
            engine,
            actor,
            reader,
            request(graph, search_clinicaltrials=True, max_followups=1),
            execution_id="unavailable-projection",
            clinical_reader=current,
        )
    assert result.completion_status == "partial"
    assert "clinicaltrials_protocol_projection_unavailable" in result.qualification_gaps
    assert any(
        row["status"] == "unavailable" and row.get("document_uuid") == document["uuid"]
        for row in result.reads
    )
    assert result.investigation.material_count == 1  # Only the actual graph was available.


@pytest.mark.parametrize("failure_kind", ["revoked", "unavailable"])
def test_three_lists_keep_ordinary_text_when_optional_graph_permission_is_unavailable(
    runtime,
    database,
    failure_kind,
):
    from bioradar.research.engine.schemas import RunCreate
    from bioradar.research.investigations.models import InvestigationCreate
    from bioradar.research.mining.models import EngineRecord, NormalizedSnapshot, ProviderRef

    engine, actor = runtime
    ordinary_ref = ProviderRef(
        provider="fixture",
        object_type="document",
        object_id="SYNTHETIC ordinary text",
        adopted_version="v1",
        version_source="export_hash",
        content_hash="synthetic",
        authorization_ref=GRANT,
    )
    ordinary_snapshot = NormalizedSnapshot(
        snapshot_id="SYNTHETIC ordinary",
        source_instance="fixture",
        is_demo=True,
        observed_at=AS_OF,
        records=(
            EngineRecord(
                record_ref=ordinary_ref,
                source_instance="fixture",
                source_id="fixture-source",
                observed_at=AS_OF,
                document_hash="synthetic",
                normalized_text="SYNTHETIC ordinary text without a graph provider",
                text_status="inline_authorized",
                normalization_version="synthetic-v1",
            ),
        ),
    )
    _, ordinary = engine.adopt_snapshot(actor, ordinary_snapshot, "ordinary")
    policy = MiningPolicy(as_of=AS_OF, method_ids=("lexical_emergence",))
    _, ordinary_run = engine.create_run(
        actor, RunCreate(snapshot_id=ordinary["id"], policy=policy), "ordinary-run"
    )
    value, document = bundle()
    reader, graph, _ = source(value, document)
    graph_result = run_graph_investigation(engine, actor, reader, request(graph), execution_id="optional")
    for number, identifier in enumerate((graph_result.graph_snapshot_id, graph_result.mining_snapshot_id)):
        engine.create_run(actor, RunCreate(snapshot_id=identifier, policy=policy), "graph-run-" + str(number))
    ordinary_runtime = InvestigationService(engine)
    _, ordinary_case = ordinary_runtime.create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC ordinary case",
            question="SYNTHETIC ordinary question",
            is_demo=True,
        ),
        "ordinary-case",
    )
    ordinary_runtime.append(
        actor,
        ordinary_case["id"],
        InvestigationEntryCreate.model_validate(
            {
                "kind": "material",
                "expected_revision": ordinary_case["revision"],
                "snapshot_id": ordinary["id"],
                "record_ref": ordinary_ref.model_dump(mode="json"),
                "text": "SYNTHETIC ordinary context",
                "stance": "context",
            }
        ),
        "ordinary-material",
    )
    reads = []

    def permission():
        reads.append("catalog")
        if failure_kind == "unavailable":
            raise DomainError("history_source_unavailable", "SYNTHETIC provider unavailable", 503)
        return []

    reading = EngineService(database, engine.settings)
    reading.history_source_reader = permission
    packages = reading.list_snapshots(actor, limit=100)
    assert [item.id for item in packages.items] == [ordinary["id"]]
    assert packages.unavailable_count >= 2 and packages.availability_note
    runs = reading.list_runs(actor, limit=100)
    assert [item.id for item in runs.items] == [ordinary_run["id"]]
    assert runs.unavailable_count == 2
    cases = InvestigationService(reading).list(actor, limit=100)
    assert [item.id for item in cases.items] == [ordinary_case["id"]]
    assert cases.unavailable_count == 1
    assert reads == ["catalog"]  # Failure and success permissions are request-scoped, never global.
    with pytest.raises(DomainError) as denied:
        reading.get_snapshot(actor, graph_result.graph_snapshot_id)
    assert denied.value.status_code == (503 if failure_kind == "unavailable" else 403)
    # Hidden first-page rows must still advance the original database cursor.
    first_page = reading.list_runs(actor, limit=1)
    assert first_page.items == [] and first_page.next_cursor and first_page.unavailable_count == 1
    second_page = reading.list_runs(actor, first_page.next_cursor, limit=1)
    assert second_page.items == [] and second_page.next_cursor
    third_page = reading.list_runs(actor, second_page.next_cursor, limit=1)
    assert [item.id for item in third_page.items] == [ordinary_run["id"]]
    assert reading.get_snapshot(actor, ordinary["id"]).snapshot.records[0].record_ref == ordinary_ref


def test_list_does_not_hide_programming_or_integrity_failures(runtime, monkeypatch):
    engine, actor = runtime
    value, doc = bundle()
    reader, graph, _ = source(value, doc)
    run_graph_investigation(engine, actor, reader, request(graph), execution_id="integrity")

    def broken(*_args, **_kwargs):
        raise DomainError("snapshot_integrity_error", "SYNTHETIC invalid stored hash", 502)

    monkeypatch.setattr(engine, "_snapshot", broken)
    with pytest.raises(DomainError) as failure:
        engine.list_snapshots(actor)
    assert failure.value.code == "snapshot_integrity_error"
