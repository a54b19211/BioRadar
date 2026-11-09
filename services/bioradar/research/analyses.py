"""Create, queue, run and read analyses. Manual questions, follow-ups, topics and replays all use this."""

import hashlib
import json
import logging
import traceback
from contextlib import nullcontext
from datetime import datetime, timedelta

from sqlalchemy import func, select

from bioradar import db, guard, jobs
from bioradar.adapters.fabric import CURRENT, MODES
from bioradar.config import settings
from bioradar.research import lease, propositions, revision, topics, views
from bioradar.research.pipeline import Failed, Run

log = logging.getLogger("bioradar")
LEASE = lease.ANALYSIS_LEASE
DIRECTIONS = {"pipeline", "science", "business", "public_health", "policy"}


class NotFound(Exception):
    pass


class InvalidInput(ValueError):
    pass


class SourceNotSelectable(InvalidInput):
    """A source named for a new analysis that Fabric does not offer for selection (a retired source such as
    legacy_documents, one not readable now, a graph, a misspelling)."""

    def __init__(self, sources):
        super().__init__("source_not_selectable: " + ", ".join(sources))
        self.sources = list(sources)


class CapabilitiesUnavailable(Exception):
    """Fabric's source capabilities cannot be read now: which sources may be chosen is not known."""


def queue():
    return jobs.queue(settings())


def input_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _check_input(direction, method, cutoff, replay_mode):
    """What a new task is refused for before anything is stored. A named replay mode is one of the three;
    current is the live state, so it has no cutoff, and the two replay modes replay a moment, so they need
    one."""
    if direction not in DIRECTIONS:
        raise InvalidInput("direction")
    if method not in {"planner", "baseline"}:
        raise InvalidInput("method")
    if cutoff and datetime.fromisoformat(cutoff).tzinfo is None:
        raise InvalidInput("cutoff_needs_timezone")
    if replay_mode is None:
        return
    if replay_mode not in MODES:
        raise InvalidInput("replay_mode")
    if replay_mode == CURRENT and cutoff:
        raise InvalidInput("replay_mode_current_has_no_cutoff")
    if replay_mode != CURRENT and not cutoff:
        raise InvalidInput("replay_mode_needs_cutoff")


def create(
    *,
    question,
    direction,
    method="planner",
    is_demo=False,
    sources=None,
    cutoff=None,
    replay_mode=None,
    investigation_id=None,
    parent_id=None,
    trigger="manual",
    seed_records=(),
    scope_record_ids=(),
    topic_id=None,
    enqueue=True,
    session=None,
    previous=True,
    series_points=(),
    capabilities=None,
    reassess=(),
) -> db.Analysis:
    """With `session`, the row is added to the caller's transaction and not queued: the caller
    commits it together with its own changes and then calls dispatch(). previous=False leaves the
    parent's report out (the follow-up of a report whose sources cannot be read now).
    capabilities: a function returning Fabric's SourceCapabilities (raising CapabilitiesUnavailable). Given,
    the sources are checked against what Fabric offers for selection: a source named by the caller that is
    not offered is SourceNotSelectable; one that only comes from the configured defaults is left out.
    replay_mode: which materials are visible (fabric.MODES); None names none, so a cutoff means the public
    history reconstruction. Kept with the input and handed on to follow-ups.
    reassess: the propositions this analysis is asked about, [{prop_id, question, reason, triggers}] - a
    new version of what they rest on, or a follow-up about one. It judges those and no others, and its result
    gets the revision section (research/revision.py)."""
    s = settings()
    _check_input(direction, method, cutoff, replay_mode)
    chosen = _selectable(capabilities, list(sources or []), s.demo_sources if is_demo else s.sources)
    value = {
        "question": question.strip(),
        "direction": direction,
        "method": method,
        "is_demo": is_demo,
        "sources": chosen,
        "cutoff": cutoff,
        "seed_records": list(seed_records),
        "scope_record_ids": list(dict.fromkeys(scope_record_ids)),
        # the checked points the question states, each with the check that admitted it (read only)
        "series_points": list(series_points),
        "budget": {"max_steps": s.max_steps, "max_reads": s.max_reads, "max_llm_calls": s.max_llm_calls},
    }
    if replay_mode is not None:  # a task that names none keeps the input (and hash) it always had
        value["replay_mode"] = replay_mode
    if reassess:  # likewise
        value["reassess"] = list(reassess)
    if session is not None:
        enqueue = False
    with nullcontext(session) if session is not None else db.session() as session:
        if investigation_id is None:
            investigation_id = _new_investigation(session, value, topic_id)
        if parent_id and previous:
            earlier = _previous_report(session, parent_id)
            if earlier:
                value["previous_report"] = earlier
        row, existing = _queued(session, investigation_id, parent_id, trigger, method, value)
        if existing:
            return row
    if enqueue:
        dispatch(row.id)
    return row


def _queued(session, investigation_id, parent_id, trigger, method, value) -> tuple[db.Analysis, bool]:
    """(the analysis of this investigation with this input - the one already there, else a new queued row
    added to the session -, whether it was already there)."""
    digest = input_hash(value)
    existing = session.scalar(
        select(db.Analysis).where(
            db.Analysis.investigation_id == investigation_id, db.Analysis.input_hash == digest
        )
    )
    if existing:
        return existing, True
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
    return row, False


def _selectable(capabilities, named: list[str], defaults: list[str]) -> list[str]:
    """The sources of a new analysis: those named, else the configured defaults (a copy)."""
    if capabilities is None:
        return list(named or defaults)
    caps = capabilities()
    chosen, skipped = caps.choose(named or defaults)
    if named and skipped:
        raise SourceNotSelectable(skipped)
    if skipped:
        log.warning("configured sources skipped, Fabric does not offer them for selection: %s", skipped)
    if not chosen:
        raise SourceNotSelectable(skipped)
    return chosen


def _new_investigation(session, value: dict, topic_id) -> str:
    investigation = db.Investigation(
        workspace_id=settings().workspace_id,
        question=value["question"],
        direction=value["direction"],
        is_demo=value["is_demo"],
        topic_id=topic_id,
    )
    session.add(investigation)
    session.flush()
    return investigation.id


def _previous_report(session, parent_id: str) -> dict | None:
    """The parent's report as the follow-up's input, with its verification (a held report is handed on as
    pending: lineage)."""
    parent = session.get(db.Analysis, parent_id)
    if not (parent and parent.result and parent.result.get("draft")):
        return None
    return {
        "draft": parent.result["draft"],
        "date": parent.completed_at.isoformat() if parent.completed_at else None,
        "report_status": parent.result.get("report_status"),
        # the publication gate's answer as the run stored it: a PASS report that lacks a section is held too
        **{k: parent.result[k] for k in ("publishable", "hold_reason") if k in parent.result},
    }


def job_id(analysis_id: str) -> str:
    return f"analysis-{analysis_id}"


def waiting_in_queue(analysis_id: str) -> bool:
    """Whether this analysis's job is still in Redis, waiting or being worked on."""
    from rq.job import NoSuchJobError

    try:
        job = jobs.RadarJob.fetch(
            job_id(analysis_id), connection=queue().connection, serializer=jobs.JSONSerializer
        )
    except NoSuchJobError:
        return False
    return job.get_status(refresh=False) in {"queued", "started", "deferred", "scheduled"}


def dispatch(analysis_id: str):
    guard.check()
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
    """RQ job. A lease keeps two workers from running the same analysis; its token fences a worker that
    outlives the lease: once recover handed the analysis to another worker, nothing this one writes counts."""
    token = lease.new_token()
    with db.session() as session:
        row = session.get(db.Analysis, analysis_id, with_for_update=True)
        if row is None or row.state in {"completed", "failed"}:
            return
        if lease.running(row, db.now()):
            return
        # A queued live topic analysis waits for tomorrow once today's automatic budget is used up.
        if row.trigger == "topic" and not row.input.get("cutoff") and not row.input.get("is_demo"):
            if over_auto_budget():
                return
        row.state = "running"
        lease.take([row], token, db.now(), LEASE)
    try:
        result = Run(row, settings(), lease_token=token).run()
        state, error = "completed", None
    except lease.Lost:
        log.warning("analysis %s: the worker was superseded, its work is dropped", analysis_id)
        return
    except Failed as exc:
        result, state, error = None, "failed", exc.code
    except Exception:  # an unexpected bug: keep the trace for the student, do not loop
        result, state, error = {"trace": traceback.format_exc()[-4000:]}, "failed", "internal_error"
    with db.session() as session:
        if not lease.renew(session, db.Analysis, analysis_id, token, db.now()):
            log.warning("analysis %s: final write rejected, the lease was taken over or ran out", analysis_id)
            return
        row = session.get(db.Analysis, analysis_id)
        lease.release(row)
        row.state, row.error_code, row.completed_at = state, error, db.now()
        if state == "completed":
            propositions.number(session, row, result)  # under the topic's lock, in this transaction
            revision.revise(session, row, result)
        if result is not None:
            marks = (row.result or {}).get("source_updates")  # corrections that arrived during the run
            row.result = {**result, "source_updates": marks} if marks else result


UNKNOWN_OUTCOME = "recovered_unknown_outcome"
FROM_UNKNOWN = (
    "recovered_from_unknown"  # the lease ran out, RQ still listed the job as started, its worker was gone
)
WORKER_ALIVE = (
    "stale_lease_running_worker"  # the lease ran out, but the started job's worker beats: left alone
)
MAX_UNKNOWN_RECOVERIES = 3  # a run that keeps dying with material possibly out is stopped for review


def worker_gone(analysis_id: str) -> bool | None:
    """jobs.started_worker_dead for this analysis's job (a seam the tests replace)."""
    return jobs.started_worker_dead(job_id(analysis_id), settings())


def _unknown_outcomes(session, row) -> int:
    """Outbound records of this analysis left `prepared`: the worker died around a send, so whether the
    material went out is not known (db.MaterialRead). Nothing is guessed: the records stay as they are and
    the resumed run sends again under a new attempt."""
    return (
        session.scalar(
            select(func.count()).where(
                db.MaterialRead.analysis_id == row.id, db.MaterialRead.status == "prepared"
            )
        )
        or 0
    )


def _note(row, code: str, detail: str):
    """Add a note to the run's progress (the run reads it back when it resumes)."""
    progress = dict(row.progress or {})
    progress["notes"] = [*(progress.get("notes") or []), {"code": code, "detail": detail, "stage": row.stage}]
    row.progress = progress


def _requeue(session, row, reason: str | None = None) -> bool:
    """Hand a crashed analysis on to a new worker, or - when its outcome was unknown too often - stop it for
    review. The reason is written to the run's notes either way; returns whether it was sent again."""
    if reason:
        _note(row, reason, "the lease ran out and RQ listed the job as started, but its worker is gone")
    unknown = _unknown_outcomes(session, row)
    if not unknown:
        lease.release(row)
        row.state = "queued"
        return True
    spent = sum(1 for n in (row.progress or {}).get("notes") or [] if n.get("code") == UNKNOWN_OUTCOME)
    stop = spent + 1 >= MAX_UNKNOWN_RECOVERIES
    detail = f"{unknown} outbound records not settled, the material may have gone out"
    _note(row, UNKNOWN_OUTCOME, detail + (" (stopped)" if stop else ""))
    lease.release(row)
    if stop:
        row.state, row.error_code, row.completed_at = "failed", "recovery_unknown_outcome", db.now()
        return False
    row.state = "queued"
    return True


def _judge(row) -> str | None:
    """What recover does with a candidate row: "requeue", "requeue_unknown" (RQ says started, its worker is
    gone), "worker_alive" (started and its worker still beats: left alone, noted), or None (leave it). When
    Redis cannot be asked the row is left: unknown is not dead.
    - a queued row with no lease: lost when its job is not in Redis;
    - a row whose lease ran out: lost when its job is not in Redis; when RQ still lists the job, only a job
      that is started tells more - with a worker gone it is dead, with a worker beating it is alive, and a
      job that is merely waiting will claim the row itself."""
    try:
        if not waiting_in_queue(row.id):
            return "requeue"
        if row.lease_until is None:
            return None
        gone = worker_gone(row.id)
    except Exception:  # noqa: BLE001 - Redis unreachable: not known to be lost
        return None
    if gone is None:
        return None
    return "requeue_unknown" if gone else "worker_alive"


RECOVER_SCAN = 100  # rows looked at per round; the next round goes on from where this one stopped
_scan = {"offset": 0}


def recover(limit=4) -> int:
    """Re-queue analyses whose worker died (lease expired) or whose job never reached Redis.
    A queued row whose job is still waiting in Redis is only behind others: re-queuing it every round
    piled up copies of the same job (24 on the live queue, 2026-11-03). Rows are looked at a bounded
    page at a time, moving on each round, so healthy waiting rows at the front cannot hide a lost one
    behind them. A row whose lease ran out is not taken for dead because the lease ran out: Redis is asked
    (_judge), and when it cannot be asked the row is left alone.

    The rows are locked (FOR UPDATE SKIP LOCKED) from the look to the commit, and the job is sent before
    the commit: a second recover at the same moment skips the rows the first one holds, and one that comes
    after finds the row queued with its job in Redis. Exactly one recover re-queues a given row."""
    stale = db.now() - timedelta(minutes=10)
    lost = 0
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
                .with_for_update(skip_locked=True)
            )
        )
        _scan["offset"] = _scan["offset"] + len(rows) if len(rows) == RECOVER_SCAN else 0
        for row in rows:
            if lost >= limit:
                break
            verdict = _judge(row)
            if verdict == "worker_alive":
                last = ((row.progress or {}).get("notes") or [{}])[-1]
                if last.get("code") != WORKER_ALIVE:  # once, not every round
                    _note(
                        row, WORKER_ALIVE, "the lease ran out but the worker of the started job still beats"
                    )
                    log.warning("analysis %s: lease ran out, its worker still beats; left to run", row.id)
            elif verdict in ("requeue", "requeue_unknown"):
                if _requeue(session, row, FROM_UNKNOWN if verdict == "requeue_unknown" else None):
                    dispatch(row.id)
                lost += 1
    return lost


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
        "result": views.result(row.result) if full else views.summary(row.result),
    }
    if full:
        value["progress"] = views.progress(row.progress)
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


def reassessing(parent: db.Analysis, prop_id: str) -> list[dict]:
    """The `reassess` of a follow-up about one proposition, once the proposition is checked to be one the
    parent analysis stands on: its own, or one of the rounds before it. The follow-up inherits the parent's
    sources, scope, mode and cutoff like any other, so naming a proposition widens nothing it may read."""
    with db.session() as session:
        found = propositions.standing(session, parent.id).get(prop_id)
    if found is None:
        raise InvalidInput("about_not_in_analysis")
    return [{"prop_id": prop_id, "question": found["question"], "reason": "followup", "triggers": []}]


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
    at = db.now()
    with db.session() as session:
        if action == "set_alert":
            topic = _set_alert(session, user, topic_id, alert, at)
            analysis_id = sentence = evidence_ref = None
        else:
            row = session.get(db.Analysis, analysis_id) if analysis_id else None
            if row is None:
                raise NotFound(analysis_id or "analysis_id")
            sentence, evidence_ref = _reviewed_part(session, row, action, sentence, evidence_ref)
            investigation = session.get(db.Investigation, row.investigation_id)
            topic = session.get(db.Topic, investigation.topic_id) if investigation.topic_id else None
            if action in {"reject", "flag_source"} and topic is not None:
                topics.owe(topic, "analyst_review", at)
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


def _set_alert(session, user: db.User, topic_id, alert, at) -> db.Topic:
    topic = session.get(db.Topic, topic_id) if topic_id else None
    if topic is None or alert is None:
        raise InvalidInput("set_alert 需要存在的 topic_id 和 alert")
    if topic.alert != alert:
        topics.event(session, topic, at, "alert", before=topic.alert, after=alert, by=user.name)
        topic.alert = alert
    return topic


def _reviewed_part(session, row: db.Analysis, action, sentence, evidence_ref):
    """(sentence, evidence_ref) a decision is about, checked against the analysis: flag_source names one
    of its materials, confirm / reject one sentence of its BioCheck review."""
    if action == "flag_source":
        found = session.scalar(
            select(db.Evidence.id).where(
                db.Evidence.analysis_id == row.id, db.Evidence.ref == (evidence_ref or "")
            )
        )
        if found is None:
            raise InvalidInput(f"这份分析没有材料 {evidence_ref}")
        return None, evidence_ref
    claims = ((row.result or {}).get("review") or {}).get("claims") or []
    if sentence not in {c.get("index") for c in claims}:
        raise InvalidInput(f"这份报告没有第 {sentence} 句")
    return sentence, None


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
