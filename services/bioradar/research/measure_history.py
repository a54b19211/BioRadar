"""The radar's own measurement chain from before Fabric took it over, read only
(docs/decisions/0010-fabric-measurements.md).

Until the cut-over the radar extracted measures in triage and continuation reading, checked them with BioCheck
and stored series points itself. Those tables are kept, unchanged and frozen: measurements,
measurement_candidates, measurement_check_tasks, measurement_checks, record_reads, record_read_windows,
record_read_attempts and record_corrections. Nothing writes them any more; what is here only reads them, so
an analysis that rests on one of those points, or a topic event that names one, still shows it as it was
recorded. Such a point is history (authority radar_history): it is never current - the checks it went through
are no longer kept up to date - and nothing is computed from it."""

from sqlalchemy import select

from bioradar import db
from bioradar.research.clock import utc

# the last checker of the radar's own chain: its checks are the ones a frozen point is described by
CHECKER = "biocheck.extraction_rules.v16"
COMPARABILITY = "series_domain.v2"
AUTHORITY = "radar_history"


def latest_check(session, candidate_id):
    """The candidate's latest check under the chain's last checker, or None."""
    if not candidate_id:
        return None
    return session.scalar(
        select(db.MeasurementCheck)
        .where(
            db.MeasurementCheck.candidate_id == candidate_id,
            db.MeasurementCheck.checker_version == CHECKER,
        )
        .order_by(db.MeasurementCheck.id.desc())
        .limit(1)
    )


def describe(check):
    return {
        "status": check.status,
        "gate": check.gate,
        "checker_version": check.checker_version,
        "check_id": check.id,
        "reason": check.gate_reason or check.error_code,
    }


def withdrawn(session, ids, at=None):
    """The ids of the points (of `ids`) whose record a recorded correction or retraction withdrew as of `at`
    (None: every recorded correction)."""
    if not ids:
        return set()
    query = (
        select(db.Measurement.id)
        .join(
            db.RecordCorrection,
            (db.RecordCorrection.workspace_id == db.Measurement.workspace_id)
            & (db.RecordCorrection.is_demo == db.Measurement.is_demo)
            & (db.RecordCorrection.target_source == db.Measurement.source)
            & (db.RecordCorrection.target_record_id == db.Measurement.record_id)
            & (
                (db.RecordCorrection.target_version_id == "")
                | (db.RecordCorrection.target_version_id == db.Measurement.version_id)
            ),
        )
        .where(db.Measurement.id.in_(list(ids)))
    )
    if at is not None:
        query = query.where(db.RecordCorrection.visible_at <= at)
    return set(session.scalars(query))


def comparability(session, m, at=None):
    """What the frozen record says about comparing point m, as of `at`: its domain, a correction that withdrew
    it (also across topics), an upgraded artifact that retired it, and the gaps of its complete domain."""

    def seen(point):
        return at is None or utc(point.available_at) <= at

    reasons = []
    if m.domain_basis != "topic":
        reasons.append("domain_unknown")
    if m.retired_reason:
        reasons.append("superseded_by_artifact")
    by = session.get(db.Measurement, m.superseded_by) if m.superseded_by is not None else None
    by = by if by is not None and seen(by) else None
    if by is not None or m.id in withdrawn(session, [m.id], at):
        reasons.append("withdrawn_by_correction")
        if by is not None and (by.domain_basis, by.domain_id) != (m.domain_basis, m.domain_id):
            reasons.append("cross_domain_correction")
    elif any(
        seen(c)
        for c in session.scalars(
            select(db.Measurement).where(
                db.Measurement.superseded_by == m.id,
                (db.Measurement.domain_basis != m.domain_basis)
                | (db.Measurement.domain_id.is_distinct_from(m.domain_id)),
            )
        )
    ):
        reasons.append("cross_domain_correction")
    gaps = list(m.dims_reasons or [])
    reasons += gaps
    blocked = {"domain_unknown", "withdrawn_by_correction", "superseded_by_artifact"} & set(reasons) or gaps
    return {
        "version": COMPARABILITY,
        "state": "not_comparable" if blocked else "comparable",
        "family": m.family,
        "domain_basis": m.domain_basis,
        "domain_id": m.domain_id,
        "reasons": reasons,
    }


def series_points(session, ids, at=None):
    """The recorded points of `ids` as the frozen chain left them: exact value, latest check, comparability as
    of `at` - and current false, always (history, not a current data-layer object)."""
    out = []
    points = (
        select(db.Measurement)
        .where(db.Measurement.id.in_(sorted(set(ids))))
        .order_by(db.Measurement.period_end)
    )
    for m in session.scalars(points):
        check = latest_check(session, m.candidate_id)
        out.append(
            {
                "measurement_id": m.id,
                "authority": AUTHORITY,
                "current": False,
                "object": m.object,
                "metric": m.metric,
                "value": m.value_text or "",
                "unit": m.unit,
                "period": m.period,
                "period_type": m.period_type,
                "period_precision": m.period_precision,
                "scope": m.scope,
                "scope_mode": m.scope_mode,
                "source": m.source,
                "record_id": m.record_id,
                "version_id": m.version_id,
                "extraction_check": describe(check) if check else None,
                "comparability": comparability(session, m, at),
            }
        )
    return out


def coverage(session, read):
    """What a model read of one recorded reading plan: characters read (overlaps once), skipped (no digit),
    not read and why; and how many read windows gave no measure."""
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
        "unread_by_reason": {st: union(("sent", st)) - union(("sent",)) for st in unread},
        "read_until": read.read_until,
        "windows": {st: sum(1 for w in ws if w.state == st) for st in sorted({w.state for w in ws})},
    }
