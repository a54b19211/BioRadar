"""Automatic analyses: which topics are owed one now, and what each is handed - the series it rests on,
the corrections and record contradictions since its last analysis, the analysts' reviews."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from bioradar import db
from bioradar.config import Settings
from bioradar.research import analyses, measure_history, prompts
from bioradar.research.clock import parse_time
from bioradar.research.topics import ALERTS, event, owed_at

log = logging.getLogger("bioradar")


def series_basis_points(session, topic_id, at=None) -> list:
    """The structured record of the points the latest series event used, each with whether it is current and
    its latest check (series_basis states them only when all are current). As of `at` (a cutoff): the latest
    series event visible then, never one computed later."""
    last = session.scalar(
        visible(select(db.TopicEvent), db.TopicEvent.at, at)
        .where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "series")
        .order_by(db.TopicEvent.id.desc())
        .limit(1)
    )
    used = sorted({i for c in (last.detail["changes"] if last else []) for i in c.get("rows", [])})
    return measure_history.series_points(session, used, at) if used else []  # current or not, with its state


def series_basis(session, topic_id, at=None) -> str:
    """The latest computed series change of a topic in words, for the analysis it triggers. The series events
    recorded by the radar's own chain from before Fabric took the measurements over name frozen points, which
    are history and never a current conclusion: they give no basis."""
    return ""


def visible(query, column, at):
    """The query limited to what was visible at the cutoff `at` (None: now)."""
    return query if at is None else query.where(column <= at)


def handed_events(session, topic_id):
    """The ids of the events earlier analyses of the topic were handed."""
    out = set()
    for e in session.scalars(
        select(db.TopicEvent).where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "analysis")
    ):
        out.update(e.detail.get("handed_events") or [])
    return out


def correction_batch(session, topic_id, since, at=None, limit=10):
    """(events, handed ids, earliest time of what is left over or None) of the topic's correction events not
    handed to an analysis yet, as of the cutoff: at most `limit` correcting record versions, oldest first; the
    repeats of one in the batch are handed with it."""
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
    of the record contradictions a topic is owed an analysis for: each record at the version
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
    """The correcting records themselves, as sources the analysis must read: a correction that
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
    """The corrections Fabric stated for this topic's points, as of the cutoff: which
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
        reason = owed_topic.pending_reason
        later = []
        if cutoff is not None:
            # a replay takes only the debt visible at its cutoff; what is owed later stays owed
            with db.session() as session:
                visible_reasons, later = owed_at(session, session.get(db.Topic, owed_topic.id), cutoff)
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
            row = _start_one(session, owed_topic, reason, later, cutoff)
        analyses.dispatch(row.id)
        created.append(row.id)
    return created


def _start_one(session, owed_topic, reason, later, cutoff):
    """The analysis a topic's debt asks for, created in the caller's transaction together with the change of
    the debt itself and the topic's events."""
    topic_id = owed_topic.id
    topic = session.get(db.Topic, topic_id)
    question, seeds = _base_question(session, topic, reason, cutoff)
    question, seeds, points, handed, leftover = _bases(session, owed_topic, reason, cutoff, question, seeds)
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
    _settle_debt(topic, reason, later, leftover, cutoff)
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
    return row


def _base_question(session, topic, reason, cutoff):
    """The question of the signal the topic's last record showed, and the topic's latest records as seeds."""
    records = list(
        session.scalars(
            visible(select(db.Observation), db.Observation.available_at, cutoff)
            .where(db.Observation.topic_id == topic.id)
            .order_by(db.Observation.available_at.desc())
            .limit(8)
        )
    )
    last = session.scalar(
        visible(select(db.TopicEvent), db.TopicEvent.at, cutoff)
        .where(db.TopicEvent.topic_id == topic.id, db.TopicEvent.kind == "observed")
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
    seeds = [{"source": r.source, "record_id": r.record_id, "title": r.title} for r in records]
    return question, seeds


def _bases(session, owed_topic, reason, cutoff, question, seeds):
    """What each reason of the debt adds to the question, the seeds and the series points; returns
    (question, seeds, points, events handed over, events left over with the time they stay owed from)."""
    topic_id = owed_topic.id
    reasons = reason.split("+")
    points = []
    if "series_change" in reasons:
        question += series_basis(session, topic_id, cutoff)
        points = series_basis_points(session, topic_id, cutoff)
    if "analyst_review" in reasons:
        question += review_basis(session, topic_id, owed_topic.pending_since)
    handed, leftover = [], {}  # what this analysis is given; what stays owed, from when
    corrected = [r for r in reasons if r in {"cross_domain_correction", "source_corrected"}]
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
    if "record_conflict" in reasons:
        text, pinned, ids, given, left = conflict_basis(session, topic_id, owed_topic.pending_since, cutoff)
        handed += given
        if left is not None:
            leftover["record_conflict"] = left
        question += text
        seeds += pinned  # the version that contradicts itself, read as that version (fixed read)
        known = {p["measurement_id"] for p in points}
        points = list(points) + [
            p for p in measure_history.series_points(session, ids, cutoff) if p["measurement_id"] not in known
        ]
    return question, seeds, points, handed, leftover


def _settle_debt(topic, reason, later, leftover, cutoff):
    """Clear the debt this analysis answers; what is owed later, or was not handed over, stays owed."""
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
