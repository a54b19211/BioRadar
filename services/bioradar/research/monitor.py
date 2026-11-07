"""Continuous monitoring, one round at a time (tick): new Fabric records -> routing and triage -> topics
-> Fabric's measurement objects into the topics' series -> topic states -> automatic analyses. The parts are
research/triage.py, series_sync.py (docs/decisions/0010), topics.py and scheduling.py."""

import logging
from datetime import datetime

from bioradar import db
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import analyses, series_sync
from bioradar.research.clock import parse_time
from bioradar.research.revision import arrive
from bioradar.research.scheduling import start_analyses
from bioradar.research.topics import advance
from bioradar.research.triage import (
    BACKFILL_SOURCES,
    BACKGROUND,
    DEFERRED,
    defer,
    defer_backfill_source,
    monitored_sources,
    new_documents,
    observe,
    route,
    settle_attachments,
    settle_background,
)

log = logging.getLogger("bioradar")


def tick(*, is_demo=False, cutoff: datetime | None = None, clock: datetime | None = None, fabric=None):
    """One monitoring round. `clock` moves topic states during blank periods with no new records.
    Live real monitoring pauses new triage and automatic analyses once today's automatic tokens reach
    daily_auto_tokens; the backlog stays in the feed and owed analyses stay owed. The round starts by reading
    what Fabric declares its sources can do; without that it reads nothing and moves no cursor."""
    s = settings()
    fabric = fabric or Fabric(s)
    if fabric.load_capabilities() is None:
        return _without_capabilities((clock or cutoff or db.now()).isoformat())
    return _round(s, fabric, is_demo=is_demo, cutoff=cutoff, clock=clock)


def _round(s, fabric, *, is_demo, cutoff, clock):
    budget = cutoff is None and not is_demo  # live real monitoring only
    paused = budget and analyses.over_auto_budget()
    skipped = monitored_sources(s, fabric.caps, is_demo)[1]
    # v3 3.7：额度只暂停模型阶段。读取、确定性路由、附件待办照常；要模型的记成 triage_deferred 待办。
    for source in skipped:
        log.warning(
            "monitor: %s is not offered by Fabric for selection (source_not_selectable), skipped", source
        )
    docs, ends, backlog = new_documents(fabric, s, is_demo=is_demo, cutoff=cutoff, retry=not paused)
    background, news, set_apart = _set_background_apart(s, fabric, docs, is_demo)
    arrive(fabric, news, is_demo=is_demo)  # what new versions mean for the propositions that rest on them
    routed, triage_docs = _route_documents(news, is_demo)
    routed.update(set_apart)
    if paused:
        log.info("monitor: daily_auto_tokens reached; model triage deferred, program routing continues")
        reasons, whole = {}, False
    else:
        reasons, whole = (
            observe(s, triage_docs, is_demo=is_demo, budget=budget, background=background)
            if triage_docs
            else ({}, True)
        )
    if not whole:
        # the budget ran out before or during triage: what was not triaged is owed, durably, and the
        # cursors move on (the retry path triages it once the budget allows)
        paused = True
        deferred = defer(triage_docs, is_demo=is_demo)
        if deferred:
            routed[DEFERRED] = routed.get(DEFERRED, 0) + deferred
    if not paused:  # the objects of the versions placed now, in case their events are not in the feed yet
        series_sync.link_versions(s, fabric, triage_docs, is_demo=is_demo, cutoff=cutoff)
    measured = series_sync.sync(s, fabric, is_demo=is_demo, cutoff=cutoff)  # no model call: never paused
    _move_cursors(ends, docs, is_demo)
    latest = max([parse_time(d["available_at"]) for d in docs], default=None)
    clock = clock or cutoff or latest or db.now()
    rising = advance(s, clock, is_demo=is_demo, background=background)
    reasons.update({k: v for k, v in rising.items() if k not in reasons})
    created = (
        start_analyses(s, is_demo=is_demo, cutoff=cutoff, budget=budget)
        if s.auto_analysis and not paused
        else []
    )
    return {
        "source_not_selectable": skipped,
        "records": len(docs),
        # how many records went to model triage and how many the program settled, by reason
        "routed": routed,
        "backlog": backlog,  # more new records are waiting in the feed than one round takes
        "paused": "daily_auto_tokens" if paused else None,
        "clock": clock.isoformat(),
        "topics": reasons,
        "measurements": measured,
        "analyses": created,
    }


def _set_background_apart(s, fabric, docs, is_demo):
    """(the background versions, the records that are news, how many records the round set apart, by reason).
    Backfilled versions are observed and nothing more; while a receipt is unreadable the backfill source's
    other versions are put off a round, as triage_deferred."""
    background, unread = _background(s, fabric)
    news = settle_background(docs, background, is_demo=is_demo)
    apart = {BACKGROUND: len(docs) - len(news)}
    if unread:
        news, apart[DEFERRED] = defer_backfill_source(news, is_demo=is_demo)
    return background, news, {reason: count for reason, count in apart.items() if count}


def _background(s, fabric) -> tuple[set[str], list[str]]:
    """(the versions the configured Fabric backfill runs created, from their receipts: background, settled by
    the program and kept out of the topics' counts as research/groups.py keeps them out of its own; the runs
    whose receipt cannot be read). With a run unread the round cannot tell the backfill source's new versions
    from a backfill, so it does not treat them as news (defer_backfill_source)."""
    if not s.backfill_run_ids:
        return set(), []
    background, unread = fabric.background_version_ids(s.backfill_run_ids)
    if unread:
        log.warning(
            "monitor: backfill runs %s: receipt unreadable, the new versions of %s wait for the next round",
            unread,
            sorted(BACKFILL_SOURCES),
        )
    return background, unread


def _without_capabilities(clock):
    """A round when Fabric cannot say which sources may be read and what text to expect of them: nothing is
    read and no cursor moves, so the round is simply done again once Fabric can say."""
    log.warning("monitor: Fabric's source capabilities cannot be read, the round does nothing")
    return {
        "records": 0,
        "routed": {},
        "backlog": False,
        "paused": "source_capabilities_unavailable",
        "clock": clock,
        "topics": {},
        "analyses": [],
        "source_not_selectable": [],
    }


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
