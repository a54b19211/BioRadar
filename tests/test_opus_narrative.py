"""Natural source prose only: no prefilled entities, metrics, importance or scenario labels."""

import copy

import pytest
from bioradar.research.datasets.build import build_dataset
from bioradar.research.datasets.narrative import (
    extract_narrative_observations,
    validate_narrative_observations,
)
from bioradar.research.mining.models import (
    DiscoveryPolicy,
    KddPolicy,
    ResourceLimits,
    SignalContext,
    SourceRelation,
    canonical_hash,
)
from bioradar.research.mining.runner import run_mining
from test_mining import AS_OF, record, snapshot

pytestmark = pytest.mark.ENGINE


def prose(name, text):
    return record(
        name,
        text,
        source_id="external_archive",
        source_family=None,
        source_independence_status="unresolved",
        study_cluster_ref=None,
        study_independence_status="unresolved",
    )


def test_plain_chinese_prose_binds_numeric_roles_and_program_computes_utilization():
    row = prose(
        "business-note",
        "北川设备公司：2025年第一季度，有效工时240小时，可用工时600小时，净订单900万元，毛订单1200万元，确认收入700万元，回款650万元。\n北川设备公司：2026年第一季度，有效工时280小时，可用工时800小时，净订单1100万元，毛订单1500万元，确认收入850万元，回款720万元。",
    )
    fixed = snapshot([row])
    original = canonical_hash(fixed)
    extracted = extract_narrative_observations(row)
    roles = {m.metric_id for m in extracted["observations"]}
    assert {
        "used_work_hours",
        "available_work_hours",
        "net_orders",
        "gross_orders",
        "recognized_revenue",
        "cash_receipts",
    } <= roles
    result = run_mining(fixed, KddPolicy(as_of=AS_OF, kdd_direction="business"))
    method = next(m for m in result.method_results if m.method_id == "business_utilization")
    assert method.execution_status == "success"
    calculations = [
        c.metrics["calculation"] for c in method.candidates if "before" not in c.metrics["calculation"]
    ]
    changes = [c for c in method.candidates if "before" in c.metrics["calculation"]]
    assert len(changes) == 1 and changes[0].metrics["comparison_kind"] == "year_over_year"
    assert changes[0].metrics["calculation"]["result"] == {
        "lower": "-5.00",
        "upper": "-5.00",
        "unit": "percentage_points",
    }
    assert changes[0].metrics["calculation"]["relative_change"]["lower"] == "-0.125"
    assert {c["result"]["lower"] for c in calculations} == {"0.4", "0.35"}
    for calculation in calculations:
        for role in ["numerator", "denominator"]:
            loc = calculation[role]["field_locators"]["value"]
            assert row.normalized_text[loc["start"] : loc["end"]] == loc["quote"]
    assert canonical_hash(fixed) == original
    raw = copy.deepcopy(extracted["extraction"])
    raw["observations"][0]["value"]["text"] = "999999小时"
    invalid = validate_narrative_observations(raw, row)
    assert invalid["gaps"]
    assert len(invalid["observations"]) < len(extracted["observations"])


def test_same_archive_source_discovers_multiple_directions_with_one_global_capacity():
    rows = [
        prose("health-report", "北江医院报告7例不明原因肺炎，目前病原尚未确认。"),
        prose("trial-report", "CX-893随机三期试验纳入240名患者，预设主要终点PFS的风险比下降，OS尚未成熟。"),
        prose("business-report", "南川设备公司2026年第一季度：有效工时300小时，可用工时900小时。"),
    ]
    fixed = snapshot(rows)
    original = canonical_hash(fixed)
    policy = DiscoveryPolicy(
        as_of=AS_OF,
        discovery_directions=("research", "pipeline", "business", "public_health"),
        limits=ResourceLimits(review_capacity=2),
    )
    result = run_mining(fixed, policy)
    assert len(result.candidates) <= 2
    assert result.data_preparation["input_count"] == 3
    assert set(result.data_preparation["direction_datasets"]) == set(policy.discovery_directions)
    assert result.data_preparation["denominator"]["count"] is None
    generated = [c for m in result.method_results for c in m.candidates]
    assert {"pipeline", "business", "public_health"} <= {c.metrics["kdd_direction"] for c in generated}
    assert all(m.execution_status != "failed" for m in result.method_results)
    assert canonical_hash(fixed) == original
    noise = snapshot([prose("noise", "不是没有，目前公司表示我们需要进一步研究。")])
    assert not run_mining(noise, policy).candidates


def test_exact_source_correction_replaces_only_same_metric_period_and_retains_original_values():
    original = prose("old-order", "北川设备公司2026年第一季度：净订单900万元，毛订单1200万元。")
    correction = prose("correction", "北川设备公司2026年第一季度净订单更正为850万元。")
    correction = correction.model_copy(
        update={
            "signal_context": SignalContext(
                source_relations=(SourceRelation(kind="correction", target_ref=original.record_ref),),
            )
        }
    )
    fixed = snapshot([original, correction])
    before = canonical_hash(fixed)
    dataset = build_dataset(fixed, KddPolicy(as_of=AS_OF, kdd_direction="business"))
    net = next(u for u in dataset.units if u["dimensions"].get("metric_id") == "net_orders")
    gross = next(u for u in dataset.units if u["dimensions"].get("metric_id") == "gross_orders")
    assert not net["features"]["conflicting_values"]
    assert [m["value_lower"] for m in net["features"]["observations"]] == ["8500000"]
    assert net["features"]["superseded_observations"][0]["observation"]["value_lower"] == "9000000"
    assert len(net["members"]) == 2
    assert gross["features"]["observations"][0]["value_lower"] == "12000000"
    assert canonical_hash(fixed) == before
    unresolved = correction.model_copy(update={"signal_context": None})
    unknown = build_dataset(
        snapshot([original, unresolved]), KddPolicy(as_of=AS_OF, kdd_direction="business")
    )
    net_unknown = next(u for u in unknown.units if u["dimensions"].get("metric_id") == "net_orders")
    assert net_unknown["features"]["conflicting_values"]  # No guessed target or averaging.


def test_literal_span_end_cannot_clip_past_fixed_source_end():
    row = prose("span-bound", "甲公司2025年第一季度可用工时200小时")
    text = row.normalized_text

    def span(quote):
        start = text.index(quote)
        return {"text": quote, "start": start, "end": start + len(quote)}

    observation = {
        "subject": span("甲公司"),
        "period": span("2025年第一季度"),
        "metric": span("可用工时"),
        "value": span("200小时"),
    }
    valid = validate_narrative_observations({"observations": [observation]}, row)
    assert len(valid["observations"]) == 1 and not valid["gaps"]
    overflow = copy.deepcopy(observation)
    overflow["value"]["end"] = len(text) + 10  # Python slice used to silently clip this.
    invalid = validate_narrative_observations({"observations": [overflow]}, row)
    assert not invalid["observations"] and invalid["gaps"]
