"""Human interpretations and adopted graph packages stay qualified, fixed input context."""

import copy
from types import SimpleNamespace

from bioradar.adapters.fabric_graph import adopted_graph_snapshot
from bioradar.research.investigations.analysis import (
    basis_hash,
    evidence_basis_identity,
    freeze_passages,
    freeze_payload,
    material_context_identity,
    source_excerpt,
)
from bioradar.research.investigations.analysis_service import AnalysisService
from test_graph import bundle, source


def projection():
    graph_bundle, document = bundle(background=False, demo=True)
    reader, graph, requests = source(graph_bundle, document)
    graph_snapshot = adopted_graph_snapshot(reader, graph, graph_bundle)
    document_snapshot = reader.snapshot(
        {
            "items": [document],
            "is_demo": True,
            "source_id": document["source_id"],
            "coverage": {"scope_description": "synthetic fixed original"},
        }
    )
    snapshots = {"graph-snapshot": graph_snapshot, "document-snapshot": document_snapshot}
    service = AnalysisService.__new__(AnalysisService)
    service.engine = SimpleNamespace(
        _snapshot=lambda session, actor, key: SimpleNamespace(payload=snapshots[key].model_dump(mode="json"))
    )
    view = SimpleNamespace(
        entries=[
            SimpleNamespace(
                kind="material",
                id="graph-material",
                payload=SimpleNamespace(
                    snapshot_id="graph-snapshot",
                    support=SimpleNamespace(record_ref=graph_snapshot.records[0].record_ref),
                    stance="oppose",
                    text="SYNTHETIC unverified counterevidence note",
                    machine_analysis_id=None,
                ),
            ),
            SimpleNamespace(
                kind="material",
                id="document-material",
                payload=SimpleNamespace(
                    snapshot_id="document-snapshot",
                    support=SimpleNamespace(record_ref=document_snapshot.records[0].record_ref),
                    stance="context",
                    text="SYNTHETIC original selected from graph",
                    machine_analysis_id=None,
                ),
            ),
        ]
    )
    evidence, *_ = service._initial_evidence(None, None, view, [])
    originals = {graph["source_id"]: graph, document["source_id"]: document}
    prepared = [
        freeze_passages({**item, **source_excerpt(originals[item["source_id"]], 40000)}) for item in evidence
    ]
    assert requests == []
    reader.client.close()
    return service, view, evidence, prepared


def fixed(policy="query_planner", version="semantic-v2"):
    return {
        "question": "SYNTHETIC graph inquiry",
        "as_of": "2026-10-30T00:00:00Z",
        "search_scope": ["fixture-source"],
        "query_policy": {"effective": policy},
        "query_context_version": version,
    }


def test_actual_human_stance_note_and_entry_identity_survive_without_rewriting_source():
    service, view, evidence, prepared = projection()
    other = copy.deepcopy(view)
    other.entries[0].payload.stance = "support"
    other.entries[0].payload.text = "SYNTHETIC distinct unverified supporting note"
    changed, *_ = service._initial_evidence(None, None, other, [])
    assert evidence != changed
    assert material_context_identity(evidence) != material_context_identity(changed)
    assert evidence_basis_identity(evidence) != evidence_basis_identity(changed)
    assert basis_hash("Q", evidence, None, "M", None, []) != basis_hash("Q", changed, None, "M", None, [])
    for old, new in zip(evidence, changed, strict=True):
        assert (old["record_ref"], old["text"], old["text_sha256"]) == (
            new["record_ref"],
            new["text"],
            new["text_sha256"],
        )
    payload = freeze_payload("read_plan", fixed(), prepared)
    human = next(item for item in payload["material_contexts"] if item["entry_id"] == "graph-material")
    assert human["stance"] == "oppose" and human["origin"] == "user_supplied"
    assert "待验证" in human["qualification"]
    assert not any(
        human["note"] in passage["text"] for item in payload["evidence"] for passage in item["passages"]
    )


def test_graph_package_is_qualified_background_and_original_version_remains_original():
    _, _, _, prepared = projection()
    payload = freeze_payload("synthesize", fixed(), prepared)
    graph = payload["graph_materials"][0]
    assert graph["source_id"] == "reviewed_graph" and graph["evidence_role"] == "graph_background"
    assert "不能独立证明临床获益或因果机制" in graph["qualification"]
    assert graph["version"] == graph["record_ref"]["adopted_version"]
    assert any(
        item["source_id"] == "fixture-source" and item["evidence_role"] == "source_material"
        for item in prepared
    )
    assert all(item["source_id"] != "fixture-source" for item in payload["graph_materials"])
    for old in (fixed("baseline"), fixed(version=None)):
        wire = freeze_payload("synthesize", old, prepared)
        assert "material_contexts" not in wire and "graph_materials" not in wire
        stripped = [
            {k: v for k, v in item.items() if k not in {"material_context", "evidence_role"}}
            for item in prepared
        ]
        assert wire == freeze_payload("synthesize", old, stripped)


def test_empty_material_context_and_default_source_role_preserve_legacy_basis_hash():
    _, _, evidence, _ = projection()
    legacy = [
        {k: v for k, v in item.items() if k not in {"material_context", "evidence_role"}} for item in evidence
    ]
    defaults = [{**item, "material_context": None, "evidence_role": "source_material"} for item in legacy]
    assert evidence_basis_identity(defaults) == evidence_basis_identity(legacy)
    assert material_context_identity(defaults) == {}
    assert basis_hash("Q", legacy, None, "M", None, []) == basis_hash("Q", defaults, None, "M", None, [])


def test_report_assessment_receives_same_context_only_for_new_external_version():
    _, _, _, prepared = projection()
    progress = {"evidence": prepared, "queries": [], "calls": [], "query_policy": {"checks": []}}
    report = AnalysisService._report_context(fixed(), progress, {})
    read = freeze_payload("read_plan", fixed(), prepared)
    assert report["material_contexts"] == read["material_contexts"]
    assert report["graph_materials"] == read["graph_materials"]
    for old in (fixed("baseline"), fixed(version=None)):
        report = AnalysisService._report_context(old, progress, {})
        assert "material_contexts" not in report and "graph_materials" not in report
