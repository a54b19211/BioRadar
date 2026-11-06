"""Continuous monitoring: new Fabric records -> triage -> topics -> automatic analyses.

Topic states follow the source clock (when records became available), so a simulation or a
historical replay behaves the same way as live monitoring:
  emerging --(3 records or an important alert)--> rising
  rising --(no new record for topic_cool_days)--> cooling
  cooling --(topic_dormant_days without records, or a closure notice)--> dormant
  cooling / dormant --(a new related record)--> rising again (an awakening)"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, literal, select
from sqlalchemy.orm import object_session

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import NOT_VISIBLE, REGISTRY_DIFF_SOURCES, Fabric, FabricError, noticed
from bioradar.config import Settings, settings
from bioradar.material import of
from bioradar.research import analyses, continuation, domain, measure_checks, prompts, schemas, series

log = logging.getLogger("bioradar")
_turn = 0  # rotates which monitored source is read first in a live round
ALERTS = ["none", "normal", "important", "urgent"]
WEIGHT = {"none": 0, "normal": 1, "important": 2, "urgent": 3}
BATCH = 10
TRIAGE_CHARACTERS = continuation.WINDOW  # of each record's text, per triage call; the rest: continuation


def parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value)
    if len(text) == 10:  # day precision: the whole day counts as available
        return datetime.fromisoformat(text).replace(hour=23, minute=59, tzinfo=UTC)
    if len(text) == 7:
        return datetime.fromisoformat(text + "-28").replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


STOPPED = {"TERMINATED", "SUSPENDED", "WITHDRAWN"}
# 需要模型、但这一轮没有做成分诊的记录：triage_failed（模型调用失败）和 triage_deferred（当日自动额度
# 用完，v3 3.7：额度只暂停模型阶段）。两者都是待办，由重试路径在之后的轮次补做；游标照常前进。
DEFERRED = "triage_deferred"
RETRYABLE = ("triage_failed", DEFERRED)
ATTACHMENT_MAX_ATTEMPTS = 3  # Fabric db.derived_text.MAX_ATTEMPTS
NEW_REGISTRATION_DAYS = 30
RECENT_UPDATE_DAYS = 7


def route(doc) -> str | None:
    """Whether a record goes to model triage (None) or is settled by the program (the reason).

    Trial registries are settled by Fabric's field-by-field diff against the previous version: a version
    whose changes are all minor (contact or site list edits, wording, a start date moved) needs no model.
    A first adoption has no previous version to compare with; it is triaged only when the registry itself
    says it is new or just changed in a way worth checking (first posted within 30 days; updated within 7
    days and stopped, or results first posted within 30 days). The rest is inventory the collector is
    catching up on, not news. Anything the diff could not answer (older Fabric, not processed, not paired)
    goes to triage as before, and a retried triage is never re-routed.

    Attachment versions (v3 3.3a): triaged only with their parsed text. Not parsed yet (or Fabric unable to
    say) -> attachment_pending, kept for the retry path; parsing finally failed or the format is unsupported
    -> attachment_unparsed, recorded as a gap. An attachment is never triaged with an empty text."""
    if doc.get("role") == "attachment":
        state = doc.get("attachment_text") or {}
        if state.get("status") == "parsed" and (doc.get("text") or "").strip():
            return None
        if state.get("status") == "unsupported" or (
            state.get("status") == "failed" and (state.get("attempt") or 0) >= ATTACHMENT_MAX_ATTEMPTS
        ):
            return "attachment_unparsed"
        return "attachment_pending"
    if doc["source"] not in REGISTRY_DIFF_SOURCES or doc.get("retry"):
        return None
    diff = doc.get("registry_diff") or {}
    if diff.get("state") == "processed":
        return None if diff.get("material_count") else "registry_minor_change"
    if diff.get("state") != "first_adoption":
        return None
    at = parse_time(doc["available_at"])
    reg = doc.get("registry") or {}

    def within(value, days):
        try:
            return value is not None and at - parse_time(value) <= timedelta(days=days)
        except ValueError:
            return False

    if within(doc.get("published_at"), NEW_REGISTRATION_DAYS):
        return None
    if within(reg.get("updated_at"), RECENT_UPDATE_DAYS) and (
        reg.get("status") in STOPPED or within(reg.get("results_first_posted"), NEW_REGISTRATION_DAYS)
    ):
        return None
    return "inventory_first_adoption"


def settle_attachments(docs, *, is_demo: bool):
    """Attachments routed away from triage. Pending: an observation marked triage_failed, so the retry path
    reads it again until Fabric has parsed it. Unparsed: a recorded gap (alert none) replacing that mark."""
    s = settings()
    with db.session() as session:
        for doc in docs:
            reason = route(doc)
            if reason not in {"attachment_pending", "attachment_unparsed"}:
                continue
            existing = session.scalar(
                select(db.Observation).where(db.Observation.version_id == doc["version_id"])
            )
            if reason == "attachment_pending":
                if existing is not None:
                    continue
                text = "triage_failed"
            else:
                state = doc.get("attachment_text") or {}
                text = (
                    f"附件未能解析（{state.get('error') or state.get('status')}）："
                    "附件内容没有进入分析，不表示附件为空"
                )
                if existing is not None:
                    if existing.reason != "triage_failed":
                        continue
                    session.delete(existing)
                    session.flush()
            session.add(
                db.Observation(
                    workspace_id=s.workspace_id,
                    source=doc["source"],
                    record_id=doc["record_id"],
                    version_id=doc["version_id"],
                    is_demo=is_demo,
                    title=doc["title"],
                    available_at=parse_time(doc["available_at"]),
                    topic_id=None,
                    alert="none",
                    closure=False,
                    activity_key=None,
                    reason=text,
                )
            )


def defer(docs, *, is_demo: bool) -> int:
    """记下需要模型但这一轮没分诊的记录（triage_deferred）：之后由重试路径补做。已有观测的不动。"""
    s = settings()
    added = 0
    with db.session() as session:
        have = set(
            session.scalars(
                select(db.Observation.version_id).where(
                    db.Observation.version_id.in_([d["version_id"] for d in docs])
                )
            )
        )
        for doc in docs:
            if doc["version_id"] in have:
                continue
            session.add(
                db.Observation(
                    workspace_id=s.workspace_id,
                    source=doc["source"],
                    record_id=doc["record_id"],
                    version_id=doc["version_id"],
                    is_demo=is_demo,
                    title=doc["title"],
                    available_at=parse_time(doc["available_at"]),
                    topic_id=None,
                    alert="none",
                    closure=False,
                    activity_key=None,
                    reason=DEFERRED,
                )
            )
            have.add(doc["version_id"])
            added += 1
    return added


def unseen(session, items):
    """Items whose version has not been observed yet; a failed triage counts as not observed.
    Only this page's versions are queried, so a round costs the same however long the history is."""
    if not items:
        return []
    done = set(
        session.scalars(
            select(db.Observation.version_id).where(
                db.Observation.version_id.in_([d["version_id"] for d in items]),
                db.Observation.reason.not_in(RETRYABLE),
            )
        )
    )
    return [d for d in items if d["version_id"] not in done]


def new_documents(fabric: Fabric, s: Settings, *, is_demo: bool, cutoff: datetime | None, retry: bool = True):
    """Live: the change feed after the saved cursor, at most monitor_max_records per round; the rest
    stays in the feed (backlog) and the cursor only moves past what this round takes. After failed
    retries, the remaining room is split evenly across sources, starting from a different source each
    round, so one flooding source cannot starve WHO; leftover room goes to sources that still have more.
    Every scanned record counts against the room, seen or not, so a lagging cursor cannot scan forever.
    Replay: everything visible at the cutoff that was not observed yet.
    Returns (docs, cursor ends, backlog)."""
    sources = s.demo_sources if is_demo else (s.monitor_sources or s.sources)
    live = cutoff is None
    room = s.monitor_max_records
    docs, ends, more = [], {}, set()
    with db.session() as session:
        # Records still owed a triage (the call failed, or the daily automatic budget deferred it) are read
        # again (live mode: the change cursor moved on), using at most half of the round. Not while the
        # budget is used up: they would only be deferred again.
        # Owed work is on one fixed version: read that version, not the record's current head (Codex r62:
        # an older deferred version read by record id never got done and blocked the queue head).
        failed = {}
        for source, record_id, version_id in session.execute(
            select(db.Observation.source, db.Observation.record_id, db.Observation.version_id)
            .where(
                db.Observation.is_demo == is_demo,
                db.Observation.reason.in_(RETRYABLE),
                db.Observation.source.in_(sources),  # same source set as this round
            )
            .order_by(db.Observation.id)
            .limit(max(1, room // 2) if retry else 0)
        ):
            failed.setdefault(source, []).append((record_id, version_id))
        for source, refs in failed.items():
            items, missing = fabric.read_versions(source, refs, is_demo=is_demo, cutoff=cutoff)
            docs += [dict(d, retry=True) for d in unseen(session, items)]
            if missing:
                # the owed version can no longer be read (revoked, or not visible): a recorded gap, so it
                # stops occupying the queue; not a triage result
                for obs in session.scalars(
                    select(db.Observation).where(
                        db.Observation.source == source,
                        db.Observation.is_demo == is_demo,
                        db.Observation.version_id.in_(missing),
                        db.Observation.reason.in_(RETRYABLE),
                    )
                ):
                    obs.reason = "triage_unavailable: 固定版本已不可读（撤权或不可见），未分诊"
        if not live:
            for source in sources:
                page = None
                while True:
                    items, page = fabric.search(
                        source, (), is_demo=is_demo, cutoff=cutoff, cursor=page, limit=100
                    )
                    docs += unseen(session, items)
                    if not page:
                        break
        else:
            for source in sources:
                cursor = session.get(db.Cursor, (source, is_demo))
                if cursor is None:
                    # A real feed starts at its current end (legacy_documents alone holds 600k records);
                    # history is for replays. Demo feeds are scenario data and start at 0.
                    try:
                        start = 0 if is_demo else fabric.head(source, is_demo=is_demo)
                    except FabricError as exc:
                        if exc.code not in NOT_VISIBLE:
                            raise
                        log.info("monitor: %s not readable now (%s), skipped this round", source, exc.code)
                        continue  # no cursor yet: it starts at the end once the source is readable
                    cursor = db.Cursor(source=source, is_demo=is_demo, after_seq=start)
                    session.add(cursor)
                ends[source] = cursor.after_seq
            sources = [x for x in sources if x in ends]

            scanned = len(docs)

            def take(source, limit):
                """Scan up to `limit` records of one feed; ends[source] moves past what was scanned."""
                nonlocal scanned
                got = 0
                while got < limit:
                    at = ends[source]
                    try:
                        items, after = fabric.changes(
                            source, is_demo=is_demo, after=at, limit=min(100, limit - got)
                        )
                    except FabricError as exc:
                        if exc.code not in NOT_VISIBLE:
                            raise
                        # this source is not readable now (revoked): its cursor stays, the other feeds go on
                        log.info("monitor: %s not readable now (%s), skipped this round", source, exc.code)
                        more.discard(source)
                        return
                    docs.extend(unseen(session, items))
                    got += len(items)
                    scanned += len(items)
                    if after == at:
                        more.discard(source)
                        return
                    ends[source] = after
                more.add(source)

            global _turn
            _turn += 1
            k = _turn % max(1, len(sources))
            order = sources[k:] + sources[:k]  # a different source goes first each round
            share = max(1, (room - scanned) // max(1, len(order)))
            for source in order:
                take(source, min(share, max(0, room - scanned)))
            for source in [x for x in order if x in more]:
                if scanned >= room:
                    break
                take(source, room - scanned)
    unique = {d["version_id"]: d for d in docs}
    return sorted(unique.values(), key=lambda d: parse_time(d["available_at"])), ends, bool(more)


def watches(session, topic_ids) -> dict:
    """Each topic's current watch list (its latest watch event): {topic_id: [{id, text}]}."""
    current = {}
    for e in session.scalars(
        select(db.TopicEvent)
        .where(db.TopicEvent.topic_id.in_(list(topic_ids)), db.TopicEvent.kind == "watch")
        .order_by(db.TopicEvent.id)
    ):
        current[e.topic_id] = e.detail.get("items", [])
    return current


KEY = (
    db.Measurement.object,
    db.Measurement.metric,
    db.Measurement.unit,
    db.Measurement.kind,
    db.Measurement.scope,
)


def known_series(session, s: Settings, is_demo: bool, clock: datetime, limit=40):
    """The most recently measured series keys visible at `clock`, shown to triage so names stay the same
    (a replay is not shown names that only appeared later)."""
    rows = session.execute(
        select(*KEY)
        .where(
            db.Measurement.workspace_id == s.workspace_id,
            db.Measurement.is_demo == is_demo,
            db.Measurement.available_at <= clock,
            measure_checks.effective_clause(),  # unchecked, old-checker or unreadable points are not shown
        )
        .group_by(*KEY)
        .order_by(func.max(db.Measurement.available_at).desc())
        .limit(limit)
    ).all()
    return [dict(zip(("object", "metric", "unit", "kind", "scope"), r)) for r in rows]


def triage(s: Settings, docs, topics, watch=None, known=None, reads=None):
    """One triage call for a batch of records. reads: when given, a list filled with the material_read id of
    each record's window, in order - the send each candidate was read from (B r2)."""
    watch = watch or {}
    # what the model reads of each record, with the notice when it is not the cleaned text
    windows = [noticed(of(d).slice(0, TRIAGE_CHARACTERS), d.get("clean_text_state")) for d in docs]
    payload = {
        "series": known or [],
        "topics": [
            {"index": i, "title": t.title, "state": t.state, "watch": watch.get(t.id, [])}
            for i, t in enumerate(topics, 1)
        ],
        "records": [
            {
                "index": i,
                "title": d["title"],
                "published_at": d["published_at"],
                "material_kind": d["material_kind"],
                "verified": d["verified"],
                "events": d["events"],
                "text": w.text,
            }
            for i, (d, w) in enumerate(zip(docs, windows), 1)
        ],
    }
    # each record's window, recorded with its binding before it goes out (v3 6.1); settled after the call
    with db.session() as session:
        rows = [
            db.material_read(
                w,
                stage="triage",
                service="radar",
                ref=f"R{i}",
                source=d["source"],
                record_id=d["record_id"],
                version_id=d["version_id"],
            )
            for i, (d, w) in enumerate(zip(docs, windows), 1)
        ]
        session.add_all(rows)
        session.flush()
        ids = [r.id for r in rows]
    if reads is not None:
        reads.extend(ids)
    try:
        result, call = llm.chat_json(s, "triage", prompts.TRIAGE, payload, schemas.Triage)
    except BaseException:
        with db.session() as session:
            for r in session.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(ids))):
                r.status = "failed"
        raise
    with db.session() as session:
        u = call["usage"]
        row = db.LlmCall(
            stage="triage",
            service="radar",
            model=call["model"],
            error=call["error"],
            finish_reason=call["finish_reason"],
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            total_tokens=u.get("total_tokens", 0),
            seconds=call["seconds"],
            requests=call.get("requests") or 1,
            http=call.get("http"),
        )
        session.add(row)
        session.flush()
        db.keep_outputs(session, row, call)
        for r in session.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(ids))):
            r.llm_call_id, r.status = row.id, "sent"
    items = {item.index: item for item in (result.items if result else []) if 1 <= item.index <= len(docs)}
    return [items.get(i) for i in range(1, len(docs) + 1)]


def event(session, topic, at, kind, **detail):
    session.add(db.TopicEvent(topic_id=topic.id, at=at, kind=kind, detail=detail))


def observe(s: Settings, docs, *, is_demo: bool, budget=False):
    """Triage new documents and attach them to topics. Returns (topics that need an analysis,
    whether every document was triaged). With `budget`, stops before a batch once today's
    automatic tokens reach the ceiling."""
    wake = {}
    for start in range(0, len(docs), BATCH):
        if budget and analyses.over_auto_budget():
            log.info("triage stopped: daily_auto_tokens reached after %d of %d records", start, len(docs))
            return wake, False
        batch = docs[start : start + BATCH]
        with db.session() as session:
            topics = list(
                session.scalars(
                    select(db.Topic)
                    .where(db.Topic.is_demo == is_demo)
                    .order_by(db.Topic.last_activity_at.desc())
                    .limit(30)
                )
            )
        with db.session() as session:
            watch = watches(session, [t.id for t in topics])
            known = known_series(session, s, is_demo, max(parse_time(d["available_at"]) for d in batch))
        reads = []
        decisions = triage(s, batch, topics, watch, known, reads=reads)
        with db.session() as session:
            topics = [session.get(db.Topic, t.id) for t in topics]
            flag_corrections(batch, is_demo=is_demo, session=session)  # same transaction as the observations
            touched = {}  # topic id -> latest record time in this batch, for the activity baseline
            for n, (doc, item) in enumerate(zip(batch, decisions)):
                at = parse_time(doc["available_at"])
                alert = item.alert if item else "none"
                topic = None
                if item and item.topic and 1 <= item.topic <= len(topics):
                    topic = topics[item.topic - 1]
                elif (
                    item
                    and item.new_topic
                    and alert != "none"
                    and any(t.title == item.new_topic[:200] for t in topics)
                ):
                    topic = next(t for t in topics if t.title == item.new_topic[:200])
                elif item and item.new_topic and alert != "none":
                    topic = db.Topic(
                        workspace_id=s.workspace_id,
                        title=item.new_topic[:200],
                        direction=item.direction,
                        is_demo=is_demo,
                        first_seen_at=at,
                        last_activity_at=at,
                        alert=alert,
                    )
                    session.add(topic)
                    session.flush()
                    topics.append(topic)
                    event(session, topic, at, "created", record=doc["record_id"], title=doc["title"])
                session.execute(
                    delete(db.Observation).where(
                        db.Observation.version_id == doc["version_id"],
                        db.Observation.reason.in_(RETRYABLE),
                    )
                )
                session.add(
                    db.Observation(
                        workspace_id=s.workspace_id,
                        source=doc["source"],
                        record_id=doc["record_id"],
                        version_id=doc["version_id"],
                        is_demo=is_demo,
                        title=doc["title"],
                        available_at=at,
                        topic_id=topic.id if topic else None,
                        alert=alert,
                        closure=bool(item and item.closure),
                        activity_key=activity_key(doc),
                        reason=(item.reason + (" [观测超过3个，只记前3个]" if len(item.measures) > 3 else ""))
                        if item
                        else "triage_failed",
                    )
                )
                # measures become candidates; only the check worker turns a checked one into a series point
                measure_checks.enqueue(
                    session,
                    s,
                    doc,
                    item,
                    topic,
                    at,
                    is_demo,
                    window=(0, TRIAGE_CHARACTERS),
                    material_read_id=reads[n] if n < len(reads) else None,
                )
                if item:  # its first window was read: the windows after it are owed (T-31)
                    continuation.plan(session, s, doc, topic, item.direction, at, is_demo)
                if topic is None:
                    continue
                if topic.state in {"cooling", "dormant"} and not (item and item.closure):
                    topic.awakenings += 1
                    topic.closed = False
                    set_state(
                        session, topic, "rising", at, reason="new_related_record", record=doc["record_id"]
                    )
                    wake[topic.id] = "awakened"
                    owe(topic, "awakened", at)
                topic.last_activity_at = max(parse_time(topic.last_activity_at), at)
                topic.heat += WEIGHT[alert]
                if ALERTS.index(alert) > ALERTS.index(topic.alert):
                    topic.alert = alert
                    if alert in {"important", "urgent"}:
                        wake[topic.id] = f"alert_{alert}"
                        owe(topic, f"alert_{alert}", at)
                if item and item.closure:
                    topic.closed = True
                hit = (
                    item.watch if item and item.watch in {w["id"] for w in watch.get(topic.id, [])} else None
                )
                if hit:
                    wake[topic.id] = "watch_hit"
                    owe(topic, "watch_hit", at)
                event(
                    session,
                    topic,
                    at,
                    "observed",
                    record=doc["record_id"],
                    alert=alert,
                    closure=bool(item and item.closure),
                    signal=item.signal if item else "other",
                    objects=[o.name for o in item.objects] if item else [],
                    watch=hit,
                )
                touched[topic.id] = max(touched.get(topic.id, at), at)
            session.flush()
            for topic_id, at in sorted(touched.items()):
                if activity(session, s, session.get(db.Topic, topic_id), at, is_demo):
                    wake[topic_id] = "activity_anomaly"
    return wake, True


def period_bounds(period, at):
    """(start, end) of a checked period: a month range spans whole months; otherwise series.period_bounds."""
    if period.get("start") and period["precision"] == "month":
        (y1, m1), (y2, m2) = (map(int, period["start"].split("-")), map(int, period["end"].split("-")))
        return datetime(y1, m1, 1, tzinfo=UTC), series._month_end(y2, m2)
    text = period.get("value") or f"{period['start']}/{period['end']}"
    return series.period_bounds(text, at)


RECORD_CONFLICT = "record_internal_conflict"


def adopt(session, s: Settings, cand, final, check_id):
    """Store one checked point (T-24 series logic over the effective view) and compare it with its series as
    visible at the record's time. A correction of an earlier record (of the named version, when Fabric names
    one) replaces that record's value for the same series and period. A series is one key in one comparable
    domain (T-24): the topic the record was placed in. A point of unknown domain (no topic) is stored and
    effective but nothing is computed from it, and no topic is opened from it - a shared object/metric name is
    not a business identity. Writes a `series` topic event per change.
    Returns (point or None, topic ids owed)."""
    at = measure_checks._utc(cand.observed_at)
    period = final["period"]
    period_text = period.get("value") or f"{period['start']}/{period['end']}"
    bounds = period_bounds(period, at)
    if bounds is None:
        return None, []
    # the comparable domain (T-24): the topic the record was placed in; without one the domain is unknown
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
    )  # a retried triage: the same point in the same group (Codex r171)
    existing = session.scalar(select(db.Measurement).filter_by(candidate_id=cand.id))
    if existing is not None:
        # checked again (recovery, checker upgrade): the point stands and now cites its current check; its
        # stored
        # values are compared with this check's final values again; nothing new to compute for the series
        existing.check_id = check_id
        # as this check proves them: family, group key, dimensions and gaps move together (Codex r171)
        existing.family, existing.dims_key = key["family"], key["dims_key"]
        kept = [RECORD_CONFLICT] if RECORD_CONFLICT in (existing.dims_reasons or []) else []
        existing.dims, existing.dims_reasons = gate["dims"], gate["reasons"] + kept  # a contradiction stays
        return existing, []
    art = db.artifact_key(cand.processor_version, cand.text_sha256)
    identity = {k: v for k, v in key.items() if k not in unplaced}
    # one artifact's reading of the version: another artifact of the same raw version is not the record
    # stating a second value - an upgrade's point takes the old one's place instead (hand_over, T-26)
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
    # both values kept, neither compared (Codex r177); never "the first one wins"
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
    # whatever domain that point is (T-24, Codex 695); only the comparison stays within one domain
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
        # whose domain is not known, and no topic is opened from it (T-24)
        return row, owed
    if row.dims_reasons:
        # not a complete, proven domain (family or a required dimension unknown or unchecked), or a value the
        # same record contradicts: stored and effective, a lead with its gaps - nothing computed
        # (Codex 725/r177)
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
        and not r.dims_reasons  # only proven points: a gap in its domain makes no baseline (Codex r178)
    ]
    # a point whose record a stated correction withdrew by then leaves the series - whether or not a
    # replacement passed checking, and whatever domain it went to (Codex r166); a replacement inside this
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


def series_basis_points(session, topic_id, at=None) -> list:
    """The structured record of the points the latest series event used, each with whether it is current and
    its latest check (series_basis states them only when all are current). As of `at` (a cutoff): the latest
    series event visible then, never one computed later (Codex r167)."""
    last = session.scalar(
        visible(select(db.TopicEvent), db.TopicEvent.at, at)
        .where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "series")
        .order_by(db.TopicEvent.id.desc())
        .limit(1)
    )
    used = sorted({i for c in (last.detail["changes"] if last else []) for i in c.get("rows", [])})
    return measure_checks.series_points(session, used, at) if used else []  # current or not, with its state


def series_basis(session, topic_id, at=None) -> str:
    """The latest computed series change of a topic in words, for the analysis it triggers: values, periods
    and records, so the investigation starts from what the program found instead of searching blind. As of
    `at` (a cutoff): the latest series event visible then (Codex r167)."""
    last = session.scalar(
        visible(select(db.TopicEvent), db.TopicEvent.at, at)
        .where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "series")
        .order_by(db.TopicEvent.id.desc())
        .limit(1)
    )
    if last is None:
        return ""
    d = last.detail
    used = sorted({i for c in d["changes"] for i in c.get("rows", [])})
    if not used or measure_checks.is_effective(session, used) != set(used):
        return ""  # a point it used is no longer checked and readable: not a current conclusion
    rows = list(
        session.scalars(
            select(db.Measurement).where(db.Measurement.id.in_(used)).order_by(db.Measurement.period_end)
        )
    )
    states = [measure_checks.comparability(session, r, at) for r in rows]
    if any(c["state"] != "comparable" for c in states):
        return ""  # a point it used is no longer comparable (withdrawn, gaps): not a current conclusion
    points = "；".join(
        f"{r.period or '当时'} {r.value_text or format(r.value, 'g')}{r.unit}"
        f"（{r.status}，记录 {r.record_id}，"
        f"已核 check {r.check_id}）"
        for r in rows
    )
    scope = f"（{d['scope']}）" if d.get("scope") else ""
    found = "、".join(sorted({c["kind"] for c in d["changes"]}))
    return (
        f"程序按已入账观测算出：{d['object']}·{d['metric']}{scope}：{points}。判断：{found}。"
        f"比较组：{rows[0].family}，必要维度（病种、人群、日期口径等）均已核实或由已核字段承担。"
        "这只是待核实的线索，请回原文核对数值、口径和期间。"
    )


def visible(query, column, at):
    """The query limited to what was visible at the cutoff `at` (None: now)."""
    return query if at is None else query.where(column <= at)


def handed_events(session, topic_id):
    """The ids of the events earlier analyses of the topic were handed (Codex r182)."""
    out = set()
    for e in session.scalars(
        select(db.TopicEvent).where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "analysis")
    ):
        out.update(e.detail.get("handed_events") or [])
    return out


def correction_batch(session, topic_id, since, at=None, limit=10):
    """(events, handed ids, earliest time of what is left over or None) of the topic's correction events not
    handed to an analysis yet, as of the cutoff: at most `limit` correcting record versions, oldest first; the
    repeats of one in the batch are handed with it (Codex r182)."""
    handed = handed_events(session, topic_id)
    rows = session.scalars(
        visible(select(db.TopicEvent), db.TopicEvent.at, at)
        .where(
            db.TopicEvent.topic_id == topic_id,
            db.TopicEvent.kind == "correction",
            db.TopicEvent.at >= since,
        )
        .order_by(db.TopicEvent.id)
    )
    out, given, keys, left = [], [], set(), None
    for e in rows:
        if e.id in handed:
            continue
        d = e.detail
        key = (d.get("by_source"), d["record"], d.get("by_version_id"), d.get("reason"))
        if key not in keys and len(out) >= limit:
            left = min(left, parse_time(e.at)) if left else parse_time(e.at)
            continue
        if key not in keys:
            keys.add(key)
            out.append(e)
        given.append(e.id)
    return out, given, left


def correction_events(session, topic_id, since, at=None, limit=10):
    """The correction events of a topic since it was owed and not handed yet, as of the cutoff: one per
    correcting record version (bounded), oldest first."""
    return correction_batch(session, topic_id, since, at, limit)[0]


def conflict_basis(session, topic_id, since, at=None, limit=10):
    """(question text, pinned seeds, point ids, handed event ids, earliest time of what is left over or None)
    of the record contradictions a topic is owed an analysis for (Codex r180/r182): each record at the version
    that states one identity with two values, both values and their quotes; as of the cutoff, the `limit`
    oldest events not handed to an analysis yet, one seed per (source, record, version)."""
    handed = handed_events(session, topic_id)
    rows = [
        e
        for e in session.scalars(
            visible(select(db.TopicEvent), db.TopicEvent.at, at)
            .where(
                db.TopicEvent.topic_id == topic_id,
                db.TopicEvent.kind == "record_conflict",
                db.TopicEvent.at >= since,
            )
            .order_by(db.TopicEvent.id)
        )
        if e.id not in handed
    ]
    rows, rest = rows[:limit], rows[limit:]
    left = min(parse_time(e.at) for e in rest) if rest else None
    lines, seeds, ids, seen = [], [], [], set()
    for e in rows:
        d = e.detail
        points = [session.get(db.Measurement, x["id"]) for x in d.get("points") or []]
        points = [p for p in points if p is not None]
        if not points:
            continue
        ids += [p.id for p in points if p.id not in ids]
        key = (points[0].source, d["record"], d.get("version"))
        if key not in seen:
            seen.add(key)
            seeds.append(
                {
                    "source": points[0].source,
                    "record_id": d["record"],
                    "version_id": d.get("version"),
                    "title": "原文内部矛盾",
                }
            )
        values = "；".join(f"{p.value_text}{p.unit}（原句：{p.quote}）" for p in points)
        lines.append(
            f"记录 {d['record']}（版本 {d.get('version')}）对 {d.get('period')} 的同一指标"
            f"给出不同数值：{values}"
        )
    text = (
        (
            "\n原文内部矛盾，两个值都已保留、都不参与比较，请读这一版原文核实：\n"
            + "\n".join(f"- {line}" for line in lines)
        )
        if lines
        else ""
    )
    return text, seeds, ids, [e.id for e in rows], left


def correction_seeds(session, topic_id, since, at=None, events=None):
    """The correcting records themselves, as sources the analysis must read (Codex r167): a correction that
    withdrew a point is named even when its replacement never passed checking."""
    seeds, seen = [], set()
    for e in events if events is not None else correction_events(session, topic_id, since, at):
        d = e.detail
        key = (d.get("by_source"), d["record"], d.get("by_version_id"))
        if d.get("by_source") and key not in seen:
            seen.add(key)
            kind = "撤回通知" if d.get("relation") == "retracts" else "更正通知"
            seeds.append(
                {
                    "source": d["by_source"],
                    "record_id": d["record"],
                    "version_id": d.get("by_version_id"),
                    "title": kind,
                }
            )
    return seeds


def correction_basis(session, topic_id, since, at=None, events=None) -> str:
    """The corrections Fabric stated for this topic's points, as of the cutoff (T-24, Codex r166/r167): which
    notice withdrew which values - whether or not a replacement passed checking - and, when the replacement
    was placed in another topic, that the program did not use it here. The notices are seeds of the
    analysis."""
    lines = []
    for e in events if events is not None else correction_events(session, topic_id, since, at):
        d = e.detail
        old = session.scalars(select(db.Measurement).where(db.Measurement.id.in_(d["withdrawn"])))
        olds = "、".join(f"{m.period} {m.value_text}{m.unit}（记录 {m.record_id}）" for m in old)
        notice = f"记录 {d['record']}" + (
            f"（{d['by_source']}，版本 {d['by_version_id']}）" if d.get("by_source") else ""
        )
        if d.get("reason") == "cross_domain_correction":
            new = session.get(db.Measurement, d["by"])
            value = f"{new.value_text}{new.unit}" if new else "?"
            lines.append(
                f"{notice} 的更正值 {value} 被归到另一个领域，程序没有用它替代本专题的 {olds}，"
                "请核实更正是否适用于本专题。"
            )
        else:
            verb = "撤回" if d.get("relation") == "retracts" else "更正"
            lines.append(
                f"{notice} {verb}了本专题的 {olds}：旧值已不再计入本专题的序列；"
                "更正后的数值是否已核实、是否属于本专题，请读这份通知核实。"
            )
    return ("\n来源更正，请逐条核实：\n" + "\n".join(f"- {line}" for line in lines)) if lines else ""


BASELINE_DAYS = 7


def activity_key(doc) -> str:
    """What one record counts as in daily activity: an explicit repost is its original (source + record),
    else Fabric's same-origin group, else the record itself. Sources stay apart (two sources may share an
    id) and group names never mix with record ids; nothing is guessed from titles."""
    repost = next((r for r in doc["relations"] if r["kind"] == "reposts"), None)
    if repost:
        return f"record:{repost.get('target_source') or doc['source']}:{repost['target_record_id']}"
    if doc.get("origin_group"):
        return f"group:{doc['origin_group']}"
    return f"record:{doc['source']}:{doc['record_id']}"


def activity(session, s: Settings, topic, at, is_demo) -> bool:
    """T-15, a naive baseline of information activity (not case incidence): the topic's records on the day
    of `at`, each counted once by its activity_key, against the previous 7 days the monitor received anything
    at all; days with no records from any source (collection stopped or missing) are left out, not zero.
    Above mean + 3 standard deviations (mean + 3 when they are all equal) is an anomaly; fewer than 7
    days is not judged. Owes the topic an analysis once per day."""
    day = func.date(db.Observation.available_at)
    scope = (
        db.Observation.workspace_id == s.workspace_id,
        db.Observation.is_demo == is_demo,
        db.Observation.available_at >= at - timedelta(days=4 * BASELINE_DAYS),
        db.Observation.available_at <= at,
    )
    received = sorted(str(d) for d in session.scalars(select(day).where(*scope).distinct()))
    today = str(at.astimezone(UTC).date())  # the database groups by UTC day
    before = [d for d in received if d < today][-BASELINE_DAYS:]
    if len(before) < BASELINE_DAYS:
        return False
    itself = literal("record:") + db.Observation.source + literal(":") + db.Observation.record_id
    counted = func.count(func.distinct(func.coalesce(db.Observation.activity_key, itself)))
    counts = {
        str(d): n
        for d, n in session.execute(
            select(day, counted).where(*scope, db.Observation.topic_id == topic.id).group_by(day)
        ).all()
    }
    values = [counts.get(d, 0) for d in before]
    mean = sum(values) / len(values)
    spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    threshold = mean + 3 * spread if spread else mean + 3
    if counts.get(today, 0) <= threshold:
        return False
    events = session.scalars(
        select(db.TopicEvent).where(db.TopicEvent.topic_id == topic.id, db.TopicEvent.kind == "activity")
    )
    if any(e.detail.get("day") == today for e in events):
        return False
    detail = dict(day=today, records=counts.get(today, 0), mean=round(mean, 2), sd=round(spread, 2))
    event(session, topic, at, "activity", **detail, threshold=round(threshold, 2), days=before)
    owe(topic, "activity_anomaly", at)
    return True


def owe(topic, reason, at):
    """Record, in the same transaction as the change, that this topic is owed an analysis - each new reason
    also as a dated "owed" event, so an analysis at a cutoff can take only the reasons visible then
    (Codex 718)."""
    if topic.pending_reason is None:
        topic.pending_reason, topic.pending_since = reason, at
    elif reason not in topic.pending_reason.split("+"):
        topic.pending_reason = (topic.pending_reason + "+" + reason)[:200]
    # every occurrence is dated, also of a reason already owed: an answer at a cutoff between two occurrences
    # must not settle the later one (Codex r169)
    session = object_session(topic)
    if session is not None:
        event(session, topic, at, "owed", reason=reason)


def owed_at(session, topic, cutoff):
    """(reasons owed by the cutoff, [(reason, at)] occurrences after it) of the topic's current debt - one
    reason may be both (owed before and again after). A reason without any dated event (owed before they were
    kept) cannot be placed in time: it is neither."""
    reasons = (topic.pending_reason or "").split("+")
    # the current debt: owed since the topic's last analysis cleared the one before
    last = session.scalar(
        select(func.max(db.TopicEvent.id)).where(
            db.TopicEvent.topic_id == topic.id, db.TopicEvent.kind == "analysis"
        )
    )
    seen = {}  # reason -> every dated occurrence of the current debt
    for e in session.scalars(
        select(db.TopicEvent)
        .where(
            db.TopicEvent.topic_id == topic.id,
            db.TopicEvent.kind == "owed",
            db.TopicEvent.id > (last or 0),
        )
        .order_by(db.TopicEvent.id)
    ):
        r = e.detail.get("reason")
        if r in reasons:
            seen.setdefault(r, []).append(e.at)
    shown = [r for r in reasons if any(parse_time(at) <= cutoff for at in seen.get(r, []))]
    later = [(r, at) for r in reasons for at in seen.get(r, []) if parse_time(at) > cutoff]
    return shown, later


def set_state(session, topic, state, at, **detail):
    if topic.state != state:
        event(session, topic, at, "state", before=topic.state, after=state, **detail)
        topic.state = state


def advance(s: Settings, clock: datetime, *, is_demo: bool):
    """Move topic states forward to the given source clock. Returns topics that started rising."""
    rising = {}
    with db.session() as session:
        for topic in session.scalars(select(db.Topic).where(db.Topic.is_demo == is_demo)):
            idle = clock - parse_time(topic.last_activity_at)
            count = session.scalar(select(func.count()).where(db.Observation.topic_id == topic.id)) or 0
            if topic.closed and topic.state != "dormant":
                set_state(session, topic, "dormant", clock, reason="closure_notice")
            elif topic.state == "emerging" and (count >= 3 or topic.alert in {"important", "urgent"}):
                set_state(session, topic, "rising", clock, reason="records_or_alert", records=count)
                rising[topic.id] = "rising"
                owe(topic, "rising", clock)
            elif topic.state == "rising" and idle >= timedelta(days=s.topic_cool_days):
                set_state(session, topic, "cooling", clock, reason="no_new_records", idle_days=idle.days)
            elif topic.state == "cooling" and idle >= timedelta(days=s.topic_dormant_days):
                set_state(session, topic, "dormant", clock, reason="no_new_records", idle_days=idle.days)
    return rising


def review_basis(session, topic_id, since) -> str:
    """What analysts rejected or flagged since this topic was owed, for the new analysis to address."""
    rows = session.scalars(
        select(db.Review)
        .where(
            db.Review.topic_id == topic_id,
            db.Review.action.in_(("reject", "flag_source")),
            db.Review.created_at >= since,
        )
        .order_by(db.Review.id)
        .limit(10)
    )
    lines = []
    for r in rows:
        what = (
            f"报告第 {r.sentence} 句被驳回" if r.action == "reject" else f"材料 {r.evidence_ref} 被标记有误"
        )
        lines.append(f"{what}（上一轮分析 {r.analysis_id[:8]}）" + (f"：{r.note}" if r.note else ""))
    return ("\n分析员复核意见，请逐条回应：\n" + "\n".join(f"- {line}" for line in lines)) if lines else ""


def start_analyses(s: Settings, *, is_demo: bool, cutoff: datetime | None, budget=False):
    """One automatic analysis per topic that is owed one (started rising, awoke or raised its alert),
    most urgent first. Live monitoring is capped per hour; what the cap holds back stays owed."""
    created = []
    live = cutoff is None
    with db.session() as session:
        owed = list(
            session.scalars(
                select(db.Topic).where(db.Topic.is_demo == is_demo, db.Topic.pending_reason.is_not(None))
            )
        )
        owed.sort(key=lambda t: (-ALERTS.index(t.alert), parse_time(t.pending_since)))
        recent = session.scalar(
            select(func.count())
            .select_from(db.Analysis)
            .where(db.Analysis.trigger == "topic", db.Analysis.created_at > db.now() - timedelta(hours=1))
        )
    for owed_topic in owed:
        topic_id, reason = owed_topic.id, owed_topic.pending_reason
        later = []
        if cutoff is not None:
            # a replay takes only the debt visible at its cutoff; what is owed later stays owed (Codex 718)
            with db.session() as session:
                visible_reasons, later = owed_at(session, session.get(db.Topic, topic_id), cutoff)
            if not visible_reasons:
                continue
            reason = "+".join(visible_reasons)
        if live and recent + len(created) >= s.auto_analyses_per_hour:
            log.info("auto analysis held (hourly cap): %d topics still owed", len(owed) - len(created))
            break
        if budget and analyses.over_auto_budget():
            log.info("auto analysis held (daily_auto_tokens): %d topics still owed", len(owed) - len(created))
            break
        # The new analysis and the cleared debt commit together; a round that dies before dispatch
        # leaves a queued row that analyses.recover() picks up, never a second analysis.
        with db.session() as session:
            topic = session.get(db.Topic, topic_id)
            records = list(
                session.scalars(
                    visible(select(db.Observation), db.Observation.available_at, cutoff)
                    .where(db.Observation.topic_id == topic_id)
                    .order_by(db.Observation.available_at.desc())
                    .limit(8)
                )
            )
            last = session.scalar(
                visible(select(db.TopicEvent), db.TopicEvent.at, cutoff)
                .where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "observed")
                .order_by(db.TopicEvent.id.desc())
                .limit(1)
            )
            template = prompts.SIGNAL_QUESTIONS.get((last.detail if last else {}).get("signal", "other"))
            question = (
                template.format(title=topic.title) + f"（触发：{reason}）"
                if template
                else f"专题“{topic.title}”出现新进展（{reason}）。基于目前可见的材料，现在应当怎样判断？"
                "请区分已证实、待证实和被更正的内容，给出真实性置信、潜在后果和核实优先级，并指出最该补查的证据。"
            )
            points = []
            if "series_change" in reason.split("+"):
                question += series_basis(session, topic_id, cutoff)
                points = series_basis_points(session, topic_id, cutoff)
            if "analyst_review" in reason.split("+"):
                question += review_basis(session, topic_id, owed_topic.pending_since)
            seeds = [{"source": r.source, "record_id": r.record_id, "title": r.title} for r in records]
            handed, leftover = [], {}  # what this analysis is given; what stays owed, from when (Codex r182)
            corrected = [r for r in reason.split("+") if r in {"cross_domain_correction", "source_corrected"}]
            if corrected:
                since = owed_topic.pending_since
                batch, given, left = correction_batch(session, topic_id, since, cutoff)
                handed += given
                if left is not None:
                    leftover.update(dict.fromkeys(corrected, left))
                question += correction_basis(session, topic_id, since, cutoff, events=batch)
                seeds += correction_seeds(
                    session, topic_id, since, cutoff, events=batch
                )  # pinned: never merged with a head
            if "record_conflict" in reason.split("+"):
                text, pinned, ids, given, left = conflict_basis(
                    session, topic_id, owed_topic.pending_since, cutoff
                )
                handed += given
                if left is not None:
                    leftover["record_conflict"] = left
                question += text
                seeds += pinned  # the version that contradicts itself, read as that version (fixed read)
                known = {p["measurement_id"] for p in points}
                points = list(points) + [
                    p
                    for p in measure_checks.series_points(session, ids, cutoff)
                    if p["measurement_id"] not in known
                ]
            row = analyses.create(
                question=question,
                direction=topic.direction,
                is_demo=topic.is_demo,
                cutoff=cutoff.isoformat() if cutoff else None,
                trigger="topic",
                topic_id=topic.id,
                parent_id=topic.last_analysis_id,
                seed_records=seeds,
                series_points=points,
                session=session,
            )
            topic.last_analysis_id = row.id
            asked = reason.split("+")
            unplaced = [r for r in (topic.pending_reason or "").split("+") if r and r not in asked]
            # occurrences after the cutoff stay owed: one mark per reason, each occurrence a dated owed event
            unplaced += [r for r in dict.fromkeys(r for r, _ in later) if r not in unplaced]
            if cutoff is not None and unplaced:
                # later reasons (and any that cannot be placed in time) are still owed, from their own time
                times = [parse_time(at) for r, at in later]
                topic.pending_reason = "+".join(unplaced)
                topic.pending_since = min(times) if times else topic.pending_since
            else:
                topic.pending_reason = topic.pending_since = None
            if leftover:
                # the events this bounded hand-off did not reach stay owed, from the earliest of them
                kept = [r for r in (topic.pending_reason or "").split("+") if r]
                times = list(leftover.values()) + ([parse_time(topic.pending_since)] if kept else [])
                topic.pending_reason = "+".join(kept + [r for r in leftover if r not in kept])[:200]
                topic.pending_since = min(times)
            event(
                session,
                topic,
                cutoff or db.now(),
                "analysis",
                analysis_id=row.id,
                reason=reason,
                handed_events=sorted(set(handed)),
            )
            for r, at in later:  # still owed after this analysis, from their own time
                event(session, topic, at, "owed", reason=r, carried=True)
            for r, at in leftover.items():  # dated after the analysis event, so the next round sees them
                event(session, topic, at, "owed", reason=r, carried=True, left_over=True)
        analyses.dispatch(row.id)
        created.append(row.id)
    return created


def hand_over(session, row, identity, relocated_from=None):
    """T-26: a point read from the upgraded artifact of its version takes the place of the points of the same
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
        # never a change of the source at the record's time (Codex 770)
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
    """Record each correction or retraction a new record states (T-24, Codex r166), visible from the record's
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


def flag_corrections(docs, *, is_demo: bool, session=None) -> int:
    """A new correction or retraction marks every analysis that cited the old record (matched on the
    relation's target source + record, and version when Fabric names one; demo and real kept apart),
    and owes its topic a new analysis. Runs in the triage transaction, so the mark is never lost
    between "record seen" and "analyses marked". Analyses still running keep the mark when they
    finish (analyses.execute merges it). Analyses without a topic carry it on the page ("需复核")."""
    if session is None:
        with db.session() as own:
            return flag_corrections(docs, is_demo=is_demo, session=own)
    flagged = 0
    for doc in docs:
        for r in doc["relations"]:
            if r["kind"] not in {"corrects", "retracts"}:
                continue
            query = (
                select(db.Evidence.ref, db.Analysis, db.Investigation.topic_id)
                .join(db.Analysis, db.Analysis.id == db.Evidence.analysis_id)
                .join(db.Investigation, db.Investigation.id == db.Analysis.investigation_id)
                .where(
                    db.Evidence.source == r.get("target_source", doc["source"]),
                    db.Evidence.record_id == r["target_record_id"],
                    db.Investigation.is_demo == is_demo,
                    db.Analysis.state != "failed",
                )
            )
            if r.get("target_version_id"):
                query = query.where(db.Evidence.version_id == r["target_version_id"])
            for ref, analysis, topic_id in session.execute(query).all():
                entry = {
                    "ref": ref,
                    "record_id": r["target_record_id"],
                    "kind": "correction" if r["kind"] == "corrects" else "retraction",
                    "by_record": doc["record_id"],
                }
                result = dict(analysis.result or {})
                updates = list(result.get("source_updates") or [])
                if all({k: u.get(k) for k in entry} != entry for u in updates):
                    result["source_updates"] = updates + [{**entry, "at": db.now().isoformat()}]
                    analysis.result = result
                    flagged += 1
                if topic_id:
                    owe(session.get(db.Topic, topic_id), "source_corrected", db.now())
    return flagged


def tick(*, is_demo=False, cutoff: datetime | None = None, clock: datetime | None = None, fabric=None):
    """One monitoring round. `clock` moves topic states during blank periods with no new records.
    Live real monitoring pauses new triage and automatic analyses once today's automatic tokens reach
    daily_auto_tokens; the backlog stays in the feed and owed analyses stay owed."""
    s = settings()
    fabric = fabric or Fabric(s)
    budget = cutoff is None and not is_demo  # live real monitoring only
    paused = budget and analyses.over_auto_budget()
    with db.session() as session:
        measure_checks.backfill(session, s)  # a checker upgrade owes every candidate a new check
        measure_checks.legacy_backlog(session, s)  # points stored before checking owe a re-extraction
    # current read permission and binding of every stored point, before anything this round reads a series
    measure_checks.refresh_bindings(s, fabric, is_demo=is_demo)
    # v3 3.7：额度只暂停模型阶段。读取、确定性路由、附件待办照常；要模型的记成 triage_deferred 待办。
    docs, ends, backlog = new_documents(fabric, s, is_demo=is_demo, cutoff=cutoff, retry=not paused)
    with db.session() as session:  # a stated correction counts from when it is seen, triaged or not
        record_corrections(session, s, docs, is_demo=is_demo)
    routed = {}
    for d in docs:
        reason = route(d)
        routed[reason or "triage"] = routed.get(reason or "triage", 0) + 1
    triage_docs = [d for d in docs if route(d) is None]
    settle_attachments(docs, is_demo=is_demo)
    if len(triage_docs) < len(docs):
        log.info("monitor routing %s", routed)
    if paused:
        log.info("monitor: daily_auto_tokens reached; model triage deferred, program routing continues")
        reasons, whole = {}, False
    else:
        reasons, whole = (
            observe(s, triage_docs, is_demo=is_demo, budget=budget) if triage_docs else ({}, True)
        )
    if not paused:  # a re-extraction is a model call: it waits while the token budget is spent
        measure_checks.reextract(s, fabric, is_demo=is_demo, triage=triage, plan=continuation.plan)
    with db.session() as session:  # plans paused by a withdrawn permission read on once it is back
        continuation.periodic_resume(session, s, fabric, is_demo, db.now())
    if not paused:  # continuation windows are model calls too (T-31)
        continuation.run(s, fabric, is_demo=is_demo, budget=budget)
    for topic_id, reason in measure_checks.run(s, fabric, is_demo=is_demo, adopt=adopt).items():
        reasons.setdefault(topic_id, reason)
    if not whole:
        # the budget ran out before or during triage: what was not triaged is owed, durably, and the
        # cursors move on (the retry path triages it once the budget allows)
        paused = True
        deferred = defer(triage_docs, is_demo=is_demo)
        if deferred:
            routed[DEFERRED] = deferred
    fresh = {d["source"] for d in docs if not d.get("retry")}  # a retried old record is not new
    with db.session() as session:  # a round that died before this line reads the same records again
        for source, at in ends.items():
            cursor = session.get(db.Cursor, (source, is_demo))
            cursor.after_seq = at
            if source in fresh:
                cursor.last_new_at = db.now()
    latest = max([parse_time(d["available_at"]) for d in docs], default=None)
    clock = clock or cutoff or latest or db.now()
    reasons.update({k: v for k, v in advance(s, clock, is_demo=is_demo).items() if k not in reasons})
    created = (
        start_analyses(s, is_demo=is_demo, cutoff=cutoff, budget=budget)
        if s.auto_analysis and not paused
        else []
    )
    return {
        "records": len(docs),
        # how many records went to model triage and how many the program settled, by reason
        "routed": routed,
        "backlog": backlog,  # more new records are waiting in the feed than one round takes
        "paused": "daily_auto_tokens" if paused else None,
        "clock": clock.isoformat(),
        "topics": reasons,
        "analyses": created,
    }


def topic_view(topic: db.Topic, *, events=False) -> dict:
    value = {
        "id": topic.id,
        "title": topic.title,
        "direction": topic.direction,
        "is_demo": topic.is_demo,
        "state": topic.state,
        "alert": topic.alert,
        "heat": topic.heat,
        "awakenings": topic.awakenings,
        "closed": topic.closed,
        "first_seen_at": topic.first_seen_at.isoformat(),
        "last_activity_at": topic.last_activity_at.isoformat(),
        "last_analysis_id": topic.last_analysis_id,
        "pending_reason": topic.pending_reason,
        "summary": topic.summary,
    }
    if events:
        with db.session() as session:
            value["events"] = [
                {"at": e.at.isoformat(), "kind": e.kind, **e.detail}
                for e in session.scalars(
                    select(db.TopicEvent).where(db.TopicEvent.topic_id == topic.id).order_by(db.TopicEvent.id)
                )
            ]
            value["observations"] = [
                {
                    "title": o.title,
                    "source": o.source,
                    "record_id": o.record_id,
                    "available_at": o.available_at.isoformat(),
                    "alert": o.alert,
                    "closure": o.closure,
                    "reason": o.reason,
                }
                for o in session.scalars(
                    select(db.Observation)
                    .where(db.Observation.topic_id == topic.id)
                    .order_by(db.Observation.available_at)
                )
            ]
    return value
