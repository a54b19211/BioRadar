"""Topics: their state machine on the source clock, their events, what they are owed, and the topic
view. States follow when records became available, so a simulation or a historical replay behaves the
same way as live monitoring:
  emerging --(3 records or an important alert)--> rising
  rising --(no new record for topic_cool_days)--> cooling
  cooling --(topic_dormant_days without records, or a closure notice)--> dormant
  cooling / dormant --(a new related record)--> rising again (an awakening)
Versions a Fabric backfill run created are background, not news: the monitor puts them in no topic, and the
counts here leave out any that are in one."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, literal, select
from sqlalchemy.orm import object_session

from bioradar import db
from bioradar.adapters.fabric import LEAD_SOURCES
from bioradar.config import Settings
from bioradar.research import views
from bioradar.research.clock import parse_time

ALERTS = ["none", "normal", "important", "urgent"]
WEIGHT = {"none": 0, "normal": 1, "important": 2, "urgent": 3}


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


def event(session, topic, at, kind, **detail):
    session.add(db.TopicEvent(topic_id=topic.id, at=at, kind=kind, detail=detail))


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


def previous_days(received, today: str) -> list[str] | None:
    """The BASELINE_DAYS days before `today` on which the monitor received anything; days with nothing
    received (collection stopped or missing) are left out, not counted as zero. None when there are fewer:
    not judged."""
    before = [d for d in sorted(received) if d < today][-BASELINE_DAYS:]
    return before if len(before) == BASELINE_DAYS else None


def baseline(values) -> tuple[float, float, float]:
    """(mean, standard deviation, threshold) of the daily counts of those days; above mean + 3 standard
    deviations (mean + 3 when they are all equal) is an anomaly."""
    mean = sum(values) / len(values)
    spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    return mean, spread, (mean + 3 * spread if spread else mean + 3)


def activity(session, s: Settings, topic, at, is_demo, background=frozenset()) -> bool:
    """A naive baseline of information activity (not case incidence): the topic's records on the day
    of `at`, each counted once by its activity_key, against the previous 7 days the monitor received anything
    at all; days with no records from any source (collection stopped or missing) are left out, not zero.
    Above mean + 3 standard deviations (mean + 3 when they are all equal) is an anomaly; fewer than 7
    days is not judged. Owes the topic an analysis once per day.
    `background`: the versions Fabric backfill runs created. They are no news: never counted, and a day on
    which only they arrived is not a day the monitor received anything (the rule of research/groups.py). The
    same goes for the leads of a news index (LEAD_SOURCES): they arrive every hour and are no topic's records,
    so a day with only leads is not a day with news."""
    day = func.date(db.Observation.available_at)
    scope = (
        db.Observation.workspace_id == s.workspace_id,
        db.Observation.is_demo == is_demo,
        db.Observation.available_at >= at - timedelta(days=4 * BASELINE_DAYS),
        db.Observation.available_at <= at,
        db.Observation.version_id.not_in(list(background)),
        db.Observation.source.not_in(list(LEAD_SOURCES)),
    )
    received = sorted(str(d) for d in session.scalars(select(day).where(*scope).distinct()))
    today = str(at.astimezone(UTC).date())  # the database groups by UTC day
    before = previous_days(received, today)
    if before is None:
        return False
    itself = literal("record:") + db.Observation.source + literal(":") + db.Observation.record_id
    counted = func.count(func.distinct(func.coalesce(db.Observation.activity_key, itself)))
    counts = {
        str(d): n
        for d, n in session.execute(
            select(day, counted).where(*scope, db.Observation.topic_id == topic.id).group_by(day)
        ).all()
    }
    mean, spread, threshold = baseline([counts.get(d, 0) for d in before])
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
    also as a dated "owed" event, so an analysis at a cutoff can take only the reasons visible then."""
    if topic.pending_reason is None:
        topic.pending_reason, topic.pending_since = reason, at
    elif reason not in topic.pending_reason.split("+"):
        topic.pending_reason = (topic.pending_reason + "+" + reason)[:200]
    # every occurrence is dated, also of a reason already owed: an answer at a cutoff between two occurrences
    # must not settle the later one
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


def advance(s: Settings, clock: datetime, *, is_demo: bool, background=frozenset()):
    """Move topic states forward to the given source clock. Returns topics that started rising. `background`:
    the versions Fabric backfill runs created; they are not among the records that make a topic rise."""
    rising = {}
    with db.session() as session:
        for topic in session.scalars(select(db.Topic).where(db.Topic.is_demo == is_demo)):
            idle = clock - parse_time(topic.last_activity_at)
            records = db.Observation.topic_id == topic.id, db.Observation.version_id.not_in(list(background))
            count = session.scalar(select(func.count()).where(*records)) or 0
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


def level_gates(session, topics) -> dict[str, dict]:
    """topic id -> `views.level_publication` of its level, from the stored outcome of its latest analysis."""
    ids = {t.last_analysis_id for t in topics if t.last_analysis_id}
    latest = {}
    if ids:
        query = select(db.Analysis.id, db.Analysis.state, db.Analysis.result).where(db.Analysis.id.in_(ids))
        latest = {i: (state, result) for i, state, result in session.execute(query)}
    return {
        t.id: views.level_publication(latest.get(t.last_analysis_id), bool(t.pending_reason))
        for t in topics
    }


def observation_view(o: db.Observation) -> dict:
    """A record the monitor saw, as it leaves the radar: the lead notice, and the program's own status when it
    settled the record. The triage model's alert and reason are not shown."""
    return {
        "title": o.title,
        "source": o.source,
        "record_id": o.record_id,
        "available_at": o.available_at.isoformat(),
        "lead": views.lead(o.source, o.title, o.available_at),
        "alert": None,
        "closure": o.closure,
        "reason": o.reason if views.is_program_reason(o.reason) else "",
    }


def event_view(e: db.TopicEvent) -> dict:
    """A topic event as it leaves the radar: the triage model's verdict on a record (alert, signal, objects)
    is not shown; the record, the closure flag and what the program recorded are."""
    detail = {**e.detail, "alert": None, "signal": "", "objects": []} if e.kind == "observed" else e.detail
    return {"at": e.at.isoformat(), "kind": e.kind, **detail}


def topic_view(topic: db.Topic, gate: dict, *, events=False) -> dict:
    """A topic as it leaves the radar. `gate` (level_gates) decides its level and summary: until its latest
    analysis passed they are not shown, and `hold` says why."""
    value = {
        "id": topic.id,
        "title": topic.title,
        "direction": topic.direction,
        "is_demo": topic.is_demo,
        "state": topic.state,
        "alert": topic.alert if gate["publishable"] else None,
        **gate,
        "heat": topic.heat,
        "awakenings": topic.awakenings,
        "closed": topic.closed,
        "first_seen_at": topic.first_seen_at.isoformat(),
        "last_activity_at": topic.last_activity_at.isoformat(),
        "last_analysis_id": topic.last_analysis_id,
        "pending_reason": topic.pending_reason,
        "summary": topic.summary if gate["publishable"] else "",
    }
    if events:
        with db.session() as session:
            value["events"] = [
                event_view(e)
                for e in session.scalars(
                    select(db.TopicEvent).where(db.TopicEvent.topic_id == topic.id).order_by(db.TopicEvent.id)
                )
            ]
            value["observations"] = [
                observation_view(o)
                for o in session.scalars(
                    select(db.Observation)
                    .where(db.Observation.topic_id == topic.id)
                    .order_by(db.Observation.available_at)
                )
            ]
    return value
