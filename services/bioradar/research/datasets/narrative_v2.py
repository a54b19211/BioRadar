"""Bounded literal business observations v2; source selection is never a numeric answer."""

import re
from datetime import UTC, datetime

from pydantic import Field, ValidationError

from bioradar.research.datasets.narrative import (
    ALIASES as V1_ALIASES,
)
from bioradar.research.datasets.narrative import (
    PERIOD as V1_PERIOD,
)
from bioradar.research.datasets.narrative import (
    LiteralSpan,
    _literal,
    _span,
    parse_period,
    parse_value,
)
from bioradar.research.mining.models import (
    FrozenModel,
    MetricObservation,
    ProviderRef,
    SectionProvenance,
    canonical_hash,
)

VERSION = "literal-narrative-observations-v2"
ALIASES = {
    **V1_ALIASES,
    "orders_unspecified": (*V1_ALIASES["orders_unspecified"], "正式新签", "新签订单"),
    "recognized_revenue": ("确认收入", "营业收入", "营收", "recognized revenue"),
    "revenue_unspecified": ("收入", "revenue"),
    "acceptance": (*V1_ALIASES["acceptance"], "客户验收"),
    "cash_receipts": (*V1_ALIASES["cash_receipts"], "客户现金到账"),
    # Standard hours are a separate metric. They cannot silently supply the
    # clock-hour denominator used by the existing utilization calculation.
    "available_standard_work_hours": ("可用标准工时",),
    "used_standard_work_hours": ("实际使用标准工时",),
}
NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
NAMED = re.compile(
    r"[\u3400-\u9fffA-Za-z0-9·＆&-]{1,40}?(?:股份有限公司|有限公司|公司|集团|工厂)"
    r"|[A-Z][A-Za-z0-9& .-]+(?:Inc\.?|Ltd\.?|Corp\.?)"
)
PERIOD = re.compile(rf"(?:{V1_PERIOD.pattern}|20\d{{2}}\s*年?\s*[Hh][12]|[Hh][12]\s*20\d{{2}})", re.I)
UNIT = (
    r"百万元|亿元|万元|千元|元|亿小时|万小时|千小时|小时|万套|套|台|%|％|percent\b|hours?\b"
    r"|million\s+(?:CNY|RMB|USD)\b|(?:CNY|RMB|USD)\b"
)
SCALAR = re.compile(rf"(?P<number>{NUMBER.pattern})\s*(?P<unit>{UNIT})", re.I)
SEPARATOR = re.compile(r"\s*[/／、,，|]\s*")
BARE_PERIOD = re.compile(
    r"(?<![A-Za-z0-9])[Hh][12](?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[Qq][1-4](?![A-Za-z0-9])|第?[一二三四1234]季度|[上下]半年"
)


class OrderedPair(FrozenModel):
    """Complete ordered source columns, not a model-computed pairing or ratio."""

    periods: tuple[LiteralSpan, ...] = Field(min_length=2, max_length=12)
    values: tuple[LiteralSpan, ...] = Field(min_length=2, max_length=12)


class NarrativeObservationV2(FrozenModel):
    subject: LiteralSpan
    metric: LiteralSpan
    value: LiteralSpan
    period: LiteralSpan
    reporting_scope: LiteralSpan | None = None
    unit: LiteralSpan | None = None
    period_year: LiteralSpan | None = None
    context: LiteralSpan | None = None
    shared_context: LiteralSpan | None = None
    ordered_pair: OrderedPair | None = None


class NarrativeExtractionV2(FrozenModel):
    observations: tuple[NarrativeObservationV2, ...] = Field(default=(), max_length=256)


def schema():
    return NarrativeExtractionV2.model_json_schema()


def _inside(span, container):
    return container.start <= span.start < span.end <= container.end


def _local_context(record, row):
    text = record.normalized_text
    if row.context is not None:
        if row.context.end - row.context.start > 2000:
            raise ValueError("context_too_broad")
        return row.context
    # Legacy single-span observations need no new field. A single local source
    # sentence is sufficient; disjoint headings must be explicitly selected.
    start = min(row.subject.start, row.metric.start, row.value.start, row.period.start)
    end = max(row.subject.end, row.metric.end, row.value.end, row.period.end)
    if "\n" in text[start:end] or re.search(r"[。；;]", text[start:end]):
        raise ValueError("explicit_context_required_for_disjoint_fields")
    return LiteralSpan(text=text[start:end], start=start, end=end)


def _scope(record, row, context):
    local = (row.subject, row.metric, row.value)
    if any(not _inside(span, context) for span in local):
        raise ValueError("subject_metric_value_not_in_local_context")
    if row.shared_context is not None:
        shared = row.shared_context
        if shared.end > context.start or context.start - shared.end > 500 or shared.end - shared.start > 1000:
            raise ValueError("shared_header_not_adjacent")
        # A common header may declare a period/unit, never borrow another
        # company's quantities or a hidden observation from a different row.
        if NUMBER.search(shared.text) and re.search(r"订单|收入|回款|工时", shared.text):
            raise ValueError("shared_header_contains_other_measurement")
    for span in (row.period, row.period_year, row.unit, row.reporting_scope):
        if span is not None and not _inside(span, context):
            if row.shared_context is None or not _inside(span, row.shared_context):
                raise ValueError("qualifier_outside_bound_context")


def _numeric(row):
    raw = row.value.text
    if raw != raw.strip():
        raise ValueError("numeric_token_must_not_include_surrounding_whitespace")
    if row.unit is not None:
        if NUMBER.fullmatch(raw) is None:
            raise ValueError("separate_unit_requires_single_numeric_token")
        unit = row.unit.text.strip()
        if unit == "百万元":
            return parse_value(raw + "万元")[0] * 100, "CNY", "CNY", "monetary"
        try:
            return parse_value(raw + " " + unit)
        except ValueError:
            raise ValueError("unit_or_currency_not_explicit") from None
    if re.fullmatch(rf"{NUMBER.pattern}\s*百万元", raw):
        return parse_value(raw.replace("百万元", "万元"))[0] * 100, "CNY", "CNY", "monetary"
    return parse_value(raw)


def _unit_binding(record, row):
    if row.unit is None:
        return
    text = record.normalized_text
    adjacent = row.unit.start >= row.value.end and not text[row.value.end : row.unit.start].strip()
    if adjacent:
        return
    before = text[max(0, row.unit.start - 20) : row.unit.start]
    if re.search(r"(?:单位|unit)\s*[:：(\uff08]?\s*$", before, re.I):
        line_start = text.rfind("\n", 0, row.unit.start) + 1
        line_end = text.find("\n", row.unit.end)
        line = text[line_start : line_end if line_end != -1 else len(text)]
        if len({match.group().casefold() for match in re.finditer(UNIT, line, re.I)}) != 1:
            raise ValueError("shared_unit_header_has_multiple_units")
        return
    # A shared trailing unit is valid only for an independently validated list.
    if row.ordered_pair is not None:
        last = row.ordered_pair.values[-1]
        if row.unit.start >= last.end and not text[last.end : row.unit.start].strip():
            return
    raise ValueError("separate_unit_not_a_bound_header_or_adjacent_unit")


def _period_mentions(text):
    complete = [(match.start(), match.end()) for match in PERIOD.finditer(text)]
    return sorted(
        complete
        + [
            (match.start(), match.end())
            for match in BARE_PERIOD.finditer(text)
            if not any(start <= match.start() and match.end() <= end for start, end in complete)
        ]
    )


def _period_binding(row, context):
    mentions = _period_mentions(context.text)
    relative_start, relative_end = row.period.start - context.start, row.period.end - context.start
    selected = next(
        (item for item in mentions if item[0] <= relative_start and relative_end <= item[1]), None
    )
    if row.period.start > row.value.end:
        if any(item != selected for item in mentions):
            raise ValueError("postposed_period_is_ambiguous_among_multiple_measurements")
        return
    preceding = [item for item in mentions if item[1] <= row.value.start - context.start]
    if preceding and preceding[-1] != selected:
        raise ValueError("another_reporting_period_intervenes_before_value")
    # A list of periods followed by one value is not a unique atomic period.
    for first, second in zip(preceding, preceding[1:], strict=False):
        if re.fullmatch(r"\s*(?:和|与|及|以及|[/／、,，])\s*", context.text[first[1] : second[0]]):
            raise ValueError("multiple_unpaired_reporting_periods_for_one_value")


def _calendar(row, context):
    raw = row.period.text.strip()
    qualifiers = context.text + (" " + row.shared_context.text if row.shared_context else "")
    if row.ordered_pair is None:
        _period_binding(row, context)
        if (
            row.period_year is not None
            and not re.search(r"20\d{2}", raw)
            and len(set(re.findall(r"20\d{2}", qualifiers))) != 1
        ):
            raise ValueError("year_anchor_has_multiple_unpaired_period_columns")

    combined = raw
    if row.period_year is not None:
        if re.fullmatch(r"20\d{2}年?", row.period_year.text.strip()) is None:
            raise ValueError("period_year_must_be_one_explicit_year")
        years = set(re.findall(r"20\d{2}", raw))
        year = re.search(r"20\d{2}", row.period_year.text)[0]
        if years and years != {year}:
            raise ValueError("period_year_conflicts_with_selected_period")
        if not years:
            combined = year + "年" + raw
    if raw in {"期内", "报告期内", "本期", "本报告期"}:
        header = row.shared_context.text if row.shared_context else context.text
        anchors = list(PERIOD.finditer(header))
        if len(anchors) != 1 or re.search(r"报告期|统计期|reporting period", header, re.I) is None:
            raise ValueError("relative_period_has_no_unique_report_period")
        combined = anchors[0].group()
    explicit_dates = re.fullmatch(
        r"20\d{2}-\d{2}-\d{2}\s*(?:至|到|—|–|to)\s*20\d{2}-\d{2}-\d{2}", combined, re.I
    )
    if re.search(r"财年|fiscal|\bFY", qualifiers, re.I) and explicit_dates is None:
        raise ValueError("fiscal_calendar_requires_explicit_source_date_bounds")
    half = re.fullmatch(r"(20\d{2})\s*年?\s*[Hh]([12])|[Hh]([12])\s*(20\d{2})", combined)
    if half:
        year, part = int(half[1] or half[4]), int(half[2] or half[3])
        return (
            datetime(year, 1 if part == 1 else 7, 1, tzinfo=UTC),
            datetime(year if part == 1 else year + 1, 7 if part == 1 else 1, 1, tzinfo=UTC),
            "explicit_source_calendar_half_year",
        )
    return parse_period(combined)


def _list(record, spans):
    text = record.normalized_text
    if any(
        a.end > b.start or SEPARATOR.fullmatch(text[a.end : b.start]) is None
        for a, b in zip(spans, spans[1:], strict=False)
    ):
        raise ValueError("columns_are_not_original_ordered_lists")
    # Omitting a list member would falsely bind positions even if the supplied
    # arrays happen to have the same length.
    before, after = text[: spans[0].start], text[spans[-1].end :]
    if re.search(r"(?:[0-9]|年|季度|半年)\s*[/／、,，|]\s*$", before) or re.match(
        r"\s*[/／、,，|]\s*(?:\d|[QqHh][1-4]|第?[一二三四]季度|[上下]半年)", after
    ):
        raise ValueError("ordered_columns_are_incomplete")


def _pairing(record, row, context):
    text = record.normalized_text
    pair = row.ordered_pair
    numeric_start = row.value.start
    numeric_end = (
        row.value.start + len(NUMBER.match(row.value.text)[0])
        if NUMBER.match(row.value.text)
        else row.value.end
    )
    near = text[max(0, numeric_start - 1) : numeric_start] + text[numeric_end : numeric_end + 1]
    if pair is None:
        if re.search(r"[/／、,，|]", near):
            raise ValueError("parallel_values_require_explicit_ordered_columns")
        return
    if len(pair.periods) != len(pair.values):
        raise ValueError("period_and_value_columns_have_different_lengths")
    if any(not _inside(span, context) for span in (*pair.periods, *pair.values)):
        raise ValueError("ordered_columns_outside_context")
    _list(record, pair.periods)
    _list(record, pair.values)
    if re.search(r"分别|依次|respectively", context.text, re.I) is None:
        period_line = text[text.rfind("\n", 0, pair.periods[0].start) + 1 : pair.periods[-1].end]
        if "\n" not in context.text or re.search(r"期间|报告期|年度|period", period_line, re.I) is None:
            raise ValueError("source_has_no_explicit_column_order")
    selected = row.period if row.period in pair.periods else row.period_year
    p = [i for i, span in enumerate(pair.periods) if span == selected]
    v = [i for i, span in enumerate(pair.values) if span == row.value]
    if len(p) != 1 or p != v:
        raise ValueError("selected_period_and_value_are_not_same_column")
    if any(NUMBER.fullmatch(span.text.strip()) is None for span in pair.values):
        raise ValueError("ordered_value_column_is_not_scalar")
    if len({span.text for span in pair.periods}) != len(pair.periods):
        raise ValueError("ordered_period_columns_are_not_distinct")


def _role(record, row, context, unit, kind):
    roles = [role for role, aliases in ALIASES.items() if row.metric.text.strip().casefold() in aliases]
    if len(roles) != 1:
        raise ValueError("metric_role_not_explicit")
    role = roles[0]
    between = record.normalized_text[row.metric.end : row.value.start]
    if row.metric.end > row.value.start or len(between) > 300:
        raise ValueError("metric_and_value_not_one_local_row")
    if any(alias.casefold() in between.casefold() for names in ALIASES.values() for alias in names):
        raise ValueError("another_metric_intervenes_before_value")
    if row.metric.text == "正式新签" and not re.search(r"订单|合同", context.text):
        raise ValueError("formal_new_signing_has_no_order_or_contract_context")
    if unit is not None and "work_hours" in role and unit != "hours":
        raise ValueError("work_hours_require_explicit_hour_unit")
    if kind is not None and role == "reported_utilization" and kind != "ratio":
        raise ValueError("utilization_requires_explicit_ratio_unit")
    if (
        role
        in {
            "cash_receipts",
            "advance_receipts",
            "recognized_revenue",
            "revenue_unspecified",
            "operating_cash_flow",
        }
        and kind is not None
        and kind != "monetary"
    ):
        raise ValueError("financial_role_requires_explicit_currency")
    return role


def _subject(row, context, role):
    name = row.subject.text.strip()
    if name in {"公司", "本公司", "集团", "本集团", "客户", "客户公司", "供应商", "企业", "本企业"}:
        raise ValueError("generic_party_is_not_a_named_business_subject")
    if NAMED.fullmatch(name) is None or re.match(r"某|其他|另一|客户|供应商", name):
        raise ValueError("explicit_named_business_subject_required")
    remainder = context.text.replace(name, "")
    if NAMED.search(remainder):
        raise ValueError("multiple_business_subjects_in_selected_context")
    prefix = context.text[: row.metric.start - context.start]
    if re.search(r"客户|供应商|对手", prefix[row.subject.end - context.start :]) and role in {
        "recognized_revenue",
        "revenue_unspecified",
    }:
        raise ValueError("customer_or_counterparty_role_not_reporting_company")
    if (
        row.metric.text in {"客户现金到账", "客户验收"}
        and re.search(
            re.escape(name) + r".{0,50}(?:收到|收取|交付|交货)|本公司.{0,20}(?:收到|收取|交付|交货)",
            context.text,
        )
        is None
    ):
        raise ValueError("customer_measurement_owner_not_explicit")
    return name


def _locator_fields(row):
    for name in (
        "subject",
        "metric",
        "value",
        "period",
        "reporting_scope",
        "unit",
        "period_year",
        "context",
        "shared_context",
    ):
        span = getattr(row, name)
        if span is not None:
            yield name, span
    if row.ordered_pair is not None:
        for name in ("periods", "values"):
            for index, span in enumerate(getattr(row.ordered_pair, name)):
                yield f"ordered_pair.{name}.{index}", span


def validate(raw, record):
    raw_rows = raw.get("observations") if isinstance(raw, dict) else None
    if not isinstance(raw_rows, (list, tuple)) or len(raw_rows) > 256:
        raise ValueError("observations_must_be_a_bounded_array")
    qualified, gaps = [], []
    for index, raw_row in enumerate(raw_rows):
        issues = []
        try:
            row = NarrativeObservationV2.model_validate(raw_row)
            locators = {key: _literal(record, span) for key, span in _locator_fields(row)}
        except (ValueError, TypeError, ValidationError):
            gaps.append(
                {
                    "observation_index": index,
                    "issues": [{"stage": "span", "reason": "literal_span_or_shape_invalid"}],
                }
            )
            continue

        def checked(stage, function):
            try:
                return function()
            except (ValueError, TypeError, ArithmeticError) as exc:
                issues.append({"stage": stage, "reason": str(exc)})
                return None

        context = checked("scope", lambda: _local_context(record, row))
        numeric_stage = (
            "value"
            if row.value.text != row.value.text.strip()
            or row.unit is not None
            and NUMBER.fullmatch(row.value.text) is None
            else "unit"
            if row.unit is not None or NUMBER.fullmatch(row.value.text)
            else "value"
        )
        numeric = checked(numeric_stage, lambda: _numeric(row))
        if context:
            checked("scope", lambda: _scope(record, row, context))
            checked("pairing", lambda: _pairing(record, row, context))
            checked("unit", lambda: _unit_binding(record, row))
            calendar = checked("period", lambda: _calendar(row, context))
            role = checked(
                "metric_role",
                lambda: _role(
                    record, row, context, numeric[1] if numeric else None, numeric[3] if numeric else None
                ),
            )
            name = checked("subject", lambda: _subject(row, context, role))
        else:
            calendar, role, name = None, None, None
        # No partial token selection inside a larger number, regardless of a
        # model-supplied exact quote.
        text = record.normalized_text
        if row.subject.start and re.match(r"[\w\u3400-\u9fff]", text[row.subject.start - 1]):
            issues.append({"stage": "subject", "reason": "selected_subject_is_only_part_of_a_source_name"})
        selected_number = NUMBER.match(row.value.text)
        source_number = next(
            (match for match in NUMBER.finditer(text) if match.start() <= row.value.start < match.end()), None
        )
        if selected_number is not None and (
            source_number is None
            or source_number.start() != row.value.start
            or source_number.end() != row.value.start + selected_number.end()
        ):
            issues.append({"stage": "value", "reason": "selected_value_is_only_part_of_a_numeric_token"})
        if issues:
            gaps.append({"observation_index": index, "issues": issues})
            continue
        value, unit, currency, kind = numeric
        start, end, period_basis = calendar
        scope = row.reporting_scope.text if row.reporting_scope else "source_named_subject:" + name
        subject = ProviderRef(
            provider=record.record_ref.provider,
            object_type="source_named_business_subject",
            object_id=canonical_hash([record.source_instance, record.source_id, name]),
            adopted_version=VERSION,
            version_source="provider",
            content_hash=canonical_hash(name),
            authorization_ref=record.record_ref.authorization_ref,
        )
        metric_id = role + ("_units" if kind == "count" and unit in {"套", "台"} else "")
        identifier = canonical_hash(
            [
                record.record_ref.model_dump(mode="json"),
                VERSION,
                name,
                metric_id,
                scope,
                row.value.start,
                row.value.end,
                start.isoformat(),
                end.isoformat(),
                unit,
                currency,
            ]
        )
        local_value = context.text + (" " + row.shared_context.text if row.shared_context else "")
        basis = (
            "forecast"
            if re.search(r"预计|预测|计划|forecast|expected", local_value, re.I)
            else ("estimate" if re.search(r"估算|估计|estimated", local_value, re.I) else "reported")
        )
        locators.update(
            period_basis=period_basis,
            qualification="source_statement_bound_to_fixed_projection_not_financial_audit",
        )
        qualified.append(
            MetricObservation(
                observation_id=identifier,
                domain="business",
                subject_ref=subject,
                metric_id=metric_id,
                metric_version=VERSION,
                value_lower=value,
                value_upper=value,
                unit=unit,
                currency=currency,
                value_kind=kind,
                reporting_scope=scope,
                period_start=start,
                period_end=end,
                measurement_basis=basis,
                binding_status="source_bound",
                field_locators=locators,
                evidence_refs=(record.record_ref,),
                provenance=SectionProvenance(
                    schema_version=VERSION,
                    provider_ref=record.record_ref,
                    available_at=record.public_available_at or record.available_at or record.observed_at,
                ),
            )
        )
    return {
        "observations": qualified,
        "gaps": gaps,
        "extractor_version": VERSION,
        "qualification": "source_reported_numeric_observations_not_truth_or_causal_confirmation",
    }


def extract(record):
    """Only simple scalar sentences; disjoint headers/lists use literal host selection."""
    text = record.normalized_text or ""
    subjects, periods = list(NAMED.finditer(text)), list(PERIOD.finditer(text))
    aliases = sorted({alias for names in ALIASES.values() for alias in names}, key=lambda x: -len(x))
    proposed, occupied = [], []
    for alias in aliases:
        for metric in re.finditer(re.escape(alias), text, re.I):
            if any(a <= metric.start() < b for a, b in occupied):
                continue
            occupied.append((metric.start(), metric.end()))
            value = SCALAR.search(text, metric.end(), min(len(text), metric.end() + 30))
            owners = [m for m in subjects if m.end() <= metric.start() and metric.start() - m.end() <= 240]
            dates = [m for m in periods if m.end() <= metric.start() and metric.start() - m.end() <= 240]
            if value is None or not owners or not dates:
                continue
            owner, period = owners[-1], dates[-1]
            proposed.append(
                {
                    "subject": _span(text, owner.start(), owner.end()),
                    "metric": _span(text, metric.start(), metric.end()),
                    "value": _span(text, value.start(), value.end()),
                    "period": _span(text, period.start(), period.end()),
                }
            )
    result = validate({"observations": proposed}, record)
    result["extraction"] = {"observations": proposed}
    return result


def selection_payload(record):
    return {
        "record_ref": record.record_ref.model_dump(mode="json"),
        "text_sha256": record.normalized_text_hash,
        "text": record.normalized_text,
        "extractor_version": VERSION,
        "allowed_metric_roles": ALIASES,
        "instruction": (
            "仅从固定原文选逐字字段，Unicode码点半开区间start/end。"
            "每观察只含一个明确经营主体、一个指标、一个期间、一个数值；不计算、相加或填正确答案。"
            "value可只选一个数值，unit须另外定位原文明示单位（紧邻或明确单位表头）；"
            "period可选H1/H2，period_year必须定位同一明确原文年份，不用任务日期补年。"
            "期内须选有明确报告期的shared_context；财年无日期边界不提取。"
            "context定位同一主体的完整本行，必要shared_context只能选紧邻共同表头。"
            "斜杠并列须原文明确分别/列序，ordered_pair列出完整期间列与数值列、等长同索引；"
            "拆为各自单数值观察，禁止把比例、范围或缺列当配对。"
            "客户/公司等泛称不是经营主体；跨公司、角色、单位或期间不明确时省略并保留空输出。"
            "正式新签不等于净/毛订单，收入不自动等于确认收入，验收、预收与现金到账分别选择。"
            "标准工时与普通小时不自动同口径。observations=[]合法，不强提取。"
        ),
    }


def needs_selection(record):
    text = record.normalized_text or ""
    if not re.search(r"\d", text):
        return False
    parsed = extract(record)
    if parsed["gaps"]:
        return True
    covered = {
        (item.field_locators["metric"]["start"], item.field_locators["metric"]["end"])
        for item in (*record.metric_observations, *parsed["observations"])
        if isinstance(item.field_locators.get("metric"), dict)
        and "start" in item.field_locators["metric"]
        and "end" in item.field_locators["metric"]
    }
    aliases = sorted({alias for names in ALIASES.values() for alias in names}, key=lambda x: -len(x))
    occupied = []
    for alias in aliases:
        for match in re.finditer(re.escape(alias), text, re.I):
            if any(a <= match.start() < b for a, b in occupied):
                continue
            occupied.append((match.start(), match.end()))
            if (match.start(), match.end()) not in covered and re.search(
                r"\d", text[match.end() : match.end() + 160]
            ):
                return True
    return False


def validate_cached(record, observations):
    """Recompute persisted typed values from every saved literal, without a model."""
    for observation in observations:
        row, pair = {}, {"periods": {}, "values": {}}
        for key, locator in observation.field_locators.items():
            if key in {"period_basis", "qualification"}:
                continue
            span = {"text": locator["quote"], "start": locator["start"], "end": locator["end"]}
            if key.startswith("ordered_pair."):
                _, field, index = key.split(".")
                if field not in pair or not index.isdecimal() or int(index) in pair[field]:
                    raise ValueError("cached ordered column locator changed")
                pair[field][int(index)] = span
            elif key in NarrativeObservationV2.model_fields:
                row[key] = span
            else:
                raise ValueError("cached literal locator has unknown field")
        if pair["periods"] or pair["values"]:
            if any(sorted(values) != list(range(len(values))) for values in pair.values()):
                raise ValueError("cached ordered column indexes are incomplete")
            row["ordered_pair"] = {
                key: [values[index] for index in sorted(values)] for key, values in pair.items()
            }
        rebuilt = validate({"observations": [row]}, record)
        if (
            rebuilt["gaps"]
            or len(rebuilt["observations"]) != 1
            or rebuilt["observations"][0].model_dump(mode="json") != observation.model_dump(mode="json")
        ):
            raise ValueError("cached typed observation differs from literal revalidation")
