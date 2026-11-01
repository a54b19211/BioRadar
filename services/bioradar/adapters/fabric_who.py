"""Validate Fabric's WHO count locations and adopt only explicitly bounded periods.

Extraction remains in Data-Fabric. A quoted count is not an incidence rate.
"""

from __future__ import annotations

import re
from datetime import timedelta
from html.parser import HTMLParser

from bioradar.research.mining.models import MetricObservation, canonical_hash

FIELDS = ("Summary", "Overview", "Epidemiology", "Response", "Advice", "Assessment")
METHOD = "who.explicit-count.v1"
WORDS = dict(
    zip(
        (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        ),
        range(21),
        strict=True,
    )
) | {"no": 0, "a": 1, "an": 1}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _quoted_count(quote):
    """Validate one complete source token, including consistent three-digit groups."""
    word = quote.strip().casefold()
    if word.isdecimal():
        return int(word)
    grouped = re.fullmatch(
        r"[0-9]{1,3}(?P<separator>[, \u00a0\u202f])[0-9]{3}(?:(?P=separator)[0-9]{3})*", word
    )
    if grouped:
        return int(word.replace(grouped["separator"], ""))
    return WORDS.get(word)


def validate_counts(record, original):
    """Bind every derived number/statement to unchanged raw fields and clean text."""
    # Fabric's content version binds the entire deterministic projection, not just
    # the raw text. A changed label/date/population cannot inherit the original version.
    identity_keys = (
        "source_id",
        "source_record_id",
        "is_demo",
        "raw_sha256",
        "raw_media_type",
        "parser_version",
        "record_kind",
        "title",
        "source_url",
        "clean_text",
        "facts",
        "field_locators",
        "event_time",
        "source_published_at",
        "source_updated_at",
    )
    identity = {key: record[key] for key in identity_keys}
    identity["availability_proof"] = {
        key: value for key, value in (record.get("availability_proof") or {}).items() if key != "verified_at"
    }
    mapping = record.get("mapping_review")
    identity["mapping_review"] = (
        {key: value for key, value in mapping.items() if key != "reviewed_at"} if mapping else None
    )
    digest = canonical_hash(identity)
    if digest != record["version_id"]:
        lineage = record.get("version_lineage") or {}
        previous = lineage.get("previous_version_id")
        if (
            lineage.get("content_version_id") != digest
            or not isinstance(previous, str)
            or len(previous) != 64
            or canonical_hash({"content_version_id": digest, "previous_version_id": previous})
            != record["version_id"]
        ):
            raise ValueError("WHO semantic projection does not bind its fixed version")
    metadata = record["facts"]["count_extraction"]
    rows = record["facts"]["count_statements"]
    if (
        not isinstance(rows, list)
        or len(rows) > 256
        or not isinstance(metadata, dict)
        or metadata.get("method_version") != METHOD
        or metadata.get("assertion_status") != "source_reported"
        or metadata.get("rate_eligible") is not False
        or metadata.get("denominator_available") is not False
    ):
        raise ValueError("unsupported WHO count projection")
    parts, ranges = [original["Title"]], {}
    for key in FIELDS:
        value = original.get(key)
        if value:
            if not isinstance(value, str):
                raise ValueError("invalid WHO text field")
            parser = _Text()
            parser.feed(value)
            text = " ".join(parser.parts)
            start = len("\n\n".join(parts)) + 2
            ranges["/" + key] = (start, start + len(text))
            parts.append(text)
    clean = "\n\n".join(parts)
    if clean != record["clean_text"]:
        raise ValueError("WHO derived locations do not bind original clean text")

    def located(item):
        if not isinstance(item, dict) or item.get("basis") != "clean_text":
            raise ValueError("WHO count requires clean-text location")
        low, high = ranges[item["json_pointer"]]
        start, end = item["start"], item["end"]
        if (
            type(start) is not int
            or type(end) is not int
            or not low <= start < end <= high
            or clean[start:end] != item.get("quote")
        ):
            raise ValueError("WHO count location does not match original")
        return start, end

    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("method_version") != METHOD
            or row.get("native_notice_id") != record["source_record_id"]
            or row.get("assertion_status") != "source_reported"
            or row.get("metric") not in {"case_count", "death_count"}
            or type(row.get("value")) is not int
            or row["value"] < 0
            or row.get("denominator") is not None
            or row.get("rate") is not None
        ):
            raise ValueError("invalid source-reported WHO count")
        start, end = located(row["evidence"])
        value_start, value_end = located(row["value_evidence"])
        if not start <= value_start < value_end <= end:
            raise ValueError("WHO number outside its statement")
        value = _quoted_count(row["value_evidence"]["quote"])
        if value != row["value"]:
            raise ValueError("WHO value differs from quoted number")
        expected_unit = "cases" if row["metric"] == "case_count" else "deaths"
        if row.get("unit") != expected_unit:
            raise ValueError("WHO metric and unit differ")
    for item in metadata.get("unparsed", []):
        located(item["evidence"])


def metric_observations(reader, record, provenance, *, as_of):
    from bioradar.adapters.fabric_kdd import source_date

    metrics, gaps = [], set()
    for index, row in enumerate(record["facts"].get("count_statements", [])):
        period = row.get("time_scope") or {}
        region = (row.get("geography") or {}).get("text")
        disease = row.get("disease")
        if not disease or not region or row.get("uncertainties"):
            gaps.add("who_count_scope_incomplete_statement_retained_at_source")
            continue
        if (
            period.get("kind") != "reporting_interval"
            or period.get("explicit_interval") is not True
            or not period.get("start")
            or not period.get("end")
        ):
            # An "as of" date is not a one-day observation interval.
            gaps.add("who_point_or_unspecified_count_not_an_interval")
            continue
        try:
            start, end = source_date(period["start"]), source_date(period["end"]) + timedelta(days=1)
            if start >= end or end > as_of:
                raise ValueError()
        except (ValueError, TypeError):
            gaps.add("who_count_period_not_available")
            continue
        if row.get("case_status") in {None, "not_specified"}:
            gaps.add("who_case_definition_not_specified_no_cross_population_inference")
        population = canonical_hash([disease, region, row.get("case_status"), row.get("count_role")])
        ref = reader.ref(record)
        subject = ref.model_copy(
            update={
                "provider": "who.native-reported-scope",
                "object_type": "reported_population_literal",
                "object_id": population,
            }
        )
        metrics.append(
            MetricObservation(
                observation_id=canonical_hash([record["version_id"], index, row]),
                domain="public_health",
                subject_ref=subject,
                metric_id=row["metric"],
                metric_version=METHOD,
                value_lower=row["value"],
                value_upper=row["value"],
                unit=row["unit"],
                value_kind="count",
                reporting_scope="who_reported:" + row["count_kind"],
                population="; ".join(
                    [disease, row.get("case_status") or "case_definition_not_specified", row["count_role"]]
                ),
                region=region,
                period_start=start,
                period_end=end,
                measurement_basis="reported",
                binding_status="source_bound",
                field_locators={
                    "value": row["value_evidence"],
                    "statement": row["evidence"],
                    "source_interpretation": {
                        "method": METHOD,
                        "time_scope": period,
                        "geography": row["geography"],
                        "disease": disease,
                        "case_status": row.get("case_status"),
                        "count_role": row["count_role"],
                        "count_kind": row["count_kind"],
                    },
                    "period_end_conversion": {
                        "rule": "inclusive_day_to_exclusive_next_day",
                        "source_value": period["end"],
                    },
                },
                evidence_refs=(ref,),
                provenance=provenance,
            )
        )
    if record["facts"].get("count_statements"):
        gaps.add("who_counts_source_reported_not_independent_events_or_population_rates")
    return metrics, gaps
