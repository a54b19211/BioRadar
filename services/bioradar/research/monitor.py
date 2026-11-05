"""Continuous monitoring: new Fabric records -> triage -> topics -> automatic analyses.

Topic states follow the source clock (when records became available), so a simulation or a
historical replay behaves the same way as live monitoring:
  emerging --(3 records or an important alert)--> rising
  rising --(no new record for topic_cool_days)--> cooling
  cooling --(topic_dormant_days without records, or a closure notice)--> dormant
  cooling / dormant --(a new related record)--> rising again (an awakening)"""

import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, literal, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import REGISTRY_DIFF_SOURCES, Fabric, noticed
from bioradar.config import Settings, settings
from bioradar.material import of
from bioradar.research import analyses, prompts, schemas, series

log = logging.getLogger("bioradar")
_turn = 0  # rotates which monitored source is read first in a live round
ALERTS = ["none", "normal", "important", "urgent"]
WEIGHT = {"none": 0, "normal": 1, "important": 2, "urgent": 3}
BATCH = 10
TRIAGE_CHARACTERS = 1500  # of each record's text, per triage call


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
                    start = 0 if is_demo else fabric.head(source, is_demo=is_demo)
                    cursor = db.Cursor(source=source, is_demo=is_demo, after_seq=start)
                    session.add(cursor)
                ends[source] = cursor.after_seq

            scanned = len(docs)

            def take(source, limit):
                """Scan up to `limit` records of one feed; ends[source] moves past what was scanned."""
                nonlocal scanned
                got = 0
                while got < limit:
                    at = ends[source]
                    items, after = fabric.changes(
                        source, is_demo=is_demo, after=at, limit=min(100, limit - got)
                    )
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
        )
        .group_by(*KEY)
        .order_by(func.max(db.Measurement.available_at).desc())
        .limit(limit)
    ).all()
    return [dict(zip(("object", "metric", "unit", "kind", "scope"), r)) for r in rows]


def triage(s: Settings, docs, topics, watch=None, known=None):
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
        )
        session.add(row)
        session.flush()
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
        decisions = triage(s, batch, topics, watch, known)
        with db.session() as session:
            topics = [session.get(db.Topic, t.id) for t in topics]
            flag_corrections(batch, is_demo=is_demo, session=session)  # same transaction as the observations
            touched = {}  # topic id -> latest record time in this batch, for the activity baseline
            for doc, item in zip(batch, decisions):
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
                for owed in measure(session, s, doc, item, topic, at, is_demo, topics):
                    wake[owed.id] = "series_change"
                    owe(owed, "series_change", at)
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


def measure(session, s: Settings, doc, item, topic, at, is_demo, topics) -> list:
    """Store the quoted measures of one record and compare each with its series as visible at `at`.
    A correction of an earlier record (of the named version, when Fabric names one) replaces that record's
    value for the same series and period. A notable change in a series that no topic follows yet opens one
    for the series, so values that mean nothing alone still lead to an investigation once they add up.
    Writes a `series` topic event per change; returns the topics owed an analysis."""
    text = re.sub(r"\s+", "", (doc["title"] or "") + (doc["text"] or ""))
    corrected = [
        (r.get("target_source", doc["source"]), r["target_record_id"], r.get("target_version_id"))
        for r in doc["relations"]
        if r["kind"] == "corrects"
    ]
    owed = []
    for m in item.measures[:3] if item else []:
        quote = re.sub(r"\s+", "", m.quote)
        bounds = series.period_bounds(m.period, at)
        if not quote or quote not in text or not series.quoted(m.value, quote) or bounds is None:
            log.info("measure dropped: %s %s %g", doc["record_id"], m.metric, m.value)
            continue
        key = dict(
            object=m.object.strip()[:200],
            metric=m.metric.strip()[:200],
            unit=m.unit.strip()[:50],
            kind=m.kind,
            scope=m.scope.strip()[:200],
        )
        same = dict(key, workspace_id=s.workspace_id, is_demo=is_demo)
        if session.scalar(
            select(db.Measurement.id).filter_by(version_id=doc["version_id"], period=m.period.strip(), **key)
        ):
            continue  # this version was measured before (a retried triage)
        row = db.Measurement(
            **same,
            value=m.value,
            # a rate's parts are kept only when the quote states them too
            numerator=m.numerator if m.numerator is not None and series.quoted(m.numerator, quote) else None,
            denominator=m.denominator
            if m.denominator is not None and series.quoted(m.denominator, quote)
            else None,
            period=m.period.strip()[:40],
            period_start=bounds[0],
            period_end=bounds[1],
            status=m.status,
            source=doc["source"],
            record_id=doc["record_id"],
            version_id=doc["version_id"],
            quote=m.quote[:300],
            available_at=at,
            topic_id=topic.id if topic else None,
        )
        session.add(row)
        session.flush()
        for source, record_id, version_id in corrected:
            query = select(db.Measurement).filter_by(
                source=source, record_id=record_id, period=row.period, **same
            )
            if version_id:
                query = query.filter_by(version_id=version_id)
            for old in session.scalars(query):
                # a replacement already in force keeps its date: a second notice correcting the same value
                # is not a correction of the first one, so the two stand as a conflict from now on
                if old.superseded_by is None:
                    old.superseded_by = row.id
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
                select(db.Measurement).filter_by(**same).where(db.Measurement.available_at <= at)
            )
            if series.grain(r.period) == grain  # a year is not compared with a quarter
        ]
        found = [c for c in series.changes(rows, row.id) if c["kind"] != "repeat"]
        holder = topic
        if holder is None and series.notable(found):
            title = f"{key['object']}·{key['metric']}" + (f"（{key['scope']}）" if key["scope"] else "")
            holder = next((t for t in topics if t.title == title[:200]), None)
            if holder is None:
                holder = db.Topic(
                    workspace_id=s.workspace_id,
                    title=title[:200],
                    direction=item.direction,
                    is_demo=is_demo,
                    first_seen_at=at,
                    last_activity_at=at,
                    alert="normal",
                )
                session.add(holder)
                session.flush()
                topics.append(holder)
                event(session, holder, at, "created", record=doc["record_id"], title=title, from_series=True)
            row.topic_id = holder.id
        if holder is not None and found:
            event(
                session,
                holder,
                at,
                "series",
                record=doc["record_id"],
                **{("measure_kind" if k == "kind" else k): v for k, v in key.items()},
                value=m.value,
                period=row.period,
                changes=found,
                algo=series.VERSION,
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
            if holder not in owed:
                owed.append(holder)
    return owed


def series_basis(session, topic_id) -> str:
    """The latest computed series change of a topic in words, for the analysis it triggers: values, periods
    and records, so the investigation starts from what the program found instead of searching blind."""
    last = session.scalar(
        select(db.TopicEvent)
        .where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "series")
        .order_by(db.TopicEvent.id.desc())
        .limit(1)
    )
    if last is None:
        return ""
    d = last.detail
    used = sorted({i for c in d["changes"] for i in c.get("rows", [])})
    rows = session.scalars(
        select(db.Measurement).where(db.Measurement.id.in_(used)).order_by(db.Measurement.period_end)
    )
    points = "；".join(
        f"{r.period or '当时'} {r.value:g}{r.unit}（{r.status}，记录 {r.record_id}）" for r in rows
    )
    scope = f"（{d['scope']}）" if d.get("scope") else ""
    found = "、".join(sorted({c["kind"] for c in d["changes"]}))
    return (
        f"程序按已入账观测算出：{d['object']}·{d['metric']}{scope}：{points}。判断：{found}。"
        "这只是待核实的线索，请回原文核对数值、口径和期间。"
    )


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
    """Record, in the same transaction as the change, that this topic is owed an analysis."""
    if topic.pending_reason is None:
        topic.pending_reason, topic.pending_since = reason, at
    elif reason not in topic.pending_reason.split("+"):
        topic.pending_reason = (topic.pending_reason + "+" + reason)[:200]


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
                    select(db.Observation)
                    .where(db.Observation.topic_id == topic_id)
                    .order_by(db.Observation.available_at.desc())
                    .limit(8)
                )
            )
            last = session.scalar(
                select(db.TopicEvent)
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
            if "series_change" in reason.split("+"):
                question += series_basis(session, topic_id)
            if "analyst_review" in reason.split("+"):
                question += review_basis(session, topic_id, owed_topic.pending_since)
            row = analyses.create(
                question=question,
                direction=topic.direction,
                is_demo=topic.is_demo,
                cutoff=cutoff.isoformat() if cutoff else None,
                trigger="topic",
                topic_id=topic.id,
                parent_id=topic.last_analysis_id,
                seed_records=[
                    {"source": r.source, "record_id": r.record_id, "title": r.title} for r in records
                ],
                session=session,
            )
            topic.last_analysis_id = row.id
            topic.pending_reason = topic.pending_since = None
            event(session, topic, cutoff or db.now(), "analysis", analysis_id=row.id, reason=reason)
        analyses.dispatch(row.id)
        created.append(row.id)
    return created


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
    # v3 3.7：额度只暂停模型阶段。读取、确定性路由、附件待办照常；要模型的记成 triage_deferred 待办。
    docs, ends, backlog = new_documents(fabric, s, is_demo=is_demo, cutoff=cutoff, retry=not paused)
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
