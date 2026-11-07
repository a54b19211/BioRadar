"""Investigation: reading one record - its window, the evidence row and its outbound record, the
reader's facts and what they answer, corrections and new names to follow up."""

from typing import TYPE_CHECKING

from sqlalchemy import delete

from bioradar import db
from bioradar.adapters.fabric import FabricError, fixed_version_is, noticed
from bioradar.material import of
from bioradar.research import prompts, schemas
from bioradar.research.run import search, submit
from bioradar.research.run.search import clean_terms, doc_key
from bioradar.research.run.windows import adopt, hit_spans, table_header_omitted
from bioradar.text import squash

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


MAX_FOUND = 12  # objects found in read records, beyond those the question names


def read(run: "Run", candidate):
    """Read one candidate record: its window goes to the reader as one evidence row (E1, E2 ...), and the
    facts the reader quotes from it update the checks. Returns (gain, ref): the share of the targeted
    checks this read settled, and the evidence ref."""
    source, record_id = candidate["source"], candidate["args"]["record_id"]
    pinned = candidate["args"].get("version_id")
    hit = next(
        h
        for h in run.p["hits"]
        if (h["source"], h["record_id"], h.get("version_id")) == (source, record_id, pinned)
    )
    hit["read"] = True
    doc = document(run, hit, pinned)
    material, window, omitted = reading_window(run, hit, doc)
    ref = f"E{len(run.p['refs']) + 1}"
    run.p["refs"][ref] = f"{source}|{record_id}"
    read_id = store(run, hit, ref, doc, material, window, omitted)
    follow_relations(run, hit, doc)
    checks = {c["id"]: c for c in run.p["checks"]}
    reading = ask_reader(run, ref, hit, doc, material.text, read_id)
    if reading is None or not reading.relevant:
        return 0.0, ref
    before = {c["id"] for c in run.p["checks"] if c["status"] != "open"}
    facts_before = len(run.p["facts"])
    found_before_names = {o["name"] for o in run.p.get("found_objects", [])}
    record_facts(run, reading, ref, material.text, checks)
    close_answered(run, reading, checks)
    follow_up(run, reading, hit, ref, run.p["facts"][facts_before:], found_before_names)
    after = {c["id"] for c in run.p["checks"] if c["status"] != "open"}
    return min(1.0, len(after - before) / max(1, len(candidate["target_checks"]))), ref


def document(run: "Run", hit, pinned):
    """The record as visible at the cutoff: from this process's cache, else exactly the pinned version
    (a correction notice), else the record's visible version."""
    source, record_id = hit["source"], hit["record_id"]
    doc = run.docs.get(doc_key(hit))
    if doc is None and pinned:
        # exactly the version that corrected, as visible at the cutoff - never the head
        found, _ = run.fabric.read_versions(
            source, [(record_id, pinned)], is_demo=run.input["is_demo"], cutoff=run.cutoff
        )
        if not found:
            raise FabricError("record_not_visible", f"{record_id}@{pinned[:12]}")
        if len(found) != 1 or not fixed_version_is(
            found[0], source, record_id, pinned, run.input["is_demo"], run.cutoff
        ):
            # verified here too: whatever reader served it, only the named version is that version
            raise FabricError("fixed_version_mismatch", f"{record_id}@{pinned[:12]}")
        doc = run.docs[doc_key(hit)] = found[0]
    if doc is None:
        found, _ = run.fabric.search(
            source, (), is_demo=run.input["is_demo"], cutoff=run.cutoff, record_ids=[record_id], limit=1
        )
        if not found:
            raise FabricError("record_not_visible", record_id)
        doc = found[0]
    return doc


def reading_window(run: "Run", hit, doc):
    """(material, window kind, table rows without header) the reader gets of this record: the verified
    search-hit passages first, then the lines with the search terms of the hit's checks."""
    source, record_id = hit["source"], hit["record_id"]
    terms = [t for q in run.p["queries"] if set(q["checks"]) & set(hit["checks"]) for t in q["terms"]]
    places = [
        p
        for p in run.p.get("hit_positions", {}).get(f"{source}|{record_id}", [])
        if p["version_id"] == doc["version_id"]
    ]
    spans, unresolved = hit_spans(of(doc), places)
    for reason in dict.fromkeys(unresolved):
        run.note(reason, f"{source}: {record_id}")
    material, window = adopt(of(doc), terms, run.s.read_characters, hits=spans)
    omitted = table_header_omitted(material.text)
    if omitted:
        run.note("table_header_omitted", f"{source}: {record_id}")
    material = noticed(material, doc.get("clean_text_state"))
    if doc.get("clean_text_state") not in (None, "processed"):
        # not the cleaning layer's text: read, but said so and never counted as cleaned or full text
        run.note("clean_text_unprocessed", f"{source}: {record_id} ({doc['clean_text_state']})")
    return material, window, omitted


def store(run: "Run", hit, ref, doc, material, window, omitted):
    """The evidence row of this read and the outbound record of what the reader is about to get; returns
    that record's id."""
    with run.writing() as s:
        # A worker that died mid-read may have left this ref behind without saving progress.
        s.execute(delete(db.Evidence).where(db.Evidence.analysis_id == run.id, db.Evidence.ref == ref))
        read_id = submit.sent(run, s, "read", "radar", ref, doc, material)
        s.add(
            db.Evidence(
                analysis_id=run.id,
                ref=ref,
                source=hit["source"],
                record_id=hit["record_id"],
                version_id=doc["version_id"],
                title=doc["title"],
                published_at=doc["published_at"],
                available_at=str(doc["available_at"]),
                material_kind=doc["material_kind"],
                verified=doc["verified"],
                text=material.text,
                meta={
                    "url": doc["url"],
                    "window": window,
                    "text_level": doc["text_level"],
                    "relations": doc["relations"],
                    "origin_group": doc["origin_group"],
                    "events": doc["events"],
                    # program-derived parts of the text and their inputs (both registry
                    # versions; the attachment's parse artifact and parent versions). A quote
                    # found only there quotes a derived comparison, not the source record.
                    "derived": doc.get("derived") or [],
                    # the material_reads row binding this text to its parts (v3 6.1), and whether the
                    # cleaning layer's text was read or, not processed yet, the version's own text
                    "material_read_id": read_id,
                    # table rows kept without their column header (said in the text too)
                    "table_header_omitted": omitted,
                    "clean_text_state": doc.get("clean_text_state"),
                    "clean_text_choice": doc.get("clean_text_choice"),
                },
            )
        )
    return read_id


def follow_relations(run: "Run", hit, doc):
    """The relations the record states; a correction or retraction it states is read next (critical)."""
    source, record_id = hit["source"], hit["record_id"]
    for relation in doc["relations"]:
        run.p["relations"].append(
            {
                "from": record_id,
                "source": source,  # the record that states the relation
                "to": relation["target_record_id"],
                "to_source": relation.get("target_source") or source,
                "to_version": relation.get("target_version_id"),
                "kind": relation["kind"],
            }
        )
        if relation["kind"] in {"corrects", "retracts"}:
            target = relation.get("target_source") or source
            search.add_hit(run, target, relation["target_record_id"], "", hit["checks"], critical=True)
            run.found = 1.0  # a correction or retraction arrived


def ask_reader(run: "Run", ref, hit, doc, text, read_id):
    """The reader's facts from this record's window (None: no call made, or no valid answer)."""
    material = {
        "ref": ref,
        "title": doc["title"],
        "source": hit["source"],
        "published_at": doc["published_at"],
        "material_kind": doc["material_kind"],
        "verified": doc["verified"],
        "events": doc["events"],
        "text": text,
    }
    known = {
        c["id"]: [f"{f['ref']}: {f['text']}" for f in run.p["facts"] if c["id"] in f["checks"]][-12:]
        for c in run.p["checks"]
    }
    payload = {
        "question": run.asked,
        "material": material,
        "objects": run.p.get("objects", []),
        "hypotheses": [{"id": h["id"], "statement": h["statement"]} for h in run.p.get("hypotheses", [])],
        "checks": [
            {
                "id": c["id"],
                "text": c["text"],
                "status": c["status"],
                "objects": c.get("objects", []),
                "exploratory": c.get("exploratory", []),
                "known": known[c["id"]],
            }
            for c in run.p["checks"]
        ],
    }
    # the record itself and every record a known fact came from go out with this call
    carries = {hit["source"]} | run.sources_of(f["ref"] for f in run.p["facts"] if f["checks"])
    with submit.outbound(run, [read_id], model=True):
        return run.ask("read", prompts.READ, payload, schemas.Reading, carries=carries)


def record_facts(run: "Run", reading, ref, text, checks):
    """The reader's facts whose quote is in the text it got (others are dropped with a note); a fact for or
    against a check is that check's evidence, and both directions on one check make it a conflict."""
    flat = squash(text)
    hypotheses = {h["id"] for h in run.p.get("hypotheses", [])}
    for fact in reading.facts[:10]:
        if not fact.quote or squash(fact.quote) not in flat:
            run.note("quote_not_found", f"{ref}: {fact.quote[:60]}")
            continue
        ids = [i for i in fact.checks if i in checks]
        run.p["facts"].append(
            {
                "id": f"F{len(run.p['facts']) + 1}",
                "ref": ref,
                "text": fact.text,
                "quote": fact.quote,
                "checks": ids,
                "stance": fact.stance,
                "object": object_of(run, fact, ref),
                "hypothesis": fact.hypothesis if fact.hypothesis in hypotheses else "",
                "hypothesis_stance": fact.hypothesis_stance,
            }
        )
        if fact.stance != "context":
            for i in ids:
                count_as_evidence(run, checks[i], ref)


def count_as_evidence(run: "Run", check, ref):
    if ref not in check["evidence"]:
        check["evidence"].append(ref)
    stances = {f["stance"] for f in run.p["facts"] if check["id"] in f["checks"]}
    if {"supports", "refutes"} <= stances:
        check["status"] = "conflict"


def close_answered(run: "Run", reading, checks):
    """One record often answers part of a check (one company of three); the model says when it is whole."""
    for i in reading.answered:
        check = checks.get(i)
        if not check or not check["evidence"] or check["status"] != "open":
            continue
        # a check is answered by what records say, not by what one does not mention: facts that only
        # limit ("未提及更正") never close it (PHX-W4 stopped after two routine weekly reports).
        # Every object the check names needs a supporting or refuting fact of its own.
        decisive = [f for f in run.p["facts"] if i in f["checks"] and f["stance"] in {"supports", "refutes"}]
        covered = {f.get("object") for f in decisive}
        if decisive and all(o in covered for o in check.get("objects", [])):
            check["status"] = "answered"


def follow_up(run: "Run", reading, hit, ref, new_facts, found_before_names):
    """A newly found object is followed up by name (later updates of that event); then the reader's terms,
    worth more when this record said something decisive than when it was only background. Sets what the
    read found apart from answering (run.found)."""
    open_ids = [c["id"] for c in run.p["checks"] if c["status"] != "answered"]
    decisive = any(f["stance"] in {"supports", "refutes"} for f in new_facts)
    new_names = [o["name"] for o in run.p.get("found_objects", []) if o["name"] not in found_before_names]
    run.found = max(run.found, 1.0 if new_names else 0.6 if decisive else 0.0)
    for name in new_names:
        search.add_query(run, [name], hit["checks"] or open_ids, value=3, origin=f"object:{ref}")
    for terms in reading.new_terms[:2]:
        search.add_query(
            run,
            clean_terms(terms),
            hit["checks"] or open_ids,
            value=2 if decisive else 0,
            origin=f"read:{ref}",
        )


def object_of(run: "Run", fact, ref):
    """The fact's object: one the question names, or one this fact's own quoted words name (every
    attribution needs its own quote, also for a name seen before). Only a fact that supports or refutes
    something enters a new name into found_objects, the names that are followed up; background facts
    keep their name but take no place there. Plan-stage guesses never get in; found objects do not
    narrow the question or its checks, and are never merged with each other."""
    name = " ".join((fact.object or "").split())[:80]
    if not name:
        return ""
    if name in run.p.get("objects", []):
        return name
    if len(name) < 2 or squash(name) not in squash(fact.quote):
        return ""
    found = run.p.setdefault("found_objects", [])
    known = any(o["name"] == name for o in found)
    if not known and fact.stance in {"supports", "refutes"} and len(found) < MAX_FOUND:
        found.append({"name": name, "ref": ref, "quote": fact.quote[:120]})
    return name
