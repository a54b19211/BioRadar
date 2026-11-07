"""Fabric's measurement objects in the radar's topics (docs/decisions/0010-fabric-measurements.md).

Fabric owns every objective measurement - extraction, BioCheck checking, corrections, processor and checker
upgrades (its ADR 0006). The radar keeps what is its own: which topic puts which Fabric object into which
series (series_points, the investigation's selection), the comparable-domain gate (domain.py), and the series
events and analyses that follow from them.

- sync() reads the change feed (GET /api/history/measurements/changes) page by page into measurement_inbox;
  the cursor moves to the page's next_after in the same transaction that stored the page, so a round that
  dies inside a page reads it again and adds nothing. Events Fabric withholds (their source, or the source of
  their cause, is not readable now) are only counted per source (measurement_withheld); once that source can
  be read again, every version of it this workspace placed in a topic is pulled again by version and every
  linked object is read again (the feed does not replay what it withheld).
- link_versions() pulls the objects of versions triage just put into a topic (GET /api/history/measurements),
  in case their events have not come through the feed yet.
- An inbox row is linked as soon as its version has a topic, whichever of the two came first; a row whose
  object had a later event (superseded, corrected, retracted, a correction withdrawn) is read again and its
  point leaves the series or comes back. A link is unique per (workspace, kind, topic, object), so a replayed
  feed or a second pull adds nothing.
- An object whose period has a start and no end ("since September 2012") is no point of a series: no end is
  made up, its inbox row says open_ended_period and the round counts it under that name.
- A series is one key in one topic: (object key, metric, unit, kind, scope, period type and precision, metric
  family, dimension group). The object key is Fabric's identity key only for a confirmed or rule-confirmed
  identity; otherwise it is the literal object within its own record, so two records never merge on a name."""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from bioradar import db
from bioradar.adapters.fabric import FabricError
from bioradar.config import Settings
from bioradar.research import domain, series
from bioradar.research.clock import parse_time, utc
from bioradar.research.topics import event, owe

log = logging.getLogger("bioradar")

FEED = "fabric:measurements"  # the radar's cursor on Fabric's measurement feed (monitor_cursors)
PAGE, PAGES = 100, 10  # events per feed request, requests per round
LINK_LIMIT = 200  # inbox rows linked or read again per round
VERSIONS = 100  # versions per pull (Fabric's limit)
CONFIRMED = ("confirmed", "rule_confirmed")
CORRECTED = ("corrected", "retracted")
SUPERSEDED = ("superseded",)
RECORD_CONFLICT = "record_internal_conflict"
OPEN_ENDED = "open_ended_period"  # inbox state and round count of an object whose period has no end
NO_VERSION = "0" * 64  # a pull of no version: asks only whether the source may be read now
FABRIC_ID = re.compile(r"[0-9a-f]{64}")
KINDS = {
    "created",
    "superseded_by_processor",
    "superseded_by_producer",
    "rechecked_by_checker",
    "corrected",
    "retracted",
    "correction_withdrawn",
}
UNREACHABLE = (FabricError, httpx.HTTPError)


def is_fabric_id(value) -> bool:
    return isinstance(value, str) and bool(FABRIC_ID.fullmatch(value))


def _read_at(cutoff):
    """(mode, cutoff) of a pull: what the system knew at a replay's cutoff, else the current state now."""
    return ("operational_replay", cutoff) if cutoff is not None else ("current", datetime.now(UTC))


# ---- the feed -------------------------------------------------------------------------------------------


def _valid_page(page):
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list) or not isinstance(page.get("next_after"), int):
        return False
    return all(
        is_fabric_id(i.get("measurement_id"))
        and isinstance(i.get("version_id"), str)
        and isinstance(i.get("source_id"), str)
        and i.get("kind") in KINDS
        and isinstance(i.get("seq"), int)
        for i in items
    )


def note(session, s: Settings, is_demo, measurement_id, version_id, source_id, seq=0, kind="pulled"):
    """The inbox row of one object, created on first sight; due (to be linked or read again) from now on."""
    row = session.scalar(
        select(db.MeasurementInbox).filter_by(
            workspace_id=s.workspace_id, is_demo=is_demo, measurement_id=measurement_id
        )
    )
    if row is None:
        row = db.MeasurementInbox(
            workspace_id=s.workspace_id,
            is_demo=is_demo,
            measurement_id=measurement_id,
            version_id=version_id,
            source_id=source_id,
            last_seq=0,
            last_kind=kind,
            state="pending_topic",
        )
        session.add(row)
    if seq > row.last_seq:
        row.last_seq, row.last_kind = seq, kind
    row.due, row.updated_at = True, db.now()
    return row


def _store_page(session, s, page, is_demo):
    for item in page["items"]:
        note(
            session,
            s,
            is_demo,
            item["measurement_id"],
            item["version_id"],
            item["source_id"],
            item["seq"],
            item["kind"],
        )
    for source, n in (page.get("withheld") or {}).items():
        row = session.get(db.MeasurementWithheld, (s.workspace_id, is_demo, source))
        if row is None:
            row = db.MeasurementWithheld(
                workspace_id=s.workspace_id, is_demo=is_demo, source_id=source, events=0
            )
            session.add(row)
        row.events += int(n)
        row.last_seq, row.state, row.position, row.updated_at = page["next_after"], "withheld", 0, db.now()


def read_feed(s: Settings, fabric, *, is_demo) -> dict:
    """Up to PAGES pages of the feed into the inbox. Each page and the cursor move commit together."""
    out = {"events": 0, "withheld": 0, "pages": 0}
    for _ in range(PAGES):
        with db.session() as session:
            cursor = session.get(db.Cursor, (FEED, is_demo))
            after = cursor.after_seq if cursor else 0
        try:
            page = fabric.measurement_changes(is_demo=is_demo, after_seq=after, limit=PAGE)
        except UNREACHABLE as exc:
            log.warning("series_sync: the measurement feed cannot be read (%s); the cursor stays", exc)
            break
        if not _valid_page(page):
            log.warning("series_sync: a measurement feed page that is not the contract; the cursor stays")
            break
        with db.session() as session:
            _store_page(session, s, page, is_demo)
            cursor = session.get(db.Cursor, (FEED, is_demo))
            if cursor is None:
                cursor = db.Cursor(source=FEED, is_demo=is_demo, after_seq=0)
                session.add(cursor)
            cursor.after_seq = max(cursor.after_seq, page["next_after"])
        out["pages"] += 1
        out["events"] += len(page["items"])
        out["withheld"] += sum((page.get("withheld") or {}).values())
        if not page.get("more"):
            break
    return out


# ---- pulls by version -----------------------------------------------------------------------------------


def _valid_receipt(r):
    return (
        isinstance(r, dict)
        and is_fabric_id(r.get("measurement_id"))
        and isinstance(r.get("current"), bool)
        and isinstance(r.get("inputs"), list)
        and bool(r["inputs"])
        and isinstance(r.get("identity"), dict)
        and isinstance(r.get("check"), dict)
        and isinstance(r.get("period"), dict)
        and isinstance(r.get("versions"), dict)
    )


def pull(fabric, source, versions, *, is_demo, cutoff=None) -> dict:
    """{measurement_id: receipt} of the objects of these versions of one source, current or not (FabricError /
    httpx.HTTPError when the source cannot be read now or Fabric does not answer)."""
    mode, at = _read_at(cutoff)
    out, versions = {}, sorted(set(versions))
    for start in range(0, len(versions), VERSIONS):
        data = fabric.measurements(
            source,
            versions[start : start + VERSIONS],
            is_demo=is_demo,
            mode=mode,
            cutoff=at.isoformat(),
            include_history=True,
        )
        out.update({r["measurement_id"]: r for r in data.get("measurements") or [] if _valid_receipt(r)})
    return out


def link_versions(s: Settings, fabric, docs, *, is_demo, cutoff=None) -> dict:
    """The versions triage just placed: their objects are pulled now and linked to the version's topic."""
    by_source = {}
    for d in docs:
        by_source.setdefault(d["source"], set()).add(d["version_id"])
    for source, versions in sorted(by_source.items()):
        try:
            got = pull(fabric, source, versions, is_demo=is_demo, cutoff=cutoff)
        except UNREACHABLE:
            continue  # their events come through the feed, or the next pull
        with db.session() as session:
            for r in got.values():
                note(session, s, is_demo, r["measurement_id"], r["inputs"][0]["version_id"], source)
    return link_due(s, fabric, is_demo=is_demo, cutoff=cutoff)


def _placed_versions(session, s, is_demo, source, after):
    return session.execute(
        select(db.Observation.id, db.Observation.version_id)
        .where(
            db.Observation.workspace_id == s.workspace_id,
            db.Observation.is_demo == is_demo,
            db.Observation.source == source,
            db.Observation.topic_id.is_not(None),
            db.Observation.id > after,
        )
        .order_by(db.Observation.id)
        .limit(VERSIONS)
    ).all()


def repull_withheld(s: Settings, fabric, *, is_demo, cutoff=None) -> int:
    """For each source whose events were withheld: once it can be read again, one page of the versions of it
    this workspace placed in topics is pulled per round; after the last page every linked object is due to be
    read again (a withheld correction from this source may be about another source's object). Returns the
    pages pulled."""
    with db.session() as session:
        held = [
            r.source_id
            for r in session.scalars(
                select(db.MeasurementWithheld).filter_by(
                    workspace_id=s.workspace_id, is_demo=is_demo, state="withheld"
                )
            )
        ]
    pages = 0
    for source in held:
        with db.session() as session:
            row = session.get(db.MeasurementWithheld, (s.workspace_id, is_demo, source))
            placed = _placed_versions(session, s, is_demo, source, row.position)
        try:  # a source not readable now answers this pull with an error: it stays withheld
            got = pull(fabric, source, [v for _, v in placed] or [NO_VERSION], is_demo=is_demo, cutoff=cutoff)
        except UNREACHABLE:
            continue
        pages += 1
        with db.session() as session:
            row = session.get(db.MeasurementWithheld, (s.workspace_id, is_demo, source))
            for r in got.values():
                note(session, s, is_demo, r["measurement_id"], r["inputs"][0]["version_id"], source)
            if placed:
                row.position = placed[-1][0]
            else:
                session.execute(
                    update(db.MeasurementInbox)
                    .where(
                        db.MeasurementInbox.workspace_id == s.workspace_id,
                        db.MeasurementInbox.is_demo == is_demo,
                        db.MeasurementInbox.state.in_(("linked", "not_current")),
                    )
                    .values(due=True)
                )
                row.state = "released"
            row.updated_at = db.now()
    return pages


# ---- linking ---------------------------------------------------------------------------------------------


def link_due(s: Settings, fabric, *, is_demo, cutoff=None) -> dict:
    """Link, or read again, up to LINK_LIMIT due inbox rows whose version has a topic; one pull per source. A
    source that cannot be read now keeps its rows due, at the back of the queue."""
    with db.session() as session:
        rows = session.execute(
            select(db.MeasurementInbox.id, db.MeasurementInbox.source_id, db.MeasurementInbox.version_id)
            .add_columns(db.Observation.topic_id)
            .join(db.Observation, db.Observation.version_id == db.MeasurementInbox.version_id)
            .where(
                db.MeasurementInbox.workspace_id == s.workspace_id,
                db.MeasurementInbox.is_demo == is_demo,
                db.MeasurementInbox.due.is_(True),
                db.Observation.topic_id.is_not(None),
            )
            .order_by(db.MeasurementInbox.updated_at, db.MeasurementInbox.id)
            .limit(LINK_LIMIT)
        ).all()
    by_source, counts = {}, {"linked": 0, "left": 0, "back": 0, "unreadable": 0}
    for inbox_id, source, version, topic_id in rows:
        by_source.setdefault(source, []).append((inbox_id, version, topic_id))
    for source, items in sorted(by_source.items()):
        try:
            got = pull(fabric, source, [v for _, v, _ in items], is_demo=is_demo, cutoff=cutoff)
        except UNREACHABLE:
            counts["unreadable"] += len(items)
            with db.session() as session:
                session.execute(
                    update(db.MeasurementInbox)
                    .where(db.MeasurementInbox.id.in_([i for i, _, _ in items]))
                    .values(updated_at=db.now())
                )
            continue
        for inbox_id, _, topic_id in items:
            with db.session() as session:
                row = session.get(db.MeasurementInbox, inbox_id)
                outcome = _apply(session, s, row, got.get(row.measurement_id), topic_id)
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _point(session, s, is_demo, topic_id, measurement_id):
    return session.scalar(
        select(db.SeriesPoint).filter_by(
            workspace_id=s.workspace_id, is_demo=is_demo, topic_id=topic_id, measurement_id=measurement_id
        )
    )


def _apply(session, s, row, receipt, topic_id):
    """One due row against the object as Fabric gives it now. Returns what happened."""
    row.updated_at = db.now()
    if receipt is None:
        return "missing"  # not visible to this read (a replay's cutoff): stays due, read again later
    row.due = False
    topic = session.get(db.Topic, topic_id)
    point = _point(session, s, row.is_demo, topic_id, row.measurement_id)
    if not receipt["current"]:
        row.state = "linked" if point is not None else "not_current"
        if point is not None and point.left_at is None:
            _leave(session, point, receipt, topic)
            return "left"
        return "not_current"
    if _open_ended(receipt["period"]):
        row.state = OPEN_ENDED
        return OPEN_ENDED
    row.state = "linked"
    if point is None:
        return "linked" if _join(session, s, row, receipt, topic) else "linked_elsewhere"
    if point.left_at is not None:
        point.left_at = point.left_reason = point.left_by = None
        event(
            session, topic, db.now(), "measurement_back", authority="fabric", measurement=point.measurement_id
        )
        return "back"
    return "kept"


def _object_key(receipt):
    """(object key, the rule that gave it): Fabric's confirmed identity, else the literal in its record."""
    identity, ref = receipt["identity"], receipt["inputs"][0]
    if identity.get("state") in CONFIRMED and identity.get("key"):
        return identity["key"], f"identity:{identity['state']}"
    return (
        f"literal:{ref['source_id']}|{ref['record_id']}|{domain.norm(receipt['object'])}",
        "literal_in_record",
    )


def _open_ended(period):
    """A period with its start and no end, the end absent or null (Fabric sends null: its objects are dumped
    with every field): it is not an interval a series can place."""
    return period.get("start") is not None and period.get("end") is None


def _period_text(period):
    return period.get("value") or f"{period['start']}/{period.get('end') or ''}"


def _bounds(period, at):
    """(start, end) of a structured period: a month range spans whole months; else series.period_bounds.
    None for a period open at its end: no end is made up."""
    if _open_ended(period):
        return None
    if period.get("start") and period.get("precision") == "month":
        (y1, m1), (y2, m2) = (map(int, period["start"].split("-")), map(int, period["end"].split("-")))
        return datetime(y1, m1, 1, tzinfo=UTC), series._month_end(y2, m2)
    return series.period_bounds(_period_text(period), at)


def _available(receipt):
    """When the object could be known: its material's proven availability, else when Fabric first saw it."""
    seen = receipt.get("visibility") or {}
    return parse_time(seen.get("proof_available_at") or seen["first_seen_at"])


def series_key(topic_id, key) -> str:
    body = json.dumps([topic_id, key], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _key(receipt, gate):
    period = receipt["period"]
    return {
        "object_key": _object_key(receipt)[0],
        "metric": receipt["metric"],
        "unit": receipt["unit"],
        "measure_kind": receipt["measure_kind"],
        "scope": receipt.get("scope") or "",
        "scope_mode": receipt["scope_mode"],
        "period_type": period["type"],
        "period_precision": period["precision"],
        "family": gate["family"],
        "dims_key": gate["dims_key"],
    }


def _new_point(s, row, receipt, topic, gate, key, bounds, at):
    ref, period = receipt["inputs"][0], receipt["period"]
    return db.SeriesPoint(
        workspace_id=s.workspace_id,
        is_demo=row.is_demo,
        topic_id=topic.id,
        measurement_id=row.measurement_id,
        source=ref["source_id"],
        record_id=ref["record_id"],
        version_id=ref["version_id"],
        object=receipt["object"][:300],
        metric=receipt["metric"][:200],
        unit=receipt["unit"][:50],
        measure_kind=receipt["measure_kind"],
        scope=key["scope"][:200],
        scope_mode=receipt["scope_mode"],
        period=_period_text(period),
        period_type=period["type"],
        period_precision=period["precision"],
        period_start=bounds[0],
        period_end=bounds[1],
        value_text=receipt["value_text"],
        quote=receipt["quote"],
        object_key=key["object_key"][:900],
        joined_by_rule=_object_key(receipt)[1],
        family=gate["family"],
        dims=gate["dims"],
        dims_key=gate["dims_key"],
        gate_version=domain.GATE_VERSION,
        gate_reasons=list(gate["reasons"]),
        series_key=series_key(topic.id, key),
        check_status=receipt["check"].get("status") or "",
        check_ref=receipt["check"].get("run_id") or "",
        checker_version=receipt["versions"].get("checker") or "",
        available_at=at,
    )


def _join(session, s, row, receipt, topic):
    """Put the object into the topic's series: gate, key, a record-internal contradiction, and the series
    change it makes. False when another link of the same object to this topic was written first."""
    gate, at = domain.series_gate(receipt), _available(receipt)
    bounds = _bounds(receipt["period"], at)
    if bounds is None:
        return False
    key = _key(receipt, gate)
    point = _new_point(s, row, receipt, topic, gate, key, bounds, at)
    try:
        with session.begin_nested():  # one link per (topic, object): a concurrent writer's link stands
            session.add(point)
    except IntegrityError:
        return False
    if not _record_conflict(session, point, topic, at) and not point.gate_reasons:
        _compare(session, point, topic, key, at)
    return True


def _record_conflict(session, point, topic, at) -> bool:
    """One record version stating the same series and period with another value: both are kept, neither is
    compared, and the topic is owed an analysis of the contradiction - never "the first one wins"."""
    others = list(
        session.scalars(
            select(db.SeriesPoint).where(
                db.SeriesPoint.workspace_id == point.workspace_id,
                db.SeriesPoint.is_demo == point.is_demo,
                db.SeriesPoint.topic_id == point.topic_id,
                db.SeriesPoint.version_id == point.version_id,
                db.SeriesPoint.series_key == point.series_key,
                db.SeriesPoint.period == point.period,
                db.SeriesPoint.left_at.is_(None),
                db.SeriesPoint.id != point.id,
            )
        )
    )
    if all(o.value_text == point.value_text for o in others):
        return False
    for p in others + [point]:
        if RECORD_CONFLICT not in p.gate_reasons:
            p.gate_reasons = list(p.gate_reasons) + [RECORD_CONFLICT]
    event(
        session,
        topic,
        at,
        "record_conflict",
        authority="fabric",
        record=point.record_id,
        version=point.version_id,
        period=point.period,
        points=[{"id": p.measurement_id, "value": p.value_text, "quote": p.quote} for p in others + [point]],
    )
    owe(topic, "record_conflict", at)
    return True


def _row(p):
    return {
        "id": p.measurement_id,
        "value": float(Decimal(p.value_text)),
        "status": p.status,
        "period": p.period,
        "period_start": p.period_start,
        "period_end": p.period_end,
        "available_at": p.available_at,
        "superseded_by": None,
    }


def _compare(session, point, topic, key, at):
    """The new point against the proven points of its series visible at its own time: a series event per
    change, and the topic owed an analysis when it is notable."""
    grain = series.grain(point.period)
    rows_ = [
        _row(p)
        for p in session.scalars(
            select(db.SeriesPoint).where(
                db.SeriesPoint.workspace_id == point.workspace_id,
                db.SeriesPoint.is_demo == point.is_demo,
                db.SeriesPoint.topic_id == point.topic_id,
                db.SeriesPoint.series_key == point.series_key,
                db.SeriesPoint.left_at.is_(None),
            )
        )
        if not p.gate_reasons and series.grain(p.period) == grain and utc(p.available_at) <= at
    ]
    found = [c for c in series.changes(rows_, point.measurement_id) if c["kind"] != "repeat"]
    if not found:
        return
    event(
        session,
        topic,
        at,
        "series",
        authority="fabric",
        record=point.record_id,
        object=point.object,
        **key,
        value=point.value_text,
        period=point.period,
        changes=found,
        algo=series.VERSION,
        checks=[point.check_ref],
    )
    if series.notable(found):
        owe(topic, "series_change", at)


def _cause(receipt):
    applied = [e for e in receipt.get("events_applied") or [] if e.get("kind") in CORRECTED]
    return applied[-1] if applied else {}


def _leave(session, point, receipt, topic):
    """The object is no longer current: its point leaves the series. A correction or retraction the source
    stated is a correction event of the topic from when it was visible, owed an analysis; a supersession by a
    newer processor, producer or checker is recorded (the new object joins on its own)."""
    state = receipt.get("state_at_cutoff") or "unknown"
    by = (receipt.get("status") or {}).get("superseded_by")
    point.left_at, point.left_by = db.now(), by[:64] if by else None
    point.left_reason = (state if state != "ok" else receipt.get("not_current_reason") or "not_current")[:60]
    if state not in CORRECTED:
        event(
            session,
            topic,
            db.now(),
            "measurement_left",
            authority="fabric",
            measurement=point.measurement_id,
            record=point.record_id,
            reason=point.left_reason,
            by=point.left_by,
        )
        return
    cause = _cause(receipt)
    stated = cause.get("cause") or {}
    at = parse_time(cause["effective_at"]) if cause.get("effective_at") else db.now()
    event(
        session,
        topic,
        at,
        "correction",
        authority="fabric",
        record=point.record_id,
        by_source=stated.get("source_id"),
        by_version_id=stated.get("version_id"),
        reason="source_correction",
        relation="retracts" if state == "retracted" else "corrects",
        withdrawn=[point.measurement_id],
    )
    owe(topic, "source_corrected", at)


def sync(s: Settings, fabric, *, is_demo, cutoff=None) -> dict:
    """One monitoring round's part: the feed into the inbox, what a re-granted source withheld pulled again,
    then every due row linked or read again."""
    out = read_feed(s, fabric, is_demo=is_demo)
    out["repulled"] = repull_withheld(s, fabric, is_demo=is_demo, cutoff=cutoff)
    out.update(link_due(s, fabric, is_demo=is_demo, cutoff=cutoff))
    return out


# ---- what analyses read --------------------------------------------------------------------------------


def rows(session, topic_id, ids):
    """The topic's points of these objects, by period."""
    if not ids or topic_id is None:
        return []
    return list(
        session.scalars(
            select(db.SeriesPoint)
            .where(db.SeriesPoint.topic_id == topic_id, db.SeriesPoint.measurement_id.in_(list(ids)))
            .order_by(db.SeriesPoint.period_end)
        )
    )


def proven(p) -> bool:
    """In the series now, with a complete, proven domain: what a computed conclusion may rest on."""
    return p.left_at is None and not p.gate_reasons


def _comparability(p, state=None):
    """state: the object's state as Fabric gave it now; None: as the radar last read it."""
    reasons = list(p.gate_reasons)
    left = p.left_reason if state is None else state
    if left in CORRECTED:
        reasons.append("withdrawn_by_correction")
    elif (left or "").startswith(SUPERSEDED):
        reasons.append("superseded_by_artifact")
    out_now = p.left_at is not None if state is None else state != "ok"
    return {
        "version": domain.GATE_VERSION,
        "state": "not_comparable" if reasons or out_now else "comparable",
        "family": p.family,
        "domain_basis": "topic",
        "domain_id": p.topic_id,
        "reasons": reasons,
    }


def _described(p, current, check, comparability):
    return {
        "measurement_id": p.measurement_id,
        "topic_id": p.topic_id,
        "authority": "fabric",
        "current": current,
        "object": p.object,
        "metric": p.metric,
        "value": p.value_text,
        "unit": p.unit,
        "period": p.period,
        "period_type": p.period_type,
        "period_precision": p.period_precision,
        "scope": p.scope,
        "scope_mode": p.scope_mode,
        "source": p.source,
        "record_id": p.record_id,
        "version_id": p.version_id,
        "extraction_check": check,
        "comparability": comparability,
    }


def _check(p, current, receipt=None, reason=None):
    return {
        "status": ((receipt or {}).get("check") or {}).get("status") or p.check_status or "unchecked",
        "gate": "series" if current else "candidate",
        "checker_version": ((receipt or {}).get("versions") or {}).get("checker")
        or p.checker_version
        or "unknown",
        "check_id": None,
        "reason": reason,
    }


def points(session, topic_id, ids, at=None):
    """The topic's points of these objects as the radar last read them (the series event an analysis is
    created from); the analysis reads their state again from Fabric when it runs (read_points)."""
    return [
        _described(
            p, p.left_at is None, _check(p, p.left_at is None, reason=p.left_reason), _comparability(p)
        )
        for p in rows(session, topic_id, ids)
        if at is None or utc(p.available_at) <= at
    ]


def read_points(s: Settings, fabric, recorded, cutoff, *, is_demo):
    """The points an analysis rests on, their state read from Fabric now - at the analysis' cutoff, with the
    permission of now: a source that cannot be read gives no current point. Read only."""
    with db.session() as session:
        found = [p for r in recorded for p in rows(session, r.get("topic_id"), [r["measurement_id"]])]
        session.expunge_all()
    by_source, receipts, unread = {}, {}, {}
    for p in found:
        by_source.setdefault(p.source, set()).add(p.version_id)
    for source, versions in sorted(by_source.items()):
        try:
            receipts.update(pull(fabric, source, versions, is_demo=is_demo, cutoff=cutoff))
        except FabricError as exc:
            unread[source] = exc.code[:120]
        except httpx.HTTPError:
            unread[source] = "fabric_unavailable"
    out = []
    for p in found:
        r = receipts.get(p.measurement_id)
        current = bool(r and r["current"])
        reason = r.get("not_current_reason") if r else unread.get(p.source, "not_visible")
        state = (r.get("state_at_cutoff") or "unknown") if r else "unreadable"
        out.append(_described(p, current, _check(p, current, r, reason), _comparability(p, state)))
    return out
