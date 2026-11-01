"""SYNTHETIC natural business text; no private scenarios, upstream requests or oracle."""

import copy
from types import SimpleNamespace

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.datasets.build import build_dataset
from bioradar.research.datasets.narrative import (
    NARRATIVE_VERSION,
    extract_narrative_observations,
    narrative_extraction_schema,
    validate_narrative_observations,
)
from bioradar.research.datasets.narrative_v2 import VERSION, validate_cached
from bioradar.research.investigations.kdd import policy_for
from bioradar.research.investigations.narrative_execution import prepare_narrative_snapshot
from bioradar.research.mining.models import KddPolicy, canonical_hash
from bioradar.research.mining.runner import run_mining
from test_mining import AS_OF, snapshot
from test_opus_narrative import prose

pytestmark = pytest.mark.ENGINE


def span(text, quote, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {"text": quote, "start": start, "end": start + len(quote)}


def source(text):
    return prose("SYNTHETIC-business-observation-v2", text)


def observation(text, *, subject="海岚设备公司", metric="净订单", value="23.5", period="2025年H1", **extra):
    row = {
        key: span(text, quote)
        for key, quote in {"subject": subject, "metric": metric, "value": value, "period": period}.items()
    }
    return row | extra


def validate(text, rows):
    return validate_narrative_observations({"observations": rows}, source(text), version=VERSION)


def reasons(result):
    return {(issue["stage"], issue["reason"]) for gap in result["gaps"] for issue in gap["issues"]}


def separate_header():
    header = "单位：百万元"
    line = "海岚设备公司2025年H1净订单为23.5。"
    text = header + "\n" + line
    row = observation(
        text, unit=span(text, "百万元"), context=span(text, line), shared_context=span(text, header)
    )
    return text, row


def paired():
    text = "海岚设备公司2023/2024年H1净订单分别为18/27百万元。"
    years = [span(text, "2023"), span(text, "2024")]
    values = [span(text, "18"), span(text, "27")]
    rows = [
        observation(
            text,
            value=amount,
            period="H1",
            unit=span(text, "百万元"),
            period_year=years[i],
            context=span(text, text),
            ordered_pair={"periods": years, "values": values},
        )
        for i, amount in enumerate(["18", "27"])
    ]
    return text, rows


def test_explicit_unit_header_and_half_year_bind_separate_source_locations_without_mutating_source():
    text, row = separate_header()
    record = source(text)
    original = canonical_hash(record)
    result = validate_narrative_observations({"observations": [row]}, record, version=VERSION)
    assert not result["gaps"] and len(result["observations"]) == 1
    item = result["observations"][0]
    assert str(item.value_lower) == "23500000.0" and item.unit == "CNY"
    assert item.period_start.isoformat() == "2025-01-01T00:00:00+00:00"
    assert item.period_end.isoformat() == "2025-07-01T00:00:00+00:00"
    assert item.field_locators["value"]["quote"] == "23.5"
    assert item.field_locators["unit"]["quote"] == "百万元"
    assert item.evidence_refs == (record.record_ref,)
    validate_cached(record, [item])
    assert canonical_hash(record) == original


def test_complete_source_ordered_columns_split_to_atomic_periods_and_program_compares_same_metric():
    text, rows = paired()
    record = source(text)
    result = validate(text, rows)
    assert not result["gaps"]
    assert [str(m.value_lower) for m in result["observations"]] == ["18000000", "27000000"]
    assert [m.period_start.year for m in result["observations"]] == [2023, 2024]
    derived = record.model_copy(update={"metric_observations": tuple(result["observations"])})
    mining = run_mining(
        snapshot([derived]),
        KddPolicy(
            as_of=AS_OF,
            kdd_direction="business",
            narrative_contract_version=VERSION,
        ),
    )
    comparisons = [
        c
        for m in mining.method_results
        for c in m.candidates
        if c.metrics.get("comparison_kind") == "year_over_year"
    ]
    assert comparisons
    assert any(c.metrics["effect"]["relative_minimum"] == 0.5 for c in comparisons)
    assert len({m.observation_id for m in result["observations"]}) == 2
    # JSONB/key-sorted persistence must not change explicit column order.
    for item in result["observations"]:
        cached = item.model_copy(
            update={"field_locators": dict(sorted(item.field_locators.items(), reverse=True))}
        )
        validate_cached(record, [cached])


@pytest.mark.parametrize("fault", ["no_pair", "swapped", "different_lengths", "partial_columns", "no_order"])
def test_parallel_numbers_never_become_ratios_or_guessed_period_assignments(fault):
    text, rows = paired()
    row = copy.deepcopy(rows[0])
    if fault == "no_pair":
        row.pop("ordered_pair")
    elif fault == "swapped":
        row["value"] = rows[1]["value"]
    elif fault == "different_lengths":
        row["ordered_pair"]["values"].append(row["ordered_pair"]["values"][-1])
    elif fault == "partial_columns":
        # Omit the first value but try a longer list from another position.
        row["ordered_pair"]["values"][0] = row["ordered_pair"]["values"][-1]
    else:
        text = text.replace("分别", "合计")
        row = observation(
            text,
            value="18",
            period="H1",
            unit=span(text, "百万元"),
            period_year=span(text, "2023"),
            context=span(text, text),
            ordered_pair={
                "periods": [span(text, "2023"), span(text, "2024")],
                "values": [span(text, "18"), span(text, "27")],
            },
        )
    result = validate(text, [row])
    assert not result["observations"]
    assert any(stage == "pairing" for stage, _ in reasons(result))


@pytest.mark.parametrize(
    "text,period",
    [
        ("海岚设备公司H1净订单23.5百万元。", "H1"),
        ("海岚设备公司2025财年H1净订单23.5百万元。", "H1"),
        ("海岚设备公司期内净订单23.5百万元。", "期内"),
    ],
)
def test_missing_year_fiscal_mapping_and_unanchored_relative_period_are_not_filled_from_as_of(text, period):
    row = observation(text, period=period, value="23.5百万元", context=span(text, text))
    result = validate(text, [row])
    assert not result["observations"] and any(stage == "period" for stage, _ in reasons(result))


def test_relative_period_requires_the_actual_unique_report_header():
    header = "报告期：2025年H2"
    line = "海岚设备公司期内营业收入为12百万元。"
    text = header + "\n" + line
    row = observation(
        text,
        metric="营业收入",
        value="12",
        period="期内",
        unit=span(text, "百万元"),
        context=span(text, line),
        shared_context=span(text, header),
    )
    result = validate(text, [row])
    assert not result["gaps"]
    assert result["observations"][0].period_start.month == 7
    assert result["observations"][0].period_end.year == 2026


@pytest.mark.parametrize(
    "text,subject,metric,value",
    [
        ("客户2025年H1净订单23.5百万元。", "客户", "净订单", "23.5百万元"),
        ("公司2025年H1净订单23.5百万元。", "公司", "净订单", "23.5百万元"),
        ("海岚设备公司与远河公司2025年H1净订单23.5百万元。", "海岚设备公司", "净订单", "23.5百万元"),
        ("海岚设备公司2025年H1客户收入23.5百万元。", "海岚设备公司", "收入", "23.5百万元"),
        ("海岚设备公司2025年H1客户现金到账23.5百万元。", "海岚设备公司", "客户现金到账", "23.5百万元"),
    ],
)
def test_generic_party_counterparty_and_multi_company_scope_never_become_a_reporting_owner(
    text, subject, metric, value
):
    result = validate(
        text, [observation(text, subject=subject, metric=metric, value=value, context=span(text, text))]
    )
    assert not result["observations"] and any(stage == "subject" for stage, _ in reasons(result))


def test_business_roles_remain_distinct_and_standard_hours_do_not_supply_clock_hour_ratio():
    text = (
        "海岚设备公司2025年H1合同正式新签20百万元。\n"
        "海岚设备公司2025年H1收入18百万元。\n"
        "海岚设备公司2025年H1验收金额16百万元。\n"
        "海岚设备公司2025年H1现金到账14百万元。\n"
        "海岚设备公司2025年H1可用标准工时600小时，实际工时300小时。"
    )
    parsed = extract_narrative_observations(source(text), version=VERSION)
    assert not parsed["gaps"]
    roles = {m.metric_id for m in parsed["observations"]}
    assert {
        "orders_unspecified",
        "revenue_unspecified",
        "acceptance",
        "cash_receipts",
        "available_standard_work_hours",
        "used_work_hours",
    } <= roles
    assert not {"recognized_revenue", "net_orders", "gross_orders"} & roles
    result = run_mining(
        snapshot([source(text)]),
        KddPolicy(
            as_of=AS_OF,
            kdd_direction="business",
            narrative_contract_version=VERSION,
        ),
    )
    utilization = next(m for m in result.method_results if m.method_id == "business_utilization")
    assert not utilization.candidates


def test_granular_row_failures_keep_overlap_within_one_rejection_and_forbid_wrong_metric_value():
    text = "海岚设备公司H1净订单23.5，现金到账12百万元。"
    row = observation(text, value="23.5", period="H1", context=span(text, text))
    result = validate(text, [row])
    assert len(result["gaps"]) == 1
    assert {"unit", "period"} <= {stage for stage, _ in reasons(result)}
    text = "海岚设备公司2025年H1净订单23.5百万元，现金到账12百万元。"
    result = validate(text, [observation(text, value="12百万元", context=span(text, text))])
    assert not result["observations"]
    assert ("metric_role", "another_metric_intervenes_before_value") in reasons(result)


def fixed(version=VERSION):
    return {
        "query_policy": {
            "effective": "query_planner", "research_contract_version": "bioradar-research-input-v1",
            "direction": "business",
            **({"narrative_contract_version": version} if version else {}),
        },
        "evidence_time": {"mode": "current", "cutoff": AS_OF.isoformat()},
        "reading_limits": {"search_limit": 8},
    }


class Host:
    def __init__(self, output):
        self.output = output
        self.posts = 0
        self.settings = SimpleNamespace()
        self.inputs = []

    def _call(self, *args, **kwargs):
        self.posts += 1
        self.inputs.append((args[3], args[6], kwargs))
        if isinstance(self.output, Exception):
            raise self.output
        return copy.deepcopy(self.output), {}

    def _checkpoint(self, *args, **kwargs):
        pass

    def _final_token_allowance(self, *args):
        return 0


def test_received_v2_restores_all_locators_zero_new_call_and_dataset_hash_matches_analysis_run():
    text, row = separate_header()
    original = snapshot([source(text)])
    unchanged = canonical_hash(original)
    host, progress, settings = Host({"observations": [row]}), {}, fixed()
    prepared = prepare_narrative_snapshot(
        host, "SYNTHETIC-id", "lease", settings, progress, original, allow_model=True
    )
    assert host.posts == 1 and prepared.records[0].metric_observations
    assert host.inputs[0][1]["extractor_version"] == VERSION
    restored = prepare_narrative_snapshot(
        host, "SYNTHETIC-id", "lease", settings, progress, original, allow_model=False
    )
    assert host.posts == 1
    policy = policy_for(settings)
    assert build_dataset(prepared, policy).manifest() == build_dataset(restored, policy).manifest()
    assert canonical_hash(original) == unchanged
    saved = next(iter(progress["narrative_extractions"].values()))
    saved["observations"][0]["field_locators"]["unit"]["end"] += 1
    with pytest.raises(DomainError) as raised:
        prepare_narrative_snapshot(
            host, "SYNTHETIC-id", "lease", settings, progress, original, allow_model=False
        )
    assert raised.value.code == "narrative_source_changed"
    assert host.posts == 1


def test_empty_model_observations_are_valid_and_unknown_is_not_replaced_by_empty_success():
    text, _ = separate_header()
    original = snapshot([source(text)])
    host, progress = Host({"observations": []}), {}
    result = prepare_narrative_snapshot(host, "id", "lease", fixed(), progress, original, allow_model=True)
    assert not result.records[0].metric_observations
    assert next(iter(progress["narrative_extractions"].values()))["status"] == "received"
    prepare_narrative_snapshot(host, "id", "lease", fixed(), progress, original, allow_model=True)
    assert host.posts == 1
    host = Host(DomainError("professional_outcome_unconfirmed", "SYNTHETIC unknown", 502))
    with pytest.raises(DomainError):
        prepare_narrative_snapshot(host, "id", "lease", fixed(), {}, original, allow_model=True)


def test_standalone_literal_parser_and_current_research_selection():
    old = KddPolicy(as_of=AS_OF, kdd_direction="business")
    assert "narrative_contract_version" not in old.model_dump(mode="json")
    assert "unit" not in narrative_extraction_schema()["$defs"]["NarrativeObservation"]["properties"]
    assert narrative_extraction_schema() == narrative_extraction_schema(version=NARRATIVE_VERSION)
    text = "海岚设备公司2025年第一季度净订单23.5万元。"
    rows = extract_narrative_observations(source(text))
    assert rows["observations"] and rows["extractor_version"] == NARRATIVE_VERSION
    assert rows == extract_narrative_observations(source(text), version=NARRATIVE_VERSION)
    newer = extract_narrative_observations(source(text), version=VERSION)
    assert newer["observations"]  # Old contiguous literal layout remains accepted in new tasks.
    text, _ = separate_header()
    host, progress = Host({"observations": []}), {}
    prepare_narrative_snapshot(
        host, "id", "lease", fixed(None), progress, snapshot([source(text)]), allow_model=True
    )
    assert host.inputs[0][1]["extractor_version"] == VERSION
    cached = next(iter(progress["narrative_extractions"].values()))
    assert cached["extractor_version"] == VERSION
    from bioradar.research.datasets.narrative_proposal import proposal_schema

    assert host.inputs[0][0] == proposal_schema()
    new_fixed = fixed()
    new_fixed["kdd_mining_policy"] = old.model_dump(mode="json")
    with pytest.raises(DomainError) as raised:
        policy_for(new_fixed)
    assert raised.value.code == "analysis_dataset_scope_changed"


@pytest.mark.parametrize(
    "number,prefix,reason",
    [
        ("23.5", "23", "selected_value_is_only_part_of_a_numeric_token"),
        ("1,230.5", "1", "selected_value_is_only_part_of_a_numeric_token"),
        (" 23.5", " 23", "numeric_token_must_not_include_surrounding_whitespace"),
    ],
)
def test_full_literal_number_is_required_even_when_a_shorter_prefix_is_an_exact_quote(number, prefix, reason):
    header = "单位：百万元"
    line = f"海岚设备公司2025年H1净订单为{number}。"
    text = header + "\n" + line
    row = observation(
        text,
        value=prefix,
        unit=span(text, "百万元"),
        context=span(text, line),
        shared_context=span(text, header),
    )
    value_start = text.index(number, text.index("净订单"))
    row["value"] = {"text": prefix, "start": value_start, "end": value_start + len(prefix)}
    result = validate(text, [row])
    assert not result["observations"]
    assert ("value", reason) in reasons(result)
    row["value"] = span(text, number.strip())
    complete = validate(text, [row])
    assert complete["observations"] and not complete["gaps"]


@pytest.mark.parametrize("tail", ["Q3", "H2", "第三季度"])
def test_omitting_a_non_year_period_column_cannot_make_unequal_original_columns_match(tail):
    text = f"海岚设备公司2025年Q1/Q2/{tail}净订单分别为10/20万元。"
    row = observation(
        text,
        value="10",
        period="Q1",
        period_year=span(text, "2025年"),
        unit=span(text, "万元"),
        context=span(text, text),
        ordered_pair={
            "periods": [span(text, "Q1"), span(text, "Q2")],
            "values": [span(text, "10"), span(text, "20", 1)],
        },
    )
    result = validate(text, [row])
    assert not result["observations"]
    assert ("pairing", "ordered_columns_are_incomplete") in reasons(result)


def test_shared_explicit_year_with_complete_quarter_columns_is_qualified():
    text = "海岚设备公司2025年Q1/Q2净订单分别为10/20万元。"
    row = observation(
        text,
        value="10",
        period="Q1",
        period_year=span(text, "2025年"),
        unit=span(text, "万元"),
        context=span(text, text),
        ordered_pair={
            "periods": [span(text, "Q1"), span(text, "Q2")],
            "values": [span(text, "10"), span(text, "20", 1)],
        },
    )
    result = validate(text, [row])
    assert not result["gaps"] and result["observations"][0].period_end.month == 4


def test_later_other_period_cannot_relabel_earlier_value_but_unique_postposed_period_is_valid():
    text = "海岚设备公司2024年H1净订单10万元，2025年H1净订单20万元。"
    row = observation(text, value="10万元", period="2025年H1", context=span(text, text))
    result = validate(text, [row])
    assert not result["observations"]
    assert ("period", "postposed_period_is_ambiguous_among_multiple_measurements") in reasons(result)
    text = "海岚设备公司净订单为10万元（2024年H1）。"
    row = observation(text, value="10万元", period="2024年H1", context=span(text, text))
    result = validate(text, [row])
    assert not result["gaps"] and result["observations"][0].period_start.year == 2024
    text = "海岚设备公司2025年H1净订单10万元，H2净订单20万元。"
    row = observation(
        text, value="10万元", period="H2", period_year=span(text, "2025年"), context=span(text, text)
    )
    result = validate(text, [row])
    assert ("period", "postposed_period_is_ambiguous_among_multiple_measurements") in reasons(result)
    assert not result["observations"]
    # The later actual row with that same original year remains usable.
    row["metric"] = span(text, "净订单", 1)
    row["value"] = span(text, "20万元")
    result = validate(text, [row])
    assert not result["gaps"] and result["observations"][0].period_start.month == 7
    text = "海岚设备公司2024年H1和2025年H1净订单23.5万元。"
    row = observation(text, value="23.5万元", period="2025年H1", context=span(text, text))
    result = validate(text, [row])
    assert not result["observations"]
    assert ("period", "multiple_unpaired_reporting_periods_for_one_value") in reasons(result)


def test_conflicting_shared_units_and_partial_company_name_cannot_bind_ordinary_values():
    header = "单位：百万元/万元"
    line = "海岚设备公司2025年H1净订单为23.5。"
    text = header + "\n" + line
    row = observation(
        text, unit=span(text, "百万元"), context=span(text, line), shared_context=span(text, header)
    )
    result = validate(text, [row])
    assert not result["observations"]
    assert ("unit", "shared_unit_header_has_multiple_units") in reasons(result)
    text, row = separate_header()
    row["subject"] = span(text, "岚设备公司")
    result = validate(text, [row])
    assert not result["observations"]
    assert ("subject", "selected_subject_is_only_part_of_a_source_name") in reasons(result)


def test_model_unknown_with_time_budget_code_is_never_saved_as_not_run_or_empty():
    from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt

    text, _ = separate_header()
    receipt = ModelCallReceipt(
        call_id="SYNTHETIC-unknown-call",
        stage="narrative_extract",
        input_hash="a" * 64,
        prompt_version="SYNTHETIC",
        requested_model="SYNTHETIC",
        configured_model_revision=None,
        started_at=AS_OF,
        max_output_tokens=256,
        status="unconfirmed",
    )
    failure = ModelCallError("analysis_time_budget", "SYNTHETIC unknown after send", receipt)
    host, progress = Host(failure), {}
    with pytest.raises(ModelCallError) as raised:
        prepare_narrative_snapshot(
            host, "id", "lease", fixed(), progress, snapshot([source(text)]), allow_model=True
        )
    assert raised.value is failure
    assert not progress["narrative_extractions"]


def test_shared_header_qualifiers_cannot_convert_a_fiscal_or_forecast_value_to_realized_calendar_data():
    header = "报告年度：2025年财年"
    line = "海岚设备公司H1净订单为23.5万元。"
    text = header + "\n" + line
    row = observation(
        text,
        value="23.5万元",
        period="H1",
        period_year=span(text, "2025年"),
        context=span(text, line),
        shared_context=span(text, header),
    )
    result = validate(text, [row])
    assert not result["observations"]
    assert ("period", "fiscal_calendar_requires_explicit_source_date_bounds") in reasons(result)
    header = "预测值（单位：百万元）"
    line = "海岚设备公司2025年H1净订单为23.5。"
    text = header + "\n" + line
    row = observation(
        text, unit=span(text, "百万元"), context=span(text, line), shared_context=span(text, header)
    )
    result = validate(text, [row])
    assert not result["gaps"] and result["observations"][0].measurement_basis == "forecast"
    derived = source(text).model_copy(update={"metric_observations": tuple(result["observations"])})
    data = build_dataset(
        snapshot([derived]),
        KddPolicy(as_of=AS_OF, kdd_direction="business", narrative_contract_version=VERSION),
    )
    assert not any(unit["dimensions"].get("metric_id") == "net_orders" for unit in data.units)
