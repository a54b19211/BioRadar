"""Create, queue, run and read analyses. Manual questions, follow-ups, topics and replays all use this."""

import hashlib
import json
import traceback
from contextlib import nullcontext
from datetime import datetime, timedelta

from redis import Redis
from rq import Queue
from sqlalchemy import func, select

from bioradar import db
from bioradar.config import settings
from bioradar.research.pipeline import Failed, Run, UnknownLineage, lineage, lineage_sources  # noqa: F401

LEASE = timedelta(minutes=90)
DIRECTIONS = {"pipeline", "science", "business", "public_health"}


class NotFound(Exception):
    pass


class InvalidInput(ValueError):
    pass


def queue():
    return Queue(settings().queue_name, connection=Redis.from_url(settings().redis_url))


def input_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create(
    *,
    question,
    direction,
    method="planner",
    is_demo=False,
    sources=None,
    cutoff=None,
    investigation_id=None,
    parent_id=None,
    trigger="manual",
    seed_records=(),
    scope_record_ids=(),
    topic_id=None,
    enqueue=True,
    session=None,
    previous=True,
) -> db.Analysis:
    """With `session`, the row is added to the caller's transaction and not queued: the caller
    commits it together with its own changes and then calls dispatch(). previous=False leaves the
    parent's report out (the follow-up of a report whose sources cannot be read now)."""
    s = settings()
    if direction not in DIRECTIONS:
        raise InvalidInput("direction")
    if method not in {"planner", "baseline"}:
        raise InvalidInput("method")
    if cutoff and datetime.fromisoformat(cutoff).tzinfo is None:
        raise InvalidInput("cutoff_needs_timezone")
    value = {
        "question": question.strip(),
        "direction": direction,
        "method": method,
        "is_demo": is_demo,
        "sources": list(sources or (s.demo_sources if is_demo else s.sources)),
        "cutoff": cutoff,
        "seed_records": list(seed_records),
        "scope_record_ids": list(dict.fromkeys(scope_record_ids)),
        "budget": {"max_steps": s.max_steps, "max_reads": s.max_reads, "max_llm_calls": s.max_llm_calls},
    }
    if session is not None:
        enqueue = False
    with nullcontext(session) if session is not None else db.session() as session:
        if investigation_id is None:
            investigation = db.Investigation(
                workspace_id=s.workspace_id,
                question=value["question"],
                direction=direction,
                is_demo=is_demo,
                topic_id=topic_id,
            )
            session.add(investigation)
            session.flush()
            investigation_id = investigation.id
        if parent_id and previous:
            parent = session.get(db.Analysis, parent_id)
            if parent and parent.result and parent.result.get("draft"):
                value["previous_report"] = {
                    "draft": parent.result["draft"],
                    "date": parent.completed_at.isoformat() if parent.completed_at else None,
                }
        digest = input_hash(value)
        existing = session.scalar(
            select(db.Analysis).where(
                db.Analysis.investigation_id == investigation_id, db.Analysis.input_hash == digest
            )
        )
        if existing:
            return existing
        row = db.Analysis(
            investigation_id=investigation_id,
            parent_id=parent_id,
            trigger=trigger,
            method=method,
            input=value,
            input_hash=digest,
            progress={},
            stage="plan",
            state="queued",
        )
        session.add(row)
        session.flush()
    if enqueue:
        dispatch(row.id)
    return row


def job_id(analysis_id: str) -> str:
    return f"analysis-{analysis_id}"


def waiting_in_queue(analysis_id: str) -> bool:
    """Whether this analysis's job is still in Redis, waiting or being worked on."""
    from rq.job import Job, NoSuchJobError

    try:
        job = Job.fetch(job_id(analysis_id), connection=queue().connection)
    except NoSuchJobError:
        return False
    return job.get_status(refresh=False) in {"queued", "started", "deferred", "scheduled"}


def dispatch(analysis_id: str):
    queue().enqueue(
        "bioradar.research.analyses.execute",
        analysis_id,
        job_id=job_id(analysis_id),
        job_timeout=int(LEASE.total_seconds()),
        result_ttl=3600,
        failure_ttl=86400,
    )


def auto_tokens_today() -> int:
    """Tokens spent today (UTC) on automatic work: live triage plus topic-triggered analyses."""
    start = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with db.session() as session:
        triage_tokens = session.scalar(
            select(func.coalesce(func.sum(db.LlmCall.total_tokens), 0)).where(
                db.LlmCall.stage == "triage", db.LlmCall.created_at >= start
            )
        )
        topic_tokens = session.scalar(
            select(func.coalesce(func.sum(db.LlmCall.total_tokens), 0))
            .join(db.Analysis, db.Analysis.id == db.LlmCall.analysis_id)
            .where(db.Analysis.trigger == "topic", db.LlmCall.created_at >= start)
        )
    return int(triage_tokens) + int(topic_tokens)


def over_auto_budget() -> bool:
    return auto_tokens_today() >= settings().daily_auto_tokens


def execute(analysis_id: str):
    """RQ job. A lease keeps two workers from running the same analysis."""
    with db.session() as session:
        row = session.get(db.Analysis, analysis_id, with_for_update=True)
        if row is None or row.state in {"completed", "failed"}:
            return
        if row.lease_until and row.lease_until > db.now():
            return
        # A queued live topic analysis waits for tomorrow once today's automatic budget is used up.
        if row.trigger == "topic" and not row.input.get("cutoff") and not row.input.get("is_demo"):
            if over_auto_budget():
                return
        row.state, row.lease_until = "running", db.now() + LEASE
    try:
        result = Run(row, settings()).run()
        state, error = "completed", None
    except Failed as exc:
        result, state, error = None, "failed", exc.code
    except Exception:  # an unexpected bug: keep the trace for the student, do not loop
        result, state, error = {"trace": traceback.format_exc()[-4000:]}, "failed", "internal_error"
    with db.session() as session:
        row = session.get(db.Analysis, analysis_id)
        row.state, row.error_code, row.lease_until, row.completed_at = state, error, None, db.now()
        if result is not None:
            marks = (row.result or {}).get("source_updates")  # corrections that arrived during the run
            row.result = {**result, "source_updates": marks} if marks else result


RECOVER_SCAN = 100  # rows looked at per round; the next round goes on from where this one stopped
_scan = {"offset": 0}


def recover(limit=4) -> int:
    """Re-queue analyses whose worker died (lease expired) or whose job never reached Redis.
    A queued row whose job is still waiting in Redis is only behind others: re-queuing it every round
    piled up copies of the same job (24 on the live queue, 2026-11-03). Rows are looked at a bounded
    page at a time, moving on each round, so healthy waiting rows at the front cannot hide a lost one
    behind them; when Redis cannot be asked, a row is left alone (unknown is not lost)."""
    stale = db.now() - timedelta(minutes=10)
    with db.session() as session:
        rows = list(
            session.scalars(
                select(db.Analysis)
                .where(
                    db.Analysis.state.in_(["queued", "running"]),
                    (db.Analysis.lease_until.is_(None) & (db.Analysis.created_at < stale))
                    | (db.Analysis.lease_until < db.now()),
                )
                .order_by(db.Analysis.created_at, db.Analysis.id)
                .offset(_scan["offset"])
                .limit(RECOVER_SCAN)
            )
        )
        _scan["offset"] = _scan["offset"] + len(rows) if len(rows) == RECOVER_SCAN else 0
        lost = []
        for row in rows:
            if len(lost) >= limit:
                break
            if row.lease_until is not None:  # the worker died while running it
                lost.append(row)
                continue
            try:
                if not waiting_in_queue(row.id):
                    lost.append(row)
            except Exception:  # noqa: BLE001 - Redis unreachable: not known to be lost
                continue
        for row in lost:
            row.lease_until = None
            row.state = "queued"
    for row in lost:
        dispatch(row.id)
    return len(lost)


def view(row: db.Analysis, *, full=False) -> dict:
    value = {
        "id": row.id,
        "investigation_id": row.investigation_id,
        "parent_id": row.parent_id,
        "trigger": row.trigger,
        "method": row.method,
        "state": row.state,
        "stage": row.stage,
        "error_code": row.error_code,
        "question": row.input["question"],
        "direction": row.input["direction"],
        "is_demo": row.input["is_demo"],
        "cutoff": row.input.get("cutoff"),
        "created_at": row.created_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "result": row.result
        if full
        else {k: (row.result or {}).get(k) for k in ("report_status", "usage", "assessment")},
    }
    if full:
        p = row.progress or {}
        value["progress"] = {k: p.get(k) for k in ("checks", "queries", "actions", "facts", "stop", "notes")}
        with db.session() as session:
            value["evidence"] = [
                {
                    "ref": e.ref,
                    "source": e.source,
                    "record_id": e.record_id,
                    "version_id": e.version_id,
                    "title": e.title,
                    "published_at": e.published_at,
                    "available_at": e.available_at,
                    "material_kind": e.material_kind,
                    "verified": e.verified,
                    "url": e.meta.get("url"),
                }
                for e in session.scalars(
                    select(db.Evidence).where(db.Evidence.analysis_id == row.id).order_by(db.Evidence.id)
                )
            ]
            value["calls"] = [
                {
                    "stage": c.stage,
                    "service": c.service,
                    "model": c.model,
                    "tokens": c.total_tokens,
                    "requests": c.requests,
                    "finish_reason": c.finish_reason,
                    "seconds": c.seconds,
                    "error": c.error,
                }
                for c in session.scalars(
                    select(db.LlmCall).where(db.LlmCall.analysis_id == row.id).order_by(db.LlmCall.id)
                )
            ]
            value["reviews"] = reviews(analysis_id=row.id, limit=200, session=session)
    return value


def get(analysis_id: str) -> db.Analysis:
    with db.session() as session:
        row = session.get(db.Analysis, analysis_id)
    if row is None:
        raise NotFound(analysis_id)
    return row


def evidence_text(analysis_id: str, ref: str) -> dict:
    with db.session() as session:
        row = session.scalar(
            select(db.Evidence).where(db.Evidence.analysis_id == analysis_id, db.Evidence.ref == ref)
        )
    if row is None:
        raise NotFound(ref)
    return {
        "ref": row.ref,
        "source": row.source,
        "record_id": row.record_id,
        "title": row.title,
        "text": row.text,
        "version_id": row.version_id,
        **row.meta,
    }


def review_view(r: db.Review, name: str) -> dict:
    return {
        "id": r.id,
        "by": name,
        "action": r.action,
        "analysis_id": r.analysis_id,
        "sentence": r.sentence,
        "evidence_ref": r.evidence_ref,
        "topic_id": r.topic_id,
        "alert": r.alert,
        "note": r.note,
        "created_at": r.created_at.isoformat(),
    }


def review(
    user: db.User,
    *,
    action,
    analysis_id=None,
    sentence=None,
    evidence_ref=None,
    topic_id=None,
    alert=None,
    note="",
) -> dict:
    """Record a person's decision. confirm / reject name a BioCheck sentence of a completed analysis,
    flag_source one of its materials, set_alert a topic's alert. reject and flag_source owe the analysis's
    topic a new analysis (analyst_review); an analysis without a topic only keeps the record."""
    from bioradar.research import monitor  # monitor imports this module

    at = db.now()
    with db.session() as session:
        topic = None
        if action == "set_alert":
            topic = session.get(db.Topic, topic_id) if topic_id else None
            if topic is None or alert is None:
                raise InvalidInput("set_alert 需要存在的 topic_id 和 alert")
            if topic.alert != alert:
                monitor.event(session, topic, at, "alert", before=topic.alert, after=alert, by=user.name)
                topic.alert = alert
            analysis_id = sentence = evidence_ref = None
        else:
            row = session.get(db.Analysis, analysis_id) if analysis_id else None
            if row is None:
                raise NotFound(analysis_id or "analysis_id")
            if action == "flag_source":
                found = session.scalar(
                    select(db.Evidence.id).where(
                        db.Evidence.analysis_id == row.id, db.Evidence.ref == (evidence_ref or "")
                    )
                )
                if found is None:
                    raise InvalidInput(f"这份分析没有材料 {evidence_ref}")
                sentence = None
            else:
                claims = ((row.result or {}).get("review") or {}).get("claims") or []
                if sentence not in {c.get("index") for c in claims}:
                    raise InvalidInput(f"这份报告没有第 {sentence} 句")
                evidence_ref = None
            investigation = session.get(db.Investigation, row.investigation_id)
            topic = session.get(db.Topic, investigation.topic_id) if investigation.topic_id else None
            if action in {"reject", "flag_source"} and topic is not None:
                monitor.owe(topic, "analyst_review", at)
            alert = None
        record = db.Review(
            user_id=user.id,
            action=action,
            analysis_id=analysis_id,
            sentence=sentence,
            evidence_ref=evidence_ref,
            topic_id=topic.id if topic else None,
            alert=alert,
            note=note,
            created_at=at,
        )
        session.add(record)
        session.flush()
        return review_view(record, user.name)


def reviews(*, analysis_id=None, topic_id=None, limit=50, session=None) -> list[dict]:
    with nullcontext(session) if session is not None else db.session() as session:
        query = (
            select(db.Review, db.User.name)
            .join(db.User, db.User.id == db.Review.user_id)
            .order_by(db.Review.id.desc())
            .limit(limit)
        )
        if analysis_id:
            query = query.where(db.Review.analysis_id == analysis_id)
        if topic_id:
            query = query.where(db.Review.topic_id == topic_id)
        return [review_view(r, name) for r, name in session.execute(query).all()]


def wait_until_done(analysis_id: str, timeout=3600):
    """Used by scripts that run analyses inline (scenarios, replay)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = get(analysis_id)
        if row.state in {"completed", "failed"}:
            return row
        time.sleep(3)
    return get(analysis_id)
