"""Literal narrative extraction and validation; all arithmetic stays in Python."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import Field

from bioradar.research.mining.models import (
    FrozenModel,
    MetricObservation,
    ProviderRef,
    SectionProvenance,
    canonical_hash,
)

NARRATIVE_VERSION = "literal-narrative-observations-v1"
ALIASES = {
    "used_work_hours": ("有效工时", "实际工时", "生产工时", "已用工时", "worked hours", "productive hours"),
    "available_work_hours": ("可用工时", "可供工时", "available work hours", "available hours"),
    "net_orders": ("净订单", "net orders"),
    "gross_orders": ("毛订单", "gross orders"),
    "orders_unspecified": ("订单金额", "订单额", "新增订单", "订单", "orders"),
    "recognized_revenue": ("确认收入", "营业收入", "营收", "recognized revenue", "revenue"),
    "acceptance": ("验收金额", "验收", "accepted deliveries", "acceptance"),
    "advance_receipts": ("预收款", "预收", "advance receipts", "customer advances"),
    "cash_receipts": ("现金到账", "回款", "cash receipts"),
    "operating_cash_flow": ("经营现金流", "经营活动现金流", "operating cash flow"),
    "reported_utilization": ("工时利用率", "产能利用率", "利用率", "utilization", "utilisation"),
}
SUBJECT = re.compile(
    r"[\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9·＆& -]{1,24}"
    r"(?:公司|集团|工厂|厂商|样本合计|样本企业|样本)"
    r"|[A-Z][A-Za-z0-9& .-]+(?:Inc\.?|Ltd\.?|Corp\.?)"
)
VALUE = re.compile(
    r"(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?P<unit>亿元|万元|千元|元|亿小时|万小时|千小时|小时|万套|套|台|%|％|percent\b|hours?\b|million\s+(?:CNY|RMB|USD)\b|(?:CNY|RMB|USD)\b)",
    re.I,
)
PERIOD = re.compile(
    r"(?:20\d{2}\s*年?\s*(?:第?[一二三四1234]季度|[前一二三四123456789十]+个月|[上下]半年|[Qq][1-4]|全年)|[Qq][1-4]\s*20\d{2}|20\d{2}-\d{2}-\d{2}\s*(?:至|到|—|–|to)\s*20\d{2}-\d{2}-\d{2})",
    re.I,
)


class LiteralSpan(FrozenModel):
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class NarrativeObservation(FrozenModel):
    subject: LiteralSpan
    metric: LiteralSpan
    value: LiteralSpan
    period: LiteralSpan
    reporting_scope: LiteralSpan | None = None


class NarrativeExtraction(FrozenModel):
    observations: tuple[NarrativeObservation, ...] = Field(default=(), max_length=256)


def narrative_version(version=None):
    if version not in {None, NARRATIVE_VERSION, "literal-narrative-observations-v2"}:
        raise ValueError("unknown literal narrative contract version")
    return version or NARRATIVE_VERSION


def narrative_extraction_schema(*, version=None):
    if narrative_version(version) == "literal-narrative-observations-v2":
        from bioradar.research.datasets.narrative_v2 import schema

        return schema()
    return NarrativeExtraction.model_json_schema()


def _span(text, start, end):
    return {"text": text[start:end], "start": start, "end": end}


def parse_value(raw):
    match = VALUE.fullmatch(raw.strip())
    if match is None:
        raise ValueError("literal numeric value and unit required")
    number = Decimal(match["number"].replace(",", ""))
    unit = match["unit"].casefold()
    scale = Decimal(1)
    currency, kind = None, "count"
    if unit in {"亿元", "万元", "千元", "元"}:
        scale = Decimal({"亿元": 100000000, "万元": 10000, "千元": 1000, "元": 1}[unit])
        unit, currency, kind = "CNY", "CNY", "monetary"
    elif unit.startswith("million "):
        scale = Decimal(1000000)
        currency = "CNY" if unit.split()[-1] in {"cny", "rmb"} else "USD"
        unit, kind = currency, "monetary"
    elif unit in {"cny", "rmb", "usd"}:
        currency = "CNY" if unit in {"cny", "rmb"} else "USD"
        unit, kind = currency, "monetary"
    elif unit in {"亿小时", "万小时", "千小时", "小时", "hour", "hours"}:
        scale = Decimal({"亿小时": 100000000, "万小时": 10000, "千小时": 1000}.get(unit, 1))
        unit = "hours"
    elif unit in {"%", "％", "percent"}:
        unit, kind, scale = "ratio", "ratio", Decimal("0.01")
    elif unit == "万套":
        unit, scale = "套", Decimal(10000)
    return number * scale, unit, currency, kind


def parse_period(raw):
    """Only explicit calendar periods; no task-as_of year or invented one-day range."""
    years = set(re.findall(r"20\d{2}", raw))
    explicit = re.fullmatch(
        r"(20\d{2}-\d{2}-\d{2})\s*(?:至|到|—|–|to)\s*(20\d{2}-\d{2}-\d{2})", raw.strip(), re.I
    )
    if explicit:
        return (
            datetime.fromisoformat(explicit[1]).replace(tzinfo=UTC),
            datetime.fromisoformat(explicit[2]).replace(tzinfo=UTC) + timedelta(days=1),
            "explicit_source_date_range_inclusive_end",
        )
    if len(years) != 1:
        raise ValueError("one explicit source year required")
    year = int(next(iter(years)))
    quarter = re.search(r"[Qq]([1-4])|第?([一二三四1234])季度", raw)
    if quarter:
        token = quarter[1] or quarter[2]
        q = int(token) if token.isdigit() else "一二三四".index(token) + 1
        month = 1 + 3 * (q - 1)
        start = datetime(year, month, 1, tzinfo=UTC)
        end = datetime(year + (q == 4), month % 12 + 3 if q != 4 else 1, 1, tzinfo=UTC)
        return start, end, "explicit_source_calendar_quarter"
    cumulative = re.search(r"前([一二三四五六七八九十]|1[012]|[1-9])个月", raw)
    if cumulative:
        token = cumulative[1]
        count = int(token) if token.isdigit() else "一二三四五六七八九十".index(token) + 1
        return (
            datetime(year, 1, 1, tzinfo=UTC),
            datetime(year + (count == 12), count + 1 if count < 12 else 1, 1, tzinfo=UTC),
            "explicit_source_calendar_cumulative_months",
        )
    if "上半年" in raw:
        return (
            datetime(year, 1, 1, tzinfo=UTC),
            datetime(year, 7, 1, tzinfo=UTC),
            "explicit_source_calendar_half_year",
        )
    if "下半年" in raw:
        return (
            datetime(year, 7, 1, tzinfo=UTC),
            datetime(year + 1, 1, 1, tzinfo=UTC),
            "explicit_source_calendar_half_year",
        )
    if "全年" in raw:
        return (
            datetime(year, 1, 1, tzinfo=UTC),
            datetime(year + 1, 1, 1, tzinfo=UTC),
            "explicit_source_calendar_year",
        )
    raise ValueError("calendar period boundaries ambiguous")


def _literal(record, span):
    text = record.normalized_text or ""
    if not 0 <= span.start < span.end <= len(text) or text[span.start : span.end] != span.text:
        raise ValueError("extraction span does not match the fixed source version")
    return {
        "kind": "clean_text",
        "quote": span.text,
        "start": span.start,
        "end": span.end,
        "offset_basis": "fixed_normalized_unicode",
        "text_hash": record.normalized_text_hash,
    }


def validate_narrative_observations(raw, record, *, version=None):
    """LLM may select literal roles; it may not supply or calculate numeric answers."""
    if narrative_version(version) == "literal-narrative-observations-v2":
        from bioradar.research.datasets.narrative_v2 import validate

        return validate(raw, record)
    extracted = NarrativeExtraction.model_validate(raw)
    qualified, gaps = [], []
    for index, row in enumerate(extracted.observations):
        try:
            locators = {
                key: _literal(record, getattr(row, key)) for key in ("subject", "metric", "value", "period")
            }
            if row.reporting_scope:
                locators["reporting_scope"] = _literal(record, row.reporting_scope)
            value, unit, currency, kind = parse_value(row.value.text)
            period_context = (record.normalized_text or "")[max(0, row.period.start - 20) : row.period.end]
            if re.search(r"财年|fiscal|\bFY", period_context, re.I):
                raise ValueError("fiscal calendar mapping requires explicit source bounds")
            start, end, period_basis = parse_period(row.period.text)
            roles = [
                role for role, aliases in ALIASES.items() if row.metric.text.casefold().strip() in aliases
            ]
            if len(roles) != 1:
                raise ValueError("literal metric role ambiguous")
            # A title or sentence cannot silently make an estimate a reported realization.
            line = (record.normalized_text or "")[max(0, row.value.start - 20) : row.value.end + 15]
            basis = (
                "forecast"
                if re.search(r"预计|预测|计划|forecast|expected", line, re.I)
                else "estimate"
                if re.search(r"估算|估计|estimated", line, re.I)
                else "reported"
            )
            subject_text = row.subject.text.strip()
            scope = (
                row.reporting_scope.text if row.reporting_scope else "source_named_subject:" + subject_text
            )
            subject = ProviderRef(
                provider=record.record_ref.provider,
                object_type="source_named_business_subject",
                object_id=canonical_hash([record.source_instance, record.source_id, subject_text]),
                adopted_version=NARRATIVE_VERSION,
                version_source="provider",
                content_hash=canonical_hash(subject_text),
                authorization_ref=record.record_ref.authorization_ref,
            )
            identifier = canonical_hash(
                [record.record_ref.model_dump(mode="json"), row.model_dump(mode="json")]
            )
            metric_id = roles[0] + ("_units" if kind == "count" and unit in {"套", "台"} else "")
            locators["period_basis"] = period_basis
            locators["qualification"] = "source_statement_bound_to_fixed_projection_not_financial_audit"
            qualified.append(
                MetricObservation(
                    observation_id=identifier,
                    domain="business",
                    subject_ref=subject,
                    metric_id=metric_id,
                    metric_version=NARRATIVE_VERSION,
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
                        schema_version=NARRATIVE_VERSION,
                        provider_ref=record.record_ref,
                        available_at=record.public_available_at or record.available_at or record.observed_at,
                    ),
                )
            )
        except (TypeError, ValueError, ArithmeticError):
            gaps.append(
                {"observation_index": index, "reason": "literal_metric_scope_period_or_value_requires_review"}
            )
    return {
        "observations": qualified,
        "gaps": gaps,
        "extractor_version": NARRATIVE_VERSION,
        "qualification": "source_reported_numeric_observations_not_truth_or_causal_confirmation",
    }


def extract_narrative_observations(record, *, version=None):
    """Unambiguous explicit roles with nearest preceding literal subject and period."""
    if narrative_version(version) == "literal-narrative-observations-v2":
        from bioradar.research.datasets.narrative_v2 import extract

        return extract(record)
    text = record.normalized_text or ""
    subjects, periods = list(SUBJECT.finditer(text)), list(PERIOD.finditer(text))
    aliases = sorted(
        ((alias, role) for role, names in ALIASES.items() for alias in names), key=lambda pair: -len(pair[0])
    )
    proposed, occupied = [], []
    for alias, _ in aliases:
        for match in re.finditer(re.escape(alias), text, re.I):
            if any(a <= match.start() < b for a, b in occupied):
                continue
            occupied.append((match.start(), match.end()))
            nearby = VALUE.search(text, match.end(), min(len(text), match.end() + 30))
            if nearby is None:
                continue
            between = text[match.end() : nearby.start()].casefold()
            if any(other.casefold() in between for other, _ in aliases):
                continue
            before_subjects = [
                m for m in subjects if m.end() <= match.start() and match.start() - m.end() <= 240
            ]
            before_periods = [
                m for m in periods if m.end() <= match.start() and match.start() - m.end() <= 240
            ]
            if not before_subjects or not before_periods:
                continue
            # No crossing a new named subject or source section without explicit context.
            subject, period = before_subjects[-1], before_periods[-1]
            proposed.append(
                {
                    "subject": _span(text, subject.start(), subject.end()),
                    "metric": _span(text, match.start(), match.end()),
                    "value": _span(text, nearby.start(), nearby.end()),
                    "period": _span(text, period.start(), period.end()),
                }
            )
    result = validate_narrative_observations({"observations": proposed}, record)
    result["extraction"] = {"observations": proposed}
    return result


def applicable_directions(record):
    """Method applicability comes from visible content/typed fields, never a fixture label."""
    text = record.normalized_text or ""
    reasons = {}
    if (
        record.trial_refs
        or record.project_ref
        or re.search(
            r"临床试验|随机(?:对照)?(?:三|III|Ⅲ|3)期|phase\s*(?:[123]|[iv]+)\b|\bNCT\d{8}", text, re.I
        )
    ):
        reasons["pipeline"] = "fixed_registration_or_explicit_clinical_trial_statement"
    if re.search(
        r"机制|基因|靶点|细胞|蛋白|论文|临床试验|随机(?:对照)?(?:三|III|Ⅲ|3)期|\b(?:PFS|ORR|randomi[sz]ed|mechanism|gene|protein)\b",
        text,
        re.I,
    ):
        reasons["research"] = "scientific_or_clinical_narrative_for_document_exploration"
    if record.source_id == "who_don" or re.search(
        r"不明(?:原因|病原)|肺炎|病例|疫情|医护.{0,4}感染|聚集性?|pneumonia|outbreak|cluster of cases",
        text,
        re.I,
    ):
        reasons["public_health"] = "attributed_public_health_event_or_official_notice"
    if any(re.search(re.escape(alias), text, re.I) for names in ALIASES.values() for alias in names) or any(
        m.domain == "business" for m in record.metric_observations
    ):
        reasons["business"] = "explicit_business_metric_roles_or_source_bound_observations"
    return reasons
