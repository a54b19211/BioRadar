"""Synthetic source material is retained without manufacturing quantitative facts."""

from datetime import timedelta

import pytest
from bioradar.research.datasets.intelligence import public_health_applicability, source_units
from bioradar.research.mining.models import (
    MetricObservation,
    MiningPolicy,
    SignalContext,
    SourceRelation,
    canonical_hash,
)
from bioradar.research.mining.prepare import prepare
from test_mining import AS_OF, NEW, provenance, record, ref, snapshot

pytestmark = pytest.mark.ENGINE


def material(name, text, **extra):
    return record(
        name,
        text,
        source_family=None,
        source_independence_status="unresolved",
        study_cluster_ref=None,
        study_independence_status="unresolved",
        **extra,
    )


def retained(rows, **policy_values):
    fixed = snapshot(rows)
    policy = MiningPolicy(as_of=AS_OF, **policy_values)
    prepared = prepare(fixed, policy)
    applicability = public_health_applicability(prepared)
    units = [
        unit
        for item in prepared.history
        for unit in source_units(item, applicability.get(item.record.record_ref.version_key), policy)
    ]
    return fixed, prepared, applicability, units


def test_nonnumeric_medical_advice_keeps_fixed_source_without_case_or_claim_invention():
    row = material("advice", "医疗提醒：发热者请佩戴口罩并按指引就诊。")
    original = canonical_hash(row)
    _, _, applicability, units = retained([row])
    assert row.record_ref.version_key in applicability
    assert [unit["unit_kind"] for unit in units] == ["source_statement"]
    unit = units[0]
    assert unit["features"]["claims"] == []
    assert unit["independent_study"] is False
    assert unit["dimensions"]["statement_status"] == "unknown"
    assert unit["members"][0]["record_ref"] == row.record_ref.model_dump(mode="json")
    assert unit["features"]["body_ref"]["text_hash"] == row.normalized_text_hash
    assert unit["members"][0]["source_independence_status"] == "unresolved"
    assert not {"case_count", "rate", "risk", "topic_id"}.intersection(unit["features"])
    assert canonical_hash(row) == original


def test_official_notice_requires_visible_health_relevance_not_source_or_material_label():
    unrelated = material(
        "unrelated",
        "办公楼下周暂停电梯运行。",
        source_id="unclassified-source",
        signal_context=SignalContext(material_type="official_notice"),
    )
    medical = material(
        "medical",
        "卫生机构发布健康提醒，出现不适请及时就医。",
        source_id="arbitrary-source",
        signal_context=SignalContext(material_type="official_notice", statement_status="source_reported"),
    )
    _, _, applicability, units = retained([unrelated, medical])
    assert unrelated.record_ref.version_key not in applicability
    assert medical.record_ref.version_key in applicability
    assert len(units) == 1 and units[0]["dimensions"]["statement_status"] == "source_reported"


@pytest.mark.parametrize("kind", ["correction", "retraction", "supplement", "repost"])
def test_only_same_snapshot_visible_exact_target_can_supply_relevance(kind):
    target = material("target", "医疗提醒：请按分诊安排就诊。")
    linked = material(
        "linked",
        "补充说明见原件。",
        signal_context=SignalContext(
            material_type="repost",
            statement_status="unverified_report",
            source_relations=(SourceRelation(kind=kind, target_ref=target.record_ref),),
        ),
    )
    _, _, applicability, units = retained([target, linked])
    assert applicability[linked.record_ref.version_key]["basis"][0]["target_ref"] == (
        target.record_ref.model_dump(mode="json")
    )
    linked_units = [
        unit
        for unit in units
        if unit["members"][0]["record_ref"] == (linked.record_ref.model_dump(mode="json"))
    ]
    assert {unit["unit_kind"] for unit in linked_units} == {"source_statement", "source_information_event"}
    assert all(unit["independent_study"] is False for unit in linked_units)
    event = next(unit for unit in linked_units if unit["unit_kind"] == "source_information_event")
    assert event["features"]["basis_status"] == "unavailable"
    assert "quote" not in event["features"]["event"]
    for wrong in (
        target.record_ref.model_copy(update={"adopted_version": "absent-version"}),
        target.record_ref.model_copy(update={"content_hash": "0" * 64}),
    ):
        invalid = linked.model_copy(
            update={
                "signal_context": SignalContext(
                    source_relations=(SourceRelation(kind=kind, target_ref=wrong),)
                )
            }
        )
        assert invalid.record_ref.version_key not in retained([target, invalid])[2]


def test_future_target_and_nonmedical_target_do_not_supply_domain():
    target = material("future", "医疗提醒：请按分诊安排就诊。", published=AS_OF + timedelta(days=1))
    linked = material(
        "linked",
        "原说法已更正。",
        signal_context=SignalContext(
            source_relations=(SourceRelation(kind="correction", target_ref=target.record_ref),)
        ),
    )
    _, prepared, applicability, _ = retained([target, linked], time_mode="availability_replay")
    assert prepared.excluded_records[0].record_ref == target.record_ref
    assert not applicability
    unrelated = material("business", "本次会议改至周三。")
    linked = linked.model_copy(
        update={
            "signal_context": SignalContext(
                source_relations=(SourceRelation(kind="repost", target_ref=unrelated.record_ref),)
            )
        }
    )
    assert not retained([unrelated, linked])[2]


def information_material(event_type="official_denial", **changes):
    text = "医疗提醒：原说法已撤回，后续请以正式说明为准。"
    event = {
        "event_type": event_type,
        "description": "来源报告的信息环境事件，尚未独立核实。",
        "reported_at": {"value": NEW.isoformat(), "precision": "instant"},
        "basis_locator": {"start": 5, "end": 11, "quote": text[5:11]},
    }
    event.update(changes)
    return material(
        "environment",
        text,
        signal_context=SignalContext(
            material_type="social_media",
            statement_status="unverified_report",
            document_evidence={"information_events": [event]},
        ),
    )


@pytest.mark.parametrize("kind", ["reported_deletion", "official_denial", "moderation", "access_failure"])
def test_information_events_keep_actual_quote_and_unverified_environment_qualification(kind):
    row = information_material(kind)
    original = canonical_hash(row)
    units = retained([row])[3]
    event = next(unit for unit in units if unit["unit_kind"] == "source_information_event")
    assert event["features"]["event"]["event_type"] == kind
    assert event["features"]["basis_status"] == "fixed_text_quote_verified"
    locator = event["features"]["event"]["basis_locator"]
    assert row.normalized_text[locator["start"] : locator["end"]] == locator["quote"]
    assert locator["text_hash"] == row.normalized_text_hash
    assert event["dimensions"]["statement_status"] == "unverified_report"
    assert "not_proof_of_health_claim_or_risk" in event["features"]["qualification"]
    assert canonical_hash(row) == original


@pytest.mark.parametrize(
    "locator",
    [
        {"start": 0, "end": 10000, "quote": "医疗提醒"},
        {"start": 0, "end": 4, "quote": "不存在的说法"},
        {"start": True, "end": 4, "quote": "医疗提醒"},
        {"quote": "不存在的说法"},
        {},
    ],
)
def test_direct_model_bad_locator_is_not_retained_as_an_event(locator):
    units = retained([information_material(basis_locator=locator)])[3]
    assert len(units) == 1 and units[0]["unit_kind"] == "source_statement"
    assert units[0]["features"]["information_event_gaps"]


@pytest.mark.parametrize("field", ["reported_at", "detected_at"])
def test_future_information_event_is_excluded_without_losing_source_statement(field):
    future = (AS_OF + timedelta(days=1)).isoformat()
    raw = {"value": future, "precision": "instant"} if field == "reported_at" else future
    units = retained([information_material(**{field: raw})])[3]
    assert len(units) == 1
    assert units[0]["features"]["information_event_gaps"] == [
        {"index": 0, "reason": "source_event_after_cutoff"}
    ]
    assert future not in str(units)


def test_public_replay_does_not_use_reported_time_as_event_availability_proof():
    row = information_material(basis_locator={"json_pointer": "/information"})
    units = retained([row], time_mode="availability_replay")[3]
    assert len(units) == 1
    assert units[0]["features"]["information_event_gaps"][0]["reason"] == (
        "source_event_visibility_unproven_for_replay"
    )
    current = retained([row])[3]
    assert current[1]["features"]["basis_status"] == "source_locator_not_rechecked"
    assert current[1]["features"]["temporal_status"] == "source_reported_time_not_availability_proof"


def test_deleted_version_and_fixed_original_are_distinct_without_inventing_deleted_text():
    original = material("same", "医疗提醒：请按分诊安排就诊。")
    deleted = material(
        "same",
        "",
        published=NEW + timedelta(days=1),
        record_ref=original.record_ref.model_copy(update={"adopted_version": "v2", "content_hash": "f" * 64}),
        text_status="unavailable",
        normalized_text=None,
        record_flags=("deleted",),
        signal_context=SignalContext(
            source_relations=(SourceRelation(kind="retraction", target_ref=original.record_ref),)
        ),
    )
    fixed, _, applicability, units = retained([original, deleted])
    before = canonical_hash(fixed)
    statements = [unit for unit in units if unit["unit_kind"] == "source_statement"]
    assert len(applicability) == 2 and len(statements) == 2
    assert len({unit["unit_id"] for unit in statements}) == 2
    tombstone = next(unit for unit in statements if unit["dimensions"]["source_state"])
    assert tombstone["dimensions"]["source_state"] == ["deleted"]
    assert tombstone["features"]["body_ref"]["text_status"] == "unavailable"
    assert tombstone["features"]["claims"] == []
    assert tombstone["features"]["body_ref"]["text_hash"] is None
    assert tombstone["features"]["excerpt"] is None
    assert tombstone["features"]["omitted_text_character_count"] is None
    assert canonical_hash(fixed) == before


def test_typed_public_health_metric_establishes_relevance_without_numeric_qualification():
    metric = MetricObservation(
        observation_id="synthetic",
        domain="public_health",
        subject_ref=ref("region"),
        metric_id="reported_notifications",
        metric_version="synthetic-v1",
        value_lower=2,
        value_upper=2,
        unit="notifications",
        period_start=NEW - timedelta(days=1),
        period_end=NEW,
        provenance=provenance(),
    )
    row = material("typed", "Source data table", metric_observations=(metric,))
    _, _, applicability, units = retained([row])
    assert applicability[row.record_ref.version_key]["basis"][0]["kind"] == "typed_public_health_metric"
    assert units[0]["unit_kind"] == "source_statement"
    future = metric.model_copy(update={"provenance": provenance(available=AS_OF + timedelta(days=1))})
    assert not retained([row.model_copy(update={"metric_observations": (future,)})])[2]


def test_unknown_target_version_remains_unresolved_and_input_order_is_stable():
    row = material(
        "notice",
        "医疗提醒：原建议已更正。",
        signal_context=SignalContext(
            document_evidence={
                "document_relations": [
                    {
                        "relation_type": "corrects",
                        "target_source_id": "arbitrary",
                        "target_source_record_id": "unknown",
                        "target_version_id": None,
                        "basis_locator": {"quote": "原建议已更正"},
                    }
                ]
            },
        ),
    )
    units = retained([row])[3]
    assert units[1]["features"]["event"]["target_version_id"] is None
    other = material("other", "健康提醒：请及时就医。")
    assert canonical_hash(list(retained([row, other])[2].values())) == (
        canonical_hash(list(retained([other, row])[2].values()))
    )
    _, prepared, applicability, _ = retained([row, other])
    wrong = applicability[other.record_ref.version_key]
    item = next(item for item in prepared.history if item.record.record_ref == row.record_ref)
    assert source_units(item, wrong, prepared.policy) == []


def test_existing_native_notice_channel_keeps_its_original_descriptive_eligibility():
    row = material("native", "Native publication text without recognized medical words.", source_id="who_don")
    _, _, applicability, units = retained([row])
    assert applicability[row.record_ref.version_key]["basis"][0]["kind"] == "native_who_notice"
    assert len(units) == 1 and units[0]["dimensions"]["evidence_status"] == "unverified"
    assert units[0]["features"]["claims"] == []


@pytest.mark.parametrize("mode", ["publication_retrospective", "availability_replay"])
def test_final_snapshot_resolves_raw_exact_relation_even_when_projection_was_one_record(mode):
    import json

    target = material("target-raw", "医疗提醒：请按指引就医。")
    target = target.model_copy(
        update={
            "record_ref": target.record_ref.model_copy(
                update={
                    "object_id": json.dumps([target.source_id, "native-target", True]),
                    "adopted_version": "1" * 64,
                }
            )
        }
    )
    text = "原建议已更正，详见固定原件。"
    relation = {
        "relation_type": "corrects",
        "target_source_id": target.source_id,
        "target_source_record_id": "native-target",
        "target_version_id": "1" * 64,
        "basis_locator": {"quote": "原建议已更正"},
    }
    linked = material(
        "raw-child",
        text,
        signal_context=SignalContext(
            document_evidence={"document_relations": [relation]},
        ),
    )
    _, _, applicability, units = retained([target, linked], time_mode=mode)
    assert linked.record_ref.version_key in applicability
    assert len([unit for unit in units if unit["unit_kind"] == "source_statement"]) == 2
    for changed in (
        {"target_version_id": None},
        {"target_version_id": "2" * 64},
        {"basis_locator": {"quote": "不是原文"}},
    ):
        invalid = linked.model_copy(
            update={
                "signal_context": SignalContext(
                    document_evidence={"document_relations": [relation | changed]},
                )
            }
        )
        assert invalid.record_ref.version_key not in retained([target, invalid], time_mode=mode)[2]
    without_quote = linked.model_copy(
        update={
            "signal_context": SignalContext(
                document_evidence={
                    "document_relations": [{k: v for k, v in relation.items() if k != "basis_locator"}]
                },
            )
        }
    )
    relevant = retained([target, without_quote], time_mode=mode)[2]
    assert (without_quote.record_ref.version_key in relevant) == (mode != "availability_replay")


def test_late_unproven_typed_relation_cannot_assign_a_historical_domain_or_environment_event():
    target = material("target-old", "医疗提醒：请按指引就医。")
    relation = SourceRelation(kind="repost", target_ref=target.record_ref)
    child = material(
        "old-unrelated", "转述见附件。", signal_context=SignalContext(source_relations=(relation,))
    )
    assert child.record_ref.version_key not in retained([target, child], time_mode="availability_replay")[2]
    own_medical = child.model_copy(
        update={
            "normalized_text": "健康提醒：请按指引就医。",
            "normalized_text_hash": None,
        }
    )
    own_medical = type(own_medical).model_validate(own_medical.model_dump())
    units = retained([target, own_medical], time_mode="availability_replay")[3]
    own = [
        unit for unit in units if unit["members"][0]["record_ref"] == child.record_ref.model_dump(mode="json")
    ]
    assert len(own) == 1 and own[0]["unit_kind"] == "source_statement"
    assert (
        own[0]["features"]["information_event_gaps"][0]["reason"]
        == "source_relation_visibility_unproven_for_replay"
    )


def test_excerpt_and_claims_are_bounded_exact_windows_without_summary_or_fulltext_claim():
    text = "合成前言。" * 300 + "合成医院报告7例不明原因肺炎。" * 20 + "附录。" * 500
    row = material("long-advice", text)
    unit = retained([row])[3][0]
    features = unit["features"]
    excerpt = features["excerpt"]
    assert 0 < len(excerpt["quote"]) <= 1200
    assert excerpt["start"] > 0
    assert text[excerpt["start"] : excerpt["end"]] == excerpt["quote"]
    assert excerpt["text_hash"] == row.normalized_text_hash
    assert features["omitted_text_character_count"] == len(text) - len(excerpt["quote"])
    assert len(features["claims"]) == 16 and features["omitted_claim_count"] == 4
    assert all(text[c["start"] : c["end"]] == c["text"] for c in features["claims"])


def test_historical_medical_material_keeps_unproven_raw_correction_out_of_known_events():
    row = material(
        "historical-medical",
        "医疗提醒：请按指引就医。",
        signal_context=SignalContext(
            document_evidence={
                "document_relations": [
                    {
                        "relation_type": "corrects",
                        "target_source_id": "arbitrary",
                        "target_source_record_id": "old",
                        "target_version_id": "1" * 64,
                        "basis_locator": {"json_pointer": "/later_added_relation"},
                    }
                ]
            },
        ),
    )
    units = retained([row], time_mode="availability_replay")[3]
    assert len(units) == 1 and units[0]["unit_kind"] == "source_statement"
    assert units[0]["features"]["information_event_gaps"][0]["reason"] == (
        "source_event_visibility_unproven_for_replay"
    )
