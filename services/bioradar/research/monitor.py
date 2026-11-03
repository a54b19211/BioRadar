"""Continuous monitoring: new Fabric records -> triage -> topics -> automatic analyses.

Topic states follow the source clock (when records became available), so a simulation or a
historical replay behaves the same way as live monitoring:
  emerging --(3 records or an important alert)--> rising
  rising --(no new record for topic_cool_days)--> cooling
  cooling --(topic_dormant_days without records, or a closure notice)--> dormant
  cooling / dormant --(a new related record)--> rising again (an awakening)"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import Fabric
from bioradar.config import Settings, settings
from bioradar.research import analyses, prompts, schemas

log = logging.getLogger("bioradar")
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


def new_documents(fabric: Fabric, s: Settings, *, is_demo: bool, cutoff: datetime | None):
    """Live: the change feed after the saved cursor.
    Replay: everything visible at the cutoff that was not observed yet."""
    sources = s.demo_sources if is_demo else s.sources
    docs, ends = [], {}
    with db.session() as session:
        rows = session.execute(
            select(
                db.Observation.source,
                db.Observation.record_id,
                db.Observation.version_id,
                db.Observation.reason,
            ).where(db.Observation.is_demo == is_demo)
        ).all()
        seen = {r.version_id for r in rows if r.reason != "triage_failed"}
        # Records whose triage call failed are triaged again (live mode: the change cursor moved on).
        failed = {}
        for r in rows:
            if r.reason == "triage_failed":
                failed.setdefault(r.source, []).append(r.record_id)
        for source, ids in failed.items():
            items, _ = fabric.search(
                source, (), is_demo=is_demo, cutoff=cutoff, record_ids=ids, limit=len(ids)
            )
            docs += [d for d in items if d["version_id"] not in seen]
        for source in sources:
            if cutoff is None:
                cursor = session.get(db.Cursor, (source, is_demo))
                if cursor is None:
                    # A real feed starts at its current end (legacy_documents alone holds 600k records);
                    # history is for replays. Demo feeds are scenario data and start at 0.
                    start = 0 if is_demo else fabric.head(source, is_demo=is_demo)
                    cursor = db.Cursor(source=source, is_demo=is_demo, after_seq=start)
                    session.add(cursor)
                at = cursor.after_seq
                while True:
                    items, after = fabric.changes(source, is_demo=is_demo, after=at)
                    docs += [d for d in items if d["version_id"] not in seen]
                    if after == at:
                        break
                    at = after
                ends[source] = at  # saved by tick() only after these records were triaged
            else:
                page = None
                while True:
                    items, page = fabric.search(
                        source, (), is_demo=is_demo, cutoff=cutoff, cursor=page, limit=100
                    )
                    docs += [d for d in items if d["version_id"] not in seen]
                    if not page:
                        break
    unique = {d["version_id"]: d for d in docs}
    return sorted(unique.values(), key=lambda d: parse_time(d["available_at"])), ends


def triage(s: Settings, docs, topics):
    payload = {
        "topics": [{"index": i, "title": t.title, "state": t.state} for i, t in enumerate(topics, 1)],
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


def observe(s: Settings, docs, *, is_demo: bool):
    """Triage new documents and attach them to topics. Returns topics that need an analysis."""
    wake = {}
    for start in range(0, len(docs), BATCH):
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
        decisions = triage(s, batch, topics)
        with db.session() as session:
            topics = [session.get(db.Topic, t.id) for t in topics]
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
                event(
                    session,
                    topic,
                    at,
                    "observed",
                    record=doc["record_id"],
                    alert=alert,
                    closure=bool(item and item.closure),
                )
    return wake


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


def start_analyses(s: Settings, *, is_demo: bool, cutoff: datetime | None):
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
            question = (
                f"专题“{topic.title}”出现新进展（{reason}）。基于目前可见的材料，现在应当怎样判断？"
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
                seed_records=[{"source": r.source, "record_id": r.record_id, "title": r.title} for r in records],
                session=session,
            )
            topic.last_analysis_id = row.id
            topic.pending_reason = topic.pending_since = None
            event(session, topic, cutoff or db.now(), "analysis", analysis_id=row.id, reason=reason)
        analyses.dispatch(row.id)
        created.append(row.id)
    return created


def tick(*, is_demo=False, cutoff: datetime | None = None, clock: datetime | None = None, fabric=None):
    """One monitoring round. `clock` moves topic states during blank periods with no new records."""
    s = settings()
    fabric = fabric or Fabric(s)
    docs, ends = new_documents(fabric, s, is_demo=is_demo, cutoff=cutoff)
    reasons = observe(s, docs, is_demo=is_demo) if docs else {}
    with db.session() as session:  # a round that died before this line reads the same records again
        for source, at in ends.items():
            session.get(db.Cursor, (source, is_demo)).after_seq = at
    latest = max([parse_time(d["available_at"]) for d in docs], default=None)
    clock = clock or cutoff or latest or db.now()
    reasons.update({k: v for k, v in advance(s, clock, is_demo=is_demo).items() if k not in reasons})
    created = start_analyses(s, is_demo=is_demo, cutoff=cutoff) if s.auto_analysis else []
    return {"records": len(docs), "clock": clock.isoformat(), "topics": reasons, "analyses": created}


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
