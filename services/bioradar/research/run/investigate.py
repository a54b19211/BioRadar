"""Stage investigate: Bio-Planner chooses each next action among the candidates the program offers,
until it stops or the budget does."""

from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from bioradar import db
from bioradar.adapters import services
from bioradar.adapters.fabric import FabricError, time_value
from bioradar.adapters.services import ServiceError
from bioradar.research import measure_history, series_sync
from bioradar.research.run import Failed, decisions, read, search
from bioradar.research.run.search import FABRIC_PAGE, MAX_PAGES, doc_key
from bioradar.text import squash

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


BROWSE_TRIES = 2  # attempts per page of a browsed material set
SUPPLY = 3  # usable unread records that make a plain new search worth less than reading them
PLANNER_CHECK = ("id", "text", "required", "importance", "status", "evidence")  # Bio-Planner Check


def candidates(run: "Run"):
    """Every action the planner may choose next, each with what it is expected to add (weigh): searches
    and fetches of the active searches, the next page of a large fixed material set, further pages of
    searches still finding new records, and the unread hits in reading order."""
    live = fetch_allowed(run)
    done = {
        (
            a["kind"],
            a["source"],
            search.channel_of(a),
            tuple(a["args"].get("terms", ())),
            a["args"].get("page"),
            a["args"].get("record_id"),
        )
        for a in run.p["actions"]
    }
    offered = searches(run, done, live) + browse_pages(run) + next_pages(run) + reads(run)
    return weigh(run, offered)


def fetch_allowed(run: "Run"):
    """Fresh collection only in current mode on real data, and only with Fabric's admin token; without
    the token there is no fetch step, said once."""
    live = run.cutoff is None and not run.input["is_demo"]
    if live and not run.s.fabric_admin_token:
        live = False
        if all(n["code"] != "fetch_not_configured" for n in run.p.get("notes", [])):
            run.note("fetch_not_configured", "没有 BIORADAR_FABRIC_ADMIN_TOKEN，不发起新采集")
    return live


def searches(run: "Run", done, live):
    """Each active search in every source and channel not searched yet; once searched literally, a fresh
    fetch where Fabric says the source can be collected for a search term and the search found little.
    The semantic search is verified by Fabric at the analysis cutoff, so replays use it too."""
    result = []
    for q in run.p["queries"]:
        for source, channel in search.routes(run):
            key = ("search", source, channel, tuple(q["terms"]), None, None)
            if key not in done:
                result.append(
                    {
                        "kind": "search",
                        "source": source,
                        "channel": channel,
                        "args": {"terms": q["terms"]},
                        "target_checks": q["checks"],
                        # an object a read record named with a decisive fact (Bio-Planner ranks it
                        # with the question's named objects)
                        "follows_up": q.get("origin", "").startswith("object:"),
                        "_value": q.get("value", 1),
                    }
                )
            elif (
                live
                and channel == search.LITERAL
                and run.fabric.caps is not None
                and run.fabric.caps.query_fetch(source)
                and ("fetch", source, channel, tuple(q["terms"]), None, None) not in done
            ):
                page = run.p["pages"].get(f"{source}|{'|'.join(q['terms'])}", {})
                if page.get("found", 0) < 3:
                    result.append(
                        {
                            "kind": "fetch",
                            "source": source,
                            "channel": channel,
                            "args": {"terms": q["terms"]},
                            "target_checks": q["checks"],
                            "cost": 3,
                        }
                    )
    return result


def browse_pages(run: "Run"):
    """A large fixed material set (the task's own scope): list what is visible at the cutoff, so discovery
    does not depend on a search term happening to match (PHX attempt 4, W1: 17 of 20 searches found 0-1
    records). An ordinary candidate, same budget; no ranking by importance."""
    if not (run.scope and len(run.scope) > FABRIC_PAGE):
        return []
    result = []
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
                    "channel": search.LITERAL,
                    "args": {"terms": [], "browse": "scope", "page": waiting[0]},
                    "target_checks": required,
                    "_value": 1,
                }
            )
    return result


def next_pages(run: "Run"):
    """The next page of each search that has one and still found new records, up to MAX_PAGES."""
    result = []
    for key, page in run.p["pages"].items():
        if page.get("cursor") and page["pages"] < MAX_PAGES and page["new"]:
            source, *terms = key.split("|")
            result.append(
                {
                    "kind": "next_page",
                    "source": source,
                    "channel": search.LITERAL,
                    "args": {"terms": terms, "page": page["pages"] + 1},
                    "target_checks": page["checks"],
                }
            )
    return result


class ReadingOrder:
    """Reading order (planner only; the baseline keeps its fixed order). Fabric returns literal hits in
    record-id order, so without this a tie is read alphabetically. coverage: the share of this plan's
    search terms found in the record's text, a cheap ordering feature, not relevance or truth."""

    def __init__(self, run: "Run"):
        self.run = run
        self.objects = run.p.get("objects", [])
        self.terms = list(dict.fromkeys(squash(t) for q in run.p["queries"] for t in q["terms"]))
        # full texts of what was read, only for documents in memory; after a restart those not reloaded
        # are unknown, and a copy already recorded stays recorded
        self.read_bodies = {}
        for h in run.p["hits"]:
            if h["read"] and (b := self.body(h)):
                self.read_bodies.setdefault(b, f"{h['source']}:{h['record_id']}")

    def named(self, title):
        return any(o in (title or "") for o in self.objects)

    def body(self, hit):
        doc = self.run.docs.get(doc_key(hit))
        return None if doc is None else squash((doc["title"] or "") + (doc["text"] or ""))

    def sketch(self, hit):
        """The few ordering features of a hit, kept in progress so a resumed run orders the same way
        without the texts: which search terms its text contains, its own time, and which read record
        its full text is identical to (recorded only when both texts were compared)."""
        doc = self.run.docs.get(doc_key(hit))
        if doc is not None:
            old, b = hit.get("sketch", {}), self.body(hit)
            hit["sketch"] = {
                "matched": sorted(set(old.get("matched", [])) | {t for t in self.terms if t in b}),
                "when": time_value(doc.get("published_at")) or doc.get("available_at"),
            }
            copy = old.get("duplicate_of") or (None if hit["read"] else self.read_bodies.get(b))
            if copy:
                hit["sketch"]["duplicate_of"] = copy
        return hit.get("sketch") or {}

    def coverage(self, hit):
        matched = set(self.sketch(hit).get("matched", []))
        return len(matched & set(self.terms)) / len(self.terms) if self.terms else 0.0

    def newest(self, hit):  # the record's own visible time; unknown sorts last
        value = self.sketch(hit).get("when")
        try:
            return -datetime.fromisoformat(value).timestamp() if value else 0.0
        except (TypeError, ValueError):
            return 0.0

    def key(self, hit):
        # corrections and seeds first; exact copies of a text already read go behind, never dropped
        return (
            not hit["critical"],
            bool(self.sketch(hit).get("duplicate_of")),
            not self.named(hit["title"]),
            -self.coverage(hit),
            self.newest(hit),
        )


def reads(run: "Run"):
    """The first 40 unread hits as read candidates, in reading order."""
    order = ReadingOrder(run)
    unread = [h for h in run.p["hits"] if not h["read"]]
    if run.input["method"] == "planner":
        unread.sort(key=order.key)
    else:
        unread.sort(key=lambda h: not h["critical"])
    return [
        {
            "kind": "read",
            "source": hit["source"],
            "args": {"record_id": hit["record_id"]}
            | ({"version_id": hit["version_id"]} if hit.get("version_id") else {}),
            "target_checks": hit["checks"],
            "critical": hit["critical"],
            # Bio-Planner's 0.4-1.0 range: search-term coverage of the text and a title naming an
            # object the question asks about
            "scope_match": round(
                0.4 + 0.3 * order.coverage(hit) + (0.3 if order.named(hit["title"]) else 0), 3
            ),
            "_copy": bool(order.sketch(hit).get("duplicate_of")),
        }
        for hit in unread[:40]
    ]


def weigh(run: "Run", result):
    """What each candidate is expected to add, as Bio-Planner's impact: a search is worth more the
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


def series_state(run: "Run"):
    """The series values this analysis rests on (recorded when it was created), each with its quality
    state as of now: a Fabric object's state is read again from Fabric for these points alone (at the cutoff,
    with the permission of now), so a value corrected or revoked during the run reaches the planner as it
    stands. A point of the radar's own frozen chain is history: never current. Read only - the planner may
    choose to re-read a value's record, it cannot admit a value to a series."""
    recorded = (run.input.get("series_points") or [])[:64]
    if not recorded:
        return []
    objects = [p for p in recorded if series_sync.is_fabric_id(p["measurement_id"])]
    frozen = [p["measurement_id"] for p in recorded if not series_sync.is_fabric_id(p["measurement_id"])]
    with db.session() as session:
        points = measure_history.series_points(session, frozen, run.cutoff) if frozen else []
    if objects:
        points += series_sync.read_points(
            run.s, run.fabric, objects, run.cutoff, is_demo=run.input["is_demo"]
        )
    # the planner contract's field lengths (SeriesPoint)
    keys = {"source": 80, "record_id": 400, "version_id": 128, "object": 200, "metric": 200, "value": 40}
    keys |= {"unit": 50, "period": 40}
    return [
        {k: str(p.get(k) or "")[:n] for k, n in keys.items()}
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


def planner_request(run: "Run", offered):
    """The request of one round: the question, the checks, what may be done next and what was done. A check
    goes with the status it has: answered (with its E# evidence: a candidate answer the reader grounded,
    not a verified one), conflict (facts for and against) or open. What 05, 06 and 07 know of a check
    reaches the planner as one of these three and nothing else."""
    budget = run.input["budget"]
    reads = sum(1 for a in run.p["actions"] if a["kind"] == "read" and a["status"] == "completed")
    return {
        "question": run.asked,
        # only Bio-Planner's Check contract; objects and the rest stay in the radar
        "checks": [{k: c[k] for k in PLANNER_CHECK} for c in run.p["checks"]],
        "candidates": offered,
        "history": decisions.history(run.p["actions"]),
        "budget": {
            "steps_left": max(0, budget["max_steps"] - len(run.p["actions"])),
            "reads_left": max(0, budget["max_reads"] - reads),
        },
        "policy": run.input["method"],
        # the series values the question rests on, quality state re-read now (read only)
        "series": series_state(run),
        # the material this investigation may use, and what the radar says about each way of finding it
        "sources": list(run.input["sources"]),
        "channels": decisions.channels(run),
    }


def investigate(run: "Run"):
    planner = decisions.planner_snapshot(run)
    while True:
        offered = candidates(run)
        request = planner_request(run, offered)
        run.upstream_still_readable()
        try:
            decision = services.decide(run.s, request)
        except ServiceError as exc:
            decisions.keep(run, request, planner, stop=f"planner_{exc.code}")
            if not run.p["actions"]:
                run.save()  # the record of the failed first call outlives the failed run
                raise Failed(f"planner_{exc.code}") from exc
            run.note(f"planner_{exc.code}")
            run.p["stop"] = {"decision": "insufficient", "reason": f"planner_{exc.code}"}
            return
        run.p["stop"] = {"decision": decision["decision"], "reason": decision["reason"]}
        if decision["decision"] != "continue":
            decisions.keep(run, request, planner, response=decision, stop=decision["reason"])
            return
        if run.calls_left() <= 3:  # keep the calls the report needs
            run.p["stop"] = {"decision": "insufficient", "reason": "llm_call_limit"}
            decisions.keep(run, request, planner, response=decision, stop="llm_call_limit")
            return
        # the chosen candidate becomes the action at this index, appended once it has been done
        index = len(run.p["actions"])
        decisions.keep(run, request, planner, response=decision, action_index=index)
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
    if "channel" in candidate:
        action["channel"] = candidate["channel"]
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
