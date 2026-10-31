"""SYNTHETIC independent assessment axes; no model, network or business material."""

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bioradar.research.mining.intelligence_assessment import (
    default_assessment_axes,
    finalize_human_assessment_axes,
    project_reviewed_assessment_axes,
)
from bioradar.research.mining.models import (
    AssessmentAxesReviewInput,
    EngineRecord,
    ProviderRef,
    SignalContext,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 31, 10, tzinfo=UTC)


def record(identifier, family=None, *, repost=None, version="v1"):
    text = "SYNTHETIC医院报告3例肺炎聚集，尚待核实。"
    digest = hashlib.sha256(text.encode()).hexdigest()
    ref = ProviderRef(
        provider="fixture",
        object_type="document",
        object_id=identifier,
        adopted_version=version,
        version_source="provider",
        content_hash=digest,
        authorization_ref="SYNTHETIC-grant",
    )
    relations = ({"kind": "repost", "target_ref": repost.record_ref},) if repost else ()
    return EngineRecord(
        record_ref=ref,
        document_hash=digest,
        source_instance="fixture",
        source_id="SYNTHETIC-source",
        observed_at=NOW,
        source_family=family,
        source_independence_status="confirmed" if family else "unresolved",
        normalized_text=text,
        normalization_version="SYNTHETIC-v1",
        text_status="inline_authorized",
        signal_context=SignalContext(
            material_type="official_notice",
            attribution="named_institution",
            source_relations=relations,
            document_evidence={"external_rating": {"taxonomy": "admiralty-scale", "grade": "G"}},
        ),
    )


def ref(r):
    return r.record_ref.model_dump(mode="json")


def review(records, *, info_grade="2", source_grade="B", confirmation_basis=""):
    refs = [ref(r) for r in records]
    return {
        "source_reliability": [
            {
                "grade": source_grade,
                "record_ref": refs[0],
                "publisher_scope": "SYNTHETIC publisher, declared scope only",
                "assessment_basis_refs": refs,
                "assessment_basis": "SYNTHETIC explicit reliability history basis",
            }
        ],
        "information_credibility": [
            {
                "grade": info_grade,
                "record_ref": refs[0],
                "claim_id": "SYNTHETIC-claim-1",
                "claim_text": "SYNTHETIC有3例待核实观察",
                "assessment_basis_refs": refs,
                "assessment_basis": "SYNTHETIC reviewer compares this exact claim",
                "independent_confirmation_refs": refs if info_grade == "1" else [],
                "independent_confirmation_basis": confirmation_basis,
            }
        ],
    }


def final(request, records, *, identifier="SYNTHETIC-review-1", revision=1, when=NOW):
    return finalize_human_assessment_axes(
        request, records, actor_id="SYNTHETIC-human", assessed_at=when, entry_id=identifier, revision=revision
    )


def entry(axes, *, identifier="SYNTHETIC-review-1", revision=1, when=NOW):
    return SimpleNamespace(
        id=identifier,
        kind="judgment",
        revision=revision,
        created_by="SYNTHETIC-human",
        created_at=when,
        payload={"kind": "judgment", "assessment_axes": axes.model_dump(mode="json")},
    )


def test_defaults_do_not_turn_official_identity_evidence_heat_or_native_G_into_ratings():
    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    heat = {"score": 999, "qualification": "SYNTHETIC exposure only"}
    axes = default_assessment_axes([source], evidence_level="institution_confirmed", heat=heat)
    assert axes.source_reliability[0].grade == "F"
    assert axes.information_credibility[0].grade == "6"
    assert axes.source_reliability[0].assessment_status == "not_assessed"
    assert axes.information_credibility[0].reason_code == "basis_missing"
    assert axes.source_reliability[0].assessed_at is None
    assert axes.evidence_level == "institution_confirmed" and axes.heat == heat
    assert source.signal_context.document_evidence["external_rating"]["grade"] == "G"
    assert default_assessment_axes([source]).heat is None


def test_review_input_rejects_G_and_client_claims_of_server_review_authority():
    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    data = review([source], source_grade="G")
    with pytest.raises(ValidationError):
        AssessmentAxesReviewInput.model_validate(data)
    data = review([source])
    data["source_reliability"][0]["assessment_status"] = "human_reviewed"
    with pytest.raises(ValidationError):
        AssessmentAxesReviewInput.model_validate(data)


def test_information_one_requires_explicit_basis_and_independent_originals_not_reposts():
    first = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    second = record("SYNTHETIC-two", "SYNTHETIC-family-two")
    with pytest.raises(ValueError, match="confirmation basis"):
        final(review([first, second], info_grade="1"), [first, second])
    same = record("SYNTHETIC-same", "SYNTHETIC-family-one")
    with pytest.raises(ValueError, match="independent"):
        final(
            review([first, same], info_grade="1", confirmation_basis="SYNTHETIC declaration"), [first, same]
        )
    repost = record("SYNTHETIC-repost", "SYNTHETIC-family-two", repost=first)
    with pytest.raises(ValueError, match="independent|original"):
        final(
            review([first, repost], info_grade="1", confirmation_basis="SYNTHETIC declaration"),
            [first, repost],
        )
    axes = final(
        review(
            [first, second],
            info_grade="1",
            confirmation_basis="SYNTHETIC two original accounts independently confirm this claim",
        ),
        [first, second],
    )
    assert axes.information_credibility[0].grade == "1"
    assert axes.information_credibility[0].assessed_by == "SYNTHETIC-human"
    assert axes.information_credibility[0].assessed_at == NOW
    assert first.study_independence_status == second.study_independence_status == "unresolved"


def test_explicit_review_preserves_fixed_versions_revision_history_and_future_cutoff():
    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    base = default_assessment_axes([source], evidence_level="unverified", heat={"score": 2})
    first = final(review([source]), [source])
    saved = entry(first)
    original = copy.deepcopy(saved.payload)
    latest = project_reviewed_assessment_axes(base, [saved])
    assert latest.source_reliability[0].grade == "B"
    assert (
        next(item for item in latest.information_credibility if item.claim_id == "SYNTHETIC-claim-1").grade
        == "2"
    )
    assert latest.evidence_level == "unverified" and latest.heat == {"score": 2}
    second = final(
        review([source], info_grade="5"),
        [source],
        identifier="SYNTHETIC-review-2",
        revision=2,
        when=NOW + timedelta(hours=1),
    )
    changed = project_reviewed_assessment_axes(
        base,
        [saved, entry(second, identifier="SYNTHETIC-review-2", revision=2, when=NOW + timedelta(hours=1))],
    )
    assert (
        next(item for item in changed.information_credibility if item.claim_id == "SYNTHETIC-claim-1").grade
        == "5"
        and saved.payload == original
    )
    historical = project_reviewed_assessment_axes(base, [saved], cutoff=NOW - timedelta(seconds=1))
    assert (
        historical.source_reliability[0].grade == "F" and historical.information_credibility[0].grade == "6"
    )
    newer = record("SYNTHETIC-one", "SYNTHETIC-family-one", version="v2")
    assert (
        project_reviewed_assessment_axes(default_assessment_axes([newer]), [saved])
        .source_reliability[0]
        .grade
        == "F"
    )


def test_self_asserted_human_ratings_without_matching_native_entry_are_not_authority():
    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    forged = final(review([source]), [source])
    assert project_reviewed_assessment_axes(forged, []).source_reliability[0].grade == "F"
    wrong = entry(forged)
    wrong.created_by = "SYNTHETIC-other-human"
    assert (
        project_reviewed_assessment_axes(default_assessment_axes([source]), [wrong])
        .source_reliability[0]
        .grade
        == "F"
    )


def test_review_cannot_bind_a_missing_or_changed_original_version():
    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    changed = record("SYNTHETIC-one", "SYNTHETIC-family-one", version="v2")
    with pytest.raises(ValueError, match="fixed|adopted|version"):
        final(review([source]), [changed])


def test_candidate_and_source_contribution_expose_separate_axes_without_changing_attention():
    from bioradar.research.mining.models import Candidate
    from bioradar.research.mining.signals import with_attention
    from bioradar.research.mining.topic_series import _evidence, project_contribution
    from test_history_topic_replay import DAY, sample, scope

    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    candidate = Candidate(
        candidate_key="SYNTHETIC-key",
        method_id="text_topics",
        method_version="SYNTHETIC-v1",
        group_key="SYNTHETIC-group",
        kind="hypothesis",
        title="SYNTHETIC control",
        reasons=(),
        supports=(),
    )
    annotated = with_attention(candidate, [source])
    assert annotated.assessment_axes is not None
    assert annotated.assessment_axes.evidence_level == annotated.metrics["attention"]["evidence_level"]
    assert annotated.assessment_axes.source_reliability[0].grade == "F"
    assert annotated.assessment_axes.heat is None
    historic, original = sample()
    value = project_contribution(historic, original, scope(), as_of=DAY)
    assert value["assessment_axes"]["source_reliability"][0]["grade"] == "F"
    assert value["assessment_axes"]["information_credibility"][0]["grade"] == "6"
    assert _evidence(value)[1]["assessment_axes"] == value["assessment_axes"]


def test_known_version_key_with_wrong_hash_does_not_bind_a_human_rating():
    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    wrong = review([source])
    wrong["source_reliability"][0]["record_ref"]["content_hash"] = "SYNTHETIC-wrong-hash"
    with pytest.raises(ValueError, match="exact|hash"):
        final(wrong, [source])


def analysis_fixture(source):
    from bioradar.research.mining.models import canonical_hash

    claim = {
        "id": "SYNTHETIC-claim-1",
        "text": "SYNTHETIC有3例待核实观察",
        "kind": "source_statement",
        "conditions": ["SYNTHETIC fixed condition"],
        "evidence_links": [{"evidence_key": "E1"}],
    }
    analysis = {
        "id": "SYNTHETIC-analysis-1",
        "investigation_id": "SYNTHETIC-investigation-1",
        "input_hash": "SYNTHETIC-input-1",
        "evidence": [{"key": "E1", "record_ref": ref(source)}],
        "result": {"claims": [claim], "report": {"final_text": "SYNTHETIC original checked text"}},
    }
    axes = final(review([source]), [source])
    saved = entry(axes)
    saved.investigation_id = analysis["investigation_id"]
    saved.payload["review"] = {
        "target": {
            "type": "claim",
            "analysis_id": analysis["id"],
            "claim_id": claim["id"],
            "claim_sha256": canonical_hash(claim),
        },
        "basis_record_refs": [ref(source)],
        "analysis_input_hash": analysis["input_hash"],
    }
    return analysis, saved


def test_analysis_projection_requires_complete_current_claim_binding_not_candidate_or_text_only():
    from bioradar.research.mining.intelligence_assessment import analysis_assessment_axes

    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    analysis, saved = analysis_fixture(source)
    original = copy.deepcopy(analysis)
    axes = analysis_assessment_axes(analysis, [saved])
    assert axes.information_credibility[0].grade == "2"
    changed = copy.deepcopy(analysis)
    changed["result"]["claims"][0]["conditions"] = ["SYNTHETIC changed condition"]
    assert analysis_assessment_axes(changed, [saved]).information_credibility[0].grade == "6"
    saved.payload["review"]["target"] = {"type": "candidate", "candidate_key": "SYNTHETIC-key"}
    assert analysis_assessment_axes(analysis, [saved]).source_reliability[0].grade == "F"
    assert analysis == original


def test_analysis_report_binding_is_exact_and_historical_review_remains_outside_cutoff():
    from bioradar.research.mining.intelligence_assessment import analysis_assessment_axes

    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    analysis, saved = analysis_fixture(source)
    saved.payload["review"]["target"] = {
        "type": "report",
        "analysis_id": analysis["id"],
        "text_sha256": hashlib.sha256(analysis["result"]["report"]["final_text"].encode()).hexdigest(),
    }
    assert analysis_assessment_axes(analysis, [saved]).source_reliability[0].grade == "B"
    assert (
        analysis_assessment_axes(analysis, [saved], cutoff=NOW - timedelta(seconds=1))
        .source_reliability[0]
        .grade
        == "F"
    )
    historical = copy.deepcopy(analysis)
    historical["evidence_time"] = {
        "mode": "public_history_reconstruction",
        "cutoff": (NOW - timedelta(seconds=1)).isoformat(),
    }
    assert analysis_assessment_axes(historical, [saved]).source_reliability[0].grade == "F"
    with pytest.raises(ValueError, match="cutoff"):
        analysis_assessment_axes(historical, [saved], cutoff=NOW)
    changed = copy.deepcopy(analysis)
    changed["result"]["report"]["final_text"] += "SYNTHETIC changed"
    assert analysis_assessment_axes(changed, [saved]).source_reliability[0].grade == "F"


def test_analysis_information_rating_cannot_borrow_other_source_version_for_same_claim():
    from bioradar.research.mining.intelligence_assessment import analysis_assessment_axes

    source = record("SYNTHETIC-one", "SYNTHETIC-family-one")
    analysis, saved = analysis_fixture(source)
    other = record("SYNTHETIC-other", "SYNTHETIC-family-other")
    analysis["evidence"].append({"key": "E2", "record_ref": ref(other)})
    saved.payload["assessment_axes"] = final(review([other]), [other]).model_dump(mode="json")
    saved.payload["review"]["basis_record_refs"] = [ref(other)]
    assert all(
        item.grade == "6" for item in analysis_assessment_axes(analysis, [saved]).information_credibility
    )
