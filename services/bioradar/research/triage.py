"""Monitoring input: new Fabric records, routed by program rules or triaged by the model in batches into
topics, with the measures they state handed to measurement checking."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from bioradar import db, guard
from bioradar.adapters import llm
from bioradar.adapters.fabric import NOT_VISIBLE, Fabric, FabricError, noticed
from bioradar.config import Settings, settings
from bioradar.material import of
from bioradar.research import analyses, continuation, measure_checks, prompts, schemas
from bioradar.research.clock import parse_time
from bioradar.research.topics import ALERTS, WEIGHT, activity, activity_key, event, owe, set_state, watches

log = logging.getLogger("bioradar")
_turn = 0  # rotates which monitored source is read first in a live round
BATCH = 10
TRIAGE_CHARACTERS = continuation.WINDOW  # of each record's text, per triage call; the rest: continuation
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
        return _route_attachment(doc)
    if doc.get("registry_diff") is None or doc.get("retry"):  # no diff declared for the source, or a retry
        return None
    return _route_registry(doc)


def _route_attachment(doc) -> str | None:
    state = doc.get("attachment_text") or {}
    if state.get("status") == "parsed" and (doc.get("text") or "").strip():
        return None
    if state.get("status") == "unsupported" or (
        state.get("status") == "failed" and (state.get("attempt") or 0) >= ATTACHMENT_MAX_ATTEMPTS
    ):
        return "attachment_unparsed"
    return "attachment_pending"


def _route_registry(doc) -> str | None:
    """A trial registry record: settled by Fabric's diff against the previous version, or (first adoption)
    triaged only when the registry says it is new or just changed in a way worth checking."""
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
    room = s.monitor_max_records
    ends, more = {}, False
    with db.session() as session:
        docs = _owed_again(session, fabric, sources, is_demo=is_demo, cutoff=cutoff, room=room, retry=retry)
        if cutoff is not None:
            docs += _visible_at(session, fabric, sources, is_demo=is_demo, cutoff=cutoff)
        else:
            ends, more = _feed(session, fabric, sources, docs, is_demo=is_demo, room=room)
    unique = {d["version_id"]: d for d in docs}
    return sorted(unique.values(), key=lambda d: parse_time(d["available_at"])), ends, more


def _owed_again(session, fabric, sources, *, is_demo, cutoff, room, retry):
    """Records still owed a triage (the call failed, or the daily automatic budget deferred it) are read
    again (live mode: the change cursor moved on), using at most half of the round. Not while the
    budget is used up: they would only be deferred again.
    Owed work is on one fixed version: read that version, not the record's current head (an
    older deferred version read by record id never got done and blocked the queue head)."""
    docs, failed = [], {}
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
    return docs


def _visible_at(session, fabric, sources, *, is_demo, cutoff):
    """Replay: everything visible at the cutoff that was not observed yet."""
    docs = []
    for source in sources:
        page = None
        while True:
            items, page = fabric.search(source, (), is_demo=is_demo, cutoff=cutoff, cursor=page, limit=100)
            docs += unseen(session, items)
            if not page:
                break
    return docs


def _cursor_ends(session, fabric, sources, *, is_demo):
    """source -> where its change feed was read up to; a source with no cursor starts at its current end."""
    ends = {}
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
    return ends


class _Scan:
    """One round's reading of the change feeds, within the room of the round."""

    def __init__(self, session, fabric, docs, ends, *, is_demo):
        self.session, self.fabric, self.docs, self.ends, self.is_demo = session, fabric, docs, ends, is_demo
        self.scanned = len(docs)
        self.more = set()  # sources that still have records after what was scanned

    def take(self, source, limit):
        """Scan up to `limit` records of one feed; ends[source] moves past what was scanned."""
        got = 0
        while got < limit:
            at = self.ends[source]
            try:
                items, after = self.fabric.changes(
                    source, is_demo=self.is_demo, after=at, limit=min(100, limit - got)
                )
            except FabricError as exc:
                if exc.code not in NOT_VISIBLE:
                    raise
                # this source is not readable now (revoked): its cursor stays, the other feeds go on
                log.info("monitor: %s not readable now (%s), skipped this round", source, exc.code)
                self.more.discard(source)
                return
            self.docs.extend(unseen(self.session, items))
            got += len(items)
            self.scanned += len(items)
            if after == at:
                self.more.discard(source)
                return
            self.ends[source] = after
        self.more.add(source)


def _feed(session, fabric, sources, docs, *, is_demo, room):
    """Live: the new records of every change feed, a different source first each round. Appends to `docs`;
    returns (cursor ends, whether a source still has more)."""
    global _turn
    ends = _cursor_ends(session, fabric, sources, is_demo=is_demo)
    sources = [x for x in sources if x in ends]
    scan = _Scan(session, fabric, docs, ends, is_demo=is_demo)
    _turn += 1
    k = _turn % max(1, len(sources))
    order = sources[k:] + sources[:k]  # a different source goes first each round
    share = max(1, (room - scan.scanned) // max(1, len(order)))
    for source in order:
        scan.take(source, min(share, max(0, room - scan.scanned)))
    for source in [x for x in order if x in scan.more]:
        if scan.scanned >= room:
            break
        scan.take(source, room - scan.scanned)
    return ends, bool(scan.more)


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
    each record's window, in order - the send each candidate was read from."""
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
        with guard.audit(), db.session() as session:
            for r in session.scalars(select(db.MaterialRead).where(db.MaterialRead.id.in_(ids))):
                r.status = "failed"
        raise
    db.settle_call(
        call, ids, stage="triage"
    )  # audit of what was sent, also after the leader guard was revoked
    guard.check()  # revoked meanwhile: the answer is not used
    items = {item.index: item for item in (result.items if result else []) if 1 <= item.index <= len(docs)}
    return [items.get(i) for i in range(1, len(docs) + 1)]


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
        _record_batch(s, batch, decisions, topics, watch, reads, wake, is_demo=is_demo)
    return wake, True


def _record_batch(s, batch, decisions, topics, watch, reads, wake, *, is_demo):
    """One batch's decisions in one transaction: observations, the topics they touch and their candidates."""
    with db.session() as session:
        topics = [session.get(db.Topic, t.id) for t in topics]
        flag_corrections(batch, is_demo=is_demo, session=session)  # same transaction as the observations
        touched = {}  # topic id -> latest record time in this batch, for the activity baseline
        for n, (doc, item) in enumerate(zip(batch, decisions)):
            at = parse_time(doc["available_at"])
            alert = item.alert if item else "none"
            topic = _topic_for(session, s, item, topics, alert, at, doc, is_demo)
            session.execute(
                delete(db.Observation).where(
                    db.Observation.version_id == doc["version_id"],
                    db.Observation.reason.in_(RETRYABLE),
                )
            )
            session.add(_observation(s, doc, item, topic, alert, at, is_demo))
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
            if item:  # its first window was read: the windows after it are owed
                continuation.plan(session, s, doc, topic, item.direction, at, is_demo)
            if topic is None:
                continue
            _touch_topic(session, topic, item, doc, alert, at, watch, wake)
            touched[topic.id] = max(touched.get(topic.id, at), at)
        session.flush()
        for topic_id, at in sorted(touched.items()):
            if activity(session, s, session.get(db.Topic, topic_id), at, is_demo):
                wake[topic_id] = "activity_anomaly"


def _topic_for(session, s, item, topics, alert, at, doc, is_demo):
    """The topic a decision puts the record on: the one the model numbered, an existing topic of the same
    new-topic title, or a new topic (only for a record that raises an alert); None when it joins none."""
    if item and item.topic and 1 <= item.topic <= len(topics):
        return topics[item.topic - 1]
    if not (item and item.new_topic and alert != "none"):
        return None
    title = item.new_topic[:200]
    for existing in topics:
        if existing.title == title:
            return existing
    topic = db.Topic(
        workspace_id=s.workspace_id,
        title=title,
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
    return topic


def _observation(s, doc, item, topic, alert, at, is_demo):
    return db.Observation(
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


def _touch_topic(session, topic, item, doc, alert, at, watch, wake):
    """What one observed record does to its topic: waking a cooling one, heat, alert, closure, a watch hit."""
    if topic.state in {"cooling", "dormant"} and not (item and item.closure):
        topic.awakenings += 1
        topic.closed = False
        set_state(session, topic, "rising", at, reason="new_related_record", record=doc["record_id"])
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
    hit = item.watch if item and item.watch in {w["id"] for w in watch.get(topic.id, [])} else None
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
