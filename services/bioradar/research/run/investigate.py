"""Stage investigate: query-planner chooses each next action among the candidates the program offers,
until it stops or the budget does."""

from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from bioradar import db
from bioradar.adapters import services
from bioradar.adapters.fabric import FabricError
from bioradar.adapters.services import ServiceError
from bioradar.research import measure_checks
from bioradar.research.run import Failed, read, search
from bioradar.research.run.search import FABRIC_PAGE, MAX_PAGES, doc_key
from bioradar.text import squash

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


BROWSE_TRIES = 2  # attempts per page of a browsed material set
SUPPLY = 3  # usable unread records that make a plain new search worth less than reading them
PLANNER_CHECK = ("id", "text", "required", "importance", "status", "evidence")  # query-planner Check
FETCHABLE = {"clinicaltrials", "pubmed", "fda_crl", "europe_pmc", "who_don"}


def candidates(run: "Run"):
    live = run.cutoff is None and not run.input["is_demo"]
    # fresh collection needs Fabric's admin token; without it there is no fetch step, said once
    if live and not run.s.fabric_admin_token:
        live = False
        if all(n["code"] != "fetch_not_configured" for n in run.p.get("notes", [])):
            run.note("fetch_not_configured", "没有 BIORADAR_FABRIC_ADMIN_TOKEN，不发起新采集")
    done = {
        (
            a["kind"],
            a["source"],
            tuple(a["args"].get("terms", ())),
            a["args"].get("page"),
            a["args"].get("record_id"),
        )
        for a in run.p["actions"]
    }
    # Semantic search is verified by Fabric at the analysis cutoff, so replays use it too.
    sources = list(run.input["sources"]) + (["weknora"] if run.s.semantic_search else [])
    result = []
    for q in run.p["queries"]:
        for source in sources:
            key = ("search", source, tuple(q["terms"]), None, None)
            if key not in done:
                result.append(
                    {
                        "kind": "search",
                        "source": source,
                        "args": {"terms": q["terms"]},
                        "target_checks": q["checks"],
                        # an object a read record named with a decisive fact (query-planner ranks it
                        # with the question's named objects)
                        "follows_up": q.get("origin", "").startswith("object:"),
                        "_value": q.get("value", 1),
                    }
                )
            elif (
                live and source in FETCHABLE and ("fetch", source, tuple(q["terms"]), None, None) not in done
            ):
                page = run.p["pages"].get(f"{source}|{'|'.join(q['terms'])}", {})
                if page.get("found", 0) < 3:
                    result.append(
                        {
                            "kind": "fetch",
                            "source": source,
                            "args": {"terms": q["terms"]},
                            "target_checks": q["checks"],
                            "cost": 3,
                        }
                    )
    if run.scope and len(run.scope) > FABRIC_PAGE:
        # a large fixed material set (the task's own scope): list what is visible at the cutoff, so
        # discovery does not depend on a search term happening to match (PHX attempt 4, W1: 17 of 20
        # searches found 0-1 records). An ordinary candidate, same budget; no ranking by importance.
        required = [c["id"] for c in run.p["checks"] if c.get("required")]
        pages = -(-len(run.scope) // FABRIC_PAGE)
        for source in run.input["sources"]:
            browsed = set(run.p.get("browsed", {}).get(source, []))
            failed = [
                a["args"]["page"]
                for a in run.p["actions"]
                if a["args"].get("browse") and a["source"] == source and a["status"] == "failed"
            ]
            # the next page not done yet; a page that failed twice is left (and stays visible in actions)
            waiting = [n for n in range(pages) if n not in browsed and failed.count(n) < BROWSE_TRIES]
            if waiting:
                result.append(
                    {
                        "kind": "search",
                        "source": source,
                        "args": {"terms": [], "browse": "scope", "page": waiting[0]},
                        "target_checks": required,
                        "_value": 1,
                    }
                )
    for key, page in run.p["pages"].items():
        if page.get("cursor") and page["pages"] < MAX_PAGES and page["new"]:
            source, *terms = key.split("|")
            result.append(
                {
                    "kind": "next_page",
                    "source": source,
                    "args": {"terms": terms, "page": page["pages"] + 1},
                    "target_checks": page["checks"],
                }
            )
    objects = run.p.get("objects", [])

    def named(title):
        return any(o in (title or "") for o in objects)

    # Reading order (planner only; the baseline keeps its fixed order). Fabric returns literal hits
    # in record-id order, so without this a tie is read alphabetically. coverage: the share of this
    # plan's search terms found in the record's text, a cheap ordering feature, not relevance or truth.
    terms = list(dict.fromkeys(squash(t) for q in run.p["queries"] for t in q["terms"]))

    def body(hit):
        doc = run.docs.get(doc_key(hit))
        return None if doc is None else squash((doc["title"] or "") + (doc["text"] or ""))

    # full texts of what was read, only for documents in memory; after a restart those not reloaded
    # are unknown, and a copy already recorded stays recorded
    read_bodies = {}
    for h in run.p["hits"]:
        if h["read"] and (b := body(h)):
            read_bodies.setdefault(b, f"{h['source']}:{h['record_id']}")

    def sketch(hit):
        """The few ordering features of a hit, kept in progress so a resumed run orders the same way
        without the texts: which search terms its text contains, its own time, and which read record
        its full text is identical to (recorded only when both texts were compared)."""
        doc = run.docs.get(doc_key(hit))
        if doc is not None:
            old, b = hit.get("sketch", {}), body(hit)
            hit["sketch"] = {
                "matched": sorted(set(old.get("matched", [])) | {t for t in terms if t in b}),
                "when": doc.get("published_at") or doc.get("available_at"),
            }
            copy = old.get("duplicate_of") or (None if hit["read"] else read_bodies.get(b))
            if copy:
                hit["sketch"]["duplicate_of"] = copy
        return hit.get("sketch") or {}

    def coverage(hit):
        matched = set(sketch(hit).get("matched", []))
        return len(matched & set(terms)) / len(terms) if terms else 0.0

    def newest(hit):  # the record's own visible time; unknown sorts last
        value = sketch(hit).get("when")
        try:
            return -datetime.fromisoformat(value).timestamp() if value else 0.0
        except (TypeError, ValueError):
            return 0.0

    unread = [h for h in run.p["hits"] if not h["read"]]
    if run.input["method"] == "planner":
        # corrections and seeds first; exact copies of a text already read go behind, never dropped
        unread.sort(
            key=lambda h: (
                not h["critical"],
                bool(sketch(h).get("duplicate_of")),
                not named(h["title"]),
                -coverage(h),
                newest(h),
            )
        )
    else:
        unread.sort(key=lambda h: not h["critical"])
    for hit in unread[:40]:
        result.append(
            {
                "kind": "read",
                "source": hit["source"],
                "args": {"record_id": hit["record_id"]}
                | ({"version_id": hit["version_id"]} if hit.get("version_id") else {}),
                "target_checks": hit["checks"],
                "critical": hit["critical"],
                # query-planner's 0.4-1.0 range: search-term coverage of the text and a title naming an
                # object the question asks about
                "scope_match": round(0.4 + 0.3 * coverage(hit) + (0.3 if named(hit["title"]) else 0), 3),
                "_copy": bool(sketch(hit).get("duplicate_of")),
            }
        )
    return weigh(run, result)


def weigh(run: "Run", result):
    """What each candidate is expected to add, as query-planner's impact: a search is worth more the
    more its terms come from evidence (a found object 1.0, a decisive record's terms 0.8, the plan's
    0.6, a background record's 0.2), and a plain new search less while records already found wait
    to be read; a read whose text has none of the search terms and names nothing asked about is
    background so far (0.5), an exact copy of one read 0.2. Supply of candidates is not evidence:
    PHX attempt 3 kept searching (19-25 of 30 steps) while event records waited unread."""
    reads = [c for c in result if c["kind"] == "read"]
    usable = sum(1 for c in reads if not c["_copy"] and (c["critical"] or c["scope_match"] > 0.4))
    for c in result:
        value = c.pop("_value", None)
        copy = c.pop("_copy", False)
        if c["kind"] == "search" and value is not None:
            impact = {3: 1.0, 2: 0.8, 1: 0.6}.get(value, 0.2)
            if value <= 1 and usable >= SUPPLY:
                impact *= 0.3
            c["impact"] = impact
        elif c["kind"] == "read" and not c["critical"]:
            c["impact"] = 0.2 if copy else 0.5 if c["scope_match"] <= 0.4 else 1.0
    return result


def history(run: "Run"):
    return [
        {
            "kind": a["kind"],
            "source": a["source"],
            "target_checks": a["target_checks"],
            "gain": a["gain"],
            "args": {k: v for k, v in a["args"].items()},
            "status": a["status"],
        }
        # what a read found apart from answering checks (query-planner's evidence_gain)
        | ({"evidence_gain": a["evidence_gain"]} if "evidence_gain" in a else {})
        for a in run.p["actions"]
    ]


def series_state(run: "Run"):
    """The series values this analysis rests on (recorded when it was created), each with its quality
    state as of now: a value revoked or re-checked during the run reaches the planner as it stands. Read
    only - the planner may choose to re-read a value's record, it cannot admit a value to a series."""
    recorded = [p["measurement_id"] for p in run.input.get("series_points") or []][:64]
    if not recorded:
        return []
    # the authority of these values now, read back from Fabric for them alone (bounded, batched): an
    # analysis never relies on a background round having refreshed them first
    measure_checks.refresh_bindings(run.s, run.fabric, is_demo=run.input["is_demo"], measurement_ids=recorded)
    with db.session() as session:
        points = measure_checks.series_points(session, recorded, run.cutoff)  # as of the cutoff
    keys = ("source", "record_id", "version_id", "object", "metric", "value", "unit", "period")
    return [
        {k: str(p.get(k) or "") for k in keys}
        | {
            "current": bool(p["current"]),
            "extraction_check": {
                k: p["extraction_check"][k]
                for k in ("status", "gate", "checker_version", "check_id", "reason")
            }
            if p.get("extraction_check")
            else None,
            # a layer apart from the check: whether the value may be compared now
            "comparability": p["comparability"],
        }
        for p in points
    ]


def investigate(run: "Run"):
    budget = run.input["budget"]
    while True:
        reads = sum(1 for a in run.p["actions"] if a["kind"] == "read" and a["status"] == "completed")
        offered = candidates(run)
        request = {
            "question": run.asked,
            # only query-planner's Check contract; objects and the rest stay in the radar
            "checks": [{k: c[k] for k in PLANNER_CHECK} for c in run.p["checks"]],
            "candidates": offered,
            "history": history(run),
            "budget": {
                "steps_left": max(0, budget["max_steps"] - len(run.p["actions"])),
                "reads_left": max(0, budget["max_reads"] - reads),
            },
            "policy": run.input["method"],
            # the series values the question rests on, quality state re-read now (read only)
            "series": series_state(run),
        }
        run.upstream_still_readable()
        try:
            decision = services.decide(run.s, request)
        except ServiceError as exc:
            if not run.p["actions"]:
                raise Failed(f"planner_{exc.code}") from exc
            run.note(f"planner_{exc.code}")
            run.p["stop"] = {"decision": "insufficient", "reason": f"planner_{exc.code}"}
            return
        run.p["stop"] = {"decision": decision["decision"], "reason": decision["reason"]}
        if decision["decision"] != "continue":
            return
        if run.calls_left() <= 3:  # keep the calls the report needs
            run.p["stop"] = {"decision": "insufficient", "reason": "llm_call_limit"}
            return
        act(run, offered[decision["candidate"]])
        run.save()


def act(run: "Run", candidate):
    run.upstream_still_readable()
    action = {
        "kind": candidate["kind"],
        "source": candidate["source"],
        "args": candidate["args"],
        "target_checks": candidate["target_checks"],
        "gain": 0.0,
        "status": "completed",
    }
    if candidate["kind"] in {"search", "fetch", "next_page"}:
        terms = candidate["args"].get("terms")
        action["origin"] = next((q.get("origin", "") for q in run.p["queries"] if q["terms"] == terms), "")
    try:
        if candidate["kind"] == "read":
            run.found = 0.0
            action["gain"], action["ref"] = read.read(run, candidate)
            action["evidence_gain"] = run.found
        else:
            found, new = search.search(run, candidate)
            action["found"], action["new"] = found, new
            action["gain"] = 0.3 if new else 0.0
    except (FabricError, ServiceError) as exc:
        action["status"], action["error"] = "failed", exc.code
        run.note(exc.code, f"{candidate['kind']} {candidate['source']}")
    except httpx.HTTPError as exc:  # timeout, refused connection, 5xx from WeKnora
        action["status"], action["error"] = "failed", "http_error"
        run.note("http_error", f"{candidate['kind']} {candidate['source']}: {type(exc).__name__}")
    run.p["actions"].append(action)
