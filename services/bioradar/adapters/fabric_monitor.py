"""Read committed version pages and map native, source-bound fields to existing mining input."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from bioradar.adapters.fabric_history import fact_time, history_error, time_upper, validate_record
from bioradar.research.mining.models import (
    Event,
    MiningEntity,
    NormalizedSnapshot,
    SectionProvenance,
    SnapshotCoverage,
    StateAssertion,
    TimeExpression,
    canonical_hash,
)

PROJECTION_VERSION = "bioradar-monitor-projection-v0.1"


def change_page(reader, *, source_id, is_demo, after, cursor=None, limit=50, minimum_after=None):
    params = {"source_id": source_id, "is_demo": str(is_demo).lower(), "after": after, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    page = reader._get("/api/history/changes", params)
    try:
        if (
            page["schema_version"] != "datafabric.history.changes.v1"
            or page["scope"] != "committed_adoptions_not_source_change_feed"
            or not isinstance(page["items"], list)
            or len(page["items"]) > limit
            or type(page["max_seq"]) is not int
            or page["max_seq"] < after
            or type(page["next_after"]) is not int
            or not after <= page["next_after"] <= page["max_seq"]
            or page["next_cursor"] is not None
            and not isinstance(page["next_cursor"], str)
        ):
            raise ValueError("invalid change envelope")
        now = datetime.now(UTC)
        last = max(after, minimum_after or after)
        for item in page["items"]:
            validate_record(item, source_id, is_demo, "current", now)
            seq = item["published_seq"]
            if type(seq) is not int or not last < seq <= page["max_seq"]:
                raise ValueError("unordered committed versions")
            last = seq
        if page["next_after"] < last:
            raise ValueError("change page moved backwards")
        if page["next_cursor"] and (not page["items"] or page["next_after"] != last):
            raise ValueError("cursor without progress")
        if not page["next_cursor"] and page["next_after"] != page["max_seq"]:
            raise ValueError("unacknowledged tail")
        heads = page["current_version_ids"]
        natives = {item["source_record_id"] for item in page["items"]}
        if (
            not isinstance(heads, dict)
            or set(heads) != natives
            or any(
                value is not None and (not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value))
                for value in heads.values()
            )
        ):
            raise ValueError("missing fixed-watermark current versions")
        for native, version in heads.items():
            if version is None and any(
                item["facts"].get("publication_role") != "context_reference"
                for item in page["items"]
                if item["source_record_id"] == native
            ):
                raise ValueError("a source-record change cannot lose its head")
    except (KeyError, TypeError, ValueError):
        raise history_error() from None
    return page


def source_key(record):
    return canonical_hash([record.source_id, record.record_ref.object_id])


def expression(value):
    if not value:
        return TimeExpression()
    return TimeExpression(
        raw=value["value"],
        precision=value["precision"],
        instant=time_upper(value) if value["precision"] == "instant" else None,
    )


def project_record(reader, record, *, as_of, bootstrap=False, kdd=False):
    """Native registration identity is not a drug/program or independent-study claim."""
    collection = {
        "items": [record],
        "source_id": record["source_id"],
        "is_demo": record["is_demo"],
        "coverage": {},
    }
    base = reader.snapshot(collection)
    if base is None:
        return None
    result = base.records[0]
    facts = record["facts"]
    updated = record.get("source_updated_at")
    recent_revision = updated and time_upper(updated) and time_upper(updated) >= as_of - timedelta(days=30)
    flags = set()
    if bootstrap or not recent_revision:
        flags.add("historical_backfill")
    if bootstrap:
        flags.add("new_source")
    if facts.get("publication_state") == "withdrawn":
        flags.add("deleted")
    types = facts.get("publication_types") or []
    if isinstance(types, list) and any(str(t).casefold() == "retracted publication" for t in types):
        flags.add("retracted")
    clean = record["clean_text"]
    for field in ("summary", "brief_summary", "abstract"):
        if (
            not kdd
            and record["source_id"] != "who_don"
            and isinstance(facts.get(field), str)
            and facts[field].strip()
        ):
            clean = facts[field]
            break
    provenance = SectionProvenance(
        schema_version=PROJECTION_VERSION,
        provider_ref=reader.ref(record),
        available_at=time_upper(record["received_at"]),
    )
    entities, assertions, events = [], [], []
    trial_id = facts.get("trial_id")
    if record["record_kind"] == "trial" and isinstance(trial_id, str) and re.fullmatch(r"NCT\d{8}", trial_id):
        native = reader.ref(record).model_copy(
            update={
                "provider": "clinicaltrials.gov",
                "object_type": "registered_trial",
                "object_id": trial_id,
            }
        )
        entities.append(
            MiningEntity(
                canonical_entity_ref=native,
                temporary_entity_ref=native,
                entity_type="registered_trial",
                mapping_ref=reader.ref(record),
                mapping_version=PROJECTION_VERSION,
                mapping_status="confirmed",
                mapping_available_at=time_upper(record["received_at"]),
            )
        )
        # lastUpdate is a public source revision, not the clinical event's effective date.
        when = expression(updated)
        for field, value in (
            ("registration_status", facts.get("status")),
            ("registered_phase", facts.get("phase")),
        ):
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                value = " / ".join(sorted(value))
            if isinstance(value, str) and value:
                assertions.append(
                    StateAssertion(
                        assertion_ref=reader.ref(record).model_copy(
                            update={
                                "object_type": "registration_field",
                                "object_id": json.dumps(
                                    [record["source_id"], record["source_record_id"], trial_id, field]
                                ),
                            }
                        ),
                        entity_ref=native,
                        property=field,
                        value=value,
                        effective_time=when,
                        source_ref=reader.ref(record),
                    )
                )
    project_ref = None
    project_stage = None
    review = record.get("mapping_review")
    if record["record_kind"] == "program" and review:
        keys = ("program_id", "lineage_id", "indication", "jurisdiction", "mapping_available_at")
        mapped = fact_time(facts.get("mapping_available_at"))
        reviewed = time_upper(review.get("available_at"))
        if all(facts.get(k) for k in keys) and mapped and reviewed and max(mapped, reviewed) <= as_of:
            project_ref = reader.ref(record).model_copy(
                update={
                    "object_type": "reviewed_program",
                    "object_id": json.dumps(
                        [facts["program_id"], facts["indication"], facts["jurisdiction"]]
                    ),
                }
            )
            entities.append(
                MiningEntity(
                    canonical_entity_ref=project_ref,
                    temporary_entity_ref=project_ref,
                    entity_type="program",
                    mapping_ref=reader.ref(record),
                    mapping_version=PROJECTION_VERSION,
                    mapping_status="confirmed",
                    mapping_available_at=max(mapped, reviewed),
                )
            )
            stage = facts.get("stage", facts.get("phase"))
            project_stage = " / ".join(stage) if isinstance(stage, list) else stage
    if record["source_id"] == "fda_crl" and facts.get("letter_type"):
        events.append(
            Event(
                event_ref=reader.ref(record).model_copy(update={"object_type": "regulatory_letter"}),
                event_type="regulatory_response",
                effective_time=TimeExpression(
                    raw=facts.get("letter_date"),
                    precision="day"
                    if isinstance(facts.get("letter_date"), str) and len(facts["letter_date"]) == 10
                    else "unknown",
                ),
                evidence_refs=(reader.ref(record),),
                source_status="supported",
                provenance=provenance,
            )
        )
    projected = result.model_copy(
        update={
            "ingestion_mode": "fixture"
            if record["is_demo"]
            else ("historical_backfill" if "historical_backfill" in flags else "live_delta"),
            "record_flags": tuple(sorted(flags)),
            "canonical_entity_refs": tuple(entities),
            "state_assertions": tuple(assertions),
            "project_ref": project_ref,
            "project_stage": project_stage,
            "events": tuple(events),
            "normalized_text": None if "deleted" in flags else clean or None,
            "normalized_text_hash": None
            if "deleted" in flags or not clean
            else hashlib.sha256(clean.encode()).hexdigest(),
            "text_status": "unavailable" if "deleted" in flags or not clean else "inline_authorized",
            "upstream_status": {
                "monitor_projection": PROJECTION_VERSION,
                "source_revision_time_is_not_clinical_event_time": "true",
            },
        }
    )
    from bioradar.adapters.fabric_kdd import enrich_record

    return enrich_record(reader, record, projected, as_of=as_of)


def build_snapshot(records, *, is_demo, as_of, omitted=0):
    # Selection is bounded before this function; keep old/new versions for actual revisions.
    by_ref = {tuple(r.record_ref.version_key): r for r in records}
    records = tuple(by_ref[key] for key in sorted(by_ref))
    if not records:
        return None
    instance = records[0].source_instance
    counts = {}
    for record in records:
        counts[record.source_id] = counts.get(record.source_id, 0) + 1
    return NormalizedSnapshot(
        snapshot_id="monitor-" + canonical_hash([r.record_ref.model_dump(mode="json") for r in records])[:24],
        source_instance=instance,
        is_demo=is_demo,
        observed_at=as_of,
        coverage_mode="bounded_sample",
        records=records,
        coverage=tuple(
            SnapshotCoverage(
                source_instance=instance,
                source_id=source,
                mode="bounded_sample",
                scope_description="持续采用日志的有界跨源计算窗口，不是原站全库或独立研究分母。",
                record_count=count,
                reasons=("bounded_monitor_window", "source_independence_unresolved"),
            )
            for source, count in sorted(counts.items())
        ),
        metadata_gaps=("monitor_scope_is_not_source_coverage", "omitted_window_records:" + str(omitted)),
    )


def followup_plan(records, candidate, *, limit=2):
    """Literal source identifiers and quoted phrases; no entity alignment by names."""
    plans, seen = [], set()
    supporting = {tuple(s.record_ref.version_key) for s in candidate.supports}
    for record in records:
        if tuple(record["ref_key"]) not in supporting:
            continue
        for field in ("trial_id", "application_numbers", "doi", "pmid", "interventions"):
            value = record["facts"].get(field)
            values = value if isinstance(value, list) else [value]
            for index, literal in enumerate(values):
                if isinstance(literal, dict):
                    literal = literal.get("name")
                if isinstance(literal, str) and 2 <= len(literal) <= 120 and literal.casefold() not in seen:
                    seen.add(literal.casefold())
                    plans.append(
                        {
                            "term": literal,
                            "field": field,
                            "index": index,
                            "locator": record["field_locators"].get(field),
                            "source_id": record["source_id"],
                            "source_record_id": record["source_record_id"],
                            "version_id": record["version_id"],
                        }
                    )
    phrase = candidate.metrics.get("phrase")
    if isinstance(phrase, str) and 2 <= len(phrase) <= 120:
        quote = next(
            (s for s in candidate.supports if s.quote and phrase.casefold() in s.quote.casefold()), None
        )
        if quote:
            origin = next(
                (r for r in records if tuple(r["ref_key"]) == tuple(quote.record_ref.version_key)), None
            )
            if origin is None:
                return plans[:limit]
            plans.append(
                {
                    "term": phrase,
                    "field": "candidate_quote",
                    "source_id": origin["source_id"],
                    "record_ref": quote.record_ref.model_dump(mode="json"),
                    "quote": quote.quote,
                    "normalized_start": quote.normalized_start,
                }
            )
    return plans[:limit]


def search_records(reader, *, source_id, is_demo, term):
    """One bounded page, not a collect-then-discard full-corpus search."""
    now = datetime.now(UTC)
    page = reader._get(
        "/api/history/records",
        {
            "source_id": source_id,
            "is_demo": str(is_demo).lower(),
            "mode": "current",
            "cutoff": now.isoformat(),
            "terms": term,
            "limit": 3,
        },
    )
    if (
        not isinstance(page, dict)
        or page.get("schema_version") != "datafabric.history.collection.v1"
        or not isinstance(page.get("items"), list)
        or len(page["items"]) > 3
    ):
        raise history_error()
    for record in page["items"]:
        validate_record(record, source_id, is_demo, "current", now)
    coverage = dict(page.get("coverage") or {})
    coverage["complete"] = False
    coverage["followup_limit"] = 3
    coverage["has_more"] = bool(page.get("next_cursor"))
    return {**page, "source_id": source_id, "is_demo": is_demo, "coverage": coverage}


def search_analysis_records(
    reader,
    *,
    source_id,
    is_demo,
    terms,
    cutoff,
    limit=3,
    mode="current",
    run_id=None,
    catalog_discovery=False,
):
    """Read one authorized source page; catalog discovery is an explicit bounded fallback."""
    from bioradar.api.errors import DomainError

    if (
        not isinstance(source_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", source_id)
        or type(is_demo) is not bool
        or type(limit) is not int
        or not 1 <= limit <= 20
        or type(catalog_discovery) is not bool
        or not isinstance(terms, (list, tuple))
        or (len(terms) != 0 if catalog_discovery else not 1 <= len(terms) <= 3)
        or any(not isinstance(term, str) or not 2 <= len(term.strip()) <= 120 for term in terms)
        or not isinstance(cutoff, datetime)
        or cutoff.tzinfo is None
        or cutoff.utcoffset() is None
        or mode not in {"current", "operational_replay", "public_history_reconstruction"}
    ):
        raise DomainError("invalid_analysis_query", "请提供受限来源、明确检索词和时点", 422)
    terms = sorted(set(term.strip() for term in terms))
    params = [
        ("source_id", source_id),
        ("is_demo", str(is_demo).lower()),
        ("mode", mode),
        ("cutoff", cutoff.isoformat()),
        ("limit", limit),
        *(("terms", term) for term in terms),
    ]
    if run_id is not None:
        params.append(("run_id", run_id))
    page = reader._get("/api/history/records", params)
    if (
        not isinstance(page, dict)
        or page.get("schema_version") != "datafabric.history.collection.v1"
        or not isinstance(page.get("items"), list)
        or len(page["items"]) > limit
        or not isinstance(page.get("coverage"), dict)
        or page.get("next_cursor") is not None
        and not isinstance(page["next_cursor"], str)
    ):
        raise history_error()
    identities = set()
    for record in page["items"]:
        validate_record(record, source_id, is_demo, mode, cutoff)
        identity = (record["source_record_id"], record["version_id"])
        if identity in identities:
            raise history_error()
        identities.add(identity)
    coverage = {
        **page["coverage"],
        "complete": False,
        "followup_limit": limit,
        "has_more": bool(page.get("next_cursor")),
        "search_scope": (
            "bounded_original_catalog_not_keyword_match"
            if catalog_discovery
            else "adopted_title_or_clean_text_literal_and_not_the_web"
        ),
    }
    return {**page, "source_id": source_id, "is_demo": is_demo, "coverage": coverage}
