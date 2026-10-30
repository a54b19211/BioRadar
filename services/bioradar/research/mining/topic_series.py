"""Pure descriptive topic accumulation; no network, database, clock or LLM calls."""

import hashlib
import heapq
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from statistics import median

from bioradar.research.mining.methods_text import STOP, is_structured_json, phrase_spans
from bioradar.research.mining.models import TimeExpression, canonical_hash
from bioradar.research.mining.prepare import time_interval
from bioradar.research.mining.signals import assess_records
from bioradar.research.mining.temporal import batch_bursts

TOPIC_COMPUTE_VERSION = "bioradar-topic-series-v0.5"
# This release changes assessment/follow-up semantics, not stored feature extraction.
TOPIC_FEATURE_VERSION = "bioradar-topic-series-v0.2"
MAX_FEATURES_PER_RECORD = 256
MAX_NARRATIVE_CHARACTERS = 20000
MAX_EVIDENCE_PREVIEW = 200
MAX_EVALUATION_FEATURES = 3000
MAX_METRIC_POINTS = 120


def source_site(history):
    facts = history["facts"]
    value = facts.get("source_site")
    if isinstance(value, str) and value:
        return value
    native = history["source_record_id"]
    if native.startswith("legacy:") and len(native.split(":")) >= 3:
        return native.split(":", 2)[1]
    return history["source_id"]


def _time(raw):
    if raw is None:
        return None
    if isinstance(raw, dict):
        value, precision = raw.get("value"), raw.get("precision", "unknown")
    else:
        value = str(raw)
        precision = (
            "year"
            if len(value) == 4
            else "month"
            if len(value) == 7
            else "day"
            if len(value) == 10
            else "instant"
        )
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00")) if precision == "instant" else None
        if instant is not None and instant.tzinfo is None:
            return None
        return time_interval(TimeExpression(raw=value, precision=precision, instant=instant))
    except (TypeError, ValueError, OverflowError):
        return None


def project_contribution(record, history, definition, *, as_of):
    """Reduce existing validated EngineRecord fields without copying prose or guessing identities."""
    facts, site = history["facts"], source_site(history)
    narrative = record.normalized_text or ""
    if is_structured_json(narrative):
        narrative = ""
    searchable = (history.get("title", "") + "\n" + narrative).casefold()
    matches = (
        (not definition["source_sites"] or site in definition["source_sites"])
        and all(term.casefold() in searchable for term in definition["terms_all"])
        and (
            not definition["terms_any"]
            or any(term.casefold() in searchable for term in definition["terms_any"])
        )
    )
    raw_schema = None
    # Only a fixed observation schema is read; ordinary news is never a numeric observation.
    if (
        history.get("raw_media_type") == "application/json"
        and history["parser_version"] == "datafabric.observation.projection.v0.1"
    ):
        raw_schema = json.loads(history["raw_content"])
        if raw_schema.get("schema_version") != "datafabric.observation.v1":
            raise ValueError("invalid observation schema")
        metric_filter = definition.get("metric_ids", [])
        if metric_filter and raw_schema.get("metric_id") not in metric_filter:
            matches = False
    features = {}
    feature_limited = False
    if matches and narrative and "text_exposure" in definition["profiles"]:
        text = narrative[:MAX_NARRATIVE_CHARACTERS]
        candidates = phrase_spans(text, STOP)
        # Bound feature storage before lead selection; admission does not depend on a top-candidate list.
        selected = sorted(candidates, key=lambda x: (hashlib.sha256(x.encode()).hexdigest(), x))
        feature_limited = len(selected) > MAX_FEATURES_PER_RECORD or len(text) < len(narrative)
        features = dict.fromkeys(selected[:MAX_FEATURES_PER_RECORD], 1)
    publication = (
        _time({"value": record.published_time_raw, "precision": record.time_precision})
        if record.publication_time_verified
        and "may_be_imputed" not in str((history.get("source_published_at") or {}).get("meaning", ""))
        else None
    )
    study = (
        canonical_hash(record.study_cluster_ref.identity_key)
        if record.study_independence_status == "confirmed" and record.study_cluster_ref
        else None
    )
    doc_hash = record.normalized_text_hash or record.document_hash
    # A native trial ID is sufficient for registration counts, not independent drug-program evidence.
    trial_id = facts.get("trial_id") if history["record_kind"] == "trial" else None
    native_publication = facts.get("doi") or facts.get("pmid")
    if study:
        unit = study
    elif trial_id:
        unit = canonical_hash(["registration", trial_id])
    elif native_publication:
        unit = canonical_hash(["report", native_publication])
    else:
        unit = canonical_hash(["text", doc_hash])
    retained = {
        key: facts[key]
        for key in (
            "trial_id",
            "status",
            "phase",
            "why_stopped",
            "primary_completion_date",
            "primary_completion_date_type",
            "completion_date",
            "completion_date_type",
            "publication_types",
            "correction_links",
            "letter_type",
            "application_numbers",
            "letter_date",
            "event_type",
            "native_notice_id",
            "notice_id",
            "publication_date",
            "publication_date_and_time",
            "last_modified",
            "publication_state",
        )
        if key in facts
    }
    value = {
        "attention": assess_records([record]),
        "signal_context": record.signal_context.model_dump(mode="json") if record.signal_context else None,
        "source_independence_status": record.source_independence_status,
        "feature_version": TOPIC_FEATURE_VERSION,
        "source_id": history["source_id"],
        "source_record_id": history["source_record_id"],
        "version_id": history["version_id"],
        "raw_sha256": history["raw_sha256"],
        "source_site": site,
        "record_ref": record.record_ref.model_dump(mode="json"),
        "title": history["title"][:300],
        "field_locators": {
            key: history["field_locators"][key] for key in retained if key in history["field_locators"]
        },
        "matched": matches,
        "text_eligible": bool(narrative),
        "withdrawn": bool({"deleted", "retracted"} & set(record.record_flags))
        or bool(raw_schema and raw_schema.get("status") == "withdrawn"),
        "unit": unit,
        "study_confirmed": study is not None,
        "source_family": record.source_family if record.source_independence_status == "confirmed" else None,
        "narrative_hash": doc_hash,
        "features": features,
        "features_limited": feature_limited,
        "publication_start": publication[0].isoformat() if publication else None,
        "publication_end": publication[1].isoformat() if publication else None,
        "first_seen_at": history["first_seen_at"],
        "received_at": history["received_at"],
        "source_updated_at": history.get("source_updated_at"),
        "event_time": history.get("event_time"),
        "parser_version": history["parser_version"],
        "facts": retained,
        "observation": raw_schema,
    }
    value["content_fingerprint"] = canonical_hash(
        {
            "text": doc_hash,
            "facts": retained,
            "observation": raw_schema,
            "withdrawn": value["withdrawn"],
            "publication_time": [value["publication_start"], value["publication_end"]],
        }
    )
    return value


def _bucket_start(value, bucket):
    date = value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if bucket == "hour":
        return date
    date = date.replace(hour=0)
    if bucket == "day":
        return date
    return date.replace(day=1) if bucket == "month" else date - timedelta(days=date.weekday())


def _next_bucket(value, bucket):
    if bucket in {"hour", "day"}:
        return value + timedelta(hours=1) if bucket == "hour" else value + timedelta(days=1)
    if bucket == "week":
        return value + timedelta(days=7)
    return value.replace(year=value.year + (value.month == 12), month=value.month % 12 + 1)


def _previous_bucket(value, bucket):
    if bucket in {"hour", "day"}:
        return value - timedelta(hours=1) if bucket == "hour" else value - timedelta(days=1)
    if bucket == "week":
        return value - timedelta(days=7)
    return value.replace(year=value.year - (value.month == 1), month=(value.month - 2) % 12 + 1)


def median_slope(points):
    """Median of pairwise slopes, descriptive only; no serial-independence confidence claim."""
    pairs = [
        (y2 - y1) / Decimal(x2 - x1)
        for i, (x1, y1) in enumerate(points)
        for x2, y2 in points[i + 1 :]
        if x2 != x1
    ]
    return str(median(pairs)) if pairs else None


def _evidence(value):
    key = canonical_hash([value["source_id"], value["source_record_id"], value["version_id"]])
    return key, {
        key: value[key]
        for key in (
            "record_ref",
            "source_id",
            "source_record_id",
            "version_id",
            "raw_sha256",
            "source_site",
            "field_locators",
        )
    }


def followup_term(original):
    """A literal native identifier or a bounded title prefix, never an inferred entity."""
    facts = original["facts"]
    for field in ("trial_id", "application_numbers"):
        value = facts.get(field)
        if isinstance(value, list):
            value = next((v for v in value if isinstance(v, str) and v), None)
        if isinstance(value, str) and 2 <= len(value) <= 120:
            return {
                "term": value,
                "field": field,
                "locator": original["field_locators"].get(field),
                "reason": "原件中明确记载的字符串标识",
            }
    title = facts.get("title") or original.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    prefix = title
    for separator in (" – ", " — ", " - ", "\n"):
        prefix = prefix.split(separator, 1)[0]
    prefix = prefix.strip()[:120]
    if len(prefix) < 3:
        return None
    start = title.find(prefix)
    return {
        "term": prefix,
        "field": "title",
        "field_value": title,
        "locator": original["field_locators"].get("title"),
        "literal_start": start,
        "literal_end": start + len(prefix),
        "offset_basis": "decoded_field_value",
        "reason": "固定原件标题中的前缀字符串，不是实体识别",
    }


def _native_event(value):
    facts = value["facts"]
    if value["source_id"] == "who_don" and value["parser_version"] == "datafabric.who.don.projection.v0.1":
        return {
            "event_type": "outbreak_notice_published",
            "event_time": value.get("event_time")
            or facts.get("publication_date_and_time")
            or facts.get("publication_date"),
            "basis": {"kind": "source_bound"},
        }
    if value["source_id"] == "fda_crl" and facts.get("letter_type") == "COMPLETE RESPONSE":
        return {
            "event_type": "complete_response_letter_signed",
            "event_time": value.get("event_time"),
            "basis": {"kind": "source_bound"},
        }
    return None


def evaluate_series(
    observations,
    definition,
    *,
    as_of,
    seed_complete,
    source_watermarks,
    source_errors=None,
    omitted_records=0,
):
    """Evaluate a frozen adopted prefix. Revisions replace current contributions, never old reports."""
    source_errors = source_errors or {}
    all_values = list(observations)
    active = [v for v in all_values if v["matched"] and not v["withdrawn"]]
    evidence = {}
    for value in all_values:
        key, item = _evidence(value)
        evidence[key] = {"key": key, **item}
    start = _bucket_start(as_of, definition["bucket"])
    for _ in range(definition["lookback_buckets"] - 1):
        start = _previous_bucket(start, definition["bucket"])
    bounds, cursor = [], start
    for _ in range(definition["lookback_buckets"]):
        end = _next_bucket(cursor, definition["bucket"])
        bounds.append((cursor, end))
        cursor = end
    units, studies, sources, features = [], [], [], []
    undated = 0
    for left, right in bounds:
        units.append(set())
        studies.append(set())
        sources.append(defaultdict(set))
        features.append(defaultdict(set))
    admitted, feature_heap = set(), []
    feature_limited = False
    for value in active:
        for phrase in value["features"]:
            if phrase in admitted:
                continue
            score = int(hashlib.sha256(phrase.encode()).hexdigest(), 16)
            if len(admitted) < MAX_EVALUATION_FEATURES:
                admitted.add(phrase)
                heapq.heappush(feature_heap, (-score, phrase))
            else:
                feature_limited = True
                if score < -feature_heap[0][0]:
                    _, removed = heapq.heapreplace(feature_heap, (-score, phrase))
                    admitted.remove(removed)
                    admitted.add(phrase)
    for value in active:
        if not value.get("text_eligible", True):
            continue
        if not value["publication_start"] or not value["publication_end"]:
            undated += 1
            continue
        left, right = (
            datetime.fromisoformat(value["publication_start"]),
            datetime.fromisoformat(value["publication_end"]),
        )
        index = next(
            (i for i, (a, b) in enumerate(bounds) if a <= left and right <= b and right <= as_of), None
        )
        if index is None:
            continue  # Coarse dates are not spread into invented weekly dates.
        unit = value["unit"]
        units[index].add(unit)
        if value["study_confirmed"]:
            studies[index].add(unit)
        sources[index][value["source_site"]].add(unit)
        for phrase in value["features"]:
            if phrase in admitted:
                features[index][phrase].add(unit)
    buckets = []
    for i, (left, right) in enumerate(bounds):
        buckets.append(
            {
                "start": left.isoformat() if definition["bucket"] == "hour" else left.date().isoformat(),
                "end": right.isoformat() if definition["bucket"] == "hour" else right.date().isoformat(),
                "complete_time_bucket": right <= as_of,
                "exposures": len(units[i]),
                "confirmed_study_units": len(studies[i]),
                "unresolved_units": len(units[i] - studies[i]),
                "source_counts": {k: len(v) for k, v in sorted(sources[i].items())},
                "feature_counts": {
                    k: len(v) for k, v in sorted(features[i].items(), key=lambda x: (-len(x[1]), x[0]))[:50]
                },
                "qualification": "adopted_exposure_only",
            }
        )
    signals, states, metric_series, gaps, questions = [], [], [], set(), []
    gaps.add("adopted_records_not_complete_domain_denominator")
    if feature_limited:
        gaps.add("evaluation_feature_admission_limited")
    if not seed_complete:
        gaps.add("topic_seed_or_catchup_incomplete")
    if source_errors:
        gaps.add("source_unavailable_not_no_change")
    if omitted_records:
        gaps.add("evaluation_record_budget_incomplete")
    if any(not v["study_confirmed"] for v in active):
        gaps.add("unresolved_study_independence")
    if any(v["features_limited"] for v in active):
        gaps.add("record_feature_admission_limited")
    if undated:
        gaps.add("undated_or_unverified_publication_time")
    closed = [i for i, b in enumerate(buckets) if b["complete_time_bucket"]]
    complete = [i for i in closed if buckets[i]["exposures"]]
    adjacent_recent = closed[-3:] if len(closed) >= 3 else []
    if any(not buckets[i]["exposures"] for i in adjacent_recent):
        gaps.add("missing_exposure_bucket_not_zero")
    all_features = set().union(*(set(f) for f in features)) if features else set()
    text_signals = []
    for phrase in all_features:
        support = set().union(*(f.get(phrase, set()) for f in features))
        if omitted_records or len(support) < definition["minimum_support"] or len(complete) < 3:
            continue
        counts = [len(features[i].get(phrase, set())) for i in complete]
        exposure = [len(units[i]) for i in complete]
        rates = [Decimal(c) / Decimal(n) for c, n in zip(counts, exposure)]
        slope = median_slope(list(zip(complete, rates)))
        burst = batch_bursts(counts, exposure)
        persistent = len(adjacent_recent) == 3 and all(units[i] for i in adjacent_recent)
        persistent = persistent and sum(bool(features[i].get(phrase)) for i in adjacent_recent) >= 2
        if not persistent or not (
            slope and Decimal(slope) > 0 or (burst["states"] and (burst["states"][-1] or 0) > 0)
        ):
            continue
        source_sets = [set(sources[i]) for i in complete]
        mix_stable = all(s == source_sets[0] for s in source_sets)
        # Follow the descriptive trajectory, not a phrase forever or a parser/version ID.
        # Re-reading the same units does not create a new question.
        key = canonical_hash(
            [
                "text_pattern",
                phrase,
                [
                    [
                        buckets[i]["start"],
                        buckets[i]["end"],
                        sorted(features[i].get(phrase, set())),
                        sorted(units[i]),
                    ]
                    for i in complete
                ],
            ]
        )
        text_signals.append(
            {
                "key": key,
                "kind": "text_pattern",
                "title": "持续出现的文本线索：" + phrase,
                "reason": "多个时间桶中反复出现，列为待核问题；文本出现率不能代表研究价值或真实发病率。",
                "support_units": len(support),
                "qualification": "exploratory_adopted_exposure",
                "details": {
                    "phrase": phrase,
                    "descriptive_slope": slope,
                    "burst_states": burst["states"],
                    "source_site_set_equal": mix_stable,
                    "source_share_and_original_denominator_stability": "unknown",
                    "confirmed_growth": False,
                },
                "evidence_keys": [],
                "next_questions": [
                    "这些材料是否指向同一项研究？",
                    "这项变化会改变当前研究问题的哪一条假设？",
                ],
            }
        )
    # Each method's counts are not mixed into a universal value score.
    text_signals.sort(key=lambda s: (-s["support_units"], s["key"]))
    for signal in text_signals[:12]:
        signal["evidence_keys"] = sorted(
            {_evidence(v)[0] for v in active if signal["details"]["phrase"] in v["features"]}
        )[:20]
    signals.extend(text_signals[:12])
    for value in all_values:
        facts, evidence_key = value["facts"], _evidence(value)[0]
        if (
            "registered_state" in definition["profiles"]
            and facts.get("trial_id")
            and value["matched"]
            and not value["withdrawn"]
        ):
            states.append(
                {
                    "unit_id": facts["trial_id"],
                    "status": facts.get("status"),
                    "phase": facts.get("phase"),
                    "first_observed_at": value["first_seen_at"],
                    "last_observed_at": value["received_at"],
                    "source_updated_at": value["source_updated_at"],
                    "primary_completion_date": facts.get("primary_completion_date"),
                    "completion_date": facts.get("completion_date"),
                    "evidence_key": evidence_key,
                    "qualification": "last_known_registration_not_current_clinical_truth",
                }
            )
            change = value.get("change")
            if value.get("include_attention", True) and change and change.get("state_changed"):
                signals.append(
                    {
                        "key": canonical_hash(
                            [
                                "state_change",
                                value["source_id"],
                                value["source_record_id"],
                                value["content_fingerprint"],
                            ]
                        ),
                        "kind": "state_change",
                        "title": "登记字段变化：" + facts["trial_id"],
                        "reason": "同一登记的已采用版本出现状态或阶段差异；公开修订日不等于临床变化发生日。",
                        "support_units": 1,
                        "qualification": "source_registration_change",
                        "details": change,
                        "evidence_keys": [evidence_key],
                        "next_questions": ["变化原因及实际发生时间是否已有原始说明？"],
                    }
                )
            due = facts.get("primary_completion_date") or facts.get("completion_date")
            interval = _time(due.get("date") if isinstance(due, dict) else due)
            kind = (
                facts.get("primary_completion_date_type")
                or (due.get("type") if isinstance(due, dict) else None)
                or facts.get("completion_date_type")
            )
            if (
                interval
                and interval[1] <= as_of
                and kind == "ESTIMATED"
                and facts.get("status") not in {"COMPLETED", "TERMINATED", "WITHDRAWN"}
            ):
                signals.append(
                    {
                        "key": canonical_hash(["milestone", facts["trial_id"], due]),
                        "kind": "milestone_check",
                        "title": "预计里程碑已到，需核实：" + facts["trial_id"],
                        "reason": "原登记中的预计日期已过；缺少新登记不能推断项目停滞或失败。",
                        "support_units": 1,
                        "qualification": "verification_gap_not_failure",
                        "details": {"expected_date": due, "last_known_status": facts.get("status")},
                        "evidence_keys": [evidence_key],
                        "next_questions": ["实际里程碑是否完成？登记是否已更新？"],
                    }
                )
        if (
            value.get("include_attention", True)
            and value.get("change", {}).get("evidence_changed")
            and (value["matched"] or value["change"].get("previous_matched"))
        ):
            signals.append(
                {
                    "key": canonical_hash(
                        [
                            "evidence_revision",
                            value["source_id"],
                            value["source_record_id"],
                            value["content_fingerprint"],
                        ]
                    ),
                    "kind": "evidence_revision",
                    "title": "依据发生修订：" + value["title"],
                    "reason": "原证据已修订，须重新核对依赖它的判断，旧判断与采用版本仍保留。",
                    "support_units": 1,
                    "qualification": "review_required_not_automatic_reversal",
                    "details": value["change"],
                    "evidence_keys": [evidence_key],
                    "next_questions": ["原判断的哪一条依据已不再成立？"],
                }
            )
    for value in all_values:
        if value.get("include_attention", True) and value["withdrawn"] and value["matched"]:
            evidence_key = _evidence(value)[0]
            signals.append(
                {
                    "key": canonical_hash(
                        ["withdrawn", value["source_id"], value["source_record_id"], value["version_id"]]
                    ),
                    "kind": "evidence_revision",
                    "title": "采用依据已撤回或撤下：" + value["title"],
                    "reason": "当前累计已扣除该来源贡献；这不等于相关科学主张被自动判假。",
                    "support_units": 1,
                    "qualification": "source_withdrawal_review_required",
                    "details": {"withdrawn": True},
                    "evidence_keys": [evidence_key],
                    "next_questions": ["哪些既有判断依赖此版本，是否存在仍有效的独立依据？"],
                }
            )
    if "typed_metric" in definition["profiles"]:
        grouped = defaultdict(list)
        for value in active:
            item = value.get("observation")
            if not item or item.get("kind") != "metric" or item.get("status") != "active":
                continue
            fields = (
                "subject",
                "metric_id",
                "metric_version",
                "unit",
                "population",
                "region",
                "denominator_unit",
                "measurement_basis",
            )
            grouped[canonical_hash([item.get(k) for k in fields])].append((value, item))
        for key, members in sorted(grouped.items()):
            item = members[0][1]
            points = []
            denominators = []
            for value, metric in members:
                end = _time(metric["period_end"])
                amount = metric.get("value") or {}
                numeric_safe = all(abs(Decimal(str(v)).adjusted()) <= 100 for v in amount.values())
                if not numeric_safe:
                    gaps.add("numeric_calculation_limit")
                    qualified = False
                qualified = (
                    numeric_safe
                    and (metric.get("basis") or {}).get("kind") == "source_bound"
                    and metric.get("measurement_basis") == "reported"
                    and end is not None
                    and end[0] <= as_of
                    and metric.get("value") is not None
                    and metric.get("population") is not None
                    and metric.get("region") is not None
                )
                points.append(
                    {
                        "period_start": metric["period_start"],
                        "period_end": metric["period_end"],
                        "lower": amount.get("lower"),
                        "upper": amount.get("upper"),
                        "denominator": metric.get("denominator"),
                        "measurement_basis": metric["measurement_basis"],
                        "evidence_key": _evidence(value)[0],
                        "qualified": qualified,
                    }
                )
                denominators.append(metric.get("denominator"))
            points.sort(key=lambda p: (p["period_start"], p["period_end"], p["evidence_key"]))
            points = [p for p in points if _time(p["period_end"])[0] >= start]
            point_limited = len(points) > MAX_METRIC_POINTS
            if point_limited:
                gaps.add("metric_point_computation_limited")
                points = points[-MAX_METRIC_POINTS:]
            periods = [(_time(p["period_start"])[0], _time(p["period_end"])[0]) for p in points]
            durations = [(right - left).days for left, right in periods]
            no_overlap = all(right <= following[0] for (_, right), following in zip(periods, periods[1:]))
            # Fixed daily/weekly periods must have equal lengths. Only actual calendar-month boundaries
            # justify natural 28/31-day or 90/92-day variation; Q and YTD remain separate.
            month_lengths = [
                (right.year - left.year) * 12 + right.month - left.month
                for left, right in periods
                if left.day == right.day == 1
            ]
            calendar_periods = (
                len(month_lengths) == len(periods)
                and bool(month_lengths)
                and len(set(month_lengths)) == 1
                and month_lengths[0] > 0
            )
            same_period_length = (
                bool(durations) and min(durations) > 0 and (len(set(durations)) == 1 or calendar_periods)
            )

            denominators = [p["denominator"] for p in points]
            denominator_known = [v is not None for v in denominators]
            denominator_stable = not any(denominator_known) or (
                all(denominator_known) and len({canonical_hash(v) for v in denominators}) == 1
            )
            if any(denominator_known) and not denominator_stable:
                gaps.add("metric_denominator_changed_requires_review")
            comparable = (
                not point_limited
                and no_overlap
                and same_period_length
                and denominator_stable
                and all(p["qualified"] for p in points)
            )
            if any(denominator_known) and item.get("denominator_unit") is None:
                comparable = False
            origin = periods[0][0] if periods else as_of
            with localcontext() as arithmetic:
                arithmetic.prec = 256
                mids = (
                    [
                        ((left - origin).days, (Decimal(p["lower"]) + Decimal(p["upper"])) / 2)
                        for p, (left, _) in zip(points, periods)
                    ]
                    if comparable
                    else []
                )
                slope = median_slope(mids)
                slopes_low, slopes_high = [], []
                if comparable:
                    for i, (p, (left, _)) in enumerate(zip(points, periods)):
                        for q, (later, _) in list(zip(points, periods))[i + 1 :]:
                            days = Decimal((later - left).days)
                            slopes_low.append((Decimal(q["lower"]) - Decimal(p["upper"])) / days)
                            slopes_high.append((Decimal(q["upper"]) - Decimal(p["lower"])) / days)
                slope_lower = median(slopes_low) if slopes_low else None
                slope_upper = median(slopes_high) if slopes_high else None
            series = {
                "key": key,
                **{
                    k: item.get(k)
                    for k in ("subject", "metric_id", "metric_version", "unit", "population", "region")
                },
                "denominator_unit": item.get("denominator_unit"),
                "points": points,
                "descriptive_slope": slope,
                "qualification": "source_bound_literal_scope_descriptive_per_day"
                if comparable
                else "unqualified_or_conflicting_periods",
            }
            metric_series.append(series)
            if not comparable:
                gaps.add("metric_basis_or_period_not_comparable")
            if not any(denominator_known):
                gaps.add("metric_denominator_unknown_not_inferred")
            directional = slope_lower is not None and (slope_lower > 0 or slope_upper < 0)
            if (
                comparable
                and len(points) >= 3
                and directional
                and any(v.get("include_attention", True) for v, _ in members)
            ):
                last = points[-1]
                signals.append(
                    {
                        "key": canonical_hash(
                            [
                                "metric",
                                key,
                                last["period_start"],
                                last["period_end"],
                                last["lower"],
                                last["upper"],
                            ]
                        ),
                        "kind": "metric_change",
                        "title": "同口径指标持续变化：" + item["metric_id"],
                        "reason": (
                            "原字段口径相同、期间不重叠且时长可比，区间变化方向一致；"
                            "字面绑定不等于因果或专业确认。"
                        ),
                        "support_units": len(points),
                        "qualification": "literal_scope_descriptive_not_causal",
                        "details": {
                            "descriptive_slope_per_day": slope,
                            "slope_interval_lower": str(slope_lower),
                            "slope_interval_upper": str(slope_upper),
                            "denominator_known": all(denominator_known),
                        },
                        "evidence_keys": [p["evidence_key"] for p in points[-3:]],
                        "next_questions": [
                            "该幅度是否会改变研究问题的决策条件？",
                            "是否存在报告延迟、口径重述或分母变化？",
                        ],
                    }
                )
    if "source_event" in definition["profiles"]:
        for value in active:
            if not value.get("include_attention", True):
                continue
            item = value.get("observation") or _native_event(value)
            if (
                not item
                or item.get("kind", "event") != "event"
                or (item.get("basis") or {}).get("kind") != "source_bound"
            ):
                continue
            when = _time(item.get("event_time"))
            if when is None or when[1] > as_of:
                continue
            signals.append(
                {
                    "key": canonical_hash(
                        [
                            "source_event",
                            value["source_id"],
                            value["source_record_id"],
                            item["event_type"],
                            item["event_time"],
                        ]
                    ),
                    "kind": "source_event",
                    "title": "原始事件待核：" + item["event_type"],
                    "reason": (
                        "FDA原生信函签字日期不证明当时已公开；采用时间另记，专业后果须核对。"
                        if value["source_id"] == "fda_crl"
                        else "有明确来源绑定的原始公告可单独触发核查；公告不等于病例数或暴发开始。"
                    ),
                    "support_units": 1,
                    "qualification": "source_bound_event_context",
                    "details": {"event_type": item["event_type"], "event_time": item["event_time"]},
                    "evidence_keys": [_evidence(value)[0]],
                    "next_questions": ["该事件影响哪些对象和决策条件？"],
                }
            )
    # New source events and invalidated evidence precede old routine milestones in bounded reports.
    priority = {
        "source_event": 0,
        "evidence_revision": 1,
        "state_change": 2,
        "metric_change": 3,
        "text_pattern": 4,
        "milestone_check": 5,
    }
    signals.sort(key=lambda signal: (priority[signal["kind"]], signal["key"]))
    if len(signals) > 200:
        gaps.add("signal_preview_limited")
    if not active:
        gaps.add("no_eligible_observations_not_evidence_of_absence")
    if "typed_metric" in definition["profiles"] and not metric_series:
        gaps.add("typed_metrics_not_supplied")
    if "registered_state" in definition["profiles"] and not states:
        gaps.add("registered_trial_fields_not_supplied")
    if not all_values:
        questions.append("当前来源尚未提供合格资料；请核对采集与发布覆盖。")
    questions += [q for s in signals for q in s["next_questions"]]
    questions = list(dict.fromkeys(questions))[:24]
    seen_sites = sorted({v["source_site"] for v in all_values if v["matched"]})
    missing = sorted(set(definition["source_sites"]) - set(seen_sites))
    coverage = {
        "scope": "committed_adoptions",
        "seed_complete": seed_complete,
        "source_watermarks": source_watermarks,
        "source_errors": source_errors,
        "observed_source_sites": seen_sites,
        "missing_source_sites": missing,
        "matched_records": sum(v["matched"] for v in all_values),
        "active_records": len(active),
        "withdrawn_records": sum(v["matched"] and v["withdrawn"] for v in all_values),
        "undated_records": undated,
        "limited_feature_records": sum(v["features_limited"] for v in active),
        "evaluated_records": len(all_values),
        "omitted_records": omitted_records,
        "original_source_coverage_known": False,
        "limitations": [
            "仅统计已采用版本，不代表源站或领域全量。",
            "发布心跳不能证明来源近期已检查。",
            "旧桶按本次已知资料重建，不冒充当年的系统预测。",
        ],
    }
    state = "descriptive" if active else "insufficient_data"
    summary = f"已采用范围内有{len(active)}条当前有效记录、{len(signals)}项待核变化；缺少资料的领域保持未知。"
    draft = "\n".join(
        [
            definition["title"],
            "研究问题：" + definition["question"],
            summary,
            "范围：已提交采用版本；不代表全领域覆盖。",
            *["待核：" + s["title"] + "；" + s["reason"] for s in signals[:12]],
            *["下一步：" + q for q in questions[:8]],
            "此为程序整理草稿，不是研究人员判断、专业评价或正式发布。",
        ]
    )
    # Full references live once in the contribution ledger; the report carries a bounded readable preview.
    reference_order = {}
    for signal in signals[:200]:
        for key in signal["evidence_keys"]:
            reference_order.setdefault(key, len(reference_order))
    for registered in states[:100]:
        reference_order.setdefault(registered["evidence_key"], len(reference_order))
    ordered = sorted(evidence, key=lambda k: (reference_order.get(k, len(reference_order)), k))
    return {
        "state": state,
        "summary": summary,
        "buckets": buckets,
        "signals": signals[:200],
        "registered_states": sorted(states, key=lambda s: s["unit_id"])[:100],
        "metric_series": metric_series[:100],
        "coverage": coverage,
        "evidence": [evidence[k] for k in ordered[:MAX_EVIDENCE_PREVIEW]],
        "evidence_truncated": len(evidence) > MAX_EVIDENCE_PREVIEW,
        "data_gaps": sorted(gaps),
        "next_questions": questions,
        "draft": draft,
    }
