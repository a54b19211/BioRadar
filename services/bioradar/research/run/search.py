"""Investigation: finding records - search, fetch, next page, browsing a fixed material set - and the
bounded pools of hits and searches they feed."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


MAX_PAGES = 3
MAX_QUERIES = 12
MAX_RETIRED = 6  # searches used up and replaced by new evidence's searches, per analysis
MAX_RETRIES = 6  # retries of empty searches; a fixed-order baseline must keep steps to read
FABRIC_PAGE = 100  # Fabric page cap; a larger fixed material set is filtered here, not in the query
# Fabric index target scopes searched per analysis direction (the scopes of DATAFABRIC_INDEX_TARGETS).
SEARCH_SCOPES = {
    "pipeline": ("pipeline", "research"),
    "science": ("research",),
    "business": ("business", "pipeline"),
    "public_health": ("public_health", "research"),
}


def doc_key(hit):
    """The document cache key of a hit: a pinned hit (a correction notice) has its version in the key."""
    return (hit["source"], hit["record_id"]) + ((hit["version_id"],) if hit.get("version_id") else ())


def clean_terms(terms):
    result = []
    for term in terms:
        term = " ".join(str(term).split())[:120]
        if len(term) >= 2 and term not in result:
            result.append(term)
    return result[:3]


def add_hit(run: "Run", source, record_id, title, checks, *, critical=False, version_id=None):
    """version_id: a hit pinned to that fixed version (a correction notice) - its own hit, read as that
    version, never merged with the record's head."""
    if run.scope and record_id not in run.scope:
        return False  # a seed or correction target outside the fixed material set
    for hit in run.p["hits"]:
        if (hit["source"], hit["record_id"], hit.get("version_id")) == (source, record_id, version_id):
            hit["checks"] = sorted(set(hit["checks"]) | set(checks))
            hit["critical"] = hit["critical"] or critical
            hit["found_by"] = hit.get("found_by", 1) + 1
            return False
    run.p["hits"].append(
        {
            "source": source,
            "record_id": record_id,
            "title": title[:200],
            "checks": list(checks),
            "critical": critical,
            "read": False,
            "found_by": 1,
        }
        | ({"version_id": version_id} if version_id else {})
    )
    return True


def browse(run: "Run", source, page, checks):
    """One page (FABRIC_PAGE ids) of the fixed material set: its records visible at the cutoff become
    reading candidates, read later like any other hit. One Fabric request per action, so an action's
    cost does not grow with the set; the pages done are kept in progress for a resumed run, and a
    page that failed is offered again. In current mode every page, retry and resumed run
    reads at the time of the source's first browse, kept in progress before the first request."""
    ids = run.scope[page * FABRIC_PAGE : (page + 1) * FABRIC_PAGE]
    at = run.p.setdefault("browse_at", {}).setdefault(source, datetime.now(UTC).isoformat())
    docs, _ = run.fabric.search(
        source,
        (),
        is_demo=run.input["is_demo"],
        cutoff=run.cutoff,
        record_ids=ids,
        limit=len(ids),
        now=datetime.fromisoformat(at),
    )
    new = 0
    for doc in docs:
        run.docs[(doc["source"], doc["record_id"])] = doc
        new += add_hit(run, doc["source"], doc["record_id"], doc["title"], checks)
    run.p.setdefault("browsed", {}).setdefault(source, []).append(page)
    pages = -(-len(run.scope) // FABRIC_PAGE)
    run.note(
        "scope_browsed",
        f"{source}: 第 {page + 1}/{pages} 页（{len(ids)} 个记录号），"
        f"截止时可见 {len(docs)} 条，新增候选 {new} 条",
    )
    return len(docs), new


def search(run: "Run", candidate):
    source, terms, kind = candidate["source"], candidate["args"]["terms"], candidate["kind"]
    key = f"{source}|{'|'.join(terms)}"
    if candidate["args"].get("browse"):
        return browse(run, source, candidate["args"]["page"], candidate["target_checks"])
    if source == "weknora":
        hits, summary = run.fabric.semantic_search(
            " ".join(terms),
            scopes=SEARCH_SCOPES.get(run.input["direction"], ("research",)),
            is_demo=run.input["is_demo"],
            cutoff=run.cutoff,
        )
        run.p.setdefault("weknora", []).append({"terms": terms, **summary})
        for scope in summary["recall"]:
            if scope.get("degraded"):
                run.note("weknora_scope", f"{','.join(scope['scopes'] or [])}: degraded")
        # each hit is already the version visible at this cutoff: read exactly that version
        refs = {}
        artifacts = {}
        positions = run.p.setdefault("hit_positions", {})
        for hit in hits:
            if hit["source"] in run.input["sources"]:
                refs.setdefault(hit["source"], {})[(hit["record_id"], hit["version_id"])] = None
                # read exactly the cleaned artifact the hit was verified against
                artifacts[hit["version_id"]] = hit.get("artifact") or None
                # where in that artifact the hit is: the read window takes that passage first
                art = hit.get("artifact") or {}
                place = {
                    "version_id": hit["version_id"],
                    "processor_version": art.get("processor_version"),
                    "text_sha256": art.get("text_sha256"),
                    "range": hit.get("range"),
                }
                if None in (hit.get("range") or [None, None])[:2]:
                    place["text"] = (hit.get("text") or "")[:2000]
                kept = positions.setdefault(f"{hit['source']}|{hit['record_id']}", [])
                if place not in kept and len(kept) < 5:
                    kept.append(place)
        docs = []
        for src, pairs in refs.items():
            found, missing = run.fabric.read_versions(
                src, list(pairs), is_demo=run.input["is_demo"], cutoff=run.cutoff, artifacts=artifacts
            )
            for version_id in missing:  # revoked or withdrawn between the search and the read
                run.note("weknora_version_unreadable", f"{src}: {version_id[:12]}")
            docs += found
        cursor = None
    elif kind == "fetch":
        docs, cursor = run.fabric.fetch(source, " ".join(terms)), None
    else:
        page = run.p["pages"].get(key, {})
        # current mode: a next page is read at the time of its first page (Fabric rejects the cursor
        # otherwise: invalid_cursor on live Europe PMC)
        at = page.get("at") if kind == "next_page" else None
        at = datetime.fromisoformat(at) if at else datetime.now(UTC)
        docs, cursor = run.fabric.search(
            source,
            terms,
            is_demo=run.input["is_demo"],
            cutoff=run.cutoff,
            cursor=page.get("cursor") if kind == "next_page" else None,
            record_ids=run.scope if len(run.scope) <= FABRIC_PAGE else (),
            limit=FABRIC_PAGE if len(run.scope) > FABRIC_PAGE else 10,
            now=at,
        )
    # A large set is filtered here; a page without in-set hits must not end paging.
    more = bool(docs) and len(run.scope) > FABRIC_PAGE
    if run.scope:
        inside = set(run.scope)
        docs = [d for d in docs if d["record_id"] in inside]
    new = 0
    for doc in docs:
        run.docs[(doc["source"], doc["record_id"])] = doc
        new += add_hit(run, doc["source"], doc["record_id"], doc["title"], candidate["target_checks"])
    if source != "weknora":
        page = run.p["pages"].setdefault(key, {"pages": 0, "found": 0, "checks": candidate["target_checks"]})
        page.update(cursor=cursor, pages=page["pages"] + 1, new=new or more, found=page["found"] + len(docs))
        if kind == "search" and run.cutoff is None:
            page["at"] = at.isoformat()
    if not docs and kind == "search" and len(terms) > 1 and source != "weknora":
        # Terms are ANDed: drop the last one, and also the first in case the anchor term is the miss.
        for shorter in (terms[:-1], terms[1:]):
            if run.p["retries"] < MAX_RETRIES and all(shorter != q["terms"] for q in run.p["queries"]):
                run.p["retries"] += 1
                origin = next((q.get("origin", "") for q in run.p["queries"] if q["terms"] == terms), "")
                run.p["queries"].append(
                    {"terms": shorter, "checks": candidate["target_checks"], "origin": origin}
                )
    return len(docs), new


def add_query(run: "Run", terms, checks, *, value, origin):
    """A bounded pool of active searches. When it is full, a search already run whose pages are used up
    is retired first (it stays in actions, and is never run again); otherwise a new search may take the
    place of one not run yet that is worth less (plan terms 1, terms from a background-only record 0),
    so early background records cannot fill the pool for good."""
    if not terms or any(terms == q["terms"] for q in run.p["queries"]):
        return
    query = {"terms": terms, "checks": checks, "value": value, "origin": origin}
    if len(run.p["queries"]) < MAX_QUERIES:
        run.p["queries"].append(query)
        return
    ran = {
        (a.get("source", ""), tuple(a["args"].get("terms", ())))
        for a in run.p["actions"]
        if a["kind"] in {"search", "fetch"}
    }
    paging = {
        tuple(key.split("|")[1:])
        for key, page in run.p.get("pages", {}).items()
        if page.get("cursor") and page["pages"] < MAX_PAGES and page["new"]
    }
    sources = list(run.input["sources"]) + (["weknora"] if run.s.semantic_search else [])
    # retired only once every source it applies to has been searched (or failed) and paged out
    done = [
        i
        for i, q in enumerate(run.p["queries"])
        if all((src, tuple(q["terms"])) in ran for src in sources) and tuple(q["terms"]) not in paging
    ]
    ran = {terms for _, terms in ran}
    waiting = [
        (q.get("value", 1), i)
        for i, q in enumerate(run.p["queries"])
        if tuple(q["terms"]) not in ran and q.get("value", 1) < value
    ]
    # only evidence (a found object, terms from a decisive record) may take a used-up search's place,
    # and only up to MAX_RETIRED times: PHX attempt 3 kept rotating background terms in, both methods
    # spent 19-25 of 30 steps searching and read 5-11 records
    retired = sum(1 for n in run.p.get("notes", []) if n["code"] == "query_retired")
    if done and value >= 2 and retired < MAX_RETIRED:
        i, code = done[0], "query_retired"
    elif waiting:
        i, code = min(waiting)[1], "query_replaced"
    else:
        return
    run.note(code, f"{' '.join(run.p['queries'][i]['terms'])} -> {' '.join(terms)}")
    run.p["queries"][i] = query
