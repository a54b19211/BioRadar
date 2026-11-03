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

from sqlalchemy import delete, func, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import Fabric
from bioradar.config import Settings, settings
from bioradar.research import analyses, prompts, schemas, series

log = logging.getLogger("bioradar")
_turn = 0  # rotates which monitored source is read first in a live round
ALERTS = ["none", "normal", "important", "urgent"]
WEIGHT = {"none": 0, "normal": 1, "important": 2, "urgent": 3}
BATCH = 10


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


def unseen(session, items):
    """Items whose version has not been observed yet; a failed triage counts as not observed.
    Only this page's versions are queried, so a round costs the same however long the history is."""
    if not items:
        return []
    done = set(
        session.scalars(
            select(db.Observation.version_id).where(
                db.Observation.version_id.in_([d["version_id"] for d in items]),
                db.Observation.reason != "triage_failed",
            )
        )
    )
    return [d for d in items if d["version_id"] not in done]


def new_documents(fabric: Fabric, s: Settings, *, is_demo: bool, cutoff: datetime | None):
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
        # Records whose triage call failed are triaged again (live mode: the change cursor moved on),
        # using at most a quarter of the round.
        failed = {}
        for source, record_id in session.execute(
            select(db.Observation.source, db.Observation.record_id)
            .where(
                db.Observation.is_demo == is_demo,
                db.Observation.reason == "triage_failed",
                db.Observation.source.in_(sources),  # same source set as this round
            )
            .limit(max(1, room // 4))
        ):
            failed.setdefault(source, []).append(record_id)
        for source, ids in failed.items():
            items, _ = fabric.search(
                source, (), is_demo=is_demo, cutoff=cutoff, record_ids=ids, limit=len(ids)
            )
            docs += [dict(d, retry=True) for d in unseen(session, items)]
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


KEY = (db.Measurement.object, db.Measurement.metric, db.Measurement.unit, db.Measurement.kind, db.Measurement.scope)


def known_series(session, s: Settings, is_demo: bool, limit=40):
    """The most recently measured object/metric/unit keys, shown to triage so names stay the same."""
    rows = session.execute(
        select(*KEY)
        .where(db.Measurement.workspace_id == s.workspace_id, db.Measurement.is_demo == is_demo)
        .group_by(*KEY)
        .order_by(func.max(db.Measurement.available_at).desc())
        .limit(limit)
    ).all()
    return [dict(zip(("object", "metric", "unit", "kind", "scope"), r)) for r in rows]


def triage(s: Settings, docs, topics, watch=None, known=None):
    watch = watch or {}
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
                "text": d["text"][:1500],
            }
            for i, d in enumerate(docs, 1)
        ],
    }
    result, call = llm.chat_json(s, "triage", prompts.TRIAGE, payload, schemas.Triage)
    with db.session() as session:
        u = call["usage"]
        session.add(
            db.LlmCall(
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
        )
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
            known = known_series(session, s, is_demo)
        decisions = triage(s, batch, topics, watch, known)
        with db.session() as session:
            topics = [session.get(db.Topic, t.id) for t in topics]
            flag_corrections(batch, is_demo=is_demo, session=session)  # same transaction as the observations
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
                        db.Observation.reason == "triage_failed",
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
                        reason=item.reason if item else "triage_failed",
                    )
                )
                changed = measure(session, s, doc, item.measures[:3] if item else [], topic, at, is_demo)
                if topic is None:
                    continue
                if changed:
                    wake[topic.id] = "series_change"
                    owe(topic, "series_change", at)
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
    return wake, True


def measure(session, s: Settings, doc, measures, topic, at, is_demo) -> bool:
    """Store the quoted measures of one record and compare each with its series as visible at `at`.
    A correction of an earlier record replaces that record's value for the same object/metric/period.
    Writes a `series` topic event per change; returns whether any is notable (owes an analysis)."""
    text = re.sub(r"\s+", "", (doc["title"] or "") + (doc["text"] or ""))
    corrected = [
        (r.get("target_source", doc["source"]), r["target_record_id"])
        for r in doc["relations"]
        if r["kind"] == "corrects"
    ]
    notable = False
    for m in measures:
        quote = re.sub(r"\s+", "", m.quote)
        bounds = series.period_bounds(m.period, at)
        number = f"{m.value:g}"  # the value itself must be written in the quote, not just some real text
        if not quote or quote not in text or number not in quote or bounds is None:
            log.info("measure dropped: %s %s %s", doc["record_id"], m.metric, number)
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
        for source, record_id in corrected:
            for old in session.scalars(
                select(db.Measurement).filter_by(
                    source=source, record_id=record_id, period=row.period, **same
                )
            ):
                old.superseded_by = row.id
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
        ]
        found = [c for c in series.changes(rows, row.id) if c["kind"] != "repeat"]
        if topic is not None and found:
            event(
                session,
                topic,
                at,
                "series",
                record=doc["record_id"],
                **{("measure_kind" if k == "kind" else k): v for k, v in key.items()},
                value=m.value,
                period=row.period,
                changes=found,
                algo=series.VERSION,
            )
        notable = notable or any(c["kind"] in series.NOTABLE for c in found)
    return notable


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
    if paused:
        log.info("monitor paused: daily_auto_tokens reached")
        docs, ends, backlog = [], {}, True
    else:
        docs, ends, backlog = new_documents(fabric, s, is_demo=is_demo, cutoff=cutoff)
    reasons, whole = observe(s, docs, is_demo=is_demo, budget=budget) if docs else ({}, True)
    if not whole:
        # stopped mid-round: keep the cursors, the next round re-reads and skips what was triaged
        paused, ends, backlog = True, {}, True
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
