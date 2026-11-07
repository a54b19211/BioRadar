"""The chain of earlier rounds of an analysis (a follow-up's parent, its parent, ...) and the sources
they cited."""

from sqlalchemy import select

from bioradar import db

LINEAGE_DEPTH = 500  # rounds followed back; a longer, looping or broken chain counts as unknown
# put above a pending earlier report's draft for BioCheck, whose request model has no field for it
PENDING_LABEL = (
    "【上一轮报告未通过 BioCheck 核验（report_status={status}），只能作为待核线索，不能作为已核实的依据】\n"
)
# the same, for a report the publication gate held and said why
HELD_LABEL = (
    "【上一轮报告被发布门槛挡下（report_status={status}，原因：{reason}），只能作为待核线索，不能作为已核实的依据】\n"
)


def previous_check(previous: dict) -> dict:
    """The earlier report's verification as a follow-up hands it on: its report_status, and whether it is
    pending - held by the publication gate, or recorded before the status was kept, so unknown. The gate's
    own answer (publishable, hold_reason) decides when the report carries it, so a PASS report that lacks
    a section is pending, with the reason; a record without it is pending unless its status is PASS. A
    pending report may still steer the follow-up, only as leads to verify (ADR 0006)."""
    status = previous.get("report_status")
    check = {"report_status": status or "unknown", "pending": status != "PASS"}
    if "publishable" in previous:
        check["pending"] = previous["publishable"] is not True
        if check["pending"]:
            check["hold_reason"] = previous.get("hold_reason")
    return check


def previous_for_biocheck(previous: dict) -> dict:
    """The earlier report as BioCheck gets it: the draft (labelled when pending), its date and its check."""
    check = previous_check(previous)
    status, reason = check["report_status"], check.get("hold_reason")
    label = ""
    if check["pending"] and reason:
        label = HELD_LABEL.format(status=status, reason=reason)
    elif check["pending"]:
        label = PENDING_LABEL.format(status=status)
    return {**previous, "draft": label + previous["draft"], **check}


class UnknownLineage(Exception):
    """The chain of earlier rounds could not be followed to its start."""


def lineage(session, analysis_id: str, depth=LINEAGE_DEPTH) -> list[str]:
    """The analysis's earlier rounds, nearest first (a follow-up's parent, its parent, ...). Raises
    UnknownLineage when the chain loops, breaks off at a missing row or is longer than `depth`: an
    unfinished walk must never pass for "no earlier sources" (round 21 lost its root's limits)."""
    found, current = [], session.get(db.Analysis, analysis_id)
    while current is not None and current.parent_id:
        if current.parent_id in found or current.parent_id == analysis_id or len(found) >= depth:
            raise UnknownLineage(analysis_id)
        found.append(current.parent_id)
        current = session.get(db.Analysis, current.parent_id)
        if current is None:
            raise UnknownLineage(analysis_id)
    return found


def lineage_sources(analysis_id: str) -> set[str] | None:
    """Sources cited by the analysis's earlier rounds (what its previous report was written from), or None
    when the chain cannot be followed."""
    with db.session() as session:
        try:
            ids = lineage(session, analysis_id)
        except UnknownLineage:
            return None
        if not ids:
            return set()
        return set(session.scalars(select(db.Evidence.source).where(db.Evidence.analysis_id.in_(ids))))
