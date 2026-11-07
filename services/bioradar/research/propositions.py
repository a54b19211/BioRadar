"""Proposition numbers (task book 05 2.5, docs/decisions/0012): a judgment of a topic's analysis keeps one
`prop_id` through the analyses that follow it, and says what it rests on.

The id is the topic's short id and a number taken from `topics.next_prop_seq` under the topic row's lock, in
the transaction that writes the analysis result, so two analyses of one topic that finish together never
take the same number. An earlier id is kept only when type, scope and question key are all the same as
before (the program decides; a model proposes nothing here). A changed question starts a new id; a changed
scope starts a new id that names the old one in `derived_from`; a changed conclusion under the same id
raises `content_rev`."""

import hashlib

from sqlalchemy import select

from bioradar import db
from bioradar.research.domain import norm

FREE = "free"  # the only kind of question a judgment is today: the plan's own wording
CHAIN = 50  # how many analyses back the numbers of earlier judgments are looked up
CHECKED = ("supported", "contradicted")  # BioCheck's verdicts that come from a check of the conclusion


def question_key(topic_id: str, question: str) -> str:
    """The identity of a free question: the topic and the normalized wording. The topic stands where the
    task book says investigation: each analysis of a topic opens an investigation of its own, the topic
    stays."""
    return f"{FREE}:{topic_id[:8]}:{hashlib.sha256(norm(question).encode()).hexdigest()[:16]}"


def _content(judgment: dict) -> tuple:
    return norm(judgment["conclusion"]), judgment["confidence"]


def _numbered(session, analysis_id):
    """The numbered judgments of an analysis and of the analyses it follows, nearest first, each with the
    analysis it is in."""
    seen, current = set(), analysis_id
    while current and current not in seen and len(seen) < CHAIN:
        seen.add(current)
        analysis = session.get(db.Analysis, current)
        if analysis is None:
            return
        for j in ((analysis.result or {}).get("assessment") or {}).get("judgments") or []:
            if j.get("prop_id"):
                yield {**j, "analysis_id": analysis.id}
        current = analysis.parent_id


def _earlier(session, row) -> dict[str, list[dict]]:
    """question key -> the numbered judgments of the analyses this one follows, nearest first."""
    found: dict[str, list[dict]] = {}
    for j in _numbered(session, row.parent_id):
        found.setdefault(j["question_key"], []).append(j)
    return found


def standing(session, analysis_id) -> dict[str, dict]:
    """prop_id -> the latest judgment of every proposition an analysis stands on: its own and those of the
    rounds before it that no later round judged again. A proposition whose scope moved on to a new id is
    left out: the new one names it in `derived_from`."""
    found, moved = {}, set()
    for j in _numbered(session, analysis_id):
        if j["prop_id"] not in found and j["prop_id"] not in moved:
            found[j["prop_id"]] = j
            if j.get("derived_from"):
                moved.add(j["derived_from"])
    return found


def _same(earlier, key, scope, taken) -> dict | None:
    """The earlier judgment of this question and scope whose id this result has not used yet."""
    matching = (p for p in earlier.get(key, ()) if p["scope"] == scope and p["prop_id"] not in taken)
    return next(matching, None)


def _reads(session, analysis_id: str) -> dict:
    """Evidence ref -> the material_reads row that recorded the text the model read of it."""
    query = select(db.Evidence.ref, db.Evidence.meta).where(db.Evidence.analysis_id == analysis_id)
    return {ref: (meta or {}).get("material_read_id") for ref, meta in session.execute(query)}


# what a judgment's slot item takes from it
NUMBERED = ("prop_id", "question_key", "content_rev", "derived_from")


def _stamp(sections: list[dict], judgment: dict) -> None:
    """The slot item of the judgment (the writer's paragraph of the same slot) carries its numbers."""
    for section in sections:
        for item in section.get("items", []):
            if item.get("slot") and item["slot"] == judgment.get("slot"):
                item.update({k: judgment[k] for k in NUMBERED if k in judgment})


def number(session, row: db.Analysis, result: dict) -> None:
    """Give the judgments of a finished analysis of a topic their ids and what they depend on, in place.
    Call it in the transaction that stores the result: it locks the topic row, takes the numbers, and the
    caller's commit releases the lock. An analysis without a topic is left as it is."""
    judgments = (result.get("assessment") or {}).get("judgments") or []
    query = select(db.Investigation.topic_id).where(db.Investigation.id == row.investigation_id)
    topic_id = session.scalar(query)
    if not judgments or topic_id is None:
        return
    topic = session.get(db.Topic, topic_id, with_for_update=True)
    scope = {"group": (result.get("groups") or {}).get("display", "")}
    earlier, taken = _earlier(session, row), set()
    reads = _reads(session, row.id)
    for j in judgments:
        key = question_key(topic.id, j["question"])
        old = _same(earlier, key, scope, taken)
        if old is None:
            prop_id, rev, derived = f"T{topic.id[:8]}-P{topic.next_prop_seq}", 1, None
            topic.next_prop_seq += 1
            if earlier.get(key):
                derived = earlier[key][0]["prop_id"]
        else:
            prop_id, derived = old["prop_id"], None
            rev = old.get("content_rev", 1) + int(_content(old) != _content(j))
        taken.add(prop_id)
        j.update(
            prop_id=prop_id,
            type=FREE,
            scope=scope,
            question_key=key,
            content_rev=rev,
            depends_on={
                "material_reads": [reads[g] for g in j.get("grounds", []) if reads.get(g)],
                # the llm_calls row of the call that checked this conclusion, as assess recorded it
                "checked_by": j.get("checked_by") if j.get("check") in CHECKED else None,
            },
        )
        if derived:
            j["derived_from"] = derived
        _stamp(result.get("sections") or [], j)
