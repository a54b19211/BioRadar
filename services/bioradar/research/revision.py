"""A new version of a record that propositions rest on (task book 05 2.6; docs/decisions/0015).

When a version arrives for a record that a topic placed or that an analysis of it read - or a version states
that it corrects or retracts such a record - the monitor looks at it once, with no model call. Fabric's
difference facts, asked at the mode and cutoff the version was read at, say what kind of change it is; the
reads of the older version say which propositions rest on it (`depends_on.material_reads`); the answer is a
`version` event of the topic. Nothing is skipped on the strength of a hash: no source gives the radar a
field-level difference, so a change is left for review, and a correction or retraction comes first whatever
the text says. The topic is owed an analysis of exactly the propositions that rest on the record.

That analysis is the next judgment version. It names the propositions it was asked about (`input.reassess`);
when it completes, `revise` adds the revision section: a line for every proposition the earlier rounds stand
on - before and after, why, what triggered it - or that it stands. An earlier result is never written to."""

import logging
from collections import defaultdict

import httpx
from sqlalchemy import select

from bioradar import db
from bioradar.adapters.fabric import Fabric, FabricError
from bioradar.research import propositions
from bioradar.research.clock import parse_time
from bioradar.research.run.slots import graded_status
from bioradar.research.state import PropState, SectionItem
from bioradar.research.topics import event, owe

log = logging.getLogger("bioradar")

CORRECTIONS = ("corrects", "retracts")  # the Fabric relations that withdraw an earlier record
# How a version counts as a sample of a source update, by Fabric's kind of difference: a changed original is
# a real update; the same original in another serialization only exercises the reuse path; a changed
# parser or projection is processing, no update of the source at all.
SAMPLES = {"raw_changed": "real_update", "serialization_only": "engineering_reuse"}
# The facts Fabric states about a change of projection only: a proposition rests on the text, the located
# fields and the source's own update time; a parser version alone moves none of them.
BASIS = ("body_changed", "structure_changed", "last_modified_changed")
FACTS = ("parser_changed", *BASIS)
# Why a proposition is judged again, strongest first: a correction before anything else.
REASONS = ("source_corrected", "pending_review", "processing_upgrade")
CHUNK = 500


def correction_targets(doc) -> list[tuple[tuple[str, str], str | None]]:
    """((source, record id), the version when Fabric names one) of each record the document corrects or
    retracts."""
    return [
        ((r.get("target_source") or doc["source"], r["target_record_id"]), r.get("target_version_id"))
        for r in doc["relations"]
        if r["kind"] in CORRECTIONS
    ]


def lookups(session, record_ids, *, is_demo: bool):
    """What the radar holds of these records, in two queries however many there are: the observations placed
    in a topic, {(source, record id): [(version id, topic id)]}, and the reads of the analyses of a topic,
    {(source, record id): [(version id, material_reads id, topic id)]}."""
    seen, read = defaultdict(list), defaultdict(list)
    ids = sorted(record_ids)
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        placed = select(
            db.Observation.source,
            db.Observation.record_id,
            db.Observation.version_id,
            db.Observation.topic_id,
        ).where(
            db.Observation.record_id.in_(chunk),
            db.Observation.is_demo == is_demo,
            db.Observation.topic_id.is_not(None),
        )
        for source, record, version, topic in session.execute(placed):
            seen[(source, record)].append((version, topic))
        analysed = (
            select(
                db.MaterialRead.source,
                db.MaterialRead.record_id,
                db.MaterialRead.version_id,
                db.MaterialRead.id,
                db.Investigation.topic_id,
            )
            .join(db.Analysis, db.Analysis.id == db.MaterialRead.analysis_id)
            .join(db.Investigation, db.Investigation.id == db.Analysis.investigation_id)
            .where(
                db.MaterialRead.record_id.in_(chunk),
                db.Investigation.is_demo == is_demo,
                db.Investigation.topic_id.is_not(None),
            )
        )
        for source, record, version, read_id, topic in session.execute(analysed):
            read[(source, record)].append((version, read_id, topic))
    return seen, read


def by_topic(seen, read, key, *, version=None, exclude=None) -> dict[str, set[int]]:
    """topic id -> the reads of this record's versions - only `version` when it is named, never `exclude` -
    by the analyses of that topic; a topic that only placed such a version has none."""

    def counts(v):
        return (version is None or v == version) and v != exclude

    found: dict[str, set[int]] = defaultdict(set)
    for v, topic in seen.get(key, ()):
        if counts(v):
            found.setdefault(topic, set())
    for v, read_id, topic in read.get(key, ()):
        if counts(v):
            found[topic].add(read_id)
    return found


def touching(session, docs, *, is_demo: bool):
    """[(document, [(cause, topic id, read ids)])] for the documents that touch a topic: an earlier version of
    their record was placed in the topic or read by an analysis of it (cause "version"), or they correct or
    retract such a record (cause "correction")."""
    keys = {(d["source"], d["record_id"]) for d in docs} | {k for d in docs for k, _ in correction_targets(d)}
    seen, read = lookups(session, {record for _, record in keys}, is_demo=is_demo)
    found = []
    for d in docs:
        own = by_topic(seen, read, (d["source"], d["record_id"]), exclude=d["version_id"])
        causes = [("version", topic, ids) for topic, ids in own.items()]
        corrected: dict[str, set[int]] = defaultdict(set)
        for key, version in correction_targets(d):
            for topic, ids in by_topic(seen, read, key, version=version).items():
                corrected[topic] |= ids
        causes += [("correction", topic, ids) for topic, ids in corrected.items()]
        if causes:
            found.append((d, causes))
    return found


def differences(fabric: Fabric, docs, *, is_demo: bool) -> dict[str, dict]:
    """version id -> Fabric's difference facts for it, asked at the mode and cutoff the document was read at
    (the pair its /records answer was given at, Fabric.complete). A version Fabric gave no answer for - an
    older Fabric, an error, a document that does not say how it was read - is left out."""
    asked = defaultdict(list)
    for d in docs:
        if d.get("read_at"):
            asked[(d["source"], d["read_at"]["mode"], d["read_at"]["cutoff"])].append(d["version_id"])
    found = {}
    for (source, mode, cutoff), versions in asked.items():
        try:
            found.update(
                fabric.version_differences(source, versions, is_demo=is_demo, mode=mode, cutoff=cutoff)
            )
        except (FabricError, httpx.HTTPError):
            log.warning("version differences of %s cannot be read, its new versions wait for review", source)
    return found


def judge(difference: dict | None, corrects: bool) -> tuple[str, bool, str | None]:
    """(why the propositions that rest on the record are looked at, whether they are judged again, how the
    version counts as a sample) for one new version, from Fabric's difference facts for it (None: Fabric gave
    none). A correction or retraction comes first, whatever the text. Otherwise the change is left for review
    (pending_review) - Fabric names the kind of change, not which cited field moved. The one thing that leaves
    the propositions standing is a processing upgrade that moved nothing they rest on."""
    compared = difference if (difference or {}).get("state") == "compared" else {}
    kind = compared.get("kind")
    if corrects:
        return "source_corrected", True, SAMPLES.get(kind)
    if kind == "projection_only":
        return "processing_upgrade", any(compared.get(f, True) for f in BASIS), None
    return "pending_review", True, SAMPLES.get(kind)


def depending(session, topic: db.Topic, reads: set[int]) -> list[str]:
    """The propositions the topic's latest judgments stand on that rest on any of these reads."""
    if not reads or not topic.last_analysis_id:
        return []
    return [
        prop_id
        for prop_id, j in propositions.standing(session, topic.last_analysis_id).items()
        if reads & set((j.get("depends_on") or {}).get("material_reads") or ())
    ]


def record(session, doc, cause: str, topic_id: str, reads: set[int], difference: dict | None) -> bool:
    """Write the `version` event of one new version for one topic, once, and owe the topic an analysis when
    propositions rest on the record and are to be judged again. False when the event is already there."""
    topic = session.get(db.Topic, topic_id)
    query = select(db.TopicEvent).where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "version")
    if any(
        (e.detail.get("version_id"), e.detail.get("cause")) == (doc["version_id"], cause)
        for e in session.scalars(query)
    ):
        return False
    reason, again, sample = judge(difference, cause == "correction")
    affected = depending(session, topic, reads)
    compared = (difference or {}).get("state") == "compared"
    at = parse_time(doc["available_at"])
    event(
        session,
        topic,
        at,
        "version",
        cause=cause,
        source=doc["source"],
        record=doc["record_id"],
        version_id=doc["version_id"],
        from_version_id=difference.get("from_version_id") if compared else None,
        difference=difference["kind"] if compared else (difference or {}).get("state", "unavailable"),
        facts={f: difference.get(f) for f in FACTS} if compared else {},
        reason=reason,
        sample=sample,
        read_at=doc.get("read_at"),
        affected=affected,
        judge_again=again and bool(affected),
    )
    if again and affected:
        owe(topic, "new_version", at)
    return True


def arrive(fabric: Fabric, docs, *, is_demo: bool) -> int:
    """Record what the new versions of a monitoring round mean for the topics they touch (a `version` event
    each, once). Fabric is asked only about versions that touch a topic. Returns the events written."""
    with db.session() as session:
        touched = touching(session, docs, is_demo=is_demo)
    if not touched:
        return 0
    found = differences(fabric, [d for d, _ in touched], is_demo=is_demo)
    written = 0
    with db.session() as session:
        for doc, causes in touched:
            for cause, topic_id, reads in causes:
                written += record(session, doc, cause, topic_id, reads, found.get(doc["version_id"]))
    return written


def prop_state(judgment: dict) -> PropState:
    return {
        "status": graded_status(judgment),
        "confidence": judgment["confidence"],
        "content_rev": judgment.get("content_rev", 1),
    }


def line(old: dict, ask: dict | None, judged: list[dict]) -> SectionItem:
    """The revision line of one proposition (`old`: its latest judgment before this analysis; `ask`: what the
    analysis was asked about it, None when nothing; `judged`: the numbered judgments the analysis gave).
    Not asked: it stands. Asked and judged: revised when its status, confidence or content changed, else
    maintained - it was checked. Asked and not judged (the analysis ran out of room): open, still owed."""
    before = prop_state(old)
    item: SectionItem = {"prop_id": old["prop_id"], "question": old["question"], "before": before}
    if ask is None:
        return {**item, "status": "maintained", "reason": "unaffected", "after": before}
    new = next((j for j in judged if old["prop_id"] in (j["prop_id"], j.get("derived_from"))), None)
    item.update(reason=ask["reason"], trigger=ask["triggers"])
    if new is None:
        return {**item, "status": "open", "after": {**before, "status": "open"}}
    if new["prop_id"] != old["prop_id"]:  # the scope moved on: the new number names the old one
        item.update(prop_id=new["prop_id"], derived_from=old["prop_id"])
    after = prop_state(new)
    return {**item, "status": "revised" if after != before else "maintained", "after": after}


def revise(session, row: db.Analysis, result: dict) -> None:
    """Add the revision section to the result of a completed analysis that judged propositions again
    (`input.reassess`: a new version, or a follow-up about one proposition), in place, after its judgments
    are numbered: a line for each proposition the rounds before it stand on."""
    asked = {a["prop_id"]: a for a in row.input.get("reassess") or ()}
    if not asked or not row.parent_id:
        return
    judged = [j for j in (result.get("assessment") or {}).get("judgments") or [] if j.get("prop_id")]
    before = propositions.standing(session, row.parent_id)
    items = [line(old, asked.get(prop_id), judged) for prop_id, old in before.items()]
    if items:
        result.setdefault("sections", []).append({"kind": "revision", "status": "ok", "items": items})
