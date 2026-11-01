"""Retain attributed public-health material without numeric or truth promotion."""

import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from bioradar.research.mining.models import TimeExpression, canonical_hash
from bioradar.research.mining.prepare import publication_interval, section_visible, time_interval
from bioradar.research.mining.signals import checkable_claims

SOURCE_INTELLIGENCE_VERSION = "public-health-source-intelligence-v1"
HEALTH = re.compile(
    r"不明(?:原因|病原)|肺炎|病例|疫情|医护.{0,4}感染|聚集性?(?:病例|感染)|"
    r"医疗(?:提醒|建议|预警)|健康(?:提醒|建议|警示)|就医|就诊|分诊|发热门诊|"
    r"传染病|感染(?:防控|预防)|佩戴口罩|接种疫苗|"
    r"pneumonia|outbreak|cluster of cases|public[ -]health|medical advis(?:ory|ories)|"
    r"health advis(?:ory|ories)|seek medical (?:care|attention)|infection prevention|patient triage",
    re.I,
)
RELATIONS = {"corrects", "retracts", "supplements", "reposts"}
EVENTS = {"reported_deletion", "official_denial", "moderation", "access_failure"}


def _dump(value):
    return value.model_dump(mode="json")


def _body_ref(record):
    return {
        "record_ref": _dump(record.record_ref),
        "text_hash": record.normalized_text_hash,
        "normalization_version": record.normalization_version,
        "text_status": record.text_status,
    }


def _member(record):
    interval = publication_interval(record)
    return {
        "record_ref": _dump(record.record_ref),
        "document_hash": record.document_hash,
        "text_hash": record.normalized_text_hash,
        "source_instance": record.source_instance,
        "source_id": record.source_id,
        "normalization_version": record.normalization_version,
        "source_family": record.source_family,
        "source_independence_status": record.source_independence_status,
        "study_independence_status": record.study_independence_status,
        "available_at": record.available_at.isoformat() if record.available_at else None,
        "observed_at": record.observed_at.isoformat(),
        "public_available_at": record.public_available_at.isoformat() if record.public_available_at else None,
        "publication_interval": [value.isoformat() for value in interval] if interval else None,
    }


def _text(record):
    return record.normalized_text or "" if record.text_status == "inline_authorized" else ""


def _quote(record, start, end):
    return {
        "quote": _text(record)[start:end],
        "start": start,
        "end": end,
        "offset_basis": "fixed_normalized_unicode",
        "text_hash": record.normalized_text_hash,
    }


def _native_key(record):
    try:
        identity = json.loads(record.record_ref.object_id)
    except (TypeError, ValueError):
        identity = None
    native = (
        identity[1]
        if isinstance(identity, list)
        and len(identity) == 3
        and identity[0] == record.source_id
        and isinstance(identity[1], str)
        and type(identity[2]) is bool
        else record.record_ref.object_id
    )
    return (
        record.source_instance,
        record.record_ref.provider,
        record.source_id,
        native,
        record.record_ref.adopted_version,
    )


def _related(record, known, native, policy):
    context = record.signal_context
    raw = (context.document_evidence or {}).get("document_relations", []) if context else []
    if not raw:
        if context and policy.time_mode != "availability_replay":
            for relation in context.source_relations:
                target = known.get(canonical_hash(_dump(relation.target_ref)))
                if target and target.record.source_instance == record.source_instance:
                    yield relation.kind, target
        return
    for relation in raw:
        if not isinstance(relation, dict) or relation.get("relation_type") not in RELATIONS:
            continue
        try:
            _, status = _locator(record, relation.get("basis_locator"))
        except ValueError:
            continue
        if policy.time_mode == "availability_replay" and status != "fixed_text_quote_verified":
            continue
        key = (
            record.source_instance,
            record.record_ref.provider,
            relation.get("target_source_id"),
            relation.get("target_source_record_id"),
            relation.get("target_version_id"),
        )
        if not all(isinstance(part, str) and part for part in key):
            continue
        targets = native.get(key, [])
        if len(targets) == 1:
            yield relation["relation_type"], targets[0]


def public_health_applicability(prepared):
    """Visible content/typed sections establish relevance; exact links may inherit it."""
    items = sorted(prepared.history, key=lambda item: canonical_hash(_dump(item.record.record_ref)))
    known = {canonical_hash(_dump(item.record.record_ref)): item for item in items}
    native = {}
    for item in items:
        native.setdefault(_native_key(item.record), []).append(item)
    result = {}
    for item in items:
        record = item.record
        basis = [
            {"kind": "visible_medical_semantics", "locator": _quote(record, match.start(), match.end())}
            for match in list(HEALTH.finditer(_text(record)))[:8]
        ]
        sections = [
            ("typed_public_health_metric", metric.observation_id, metric.provenance)
            for metric in record.metric_observations
            if metric.domain == "public_health"
        ] + [
            ("typed_regional_indicator", indicator.indicator, indicator.provenance)
            for indicator in record.regional_indicators
        ]
        if record.source_id == "who_don":
            basis.append({"kind": "native_who_notice", "record_ref": _dump(record.record_ref)})
        basis.extend(
            {"kind": kind, "section_id": identifier, "provenance": _dump(provenance)}
            for kind, identifier, provenance in sections
            if section_visible(provenance, record, prepared.policy)
        )
        if basis:
            result[record.record_ref.version_key] = {
                "record_ref": _dump(record.record_ref),
                "reason": "visible_public_health_content_or_typed_section",
                "basis": basis,
            }
    changed = True
    while changed:
        changed = False
        for item in items:
            record, context = item.record, item.record.signal_context
            if record.record_ref.version_key in result or context is None:
                continue
            basis = []
            for kind, target in _related(record, known, native, prepared.policy):
                if target.record.record_ref.version_key in result:
                    basis.append(
                        {
                            "kind": "exact_visible_target",
                            "relation_type": kind,
                            "target_ref": _dump(target.record.record_ref),
                        }
                    )
            if basis:
                result[record.record_ref.version_key] = {
                    "record_ref": _dump(record.record_ref),
                    "reason": "relation_to_same_snapshot_visible_public_health_material",
                    "basis": basis,
                }
                changed = True
    return result


def _upper(value):
    if isinstance(value, str):
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant.astimezone(UTC) if instant.tzinfo else None
    if isinstance(value, dict):
        precision = value.get("precision")
        if precision == "instant":
            return _upper(value.get("value"))
        interval = time_interval(TimeExpression(raw=value.get("value"), precision=precision))
        return interval[1] - timedelta(microseconds=1) if interval else None
    return None


def _locator(record, value):
    if not isinstance(value, dict):
        return None, "unavailable"
    locator = deepcopy(value)
    start, end, quote = (locator.get(key) for key in ("start", "end", "quote"))
    if start is not None or end is not None:
        if (
            type(start) is not int
            or type(end) is not int
            or not isinstance(quote, str)
            or not 0 <= start < end <= len(_text(record))
            or _text(record)[start:end] != quote
        ):
            raise ValueError("invalid_source_quote")
        return locator | _quote(record, start, end), "fixed_text_quote_verified"
    if quote is not None:
        if not isinstance(quote, str) or not quote or _text(record).count(quote) != 1:
            raise ValueError("invalid_source_quote")
        start = _text(record).index(quote)
        return locator | _quote(record, start, start + len(quote)), "fixed_text_quote_verified"
    if (
        isinstance(locator.get("json_pointer"), str)
        and locator["json_pointer"].startswith("/")
        or isinstance(locator.get("xpath"), str)
        and bool(locator["xpath"])
    ):
        # The source adapter checked the original; this helper has no raw original
        # and must not claim it re-resolved a pointer or a source XML path.
        return locator, "source_locator_not_rechecked"
    raise ValueError("invalid_source_locator")


def _event(record, value, policy, *, relation=False):
    event = deepcopy(value)
    kind = event.get("relation_type") if relation else event.get("event_type")
    if kind not in (RELATIONS if relation else EVENTS):
        raise ValueError("unsupported_source_event_type")
    locator, basis_status = _locator(record, event.get("basis_locator"))
    if not relation and locator is None:
        raise ValueError("information_event_basis_required")
    times = {}
    for name in ("reported_at", "detected_at"):
        if event.get(name) is not None:
            try:
                upper = _upper(event[name])
            except (TypeError, ValueError, OverflowError):
                upper = None
            if upper is None:
                raise ValueError("source_event_time_invalid")
            if upper > policy.as_of:
                raise ValueError("source_event_after_cutoff")
            times[name] = upper
    if policy.time_mode == "availability_replay" and basis_status != "fixed_text_quote_verified":
        if relation or "detected_at" not in times:
            raise ValueError("source_event_visibility_unproven_for_replay")
    if locator is not None:
        event["basis_locator"] = locator
    event["event_type"] = kind
    temporal = "detected_by_cutoff" if "detected_at" in times else "event_time_unknown"
    if "reported_at" in times and "detected_at" not in times:
        temporal = "source_reported_time_not_availability_proof"
    return event, basis_status, temporal


def source_units(item, applicability, policy):
    """Retained units are source statements/context, never cases, rates or risk decisions."""
    record, context = item.record, item.record.signal_context
    if not applicability or applicability.get("record_ref") != _dump(record.record_ref):
        return []
    metadata = context.document_evidence or {} if context else {}
    body = _body_ref(record)
    dimensions = {
        "direction": "public_health",
        "material_type": context.material_type if context else None,
        "statement_status": context.statement_status if context else "unknown",
        "source_state": list(record.record_flags),
        "evidence_status": "unverified",
        "applicability": deepcopy(applicability),
    }

    def unit(kind, details, identity):
        qualification = (
            "attributed_material_not_confirmed_health_event_or_numeric_observation"
            if kind == "source_statement"
            else "attributed_information_context_not_proof_of_health_claim_or_risk"
        )
        return {
            "unit_id": canonical_hash([SOURCE_INTELLIGENCE_VERSION, record.source_instance, body, identity]),
            "unit_kind": kind,
            "dimensions": deepcopy(dimensions),
            "condition_key": None,
            "independent_study": False,
            "members": [_member(record)],
            "features": {"qualification": qualification, "window": item.window, "body_ref": body, **details},
        }

    events, gaps = [], []
    values = [(False, value) for value in metadata.get("information_events", [])] + [
        (True, value) for value in metadata.get("document_relations", [])
    ]
    for index, (relation, value) in enumerate(values):
        try:
            if not isinstance(value, dict):
                raise ValueError("source_event_not_an_object")
            events.append(_event(record, value, policy, relation=relation))
        except ValueError as exc:
            gaps.append({"index": index, "reason": str(exc)})
    if not metadata.get("document_relations") and context:
        for relation in context.source_relations:
            if policy.time_mode == "availability_replay":
                gaps.append(
                    {"index": len(values), "reason": "source_relation_visibility_unproven_for_replay"}
                )
                continue
            event = {"event_type": relation.kind, "target_ref": _dump(relation.target_ref)}
            events.append((event, "unavailable", "event_time_unknown"))
    text = _text(record)
    anchor = HEALTH.search(text)
    start = max(0, anchor.start() - 160) if anchor else 0
    end = min(len(text), start + 1200)
    claims = checkable_claims(text, context.entity_names if context else ())
    statement = unit(
        "source_statement",
        {
            "excerpt": _quote(record, start, end) if text else None,
            "omitted_text_character_count": len(text) - (end - start)
            if record.text_status == "inline_authorized"
            else None,
            "excerpt_scope": "fixed_normalized_projection_not_full_original_read",
            "claims": claims[:16],
            "omitted_claim_count": max(0, len(claims) - 16),
            "source_context": {
                "attribution": context.attribution if context else "unknown",
                "claimed_origin": context.claimed_origin if context else None,
                "text_level": metadata.get("text_level"),
                "verification_status": context.verification_status if context else "unknown",
                "source_independence": deepcopy(metadata.get("source_independence") or {"status": "unknown"}),
            },
            "information_event_gaps": gaps,
        },
        "source_statement",
    )
    retained = [
        unit(
            "source_information_event",
            {
                "event": event,
                "basis_status": basis,
                "temporal_status": temporal,
            },
            ["information_event", event],
        )
        for event, basis, temporal in events
    ]
    unique = {event["unit_id"]: event for event in retained}
    return [statement, *[unique[key] for key in sorted(unique)]]
