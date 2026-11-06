"""T-26: operator-run, bounded upkeep of existing state - never a cleared cursor or a fresh database.

upgrade_reads (Codex 708): the versions of one source read from another processor's cleaned artifact get a
reading plan of `processor`'s artifact of the same raw version. The old plan, its windows, attempts and
material reads stay as they are; the new plan owes every window, the first too (triage read the old
artifact), so none of the old coverage counts for the new artifact. A version is planned only when Fabric
serves the new artifact processed and bound to this version (otherwise the run records why and tries it
again once its cursor is through). Points read from the new artifact take the place of the same points of the
old one (monitor.hand_over: artifact_upgrade; a changed value is told to the topic). Once the new plan is
complete and its candidates checked, the old artifact's points it did not give again retire
(not_in_upgraded_artifact); a capped, failed or paused new plan keeps the old points and says
upgrade_incomplete. Triage (topics, alerts) is not redone and no observation is added.

backfill_corrections (Codex r167, migration 0010 started empty): the corrections and retractions stated by
the records seen before record_corrections existed are registered from their fixed versions, dated at the
record's own time, through the same idempotent monitor.record_corrections.

Each call handles at most `limit` items and records every outcome (upkeep_items, append-only); a Fabric
that does not answer stops the call with the cursor before the item. A run is continued by id."""

from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters.fabric import CURRENT, NOT_VISIBLE, FabricError
from bioradar.config import Settings
from bioradar.material import of
from bioradar.research import continuation, measure_checks, monitor

OPEN_CHECKS = ("queued", "waiting")
UNSETTLED = ("planned", "upgrade_incomplete")  # an upgrade whose old points are not settled yet
RETRY = ("artifact_not_ready",)  # tried again once the cursor is through


def _run(session, s: Settings, kind, is_demo, run_id, params):
    if run_id is not None:
        run = session.get(db.UpkeepRun, run_id)
        if run is None or (run.kind, run.is_demo, run.workspace_id) != (kind, is_demo, s.workspace_id):
            raise ValueError(f"no {kind} run {run_id} in this workspace")
        return run
    run = db.UpkeepRun(
        workspace_id=s.workspace_id, is_demo=is_demo, kind=kind, params=params, cursor=0, counts={}
    )
    session.add(run)
    session.flush()
    return run


def _item(session, run, key, outcome, **detail):
    session.add(db.UpkeepItem(run_id=run.id, key=str(key), outcome=outcome, detail=detail))
    counts = dict(run.counts or {})
    counts[outcome] = counts.get(outcome, 0) + 1
    run.counts, run.updated_at = counts, db.now()


def latest(session, run):
    """{key: (outcome, detail)}: each item's latest outcome in the run."""
    out = {}
    for it in session.scalars(
        select(db.UpkeepItem).where(db.UpkeepItem.run_id == run.id).order_by(db.UpkeepItem.id)
    ):
        out[it.key] = (it.outcome, it.detail)
    return out


def summary(session, run):
    items = {}
    for outcome, _ in latest(session, run).values():
        items[outcome] = items.get(outcome, 0) + 1
    return {
        "run_id": run.id,
        "kind": run.kind,
        "state": run.state,
        "cursor": run.cursor,
        "params": run.params,
        "items": items,  # by each item's latest outcome
        "recorded": run.counts,  # every outcome recorded, retries included
    }


# ---- upgrade_reads -------------------------------------------------------------------------------------


def _old_plans(s, source, processor, is_demo, after):
    return (
        select(db.RecordRead)
        .where(
            db.RecordRead.workspace_id == s.workspace_id,
            db.RecordRead.is_demo == is_demo,
            db.RecordRead.source == source,
            db.RecordRead.id > after,
            db.RecordRead.superseded_by_id.is_(None),
            db.RecordRead.processor_version.is_not(None),
            db.RecordRead.processor_version != processor,
        )
        .order_by(db.RecordRead.id)
    )


def _readable(fabric, old, processor, is_demo, now):
    """(outcome, detail, stop): ("plan", {"doc": ...}) when Fabric serves `processor`'s artifact of the
    version processed, whole and bound to it; stop when Fabric did not answer (the cursor stays)."""

    def failed(exc):
        if isinstance(exc, FabricError) and exc.code in NOT_VISIBLE:
            return "not_visible", {"code": exc.code}, False
        if isinstance(exc, FabricError) and exc.code == "version_identity_mismatch":
            return "version_identity_mismatch", {}, False
        return "fabric_unavailable", {"error": getattr(exc, "code", type(exc).__name__)}, True

    try:
        found = fabric.clean_texts(
            old.source,
            [old.version_id],
            is_demo=is_demo,
            mode=CURRENT,
            cutoff=now.isoformat(),
            processor_version=processor,
        )
    except Exception as exc:  # noqa: BLE001 - classified: not visible, mismatch, or Fabric not answering
        return failed(exc)
    entry = (found or {}).get(old.version_id) or {}
    clean, state = entry.get("clean") or {}, entry.get("state")
    if state in NOT_VISIBLE or state in ("not_visible", "not_visible_or_not_processed"):
        return "not_visible", {"state": state}, False
    if state != "processed" or clean.get("status") != "parsed" or clean.get("processor_version") != processor:
        # queued, parsing, failed, or another processor's text: never planned as if it were ready
        return (
            "artifact_not_ready",
            {
                "state": state,
                "status": clean.get("status"),
                "processor_version": clean.get("processor_version"),
            },
            False,
        )
    text, sha = clean.get("text") or "", clean.get("text_sha256")
    if not text.strip() or measure_checks.sha256(text) != sha:
        return "artifact_changed", {"text_sha256": sha}, False
    if db.artifact_key(processor, sha) == old.artifact_key:
        return "already_current", {}, False
    try:
        docs, missing = fabric.read_versions(
            old.source,
            [(old.record_id, old.version_id)],
            is_demo=is_demo,
            now=now,
            artifacts={old.version_id: {"processor_version": processor, "text_sha256": sha}},
        )
    except Exception as exc:  # noqa: BLE001
        return failed(exc)
    if missing or not docs:
        return "not_visible", {"missing": list(missing)}, False
    doc = docs[0]
    part, why = continuation._clean_part(of(doc))
    if (
        doc.get("clean_text_state") != "processed"
        or part is None
        or part.get("processor_version") != processor
        or part.get("text_sha256") != sha
    ):
        return "not_planned", {"why": why or "artifact_not_bound"}, False
    return "plan", {"doc": doc}, False


def _settle_upgrades(s, run_id, is_demo, limit):
    """Old points of upgrades whose new plan is read and checked: the ones not given again retire."""
    with db.session() as session:
        run = session.get(db.UpkeepRun, run_id)
        for key, (outcome, d) in list(latest(session, run).items()):
            if outcome not in UNSETTLED or limit <= 0:
                continue
            new = session.get(db.RecordRead, d["new_read"])
            if new.state == "reading":
                continue
            if new.state != "complete":
                if outcome != "upgrade_incomplete" or d.get("state") != new.state:
                    # read only in part: the old points stay, and the run says so (read on with extend)
                    _item(
                        session,
                        run,
                        key,
                        "upgrade_incomplete",
                        **{**d, "state": new.state, "reason": new.reason},
                    )
                    limit -= 1
                continue
            candidates = select(db.MeasurementCandidate.id).where(
                db.MeasurementCandidate.workspace_id == s.workspace_id,
                db.MeasurementCandidate.is_demo == is_demo,
                db.MeasurementCandidate.version_id == new.version_id,
                db.MeasurementCandidate.processor_version == new.processor_version,
                db.MeasurementCandidate.text_sha256 == new.text_sha256,
            )
            open_checks = session.scalar(
                select(func.count())
                .select_from(db.MeasurementCheckTask)
                .where(
                    db.MeasurementCheckTask.candidate_id.in_(candidates),
                    db.MeasurementCheckTask.state.in_(OPEN_CHECKS),
                )
            )
            if open_checks:
                continue  # its points may still hand over
            points = select(db.Measurement).where(
                db.Measurement.workspace_id == s.workspace_id,
                db.Measurement.is_demo == is_demo,
                db.Measurement.version_id == new.version_id,
            )
            left = list(
                session.scalars(
                    points.where(
                        db.Measurement.artifact_key == d["old_artifact"],
                        db.Measurement.retired_reason.is_(None),
                    )
                )
            )
            at = db.now()
            for m in left:
                m.retired_reason, m.retired_at = "not_in_upgraded_artifact", at
            for topic_id in sorted({m.domain_id for m in left if m.domain_basis == "topic"}):
                topic = session.get(db.Topic, topic_id)
                if topic is not None:
                    monitor.event(
                        session,
                        topic,
                        at,
                        "artifact_points_retired",
                        version=new.version_id,
                        artifact=d["old_artifact"],
                        by_artifact=new.artifact_key,
                        retired=[m.id for m in left if m.domain_id == topic_id],
                    )
            handed = session.scalars(
                points.where(
                    db.Measurement.artifact_key == d["old_artifact"],
                    db.Measurement.retired_reason == "artifact_upgrade",
                )
            ).all()
            new_points = session.scalars(points.where(db.Measurement.artifact_key == new.artifact_key)).all()
            _item(
                session,
                run,
                key,
                "settled",
                **d,
                state=new.state,
                handed_over=sorted(m.id for m in handed),
                retired_not_given=sorted(m.id for m in left),
                new_points=sorted(m.id for m in new_points),
            )
            limit -= 1


def upgrade_reads(
    s: Settings, fabric, *, is_demo, source=None, processor=None, limit=20, run_id=None, now=None
):
    """One bounded step of an upgrade run (see the module text). Returns the run's summary."""
    now = now or db.now()
    with db.session() as session:
        run = _run(session, s, "upgrade_reads", is_demo, run_id, {"source": source, "processor": processor})
        run_id, source, processor = run.id, run.params["source"], run.params["processor"]
        todo = list(session.scalars(_old_plans(s, source, processor, is_demo, run.cursor).limit(limit)))
        fresh = bool(todo)
        if not fresh:  # the cursor is through: what was not ready is tried again
            waiting = [int(k) for k, (o, _) in latest(session, run).items() if o in RETRY][:limit]
            todo = [r for r in (session.get(db.RecordRead, k) for k in waiting) if r is not None]
        for r in todo:
            session.expunge(r)
    for old in todo:
        outcome, detail, stop = _readable(fabric, old, processor, is_demo, now)
        if stop:
            with db.session() as session:
                _item(
                    session,
                    session.get(db.UpkeepRun, run_id),
                    old.id,
                    outcome,
                    version_id=old.version_id,
                    **detail,
                )
            break  # Fabric did not answer: the cursor stays before this plan
        with db.session() as session:
            run = session.get(db.UpkeepRun, run_id)
            cur = session.get(db.RecordRead, old.id)
            if outcome != "plan":
                _item(session, run, old.id, outcome, version_id=old.version_id, **detail)
            elif cur.superseded_by_id is not None:
                _item(
                    session,
                    run,
                    old.id,
                    "already_upgraded",
                    version_id=old.version_id,
                    by=cur.superseded_by_id,
                )
            else:
                new = continuation.plan(
                    session, s, detail["doc"], None, cur.direction, cur.observed_at, is_demo, supersedes=cur
                )
                if new is None:
                    _item(session, run, old.id, "already_planned", version_id=old.version_id)
                else:
                    _item(
                        session,
                        run,
                        old.id,
                        "planned",
                        version_id=old.version_id,
                        old_read=cur.id,
                        new_read=new.id,
                        old_artifact=cur.artifact_key,
                        new_artifact=new.artifact_key,
                        windows=new.windows_total,
                    )
            if fresh:
                run.cursor = max(run.cursor, old.id)
    _settle_upgrades(s, run_id, is_demo, limit)
    with db.session() as session:
        run = session.get(db.UpkeepRun, run_id)
        more = session.scalar(_old_plans(s, source, processor, is_demo, run.cursor).limit(1))
        open_items = [o for o, _ in latest(session, run).values() if o in UNSETTLED + RETRY]
        if more is None and not open_items and run.state != "done":
            run.state, run.finished_at = "done", db.now()
        return summary(session, run)


# ---- backfill_corrections ------------------------------------------------------------------------------


def backfill_corrections(s: Settings, fabric, *, is_demo, limit=50, run_id=None, now=None):
    """One bounded step of a correction backfill run (see the module text). Returns the run's summary."""
    now = now or db.now()
    with db.session() as session:
        run = _run(session, s, "backfill_corrections", is_demo, run_id, {})
        run_id = run.id
        seen = list(
            session.scalars(
                select(db.Observation)
                .where(
                    db.Observation.workspace_id == s.workspace_id,
                    db.Observation.is_demo == is_demo,
                    db.Observation.id > run.cursor,
                )
                .order_by(db.Observation.id)
                .limit(limit)
            )
        )
        for o in seen:
            session.expunge(o)
    for o in seen:
        try:
            docs, missing = fabric.read_versions(
                o.source, [(o.record_id, o.version_id)], is_demo=is_demo, now=now
            )
        except FabricError as exc:
            if exc.code != "version_identity_mismatch":
                with db.session() as session:
                    _item(
                        session, session.get(db.UpkeepRun, run_id), o.id, "fabric_unavailable", code=exc.code
                    )
                break  # the cursor stays before this observation
            docs, missing = [], ["version_identity_mismatch"]
        except Exception as exc:  # noqa: BLE001 - Fabric not answering: stop here
            with db.session() as session:
                _item(
                    session,
                    session.get(db.UpkeepRun, run_id),
                    o.id,
                    "fabric_unavailable",
                    error=type(exc).__name__,
                )
            break
        with db.session() as session:
            run = session.get(db.UpkeepRun, run_id)
            if not docs:
                _item(session, run, o.id, "not_visible", version_id=o.version_id, missing=list(missing))
            else:
                stated = [
                    r for r in docs[0].get("relations") or [] if r.get("kind") in ("corrects", "retracts")
                ]
                made = monitor.record_corrections(session, s, docs, is_demo=is_demo) if stated else 0
                outcome = "recorded" if made else "already_recorded" if stated else "no_correction"
                _item(session, run, o.id, outcome, version_id=o.version_id, relations=len(stated), made=made)
            run.cursor = max(run.cursor, o.id)
    with db.session() as session:
        run = session.get(db.UpkeepRun, run_id)
        more = session.scalar(
            select(db.Observation.id)
            .where(
                db.Observation.workspace_id == s.workspace_id,
                db.Observation.is_demo == is_demo,
                db.Observation.id > run.cursor,
            )
            .limit(1)
        )
        if more is None and run.state != "done":
            run.state, run.finished_at = "done", db.now()
        return summary(session, run)
