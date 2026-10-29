"""Use the installed MechEvidence method; callbacks are synthetic and make no model requests."""

import copy
import json
import os
from pathlib import Path

import pytest
from bioradar.adapters.biocheck import build_packet
from bioradar.adapters.mech_evidence import evaluate_mechanism, load_mech_evidence
from bioradar.api.config import Settings

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def current_mech():
    configured = os.environ.get("BIORADAR_TEST_MECH_EVIDENCE_PROJECT_DIR")
    directory = Path(configured) if configured else Path(__file__).resolve().parents[2] / "MechEvidence"
    if not directory.is_dir():
        if configured:
            pytest.fail("Configured MechEvidence test project does not exist")
        pytest.skip("Requires the current installed MechEvidence project, not a copied source fixture")
    settings = Settings(mech_evidence_project_dir=directory)
    loaded = load_mech_evidence(settings)
    assert loaded[0] is not None and loaded[1] is not None
    return settings, loaded


def evidence(key, source, record, *, version=None, facts=None, **metadata):
    return {
        "key": key,
        "text": "SYNTHETIC A changed M. Blocking M prevented E.",
        "source_kind": "primary",
        "text_level": "abstract",
        "record_ref": {
            "provider": "data_fabric_history",
            "object_type": "source_record",
            "object_id": json.dumps([source, record, False]),
            "adopted_version": version or key + "-version",
        },
        "facts": facts or {},
        **metadata,
    }


def run(settings, loaded, originals, *, mechanism_case=None):
    packet = build_packet(
        "SYNTHETIC publication identity question",
        originals,
        {"mechanism": "SYNTHETIC A changes M, and M mediates E.", "mechanism_scope": {"species": "human"}},
    )
    if mechanism_case is not None:
        packet["mechanism_case"] = mechanism_case
    calls = []

    def observation(identifier, materials, *, join=False):
        return {
            "id": identifier,
            "claim_ids": [] if join else ["C1", "C2"],
            "join_ids": ["J1"] if join else [],
            "relation": "supports",
            "basis": "intervention",
            "causal_test": "established",
            "passage_ids": [part["id"] for material in materials for part in material["passages"]],
            "context": {"species": "human"},
            "applicability": {"species": "matched", "system": "not_required", "disease": "not_required"},
            "rationale": "SYNTHETIC identity integration callback; not a scientific quality evaluation.",
            "limitations": [],
        }

    def call(stage, _instruction, payload, _schema):
        calls.append((stage, copy.deepcopy(payload)))
        if stage == "mech_decomposition":
            return {
                "claims": [{"id": "C1", "text": "A changes M"}, {"id": "C2", "text": "M mediates E"}],
                "joins": [{"id": "J1", "text": "A produces E through M", "claim_ids": ["C1", "C2"]}],
            }
        if stage == "mech_local_review":
            return {
                "observations": [
                    observation("L" + str(index), [material])
                    for index, material in enumerate(payload["materials"], 1)
                ],
                "gaps": [],
            }
        assert stage == "mech_mechanism_review"
        local_ids = [item["id"] for item in payload["local_review"]["observations"]]
        return {
            "additional_claims": [], "additional_joins": [],
            "observations": [observation("M1", payload["materials"][:1], join=True)],
            "combinations": [{
                "id": "W1", "observation_ids": local_ids + ["M1"],
                "context_coherence": "matched", "rationale": "SYNTHETIC matched context.",
            }],
            "comparisons": [], "whole_assessment": "supported",
            "rationale": "SYNTHETIC identity boundary only.", "gaps": [],
        }

    return evaluate_mechanism(settings, packet, call, loaded=loaded), calls, packet


@pytest.mark.parametrize("native_id", ["MED:40000000", "med:40000000"])
def test_same_pmid_across_pubmed_and_europe_pmc_is_one_paper_without_urls(current_mech, native_id):
    settings, loaded = current_mech
    originals = [
        evidence("pubmed", "pubmed", "40000000", version="pubmed-fixed"),
        evidence("epmc", "europe_pmc", native_id, version="epmc-fixed"),
    ]
    result, calls, packet = run(settings, loaded, originals)
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["pmid:40000000"]
    assert result["independence"]["distinct_source_groups"] == 1
    assert result["independence"]["independent_cohorts"] is None
    references = [ref for item in result["evidence"] for ref in item["references"]]
    assert {(ref["source_id"], ref["record_id"], ref["version"]) for ref in references} == {
        ("pubmed", "40000000", "pubmed-fixed"),
        ("europe_pmc", native_id, "epmc-fixed"),
    }
    assert result["evidence_map"] == packet["evidence_map"]
    assert all("source_url" not in item for item in packet["evidence"])
    local = next(payload for stage, payload in calls if stage == "mech_local_review")
    assert {item["source_group"] for item in local["materials"]} == {"pmid:40000000"}


def test_metadata_cannot_relabel_a_fixed_source_as_pubmed(current_mech):
    settings, loaded = current_mech
    result, _, _ = run(settings, loaded, [
        evidence("registry", "clinicaltrials", "40000000",
                 source_id="pubmed", source_record_id="40000000"),
        evidence("paper", "pubmed", "40000000"),
    ])
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == [
        "pmid:40000000", "record:clinicaltrials:40000000",
    ]
    assert result["independence"]["independent_cohorts"] is None


def test_metadata_cannot_replace_the_fixed_record_identity(current_mech):
    settings, loaded = current_mech
    result, _, _ = run(settings, loaded, [
        evidence("pubmed", "pubmed", "40000000"),
        evidence("epmc", "europe_pmc", "MED:40000000",
                 source_id="europe_pmc", source_record_id="MED:50000000"),
    ])
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["pmid:40000000"]


def test_different_papers_do_not_establish_independent_cohorts(current_mech):
    settings, loaded = current_mech
    result, _, _ = run(settings, loaded, [
        evidence("first", "pubmed", "40000000"),
        evidence("second", "europe_pmc", "MED:50000000"),
    ])
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["pmid:40000000", "pmid:50000000"]
    assert result["independence"]["distinct_source_groups"] == 2
    assert result["independence"]["independent_cohorts"] is None


@pytest.mark.parametrize("field,value", [
    ("source_id", "europe_pmc"),
    ("record_id", "50000000"),
    ("source_group", "PMID:50000000"),
    ("url", "https://pubmed.ncbi.nlm.nih.gov/50000000/"),
    ("pmid", "50000000"),
    ("doi", "10.1000/unbound"),
])
def test_explicit_case_cannot_introduce_unbound_publication_identity(current_mech, field, value):
    settings, loaded = current_mech
    original = evidence("paper", "pubmed", "40000000")
    material = {
        "id": "paper", "text": original["text"], "version": "paper-version",
        "source_kind": "primary", "text_level": "abstract", field: value,
    }
    case = {
        "question": "SYNTHETIC identity", "mechanism": "SYNTHETIC mechanism",
        "scope": {"species": "human"}, "materials": [material],
    }
    before = copy.deepcopy(case)
    result, calls, _ = run(settings, loaded, [original], mechanism_case=case)
    assert result["status"] == "needs_input"
    assert result["error_code"] == "mech_evidence_material_mismatch"
    assert not calls and case == before


def test_explicit_case_inherits_the_fixed_source_identity(current_mech):
    settings, loaded = current_mech
    original = evidence("paper", "pubmed", "40000000")
    case = {
        "question": "SYNTHETIC identity", "mechanism": "SYNTHETIC mechanism",
        "scope": {"species": "human"},
        "materials": [{
            "id": "paper", "text": original["text"], "version": "paper-version",
            "source_kind": "primary", "text_level": "abstract",
        }],
    }
    result, calls, _ = run(settings, loaded, [original], mechanism_case=case)
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["pmid:40000000"]
    local = next(payload for stage, payload in calls if stage == "mech_local_review")
    assert local["materials"][0]["source_id"] == "pubmed"
    assert local["materials"][0]["record_id"] == "40000000"
    assert local["materials"][0]["version"] == "paper-version"


@pytest.mark.parametrize("pmid", ["40000000", 40000000])
def test_sourced_publication_ids_reach_upstream_unchanged(current_mech, pmid):
    settings, loaded = current_mech
    originals = [
        evidence(
            "legacy", "legacy_documents", "legacy:pubmed:fixed-uuid", version="legacy-fixed",
            facts={"pmid": pmid, "doi": "10.1234/AbC.)"},
            pmid="stale-context", doi="stale-doi",
        ),
        evidence("epmc", "europe_pmc", "med:40000000", version="epmc-fixed"),
    ]
    before = copy.deepcopy(originals)
    result, calls, packet = run(settings, loaded, originals)
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["pmid:40000000"]
    local = next(payload for stage, payload in calls if stage == "mech_local_review")
    first = next(item for item in local["materials"] if item["id"] == "legacy")
    assert first["pmid"] == pmid and type(first["pmid"]) is type(pmid)
    assert first["doi"] == "10.1234/AbC.)"
    references = [ref for item in result["evidence"] for ref in item["references"]]
    legacy = next(ref for ref in references if ref["source_id"] == "legacy_documents")
    assert legacy["record_id"] == "legacy:pubmed:fixed-uuid"
    assert legacy["version"] == "legacy-fixed" and legacy["pmid"] == pmid
    assert legacy["doi"] == "10.1234/AbC.)"
    assert result["evidence_map"] == packet["evidence_map"] and originals == before


def test_sourced_doi_grouping_is_owned_by_upstream(current_mech):
    settings, loaded = current_mech
    result, calls, _ = run(settings, loaded, [
        evidence("legacy", "legacy_documents", "legacy:pubmed:first",
                 facts={"doi": "10.1234/AbC.)"}),
        evidence("publisher", "external_archive", "publisher-record",
                 facts={"doi": "10.1234/abc.)"}),
    ])
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["doi:10.1234/abc.)"]
    local = next(payload for stage, payload in calls if stage == "mech_local_review")
    assert [item["doi"] for item in local["materials"]] == ["10.1234/AbC.)", "10.1234/abc.)"]
    assert result["independence"]["independent_cohorts"] is None


def test_pmid_doi_coexistence_does_not_invent_cross_material_aliases(current_mech):
    settings, loaded = current_mech
    result, _, _ = run(settings, loaded, [
        evidence("both", "legacy_documents", "legacy:pubmed:first",
                 facts={"pmid": "40000000", "doi": "10.1234/known"}),
        evidence("doi_only", "external_archive", "second", facts={"doi": "10.1234/known"}),
    ])
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["doi:10.1234/known", "pmid:40000000"]
    assert result["independence"]["independent_cohorts"] is None


@pytest.mark.parametrize("facts", [{}, {"pmid": None, "doi": None}])
def test_absent_or_explicit_unknown_ids_do_not_add_ids_to_old_payloads(current_mech, facts):
    settings, loaded = current_mech
    result, calls, packet = run(settings, loaded, [
        evidence("paper", "external_archive", "opaque-record", facts=facts),
    ])
    assert result["status"] == "completed"
    local = next(payload for stage, payload in calls if stage == "mech_local_review")
    assert "pmid" not in local["materials"][0] and "doi" not in local["materials"][0]
    assert result["material_assessability"]["source_groups"] == ["record:external_archive:opaque-record"]
    for key, value in facts.items():
        assert key in packet["evidence"][0] and packet["evidence"][0][key] is value


@pytest.mark.parametrize("metadata,kind,code,fields", [
    ({"source_group": "PMID:50000000"}, "pmid", "material_identity_conflict", {"source_group", "record_id"}),
    ({"source_url": "https://pubmed.ncbi.nlm.nih.gov/50000000/"},
     "pmid", "material_identity_conflict", {"record_id", "url"}),
    ({"facts": {"pmid": "50000000"}}, "pmid", "material_identity_conflict", {"pmid", "record_id"}),
    ({"facts": {"pmid": "PMID:40000000"}}, "pmid", "material_identifier_invalid", {"pmid"}),
    ({"facts": {"pmid": 0}}, "pmid", "material_identifier_invalid", {"pmid"}),
    ({"facts": {"doi": "https://doi.org/10.1234/abc"}}, "doi", "material_identifier_invalid", {"doi"}),
    ({"facts": {"doi": "10.1234/a"}, "source_group": "DOI:10.1234/b"},
     "doi", "material_identity_conflict", {"doi", "source_group"}),
])
def test_upstream_identity_errors_keep_details_and_send_no_model_request(
    current_mech, metadata, kind, code, fields,
):
    settings, loaded = current_mech
    original = evidence("paper", "pubmed", "40000000", **metadata)
    result, calls, packet = run(settings, loaded, [original])
    assert result["status"] == "needs_input" and result["error_code"] == code
    assert result["identity_error"]["status"] == "needs_input"
    assert result["identity_error"]["material_id"] == "paper"
    assert result["identity_error"]["identifier_type"] == kind
    assert set(result["identity_error"]["fields"]) == fields
    assert set(result["missing_fields"]) == {f"materials.paper.{field}" for field in fields}
    assert result["evidence_map"] == packet["evidence_map"] and not calls
    assert result["provider_version"] == loaded[2] and result["overall"] is None


@pytest.mark.parametrize("url", [
    "https://example.invalid/path/pubmed.ncbi.nlm.nih.gov/50000000/",
    "https://example.invalid/?next=https://pubmed.ncbi.nlm.nih.gov/50000000/",
    "https://pubmed.ncbi.nlm.nih.gov.example.invalid/50000000/",
    "https://pubmed.ncbi.nlm.nih.gov@example.invalid/50000000/",
])
def test_fake_official_urls_do_not_override_native_pmid(current_mech, url):
    settings, loaded = current_mech
    result, calls, _ = run(settings, loaded, [
        evidence("paper", "pubmed", "40000000", source_url=url),
    ])
    assert result["status"] == "completed"
    assert result["material_assessability"]["source_groups"] == ["pmid:40000000"]
    local = next(payload for stage, payload in calls if stage == "mech_local_review")
    assert local["materials"][0]["url"] == url
