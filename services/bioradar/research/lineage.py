"""The chain of earlier rounds of an analysis (a follow-up's parent, its parent, ...) and the sources
they cited."""

from sqlalchemy import select

from bioradar import db

LINEAGE_DEPTH = 500  # rounds followed back; a longer, looping or broken chain counts as unknown


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
