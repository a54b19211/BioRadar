"""SYNTHETIC section 16 method regressions; no private cases, model or upstream calls."""

from datetime import timedelta

import pytest
from bioradar.research.datasets.build import build_dataset
from bioradar.research.datasets.narrative_v2 import VERSION, extract
from bioradar.research.mining.models import KddPolicy, SignalContext, SourceRelation
from bioradar.research.mining.runner import run_mining
from bioradar.research.mining.signals import assess_records
from bioradar.topics.tracking import update_tracking
from test_mining import AS_OF, record, snapshot
from test_opus_tracking import value

pytestmark = pytest.mark.ENGINE


def test_attributed_cluster_and_numeric_correction_require_medium_attention():
    cluster = record(
        "SYNTHETIC-cluster",
        "锦川医院报告7例聚集病例，病原未证实。",
        source_family=None,
        source_independence_status="unresolved",
    )
    assessed = assess_records([cluster])
    assert assessed["importance"] == "medium"
    assert assessed["evidence_level"] == "unverified"
    correction = record("SYNTHETIC-correction", "锦川设备公司更正2025年上半年净订单为60万元。")
    assert assess_records([correction])["importance"] == "medium"


def test_official_confirmation_is_high_but_unverified_reposts_and_denials_are_not():
    official = record("SYNTHETIC-official", "锦川卫健委通报，锦川医院确诊7例不明原因肺炎。")
    official = official.model_copy(
        update={
            "signal_context": SignalContext(
                material_type="official_notice",
                evidence_refs=(official.record_ref,),
                statement_status="source_reported",
            )
        }
    )
    assert assess_records([official])["importance"] == "high"
    rumor = record(
        "SYNTHETIC-rumor",
        "网传锦川医院收治7例不明原因肺炎，尚未证实。",
        signal_context=SignalContext(attribution="anonymous", statement_status="unverified_report"),
    )
    repost = record(
        "SYNTHETIC-repost",
        rumor.normalized_text,
        source_family="SYNTHETIC-other",
        signal_context=SignalContext(
            source_relations=(SourceRelation(kind="repost", target_ref=rumor.record_ref),)
        ),
    )
    assert assess_records([rumor, repost])["importance"] == "medium"
    denied = official.model_copy(update={"normalized_text": "锦川卫健委辟谣锦川医院7例不明原因肺炎的说法。"})
    assert assess_records([denied])["importance"] != "high"


@pytest.mark.parametrize(
    "text,kind",
    [
        ("锦川医院未发现7例不明原因肺炎的传播证据。", "not_found"),
        ("锦川医院明确已排除7例不明原因肺炎聚集。", "excluded"),
        ("锦川医院辟谣7例不明原因肺炎，相关讨论被删帖。", "reaction"),
    ],
)
def test_negation_categories_keep_attribution_and_distinguish_risk_effect(text, kind):
    row = record("SYNTHETIC-negation-" + kind, text)
    result = assess_records([row])
    signals = result["negative_signals"]
    assert kind in {item["kind"] for item in signals}
    assert all(item["record_ref"] == row.record_ref.model_dump(mode="json") for item in signals)
    assert result["checkable_claims"]
    if kind == "not_found":
        assert result["risk_not_excluded"] is True
    if kind == "reaction":
        assert result["environment_signals"] and result["importance"] == "medium"


def test_two_blank_waves_cost_no_model_and_reaction_stays_at_minimum_then_independent_wakes():
    first = record("SYNTHETIC-original", "锦川医院报告7例不明原因肺炎。", source_family="SYNTHETIC-one")
    definition = {"domain": "public_health", "cadence_hours": 24}
    state = update_tracking([value(first)], definition, {}, as_of=AS_OF)
    for hour in (1, 2):
        state = update_tracking([value(first)], definition, state, as_of=AS_OF + timedelta(hours=hour))
        assert not state["model_update"]["needed"]
    assert state["lifecycle"] in {"cooling", "dormant"}
    reaction = record("SYNTHETIC-moderation", "相关讨论被删帖并屏蔽。", source_family="SYNTHETIC-one")
    state = update_tracking(
        [value(first), value(reaction)], definition, state, as_of=AS_OF + timedelta(hours=3)
    )
    assert state["lifecycle"] != "dormant" and state["cadence_hours"] == 1
    new = record("SYNTHETIC-independent", "锦川第二医院报告3例不明原因肺炎。", source_family="SYNTHETIC-two")
    state = update_tracking(
        [value(first), value(reaction), value(new)], definition, state, as_of=AS_OF + timedelta(hours=4)
    )
    assert state["lifecycle"] == "warming" and state["model_update"]["needed"]
    # A reaction keeps even a scope whose reviewed risk is resolved awake.
    state = update_tracking(
        [value(reaction)], definition, {"lifecycle": "dormant", "_quiet_rounds": 4}, as_of=AS_OF
    )
    assert state["lifecycle"] != "dormant" and state["cadence_hours"] == 1


def test_every_rejected_numeric_candidate_retains_dimension_reasons_in_dataset():
    row = record("SYNTHETIC-unpaired", "锦川设备公司2025年上半年净订单为40/60万元。")
    extracted = extract(row)
    assert extracted["gaps"]
    policy = KddPolicy(as_of=AS_OF, kdd_direction="business", narrative_contract_version=VERSION)
    manifest = build_dataset(snapshot([row]), policy).manifest()
    rejected = [
        entry for entry in manifest["excluded_records"] if entry["reason"] == "narrative_observation_rejected"
    ]
    assert len(rejected) == len(extracted["gaps"])
    assert rejected[0]["issues"] == extracted["gaps"][0]["issues"]
    assert rejected[0]["observation_index"] == 0
    assert rejected[0]["record_ref"] == row.record_ref.model_dump(mode="json")


def test_missing_numeric_qualifiers_are_retained_as_rejections_not_disappearing_candidates():
    rows = [
        record("SYNTHETIC-no-unit", "锦川设备公司2025年上半年净订单为40。"),
        record("SYNTHETIC-no-period", "锦川设备公司净订单为40万元。"),
        record("SYNTHETIC-no-subject", "2025年上半年净订单为40万元。"),
    ]
    expected = ["unit", "period", "subject"]
    for row, stage in zip(rows, expected, strict=True):
        result = extract(row)
        assert not result["observations"] and result["gaps"]
        assert stage in {issue["stage"] for gap in result["gaps"] for issue in gap["issues"]}


def test_source_bound_hours_are_computed_and_correction_points_to_old_observation():
    old = record("SYNTHETIC-hours-old", "锦川设备公司2025年上半年可用工时为200小时，实际工时为160小时。")
    correction = record(
        "SYNTHETIC-hours-correction",
        "锦川设备公司2025年上半年实际工时更正为150小时。",
        signal_context=SignalContext(
            source_relations=(SourceRelation(kind="correction", target_ref=old.record_ref),)
        ),
    )
    policy = KddPolicy(as_of=AS_OF, kdd_direction="business", narrative_contract_version=VERSION)
    source = snapshot([old, correction])
    data = build_dataset(source, policy)
    links = [link for unit in data.units for link in unit["features"].get("correction_links", [])]
    prior = next(item for item in extract(old)["observations"] if item.metric_id == "used_work_hours")
    current = extract(correction)["observations"][0]
    assert any(
        link["observation_id"] == current.observation_id
        and link["corrects_observation_ids"] == [prior.observation_id]
        for link in links
    )
    candidates = [
        candidate
        for outcome in run_mining(source, policy).method_results
        for candidate in outcome.candidates
        if candidate.method_id == "business_utilization"
    ]
    assert candidates
    calculation = candidates[0].metrics["calculation"]
    assert calculation["formula"] == "used_work_hours / available_work_hours"
    assert calculation["result"]["lower"] == "0.75"


def test_hypothesis_judgment_reuses_model_reasoning_not_counterevidence_counts():
    from bioradar.topics.tracking import hypothesis_comparison

    hypotheses = [
        {
            "id": "SYNTHETIC-H1",
            "supporting_claim_ids": ["SYNTHETIC-C1"],
            "opposing_claim_ids": ["SYNTHETIC-C2"],
        },
        {"id": "SYNTHETIC-H2", "supporting_claim_ids": [], "opposing_claim_ids": []},
    ]
    claims = [{"id": "SYNTHETIC-C1"}, {"id": "SYNTHETIC-C2"}]
    judgment = {
        "least_refuted_hypothesis_id": "SYNTHETIC-H1",
        "reason": "SYNTHETIC: H2 has no testable evidence; C2 addresses only a narrower condition.",
        "next_discriminating_evidence": "SYNTHETIC: an independent sample with a pathogen assay.",
        "qualification": "这是模型判断，未证实",
    }
    comparison = hypothesis_comparison(hypotheses, claims, model_judgment=judgment)
    assert comparison["selection"] == "model_judgment_unverified"
    assert comparison["current_judgment"] == judgment
    assert not any("winner" in item for item in comparison["hypotheses"])
    with pytest.raises(ValueError, match="undeclared"):
        hypothesis_comparison(
            hypotheses,
            claims,
            model_judgment={**judgment, "least_refuted_hypothesis_id": "SYNTHETIC-MISSING"},
        )


def test_two_independent_institutions_must_confirm_and_reactions_never_supply_confirmation():
    institutions = []
    for number in (1, 2):
        row = record(
            "SYNTHETIC-institution-" + str(number),
            "锦川医院确认7例不明原因肺炎。",
            source_family="SYNTHETIC-institution-" + str(number),
        )
        row = row.model_copy(
            update={
                "signal_context": SignalContext(
                    attribution="named_institution",
                    verification_status="source_confirmed",
                    evidence_refs=(row.record_ref,),
                )
            }
        )
        institutions.append(row)
    assert assess_records(institutions[:1])["importance"] == "medium"
    assert assess_records(institutions)["importance"] == "high"
    unverified = [
        row.model_copy(update={"signal_context": SignalContext(attribution="named_institution")})
        for row in institutions
    ]
    assert assess_records(unverified)["importance"] == "medium"
    reaction = institutions[1].model_copy(update={"normalized_text": "锦川医院辟谣7例不明原因肺炎。"})
    assert assess_records([institutions[0], reaction])["importance"] == "medium"


def test_legacy_numeric_rejection_keeps_its_reason_in_dataset():
    row = record("SYNTHETIC-legacy-fiscal", "锦川设备公司2025财年2025年上半年净订单为40万元。")
    policy = KddPolicy(as_of=AS_OF, kdd_direction="business")
    manifest = build_dataset(snapshot([row]), policy).manifest()
    rejected = [
        item for item in manifest["excluded_records"] if item["reason"] == "narrative_observation_rejected"
    ]
    assert rejected[0]["issues"] == [
        {"stage": "scope", "reason": "literal_metric_scope_period_or_value_requires_review"}
    ]


def test_official_repetition_of_an_unverified_rumor_is_not_confirmation():
    row = record("SYNTHETIC-official-rumor", "锦川卫健委提醒：网传锦川医院7例不明原因肺炎，尚待核实。")
    row = row.model_copy(
        update={
            "signal_context": SignalContext(
                material_type="official_notice",
                statement_status="source_reported",
                evidence_refs=(row.record_ref,),
            )
        }
    )
    result = assess_records([row])
    assert result["importance"] == "medium" and result["evidence_level"] != "institution_confirmed"


def test_numeric_correction_with_a_separate_unit_header_still_requires_medium_attention():
    row = record("SYNTHETIC-correction-header", "单位：百万元。锦川设备公司更正2025年H1净订单为23.5。")
    assert assess_records([row])["importance"] == "medium"


def test_invalid_information_event_is_retained_as_a_gap_without_synthesizing_reaction():
    row = record(
        "SYNTHETIC-invalid-information-event",
        "锦川医院报告7例不明原因肺炎。",
        signal_context=SignalContext(document_evidence={"information_events": ["SYNTHETIC-invalid-event"]}),
    )
    assessment = assess_records([row])
    assert assessment["importance"] == "medium" and not assessment["environment_signals"]
    policy = KddPolicy(
        as_of=AS_OF,
        kdd_direction="public_health",
        source_intelligence_version="public-health-source-intelligence-v1",
    )
    data = build_dataset(snapshot([row]), policy)
    statements = [unit for unit in data.units if unit["unit_kind"] == "source_statement"]
    assert statements and statements[0]["features"]["information_event_gaps"] == [
        {"index": 0, "reason": "source_event_not_an_object"},
    ]
    assert row.signal_context.document_evidence["information_events"] == ["SYNTHETIC-invalid-event"]
