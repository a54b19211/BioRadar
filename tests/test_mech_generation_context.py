"""SYNTHETIC reviewed Mech output: effective context is separate from raw audit reviews."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from bioradar.adapters import mech_evidence
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import (
    compact_mechanism,
    compact_query_context,
    freeze_payload,
    report_context,
    semantic_reading_state,
)
from bioradar.research.investigations.query_policy import mechanism_query_context
from test_shared_research_adapters import installed_mech as installed_mech  # noqa: F401
from test_shared_research_adapters import mechanism_packet

pytestmark = pytest.mark.BOOT


def native_result():
    note = {
        "target": "local.observations.L1.limitations.0",
        "status": "not_reported",
        "statement": "SYNTHETIC method use was not reported; its conduct remains unknown.",
        "negative_kind": None,
        "anchors": [
            {
                "material_id": "e1",
                "passage_id": "e1:P001",
                "start": 0,
                "end": 9,
                "quote": "SYNTHETIC",
                "version": "fixed-v1",
            }
        ],
        "scope_passage_ids": ["e1:P001"],
        "original_start": 0,
        "original_end": 15,
    }
    return {
        "provider": "mech_evidence",
        "provider_version": "SYNTHETIC-current-provider",
        "status": "completed",
        "overall": {"verdict": "insufficient"},
        "claims": [{"id": "C1", "text": "SYNTHETIC bounded hypothesis", "opposing_observation_ids": ["L1"]}],
        "joins": [],
        "critical_gaps": [{"reason": "SYNTHETIC effective unresolved method gap"}],
        "unresolved_counterevidence": ["L1"],
        "conflicts": [],
        "evidence": [
            {
                "id": "L1",
                "rationale": "SYNTHETIC revised effective statement",
                "references": [{"id": "R1", "material_id": "e1", "version": "fixed-v1"}],
            }
        ],
        "local_review": {"observations": [{"id": "L1", "limitations": [note["statement"]]}]},
        "mechanism_review": {"observations": []},
        "source_reporting": {
            "status": "reviewed",
            "method": "source-reporting-v1",
            "notes": [note],
            "findings": {"L1": {"status": "not_reported", "negative_kind": "not_disclosed"}},
            "raw_reviews": {
                "local_review": {"old": "AUDIT_ONLY_UNREVISED"},
                "mechanism_review": {"old": "AUDIT_ONLY_UNREVISED"},
            },
        },
    }


def context(result):
    return {"coverage_checks": [], "query_history": [], "mechanism_evaluation": result}


def test_compact_projection_retains_reviewed_modality_and_excludes_only_declared_raw_reviews():
    native = native_result()
    before = copy.deepcopy(native)
    model = compact_mechanism(native)
    expected = copy.deepcopy(native["source_reporting"])
    expected.pop("raw_reviews")
    assert model["source_reporting"] == expected
    for key in ("claims", "joins", "critical_gaps", "conflicts", "unresolved_counterevidence", "evidence"):
        assert model[key] == native[key]
    assert "AUDIT_ONLY_UNREVISED" not in json.dumps(model)
    assert native == before
    model["source_reporting"]["notes"][0]["statement"] = "SYNTHETIC independent copy"
    assert native == before


def test_transport_projection_preserves_native_fields_and_scientific_quotes_without_mutation():
    native = native_result()
    native["scientific_extension"] = {"raw_reviews": "SYNTHETIC legitimate non-audit field"}
    expected = copy.deepcopy(native)
    expected["source_reporting"].pop("raw_reviews")
    assert mech_evidence.mechanism_model_context(native) == expected
    assert "raw_reviews" in native["source_reporting"]


@pytest.mark.parametrize(
    "reporting",
    [None, [], {"status": "legacy_unreviewed"}, {"status": "reviewed", "method": "SYNTHETIC-other-method"}],
)
def test_explicit_unreviewed_or_wrong_method_cannot_become_generation_context(reporting):
    native = native_result()
    native["source_reporting"] = reporting
    with pytest.raises(DomainError) as error:
        compact_mechanism(native)
    assert error.value.code == "mech_evidence_source_reporting_invalid"


def test_all_existing_read_synthesis_and_report_context_boundaries_use_the_same_projection():
    native = native_result()
    fixed = {
        "question": "SYNTHETIC question",
        "as_of": "2026-10-20T00:00:00Z",
        "search_scope": ["fixture-source"],
        "mechanism_evaluation": native,
        "query_context_version": "semantic-v2",
        "query_policy": {"effective": "query_planner"},
    }
    read = freeze_payload("read_plan", fixed, [], query_context=context(native))
    synthesize = freeze_payload("synthesize", fixed, [], query_context=context(native))
    report = report_context({"claims": []}, native)
    compact = compact_query_context(context(native))
    projected = mechanism_query_context(native)
    for result in (read, synthesize, report, compact, projected):
        encoded = json.dumps(result)
        assert "AUDIT_ONLY_UNREVISED" not in encoded
        assert native["source_reporting"]["notes"][0]["statement"] in encoded
        assert "SYNTHETIC effective unresolved method gap" in encoded
        assert "not_disclosed" in encoded
    assert read["mechanism_query_context"]["claims"][0]["opposing_observation_ids"] == ["L1"]


def test_reading_identity_ignores_audit_only_changes_but_tracks_effective_notes_and_findings():
    native = native_result()
    before = semantic_reading_state([], context(native))
    audit = copy.deepcopy(native)
    audit["source_reporting"]["raw_reviews"]["local_review"]["old"] = "SYNTHETIC later audit copy"
    assert semantic_reading_state([], context(audit)) == before
    changed = copy.deepcopy(native)
    changed["source_reporting"]["notes"][0]["statement"] = "SYNTHETIC changed effective qualification"
    assert semantic_reading_state([], context(changed)) != before
    changed = copy.deepcopy(native)
    changed["source_reporting"]["findings"]["L1"]["status"] = "reported"
    assert semantic_reading_state([], context(changed)) != before


def test_legacy_without_source_reporting_keeps_the_existing_compact_bytes():
    legacy = {
        "provider": "mech_evidence",
        "status": "completed",
        "overall": {"verdict": "insufficient"},
        "claims": [],
        "source_internal": "SYNTHETIC old non-context field",
    }
    expected = {key: legacy[key] for key in ("provider", "status", "overall", "claims")}
    assert json.dumps(compact_mechanism(legacy), separators=(",", ":")) == json.dumps(
        expected, separators=(",", ":")
    )
    assert mech_evidence.mechanism_model_context(legacy) == legacy


def test_new_source_notes_loader_dependency_is_pinned_and_loaded_before_assess(installed_mech):
    root = installed_mech.mech_evidence_project_dir / "mechevidence"
    original_assess = (root / "assess.py").read_text()
    old_sources, old_version = mech_evidence._sources(installed_mech)
    assert (
        old_version
        == hashlib.sha256(
            "".join(name + "\0" + old_sources[name] for name in ("models.py", "assess.py")).encode()
        ).hexdigest()
    )
    (root / "source_notes.py").write_text("SYNTHETIC_DEPENDENCY = True\n")
    (root / "assess.py").write_text("from .source_notes import SYNTHETIC_DEPENDENCY\n" + original_assess)
    models, method, version = mech_evidence.load_mech_evidence(installed_mech)
    assert models and method and method.SYNTHETIC_DEPENDENCY and version != old_version
    sources, frozen = mech_evidence._sources(installed_mech)
    assert tuple(sources) == ("models.py", "source_notes.py", "assess.py")
    assert (
        frozen
        == hashlib.sha256(
            "".join(name + "\0" + source for name, source in sources.items()).encode()
        ).hexdigest()
    )
    (root / "source_notes.py").write_text("SYNTHETIC_DEPENDENCY = False\n")
    assert mech_evidence.provider_status(installed_mech)["version"] != version
    assert method.SYNTHETIC_DEPENDENCY is True
    assert not (root / "__pycache__").exists()


def test_evaluation_keeps_the_complete_native_result_separately_from_generation_context(installed_mech):
    native = native_result()
    models, _, version = mech_evidence.load_mech_evidence(installed_mech)
    method = SimpleNamespace(evaluate=lambda case, callback: copy.deepcopy(native))
    actual = mech_evidence.evaluate_mechanism(
        installed_mech, mechanism_packet(), None, loaded=(models, method, version)
    )
    assert actual["status"] == "completed"
    assert actual["source_reporting"] == native["source_reporting"]
    assert "raw_reviews" not in compact_mechanism(actual)["source_reporting"]
    assert actual["source_reporting"] == native["source_reporting"]
