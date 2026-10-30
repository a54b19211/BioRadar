"""Actual method input passages retain their independent fixed-source identity."""

import copy

from bioradar.adapters.mech_evidence import mechanism_passage_bindings
from bioradar.research.investigations.analysis import compact_mechanism


def inputs():
    text = "TITLE\nA binds B.\nThe causal test was not established."
    packet = {"evidence": [{"id": "E1", "version": "v1", "text": text}]}
    materials = [
        {
            "id": "E1",
            "version": "v1",
            "passages": [{"id": "E1:P002", "start": 6, "end": 16, "text": text[6:16]}],
        }
    ]
    assessment = {
        "overall": {"verdict": "insufficient"},
        "evidence": [
            {
                "id": "L1",
                "causal_test": "not_established",
                "relation": "compatible",
                "references": [{"id": "E1:P002", "material_id": "E1", "version": "v1"}],
            }
        ],
    }
    radar = [
        {
            "key": "E1",
            "record_ref": {"adopted_version": "v1"},
            "text": text,
            "passages": [{"id": "E1:p001", "start": 0, "end": len(text), "text": text}],
        }
    ]
    return packet, materials, assessment, radar


def test_actual_mechanism_input_passage_keeps_quote_offsets_version_and_radar_containment():
    packet, materials, assessment, radar = inputs()
    before = copy.deepcopy((packet, materials, assessment, radar))
    metadata = mechanism_passage_bindings(
        packet, materials + materials, assessment=assessment, radar_evidence=radar
    )
    assert (packet, materials, assessment, radar) == before
    assert metadata["passage_binding_errors"] == []
    assert len(metadata["passage_bindings"]) == 1
    binding = metadata["passage_bindings"][0]
    assert binding["id"] == "E1:P002" and binding["version"] == "v1"
    assert binding["quote"] == packet["evidence"][0]["text"][binding["start"] : binding["end"]]
    assert binding["radar_passages"] == [
        {"id": "E1:p001", "start": 0, "end": len(radar[0]["text"]), "contains_reference": True}
    ]
    compact = compact_mechanism(assessment | metadata)
    assert compact["passage_bindings"] == metadata["passage_bindings"]
    assert compact["passage_namespace_qualification"] == metadata["passage_namespace_qualification"]
    assert compact["evidence"] == assessment["evidence"]
    assert compact["overall"] == {"verdict": "insufficient"}


def test_wrong_version_or_literal_offsets_never_create_a_verified_binding():
    packet, materials, assessment, radar = inputs()
    materials[0]["passages"][0]["text"] = "An invented different quote"
    metadata = mechanism_passage_bindings(packet, materials, assessment=assessment, radar_evidence=radar)
    assert metadata["passage_bindings"] == [] and metadata["passage_binding_errors"]
    packet, materials, assessment, radar = inputs()
    packet["evidence"][0]["version"] = "different-fixed-version"
    metadata = mechanism_passage_bindings(packet, materials, assessment=assessment, radar_evidence=radar)
    assert metadata["passage_bindings"] == [] and metadata["passage_binding_errors"]


def test_absent_recorded_passage_is_reported_without_resegmentation_or_id_substitution():
    packet, materials, assessment, radar = inputs()
    assessment["evidence"][0]["references"][0]["id"] = "E1:P003"
    metadata = mechanism_passage_bindings(packet, materials, assessment=assessment, radar_evidence=radar)
    assert metadata["passage_bindings"] == []
    assert metadata["passage_binding_errors"] == [
        {"id": "E1:P003", "material_id": "E1", "reason": "no_verified_input_passage"}
    ]
