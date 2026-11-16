"""The radar's own measurement chain from before Fabric took it over, read only
(docs/decisions/0010-fabric-measurements.md).

Before the cut-over the radar extracted measures itself. Those tables are kept, unchanged and frozen
(migration 0023); nothing writes them and no analysis reads them. What is left here is the reading-plan
coverage the history page (/api/measure-coverage) shows."""

from sqlalchemy import func, select

from bioradar import db


def checker_versions(session, read):
    """The checker versions of the latest checks recorded for the candidates of the plan's version, as stored
    (sorted, one of each); empty when nothing of the version was checked."""
    candidates = select(db.MeasurementCandidate.id).where(
        db.MeasurementCandidate.workspace_id == read.workspace_id,
        db.MeasurementCandidate.is_demo == read.is_demo,
        db.MeasurementCandidate.version_id == read.version_id,
    )
    latest = (
        select(func.max(db.MeasurementCheck.id))
        .where(db.MeasurementCheck.candidate_id.in_(candidates))
        .group_by(db.MeasurementCheck.candidate_id)
    )
    found = select(db.MeasurementCheck.checker_version).where(db.MeasurementCheck.id.in_(latest)).distinct()
    return sorted(session.scalars(found))


def coverage(session, read):
    """What a model read of one recorded reading plan: characters read (overlaps once), skipped (no digit),
    not read and why; how many read windows gave no measure; and the checker versions its version's recorded
    checks stand under."""
    ws = list(session.scalars(select(db.RecordReadWindow).where(db.RecordReadWindow.read_id == read.id)))
    empty = session.scalars(
        select(db.RecordReadAttempt.window_index).where(
            db.RecordReadAttempt.read_id == read.id,
            db.RecordReadAttempt.outcome == "sent",
            db.RecordReadAttempt.measures_found == 0,
        )
    ).all()

    def union(states):
        spans = sorted((w.start_char, w.end_char) for w in ws if w.state in states)
        total, end = 0, -1
        for a, b in spans:
            a = max(a, end)
            if b > a:
                total += b - a
                end = b
        return total

    model_read = union(("sent",)) if read.state != "not_planned" else read.read_until
    unread = sorted({w.state for w in ws} - {"sent"})
    return {
        "version_id": read.version_id,
        "state": read.state,
        "reason": read.reason,
        "total_chars": read.total_chars,
        "model_read_chars": model_read,
        "skipped_no_digit_chars": union(("sent", "skipped_no_digit")) - union(("sent",)),
        "unread_chars": read.total_chars - model_read,
        "windows_read_without_measures": len(set(empty)),
        "checker_versions": checker_versions(session, read),
        "unread_by_reason": {st: union(("sent", st)) - union(("sent",)) for st in unread},
        "read_until": read.read_until,
        "windows": {st: sum(1 for w in ws if w.state == st) for st in sorted({w.state for w in ws})},
    }
