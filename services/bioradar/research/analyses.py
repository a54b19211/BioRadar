"""Create, queue, run and read analyses. Manual questions, follow-ups, topics and replays all use this."""

import hashlib
import json
import traceback
from datetime import datetime, timedelta

from redis import Redis
from rq import Queue
from sqlalchemy import select

from bioradar import db
from bioradar.config import settings
from bioradar.research.pipeline import Failed, Run

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
) -> db.Analysis:
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
    with db.session() as session:
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
        if parent_id:
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
    if enqueue:
        dispatch(row.id)
    return row


def dispatch(analysis_id: str):
    queue().enqueue(
        "bioradar.research.analyses.execute",
        analysis_id,
        job_timeout=int(LEASE.total_seconds()),
        result_ttl=3600,
        failure_ttl=86400,
    )


def execute(analysis_id: str):
    """RQ job. A lease keeps two workers from running the same analysis."""
    with db.session() as session:
        row = session.get(db.Analysis, analysis_id, with_for_update=True)
        if row is None or row.state in {"completed", "failed"}:
            return
        if row.lease_until and row.lease_until > db.now():
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
            row.result = result


def recover(limit=4) -> int:
    """Re-queue analyses whose worker died (lease expired) or whose job never reached Redis."""
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
                .limit(limit)
            )
        )
        for row in rows:
            row.lease_until = None
            row.state = "queued"
    for row in rows:
        dispatch(row.id)
    return len(rows)


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
                    "finish_reason": c.finish_reason,
                    "seconds": c.seconds,
                    "error": c.error,
                }
                for c in session.scalars(
                    select(db.LlmCall).where(db.LlmCall.analysis_id == row.id).order_by(db.LlmCall.id)
                )
            ]
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
    return {"ref": row.ref, "title": row.title, "text": row.text, "version_id": row.version_id, **row.meta}


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
