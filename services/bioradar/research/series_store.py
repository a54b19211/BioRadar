"""Series storage: a checked point stored and compared with its series as of the record (adopt), an
upgraded artifact's point taking an old point's place (hand_over), and the corrections and retractions
records state (record_corrections). The series arithmetic is series.py, the comparable domain
domain.py."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from bioradar import db
from bioradar.config import Settings
from bioradar.research import domain, measure_checks, series
from bioradar.research.clock import parse_time
from bioradar.research.topics import event, owe


def period_bounds(period, at):
    """(start, end) of a checked period: a month range spans whole months; otherwise series.period_bounds."""
    if period.get("start") and period["precision"] == "month":
        (y1, m1), (y2, m2) = (map(int, period["start"].split("-")), map(int, period["end"].split("-")))
        return datetime(y1, m1, 1, tzinfo=UTC), series._month_end(y2, m2)
    text = period.get("value") or f"{period['start']}/{period['end']}"
    return series.period_bounds(text, at)


RECORD_CONFLICT = "record_internal_conflict"


def adopt(session, s: Settings, cand, final, check_id):
    """Store one checked point (series logic over the effective view) and compare it with its series as
    visible at the record's time. A correction of an earlier record (of the named version, when Fabric names
    one) replaces that record's value for the same series and period. A series is one key in one comparable
    domain: the topic the record was placed in. A point of unknown domain (no topic) is stored and
    effective but nothing is computed from it, and no topic is opened from it - a shared object/metric name is
    not a business identity. Writes a `series` topic event per change.
    Returns (point or None, topic ids owed)."""
    at = measure_checks._utc(cand.observed_at)
    period = final["period"]
    period_text = period.get("value") or f"{period['start']}/{period['end']}"
    bounds = period_bounds(period, at)
    if bounds is None:
        return None, []
    # the comparable domain: the topic the record was placed in; without one the domain is unknown
    domain_basis, domain_id = ("topic", cand.topic_id) if cand.topic_id else ("unknown", None)
    # and the complete domain (r2): family and required dimensions, proven or not, from this point's check
    gate = domain.series_gate(
        cand.measure, final, session.get(db.MeasurementCheck, check_id) if check_id else None
    )
    key = dict(
        object=final["object"][:200],
        metric=final["metric"][:200],
        unit=final["unit"][:50],
        kind=final["measure_kind"],
        scope=final["scope"][:200],
        scope_mode=final["scope_mode"],
        period_type=period["type"],
        period_precision=period["precision"],
        domain_basis=domain_basis,
        domain_id=domain_id,
        family=gate["family"][:30],
        dims_key=gate["dims_key"],
    )
    same = dict(key, workspace_id=s.workspace_id, is_demo=cand.is_demo)
    domainless = ("domain_basis", "domain_id", "family", "dims_key")  # for a correction: the fact, any group
    unplaced = (
        "domain_basis",
        "domain_id",
    )  # a retried triage: the same point in the same group
    existing = session.scalar(select(db.Measurement).filter_by(candidate_id=cand.id))
    if existing is not None:
        # checked again (recovery, checker upgrade): the point stands and now cites its current check; its
        # stored
        # values are compared with this check's final values again; nothing new to compute for the series
        existing.check_id = check_id
        # as this check proves them: family, group key, dimensions and gaps move together
        existing.family, existing.dims_key = key["family"], key["dims_key"]
        kept = [RECORD_CONFLICT] if RECORD_CONFLICT in (existing.dims_reasons or []) else []
        existing.dims, existing.dims_reasons = gate["dims"], gate["reasons"] + kept  # a contradiction stays
        return existing, []
    art = db.artifact_key(cand.processor_version, cand.text_sha256)
    identity = {k: v for k, v in key.items() if k not in unplaced}
    # one artifact's reading of the version: another artifact of the same raw version is not the record
    # stating a second value - an upgrade's point takes the old one's place instead (hand_over)
    stated = list(
        session.scalars(
            select(db.Measurement).filter_by(
                version_id=cand.version_id, period=period_text, artifact_key=art, **identity
            )
        )
    )
    if any(r.value_text == final["value"] for r in stated):
        return None, []  # the same point of this version from another candidate (a retried triage)
    # the same record version states this identity with another value: a contradiction inside the record -
    # both values kept, neither compared; never "the first one wins"
    contradicted = stated
    m = cand.measure
    row = db.Measurement(
        **same,
        value=float(Decimal(final["value"])),
        value_text=final["value"],
        numerator=None,  # contract v2 checks no numerator: never stored with a checked point
        denominator=float(Decimal(final["denominator"])) if final["denominator"] else None,
        denominator_text=final["denominator"],
        period=period_text,
        period_start=bounds[0],
        period_end=bounds[1],
        status=m.get("status") or "other",
        source=cand.source,
        record_id=cand.record_id,
        version_id=cand.version_id,
        quote=m["quote"][:300],
        available_at=at,
        topic_id=cand.topic_id,
        candidate_id=cand.id,
        check_id=check_id,
        dims=gate["dims"],
        dims_reasons=gate["reasons"],
        artifact_key=art,
    )
    session.add(row)
    session.flush()
    hand_over(session, row, identity, relocated_from=cand.relocated_from)
    if contradicted:
        for r in contradicted + [row]:
            if RECORD_CONFLICT not in (r.dims_reasons or []):
                r.dims_reasons = list(r.dims_reasons or []) + [RECORD_CONFLICT]
        holder = session.get(db.Topic, domain_id) if domain_basis == "topic" else None
        if holder is not None:
            event(
                session,
                holder,
                at,
                "record_conflict",
                record=cand.record_id,
                version=cand.version_id,
                period=row.period,
                points=[
                    {"id": r.id, "value": r.value_text, "candidate": r.candidate_id, "quote": r.quote}
                    for r in contradicted + [row]
                ],
            )
            owe(holder, "record_conflict", at)
    # a correction Fabric states is about the fact, not a comparison: it withdraws the corrected point in
    # whatever domain that point is; only the comparison stays within one domain
    undomained = {k: v for k, v in key.items() if k not in domainless}
    crossed = []
    for source, record_id, version_id in cand.corrects or []:
        query = measure_checks.effective(
            session, s, cand.is_demo, source=source, record_id=record_id, period=row.period, **undomained
        )
        if version_id:
            query = query.filter_by(version_id=version_id)
        for old in session.scalars(query):
            # a replacement already in force keeps its date: a second notice correcting the same value
            # is not a correction of the first one, so the two stand as a conflict from now on
            if old.superseded_by is None and old.id != row.id:
                old.superseded_by = row.id
                if (old.domain_basis, old.domain_id) != (domain_basis, domain_id):
                    crossed.append(old)
    owed = []
    for topic_id in sorted({o.domain_id for o in crossed if o.domain_basis == "topic"}):
        # the old point's topic loses it to a record placed elsewhere: whether the new point may stand in for
        # it there is not decided by the program - the topic is routed with the reason
        old_topic = session.get(db.Topic, topic_id)
        if old_topic is None:
            continue
        event(
            session,
            old_topic,
            at,
            "correction",
            record=cand.record_id,
            by_source=cand.source,
            by_version_id=cand.version_id,
            reason="cross_domain_correction",
            withdrawn=[o.id for o in crossed if o.domain_id == topic_id],
            by=row.id,
            by_domain=[domain_basis, domain_id],
        )
        owe(old_topic, "cross_domain_correction", at)
        owed.append(old_topic.id)
    if domain_basis == "unknown":
        # stored and effective, but not comparable: no difference, trend or conflict is computed from a point
        # whose domain is not known, and no topic is opened from it
        return row, owed
    if row.dims_reasons:
        # not a complete, proven domain (family or a required dimension unknown or unchecked), or a value the
        # same record contradicts: stored and effective, a lead with its gaps - nothing computed
        return row, owed
    grain = series.grain(row.period)
    rows = [
        {
            "id": r.id,
            "value": r.value,
            "status": r.status,
            "period": r.period,
            "period_start": r.period_start,
            "period_end": r.period_end,
            "available_at": r.available_at,
            "superseded_by": r.superseded_by,
        }
        for r in session.scalars(
            measure_checks.effective(session, s, cand.is_demo, **key).where(db.Measurement.available_at <= at)
        )
        if series.grain(r.period) == grain  # a year is not compared with a quarter
        and not r.dims_reasons  # only proven points: a gap in its domain makes no baseline
    ]
    # a point whose record a stated correction withdrew by then leaves the series - whether or not a
    # replacement passed checking, and whatever domain it went to; a replacement inside this
    # domain is left to series.points, which also reports what it revised
    inside = {r["id"] for r in rows}
    out = measure_checks.withdrawn(session, [r["id"] for r in rows if r["id"] != row.id], at)
    outside = {r["superseded_by"] for r in rows if r["superseded_by"] and r["superseded_by"] not in inside}
    if outside:
        out |= {
            r["id"]
            for r in rows
            if r["superseded_by"] in outside
            and measure_checks._utc(session.get(db.Measurement, r["superseded_by"]).available_at) <= at
        }
    rows = [r for r in rows if r["id"] not in out or r["superseded_by"] in inside]
    if row.id not in {r["id"] for r in rows}:  # the new point counts once its check is the latest (it is)
        rows.append(
            {
                "id": row.id,
                "value": row.value,
                "status": row.status,
                "period": row.period,
                "period_start": row.period_start,
                "period_end": row.period_end,
                "available_at": at,
                "superseded_by": None,
            }
        )
    found = [c for c in series.changes(rows, row.id) if c["kind"] != "repeat"]
    holder = session.get(db.Topic, domain_id)
    if holder is not None and found:
        event(
            session,
            holder,
            at,
            "series",
            record=cand.record_id,
            **{("measure_kind" if k == "kind" else k): v for k, v in key.items()},
            value=final["value"],
            period=row.period,
            changes=found,
            algo=series.VERSION,
            checks=[check_id],
        )
    if holder is not None and series.notable(found):
        # the records the computation used become the topic's records, so its analysis reads them
        used = {i for c in found for i in c.get("rows", [])} | {row.id}
        versions = session.scalars(select(db.Measurement.version_id).where(db.Measurement.id.in_(used)))
        for obs in session.scalars(
            select(db.Observation).where(
                db.Observation.version_id.in_(list(versions)), db.Observation.topic_id.is_(None)
            )
        ):
            obs.topic_id = holder.id
        owe(holder, "series_change", at)
        owed.append(holder.id)
    return row, owed


def hand_over(session, row, identity, relocated_from=None):
    """A point read from the upgraded artifact of its version takes the place of the points of the same
    identity read from the artifacts that upgrade replaced - they retire (artifact_upgrade), never a record
    contradiction; a changed value is told to the topic. Only the artifact of the version's current plan, an
    upgrade's, hands over. A relocated candidate's point takes the place of exactly the point of the candidate
    it came from, whatever its group key was (one from before the complete domain has none). Returns the
    retired points."""
    plans = list(
        session.scalars(
            select(db.RecordRead).where(
                db.RecordRead.workspace_id == row.workspace_id,
                db.RecordRead.is_demo == row.is_demo,
                db.RecordRead.version_id == row.version_id,
            )
        )
    )
    current = [
        p
        for p in plans
        if p.superseded_by_id is None and p.supersedes_id is not None and p.artifact_key == row.artifact_key
    ]
    replaced = {p.artifact_key for p in plans if p.superseded_by_id is not None} - {row.artifact_key, ""}
    if not current or not replaced:
        return []
    query = select(db.Measurement).filter_by(
        workspace_id=row.workspace_id, is_demo=row.is_demo, version_id=row.version_id
    )
    if relocated_from is not None:
        query = query.filter_by(candidate_id=relocated_from)
    else:
        query = query.filter_by(period=row.period, **identity)
    old = list(
        session.scalars(
            query.where(
                db.Measurement.artifact_key.in_(sorted(replaced)),
                db.Measurement.retired_reason.is_(None),
                db.Measurement.id != row.id,
            )
        )
    )
    for o in old:
        o.retired_by, o.retired_reason, o.retired_at = row.id, "artifact_upgrade", db.now()
    changed = [o for o in old if o.value_text != row.value_text]
    holder = session.get(db.Topic, row.domain_id) if changed and row.domain_basis == "topic" else None
    if holder is not None:
        # a reprocessing of the same raw version, dated now: a replay before it never sees it, and it is
        # never a change of the source at the record's time
        event(
            session,
            holder,
            db.now(),
            "artifact_value_changed",
            reprocessed=True,
            record=row.record_id,
            version=row.version_id,
            period=row.period,
            points=[{"id": o.id, "value": o.value_text, "artifact": o.artifact_key} for o in changed]
            + [{"id": row.id, "value": row.value_text, "artifact": row.artifact_key}],
        )
    return old


def record_corrections(session, s: Settings, docs, *, is_demo: bool) -> int:
    """Record each correction or retraction a new record states, visible from the record's
    time, apart from whether its replacement value passes checking; the topics of the points it withdraws get
    a "correction" event and are owed an analysis (source_corrected). Idempotent. Returns how many new."""
    made = 0
    for doc in docs:
        at = parse_time(doc["available_at"])
        for r in doc.get("relations") or []:
            if r["kind"] not in {"corrects", "retracts"}:
                continue
            target = dict(
                workspace_id=s.workspace_id,
                is_demo=is_demo,
                target_source=r.get("target_source", doc["source"]),
                target_record_id=r["target_record_id"],
                target_version_id=r.get("target_version_id") or "",
            )
            if session.scalar(
                select(db.RecordCorrection.id).filter_by(**target, by_version_id=doc["version_id"])
            ):
                continue
            session.add(
                db.RecordCorrection(
                    **target,
                    relation=r["kind"],
                    by_source=doc["source"],
                    by_record_id=doc["record_id"],
                    by_version_id=doc["version_id"],
                    visible_at=at,
                )
            )
            session.flush()
            made += 1
            hit = select(db.Measurement).where(
                db.Measurement.workspace_id == s.workspace_id,
                db.Measurement.is_demo == is_demo,
                db.Measurement.source == target["target_source"],
                db.Measurement.record_id == target["target_record_id"],
                db.Measurement.domain_basis == "topic",
            )
            if target["target_version_id"]:
                hit = hit.where(db.Measurement.version_id == target["target_version_id"])
            by_topic = {}
            for m in session.scalars(hit):
                by_topic.setdefault(m.domain_id, []).append(m.id)
            for topic_id, ids in sorted(by_topic.items()):
                topic = session.get(db.Topic, topic_id)
                if topic is None:
                    continue
                event(
                    session,
                    topic,
                    at,
                    "correction",
                    record=doc["record_id"],
                    by_source=doc["source"],
                    by_version_id=doc["version_id"],
                    reason="source_correction",
                    relation=r["kind"],
                    withdrawn=sorted(ids),
                )
                owe(topic, "source_corrected", at)
    return made
