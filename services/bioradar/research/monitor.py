"""Continuous monitoring, one round at a time (tick): new Fabric records -> routing and triage -> topics
-> measurement checks and series -> topic states -> automatic analyses. The parts are research/triage.py,
topics.py, series_store.py and scheduling.py."""

import logging
from datetime import datetime

from bioradar import db
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import analyses, continuation, measure_checks
from bioradar.research.clock import parse_time
from bioradar.research.scheduling import start_analyses
from bioradar.research.series_store import adopt, record_corrections
from bioradar.research.topics import advance
from bioradar.research.triage import (
    DEFERRED,
    defer,
    new_documents,
    observe,
    route,
    settle_attachments,
    triage,
)

log = logging.getLogger("bioradar")


def tick(*, is_demo=False, cutoff: datetime | None = None, clock: datetime | None = None, fabric=None):
    """One monitoring round. `clock` moves topic states during blank periods with no new records.
    Live real monitoring pauses new triage and automatic analyses once today's automatic tokens reach
    daily_auto_tokens; the backlog stays in the feed and owed analyses stay owed."""
    s = settings()
    fabric = fabric or Fabric(s)
    budget = cutoff is None and not is_demo  # live real monitoring only
    paused = budget and analyses.over_auto_budget()
    _prepare(s, fabric, is_demo)
    # v3 3.7：额度只暂停模型阶段。读取、确定性路由、附件待办照常；要模型的记成 triage_deferred 待办。
    docs, ends, backlog = new_documents(fabric, s, is_demo=is_demo, cutoff=cutoff, retry=not paused)
    with db.session() as session:  # a stated correction counts from when it is seen, triaged or not
        record_corrections(session, s, docs, is_demo=is_demo)
    routed, triage_docs = _route_documents(docs, is_demo)
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
    if not paused:  # continuation windows are model calls too
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
    _move_cursors(ends, docs, is_demo)
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


def _prepare(s, fabric, is_demo):
    """What the round owes before it reads anything new."""
    with db.session() as session:
        measure_checks.backfill(session, s)  # a checker upgrade owes every candidate a new check
        measure_checks.legacy_backlog(session, s)  # points stored before checking owe a re-extraction
    # current read permission and binding of every stored point, before anything this round reads a series
    measure_checks.refresh_bindings(s, fabric, is_demo=is_demo)


def _route_documents(docs, is_demo):
    """(records by where they went - triage or the program's reason; the records to triage)."""
    routed = {}
    for d in docs:
        reason = route(d)
        routed[reason or "triage"] = routed.get(reason or "triage", 0) + 1
    triage_docs = [d for d in docs if route(d) is None]
    settle_attachments(docs, is_demo=is_demo)
    if len(triage_docs) < len(docs):
        log.info("monitor routing %s", routed)
    return routed, triage_docs


def _move_cursors(ends, docs, is_demo):
    """The feeds' cursors move past what the round took. A round that died before this reads the same records
    again."""
    fresh = {d["source"] for d in docs if not d.get("retry")}  # a retried old record is not new
    with db.session() as session:
        for source, at in ends.items():
            cursor = session.get(db.Cursor, (source, is_demo))
            cursor.after_seq = at
            if source in fresh:
                cursor.last_new_at = db.now()
