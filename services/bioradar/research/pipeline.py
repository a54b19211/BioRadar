"""One analysis, stage by stage: plan -> investigate -> compute -> mechanism -> report -> assess.

Progress is saved after every stage and after every investigation action. A restarted job continues
from the saved stage; a model call that was in flight when the worker died is sent again."""

import re
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import delete, func, select

from bioradar import db
from bioradar.adapters import llm, services, weknora
from bioradar.adapters.fabric import Fabric, FabricError
from bioradar.adapters.services import ServiceError
from bioradar.config import Settings
from bioradar.research import compute, prompts, schemas

STAGES = ["plan", "investigate", "compute", "mechanism", "report", "assess", "done"]
MAX_PAGES = 3
MAX_QUERIES = 12
PLANNER_CHECK = ("id", "text", "required", "importance", "status", "evidence")  # query-planner Check
REVISIONS = 2  # revise -> re-verify rounds; each only touches the sentences the last check flagged
MAX_RETRIES = 6  # retries of empty searches; a fixed-order baseline must keep steps to read
FABRIC_PAGE = 100  # Fabric page cap; a larger fixed material set is filtered here, not in the query
OVERALL = {"supported": "机制获得支持", "partial": "机制部分获得支持", "insufficient": "机制证据不足"}
FETCHABLE = {"clinicaltrials", "pubmed", "fda_crl", "europe_pmc", "who_don"}
EXCERPT_CHARACTERS = 4000


KIND = {
    "primary_research": "research",
    "review": "research",
    "conference_abstract": "research",
    "trial_registry": "registry",
    "press_release": "company",
    "corporate_disclosure": "company",
    "official_notice": "official",
    "news_report": "news",
    "social_media": "social",
    "repost": "social",
}
SOURCE_KIND = {
    "clinicaltrials": "registry",
    "pubmed": "research",
    "europe_pmc": "research",
    "who_don": "official",
    "fda_crl": "official",
}


# MechEvidence (汪鑫怡) only evaluates research material; news, notices and posts are not sent.
MECH_KIND = {
    "primary_research": {"source_kind": "primary", "document_type": "research_article"},
    "conference_abstract": {"source_kind": "primary", "document_type": "conference_abstract"},
    "trial_registry": {"source_kind": "primary", "document_type": "trial_registration"},
    "review": {"source_kind": "review", "document_type": "review"},
    "trial": {"source_kind": "primary", "document_type": "trial_registration"},
    "outcome": {"source_kind": "primary", "document_type": "trial_results"},
}
MECH_LEVEL = {"full_text": "full", "abstract": "abstract"}


def source_kind(material_kind, source):
    """Fabric material kind -> the six source kinds BioCheck understands."""
    return KIND.get(material_kind) or SOURCE_KIND.get(source, "news")


class Failed(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def clean_terms(terms):
    result = []
    for term in terms:
        term = " ".join(str(term).split())[:120]
        if len(term) >= 2 and term not in result:
            result.append(term)
    return result[:3]


CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*$")


def code_roots(terms):
    """Drug and trial codes refer to each other by a shared letter root: XQ-17, DEMO-XQ301 -> XQ.

    Fabric matches terms literally, so a code-like term also gets its letter roots as single-term queries."""
    roots = []
    for term in terms:
        if CODE.match(term):
            for part in re.findall(r"[A-Za-z]{2,}", term):
                if part.upper() not in {"DEMO", "TEST", "NCT", "ID"} and [part] not in roots:
                    roots.append([part])
    return roots


def squash(text):
    return re.sub(r"\s+", "", text or "")


def adopt(text, terms, limit):
    """Whole text when short; otherwise the paragraphs that mention the search terms, in original order."""
    if len(text) <= limit:
        return text, "full"
    paragraphs = [p for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    ranked = sorted(range(len(paragraphs)), key=lambda i: (-sum(paragraphs[i].count(t) for t in terms), i))
    keep, size = set(), 0
    for i in ranked:
        if size + len(paragraphs[i]) > limit:
            continue
        keep.add(i)
        size += len(paragraphs[i]) + 2
    return "\n…\n".join(paragraphs[i] for i in sorted(keep)) or text[:limit], "selected"


def excerpt(text, quotes, limit=EXCERPT_CHARACTERS):
    """Report input: windows around the quoted facts, so BioCheck sees the context of every cited fact."""
    if len(text) <= limit:
        return text
    spans = []
    for quote in quotes:
        # Quotes were checked with whitespace removed, so locate them the same way.
        found = re.search(r"\s*".join(map(re.escape, squash(quote))), text) if squash(quote) else None
        if found:
            spans.append([max(0, found.start() - 400), min(len(text), found.end() + 400)])
    spans.sort()
    merged = []
    for span in spans:
        if merged and span[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], span[1])
        else:
            merged.append(span)
    parts, size = [], 0
    for start, end in merged:
        if size + end - start > limit:
            break
        parts.append(text[start:end])
        size += end - start
    return "\n…\n".join(parts) or text[:limit]


class Run:
    def __init__(self, analysis: db.Analysis, settings: Settings, fabric: Fabric | None = None):
        self.id = analysis.id
        self.row_investigation = analysis.investigation_id
        self.input = analysis.input
        self.p = dict(analysis.progress or {})
        self.stage = analysis.stage
        self.s = settings
        self.fabric = fabric or Fabric(settings)
        self.docs = {}  # (source, record_id) -> document read in this process
        with db.session() as s:
            self.calls_used = (
                s.scalar(select(func.sum(db.LlmCall.requests)).where(db.LlmCall.analysis_id == self.id)) or 0
            )

    # ---- bookkeeping ---------------------------------------------------------------------------

    @property
    def cutoff(self):
        value = self.input.get("cutoff")
        return datetime.fromisoformat(value) if value else None

    def note(self, code, detail=""):
        self.p.setdefault("notes", []).append(
            {"code": code, "detail": str(detail)[:300], "stage": self.stage}
        )

    def log(self, calls, service="radar"):
        with db.session() as s:
            for c in calls:
                usage = c.get("usage") or {}
                prompt, completion = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                s.add(
                    db.LlmCall(
                        analysis_id=self.id,
                        stage=c.get("stage", "?"),
                        service=c.get("service", service),
                        model=c.get("model"),
                        finish_reason=c.get("finish_reason"),
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        total_tokens=usage.get("total_tokens") or prompt + completion,
                        seconds=c.get("seconds") or 0,
                        error=c.get("error"),
                        requests=c.get("requests") or 1,
                    )
                )
        self.calls_used += sum(c.get("requests") or 1 for c in calls)

    def calls_left(self):
        return self.input["budget"]["max_llm_calls"] - self.calls_used

    def ask(self, stage, system, payload, model_cls, **options):
        if self.calls_left() <= 0:
            self.note("llm_budget", stage)
            return None
        parsed, call = llm.chat_json(self.s, stage, system, payload, model_cls, **options)
        self.log([call])
        if call["error"] == "output_invalid" and self.calls_left() > 0:  # one retry; it is usually a one-off
            parsed, call = llm.chat_json(self.s, stage, system, payload, model_cls, **options)
            self.log([call])
        if call["error"]:
            self.note(call["error"], f"{stage} {call.get('detail', '')}")
        return parsed

    def save(self, stage=None):
        if stage:
            self.stage = stage
        with db.session() as s:
            row = s.get(db.Analysis, self.id)
            row.progress, row.stage = dict(self.p), self.stage

    # ---- driver --------------------------------------------------------------------------------

    def run(self):
        steps = {
            "plan": self.plan,
            "investigate": self.investigate,
            "compute": self.compute,
            "mechanism": self.mechanism,
            "report": self.report,
            "assess": self.assess,
        }
        while self.stage != "done":
            steps[self.stage]()
            self.save(STAGES[STAGES.index(self.stage) + 1])
        return self.result()

    # ---- plan ----------------------------------------------------------------------------------

    def plan(self):
        payload = {
            "question": self.input["question"],
            "direction": self.input["direction"],
            "history_cutoff": self.input.get("cutoff"),
        }
        if self.input.get("previous_report"):
            payload["previous_report"] = self.input["previous_report"]["draft"][:3000]
        plan = self.ask("plan", prompts.PLAN, payload, schemas.Plan)
        if plan is None or not plan.checks:
            raise Failed("plan_failed")
        named = [o.name.strip() for o in plan.objects[:8]]  # check indexes refer to this list
        objects = list(dict.fromkeys(n for n in named if n))
        checks = [
            {
                "id": f"Q{i}",
                "text": c.text,
                "required": c.required,
                "importance": 1.0 if c.required else 0.5,
                "status": "open",
                "evidence": [],
                # names of the objects this check is about (R8a: answered only when each is covered)
                "objects": list(
                    dict.fromkeys(named[n - 1] for n in c.objects if 1 <= n <= len(named) and named[n - 1])
                ),
            }
            for i, c in enumerate(plan.checks[:8], 1)
        ]
        if not any(c["required"] for c in checks):
            checks[0]["required"] = True
        required = [c["id"] for c in checks if c["required"]]
        queries = []
        for q in plan.queries[:10]:
            terms = clean_terms(q.terms)
            if terms and all(terms != x["terms"] for x in queries):
                ids = [f"Q{n}" for n in q.checks if 1 <= n <= len(checks)]
                queries.append({"terms": terms, "checks": ids or required})
        for root in code_roots([t for q in queries for t in q["terms"]]):
            if all(root != q["terms"] for q in queries):
                queries.append({"terms": root, "checks": required})
        if not queries:
            raise Failed("plan_without_queries")
        hypotheses = [
            {"id": f"H{n}", **h.model_dump()}
            for n, h in enumerate(plan.hypotheses[:3], 1)
            if h.statement.strip()
        ]
        self.p.update(
            hypotheses=hypotheses,
            objects=objects,
            checks=checks,
            queries=queries,
            judgments=plan.judgments[:4],
            needs_calculation=plan.needs_calculation or self.input["direction"] == "business",
            mechanism=plan.mechanism.model_dump() if plan.mechanism else None,
            actions=[],
            hits=[],
            facts=[],
            relations=[],
            pages={},
            refs={},
            retries=0,
        )
        for seed in self.input.get("seed_records", []):
            self.add_hit(seed["source"], seed["record_id"], seed.get("title", ""), required, critical=True)
        # A fixed material set: searches stay inside it, and a small set's records visible at the
        # cutoff are reading candidates.
        if self.scope and len(self.scope) <= FABRIC_PAGE:
            for source in self.input["sources"]:
                docs, _ = self.fabric.search(
                    source,
                    (),
                    is_demo=self.input["is_demo"],
                    cutoff=self.cutoff,
                    record_ids=self.scope,
                    limit=len(self.scope),
                )
                for doc in docs:
                    self.docs[(doc["source"], doc["record_id"])] = doc
                    self.add_hit(doc["source"], doc["record_id"], doc["title"], required, critical=True)

    @property
    def scope(self):
        return self.input.get("scope_record_ids") or []

    # ---- investigate ---------------------------------------------------------------------------

    def add_hit(self, source, record_id, title, checks, *, critical=False):
        if self.scope and record_id not in self.scope:
            return False  # a seed or correction target outside the fixed material set
        for hit in self.p["hits"]:
            if (hit["source"], hit["record_id"]) == (source, record_id):
                hit["checks"] = sorted(set(hit["checks"]) | set(checks))
                hit["critical"] = hit["critical"] or critical
                hit["found_by"] = hit.get("found_by", 1) + 1
                return False
        self.p["hits"].append(
            {
                "source": source,
                "record_id": record_id,
                "title": title[:200],
                "checks": list(checks),
                "critical": critical,
                "read": False,
                "found_by": 1,
            }
        )
        return True

    def candidates(self):
        live = self.cutoff is None and not self.input["is_demo"]
        done = {
            (
                a["kind"],
                a["source"],
                tuple(a["args"].get("terms", ())),
                a["args"].get("page"),
                a["args"].get("record_id"),
            )
            for a in self.p["actions"]
        }
        sources = list(self.input["sources"]) + (["weknora"] if self.s.weknora_url else [])
        result = []
        for q in self.p["queries"]:
            for source in sources:
                key = ("search", source, tuple(q["terms"]), None, None)
                if key not in done:
                    result.append(
                        {
                            "kind": "search",
                            "source": source,
                            "args": {"terms": q["terms"]},
                            "target_checks": q["checks"],
                        }
                    )
                elif (
                    live
                    and source in FETCHABLE
                    and ("fetch", source, tuple(q["terms"]), None, None) not in done
                ):
                    page = self.p["pages"].get(f"{source}|{'|'.join(q['terms'])}", {})
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
        for key, page in self.p["pages"].items():
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
        objects = self.p.get("objects", [])

        def named(title):
            return any(o in (title or "") for o in objects)

        unread = [h for h in self.p["hits"] if not h["read"]]
        if self.input["method"] == "planner":
            unread.sort(key=lambda h: (not h["critical"], not named(h["title"])))
        else:  # the baseline keeps its fixed order
            unread.sort(key=lambda h: not h["critical"])
        for hit in unread[:40]:
            result.append(
                {
                    "kind": "read",
                    "source": hit["source"],
                    "args": {"record_id": hit["record_id"]},
                    "target_checks": hit["checks"],
                    "critical": hit["critical"],
                    # A record that several different searches find is more likely on topic.
                    # Only the planner uses this; the baseline keeps its fixed order.
                    # and one whose title names an object the question asks about
                    "scope_match": min(
                        1.0, 0.4 + 0.3 * hit.get("found_by", 1) + (0.3 if named(hit["title"]) else 0)
                    ),
                }
            )
        return result

    def history(self):
        return [
            {
                "kind": a["kind"],
                "source": a["source"],
                "target_checks": a["target_checks"],
                "gain": a["gain"],
                "args": {k: v for k, v in a["args"].items()},
                "status": a["status"],
            }
            for a in self.p["actions"]
        ]

    def investigate(self):
        budget = self.input["budget"]
        while True:
            reads = sum(1 for a in self.p["actions"] if a["kind"] == "read" and a["status"] == "completed")
            candidates = self.candidates()
            request = {
                "question": self.input["question"],
                # only query-planner's Check contract; objects and the rest stay in the radar
                "checks": [{k: c[k] for k in PLANNER_CHECK} for c in self.p["checks"]],
                "candidates": candidates,
                "history": self.history(),
                "budget": {
                    "steps_left": max(0, budget["max_steps"] - len(self.p["actions"])),
                    "reads_left": max(0, budget["max_reads"] - reads),
                },
                "policy": self.input["method"],
            }
            try:
                decision = services.decide(self.s, request)
            except ServiceError as exc:
                if not self.p["actions"]:
                    raise Failed(f"planner_{exc.code}") from exc
                self.note(f"planner_{exc.code}")
                self.p["stop"] = {"decision": "insufficient", "reason": f"planner_{exc.code}"}
                return
            self.p["stop"] = {"decision": decision["decision"], "reason": decision["reason"]}
            if decision["decision"] != "continue":
                return
            if self.calls_left() <= 3:  # keep the calls the report needs
                self.p["stop"] = {"decision": "insufficient", "reason": "llm_call_limit"}
                return
            self.act(candidates[decision["candidate"]])
            self.save()

    def act(self, candidate):
        action = {
            "kind": candidate["kind"],
            "source": candidate["source"],
            "args": candidate["args"],
            "target_checks": candidate["target_checks"],
            "gain": 0.0,
            "status": "completed",
        }
        try:
            if candidate["kind"] == "read":
                action["gain"], action["ref"] = self.read(candidate)
            else:
                found, new = self.search(candidate)
                action["found"], action["new"] = found, new
                action["gain"] = 0.3 if new else 0.0
        except (FabricError, ServiceError) as exc:
            action["status"], action["error"] = "failed", exc.code
            self.note(exc.code, f"{candidate['kind']} {candidate['source']}")
        except httpx.HTTPError as exc:  # timeout, refused connection, 5xx from WeKnora
            action["status"], action["error"] = "failed", "http_error"
            self.note("http_error", f"{candidate['kind']} {candidate['source']}: {type(exc).__name__}")
        self.p["actions"].append(action)

    def search(self, candidate):
        source, terms, kind = candidate["source"], candidate["args"]["terms"], candidate["kind"]
        key = f"{source}|{'|'.join(terms)}"
        if source == "weknora":
            hits = weknora.search(self.s, " ".join(terms))
            docs = []
            for hit_source, record_id, _ in hits:
                if hit_source in self.input["sources"]:
                    found, _ = self.fabric.search(
                        hit_source,
                        (),
                        is_demo=self.input["is_demo"],
                        cutoff=self.cutoff,
                        record_ids=[record_id],
                        limit=1,
                    )
                    docs += found
            cursor = None
        elif kind == "fetch":
            docs, cursor = self.fabric.fetch(source, " ".join(terms)), None
        else:
            page = self.p["pages"].get(key, {})
            docs, cursor = self.fabric.search(
                source,
                terms,
                is_demo=self.input["is_demo"],
                cutoff=self.cutoff,
                cursor=page.get("cursor") if kind == "next_page" else None,
                record_ids=self.scope if len(self.scope) <= FABRIC_PAGE else (),
                limit=FABRIC_PAGE if len(self.scope) > FABRIC_PAGE else 10,
            )
        # A large set is filtered here; a page without in-set hits must not end paging.
        more = bool(docs) and len(self.scope) > FABRIC_PAGE
        if self.scope:
            inside = set(self.scope)
            docs = [d for d in docs if d["record_id"] in inside]
        new = 0
        for doc in docs:
            self.docs[(doc["source"], doc["record_id"])] = doc
            new += self.add_hit(doc["source"], doc["record_id"], doc["title"], candidate["target_checks"])
        if source != "weknora":
            page = self.p["pages"].setdefault(
                key, {"pages": 0, "found": 0, "checks": candidate["target_checks"]}
            )
            page.update(
                cursor=cursor, pages=page["pages"] + 1, new=new or more, found=page["found"] + len(docs)
            )
        if not docs and kind == "search" and len(terms) > 1 and source != "weknora":
            # Terms are ANDed: drop the last one, and also the first in case the anchor term is the miss.
            for shorter in (terms[:-1], terms[1:]):
                if self.p["retries"] < MAX_RETRIES and all(shorter != q["terms"] for q in self.p["queries"]):
                    self.p["retries"] += 1
                    self.p["queries"].append({"terms": shorter, "checks": candidate["target_checks"]})
        return len(docs), new

    def read(self, candidate):
        source, record_id = candidate["source"], candidate["args"]["record_id"]
        hit = next(h for h in self.p["hits"] if (h["source"], h["record_id"]) == (source, record_id))
        hit["read"] = True
        doc = self.docs.get((source, record_id))
        if doc is None:
            found, _ = self.fabric.search(
                source, (), is_demo=self.input["is_demo"], cutoff=self.cutoff, record_ids=[record_id], limit=1
            )
            if not found:
                raise FabricError("record_not_visible", record_id)
            doc = found[0]
        terms = [t for q in self.p["queries"] if set(q["checks"]) & set(hit["checks"]) for t in q["terms"]]
        text, window = adopt(doc["text"], terms, self.s.read_characters)
        ref = f"E{len(self.p['refs']) + 1}"
        self.p["refs"][ref] = f"{source}|{record_id}"
        with db.session() as s:
            # A worker that died mid-read may have left this ref behind without saving progress.
            s.execute(delete(db.Evidence).where(db.Evidence.analysis_id == self.id, db.Evidence.ref == ref))
            s.add(
                db.Evidence(
                    analysis_id=self.id,
                    ref=ref,
                    source=source,
                    record_id=record_id,
                    version_id=doc["version_id"],
                    title=doc["title"],
                    published_at=doc["published_at"],
                    available_at=str(doc["available_at"]),
                    material_kind=doc["material_kind"],
                    verified=doc["verified"],
                    text=text,
                    meta={
                        "url": doc["url"],
                        "window": window,
                        "text_level": doc["text_level"],
                        "relations": doc["relations"],
                        "origin_group": doc["origin_group"],
                        "events": doc["events"],
                    },
                )
            )
        for relation in doc["relations"]:
            self.p["relations"].append(
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
                self.add_hit(target, relation["target_record_id"], "", hit["checks"], critical=True)
        checks = {c["id"]: c for c in self.p["checks"]}
        material = {
            "ref": ref,
            "title": doc["title"],
            "source": source,
            "published_at": doc["published_at"],
            "material_kind": doc["material_kind"],
            "verified": doc["verified"],
            "events": doc["events"],
            "text": text,
        }
        reading = self.ask(
            "read",
            prompts.READ,
            {
                "question": self.input["question"],
                "material": material,
                "objects": self.p.get("objects", []),
                "hypotheses": [
                    {"id": h["id"], "statement": h["statement"]} for h in self.p.get("hypotheses", [])
                ],
                "checks": [
                    {
                        "id": c["id"],
                        "text": c["text"],
                        "status": c["status"],
                        "objects": c.get("objects", []),
                        "known": [
                            f"{f['ref']}: {f['text']}" for f in self.p["facts"] if c["id"] in f["checks"]
                        ][-12:],
                    }
                    for c in self.p["checks"]
                ],
            },
            schemas.Reading,
        )
        if reading is None or not reading.relevant:
            return 0.0, ref
        before = {c["id"] for c in self.p["checks"] if c["status"] != "open"}
        flat = squash(text)
        for fact in reading.facts[:10]:
            if not fact.quote or squash(fact.quote) not in flat:
                self.note("quote_not_found", f"{ref}: {fact.quote[:60]}")
                continue
            ids = [i for i in fact.checks if i in checks]
            self.p["facts"].append(
                {
                    "id": f"F{len(self.p['facts']) + 1}",
                    "ref": ref,
                    "text": fact.text,
                    "quote": fact.quote,
                    "checks": ids,
                    "stance": fact.stance,
                    "object": fact.object if fact.object in self.p.get("objects", []) else "",
                    "hypothesis": fact.hypothesis
                    if fact.hypothesis in {h["id"] for h in self.p.get("hypotheses", [])}
                    else "",
                }
            )
            if fact.stance == "context":
                continue
            for i in ids:
                check = checks[i]
                if ref not in check["evidence"]:
                    check["evidence"].append(ref)
                stances = {f["stance"] for f in self.p["facts"] if i in f["checks"]}
                if {"supports", "refutes"} <= stances:
                    check["status"] = "conflict"
        # One record often answers part of a check (one company of three); the model says when it is whole.
        for i in reading.answered:
            check = checks.get(i)
            if not check or not check["evidence"] or check["status"] != "open":
                continue
            # every object the check names needs a supporting or refuting fact of its own
            covered = {
                f.get("object")
                for f in self.p["facts"]
                if i in f["checks"] and f["stance"] in {"supports", "refutes"}
            }
            if all(o in covered for o in check.get("objects", [])):
                check["status"] = "answered"
        for terms in reading.new_terms[:2]:
            terms = clean_terms(terms)
            if (
                terms
                and len(self.p["queries"]) < MAX_QUERIES
                and all(terms != q["terms"] for q in self.p["queries"])
            ):
                open_ids = [c["id"] for c in self.p["checks"] if c["status"] != "answered"]
                self.p["queries"].append({"terms": terms, "checks": hit["checks"] or open_ids})
        after = {c["id"] for c in self.p["checks"] if c["status"] != "open"}
        return min(1.0, len(after - before) / max(1, len(candidate["target_checks"]))), ref

    # ---- compute -------------------------------------------------------------------------------

    def compute(self):
        numeric = [f for f in self.p["facts"] if compute.numbers_in(f["quote"])]
        self.p["computations"] = []
        if not self.p.get("needs_calculation") or len(numeric) < 2:
            return
        result = self.ask(
            "calculate",
            prompts.CALCULATE,
            {
                "question": self.input["question"],
                "corrections": self.p["relations"],
                "facts": [
                    {"id": f["id"], "ref": f["ref"], "text": f["text"], "quote": f["quote"]} for f in numeric
                ],
            },
            schemas.Calculations,
        )
        facts = {f["id"]: f for f in numeric}
        for calc in (result.calculations if result else [])[:10]:
            if not calc.operands or any(
                o.fact not in facts or not compute.appears(o.value, facts[o.fact]["quote"])
                for o in calc.operands
            ):
                self.note("calculation_operand_not_in_source", calc.label)
                continue
            try:
                value = compute.evaluate(calc.expression, [Decimal(str(o.value)) for o in calc.operands])
            except ValueError as exc:
                self.note(str(exc), calc.label)
                continue
            self.p["computations"].append(
                {
                    "id": f"A{len(self.p['computations']) + 1}",
                    "label": calc.label,
                    "value": compute.rounded(value),
                    "expression": calc.expression,
                    "unit": calc.unit,
                    "period": calc.period,
                    "population": calc.population,
                    "denominator": calc.denominator,
                    "operands": [
                        # The quote travels with the result so BioCheck can check it without the record.
                        {
                            "fact": o.fact,
                            "value": o.value,
                            "ref": facts[o.fact]["ref"],
                            "quote": facts[o.fact]["quote"],
                        }
                        for o in calc.operands
                    ],
                }
            )

    # ---- mechanism -----------------------------------------------------------------------------

    def evidence_rows(self):
        with db.session() as s:
            return list(
                s.scalars(
                    select(db.Evidence).where(db.Evidence.analysis_id == self.id).order_by(db.Evidence.id)
                )
            )

    def mechanism(self):
        target = self.p.get("mechanism")
        if not target or not self.s.mechevidence_url:
            self.p["mechanism_result"] = (
                None if not target else {"status": "not_run", "reason": "not_configured"}
            )
            return
        cited = {f["ref"] for f in self.p["facts"]}
        materials = [
            {
                "id": r.ref,
                "title": r.title,
                "text": r.text,
                **MECH_KIND[r.material_kind],
                "text_level": "excerpt"
                if r.meta.get("window") == "selected"
                else MECH_LEVEL.get(r.meta.get("text_level"), "excerpt"),
            }
            for r in self.evidence_rows()
            if r.ref in cited and r.material_kind in MECH_KIND
        ]
        if not materials:
            self.p["mechanism_result"] = {"status": "not_run", "reason": "no_research_material"}
            return
        try:
            response = services.mechevidence(
                self.s,
                {
                    "question": self.input["question"],
                    "mechanism": target["statement"],
                    "scope": {k: target.get(k, "") for k in ("species", "system", "disease")},
                    "max_llm_calls": max(1, min(10, self.calls_left() - 4)),
                    "materials": materials,
                },
            )
        except ServiceError as exc:
            self.log(exc.calls, "mechevidence")
            self.note(f"mechevidence_{exc.code}")
            self.p["mechanism_result"] = {"status": "failed", "reason": exc.code}
            return
        self.log(response.pop("llm_calls", []), "mechevidence")
        self.p["mechanism_result"] = {"status": "completed", **response}

    # ---- report --------------------------------------------------------------------------------

    def report_input(self):
        rows = self.evidence_rows()
        refs = {(r.source, r.record_id): r for r in rows}
        quotes = {}
        for fact in self.p["facts"]:
            quotes.setdefault(fact["ref"], []).append(fact["quote"])
        # Both ends of a correction go to BioCheck even if one yielded no fact.
        related = {r["to"] for r in self.p["relations"]} | {r["from"] for r in self.p["relations"]}
        evidence = [
            {
                "id": r.ref,
                "title": r.title,
                "source": r.source,
                "published_at": r.published_at,
                "source_kind": source_kind(r.material_kind, r.source),
                "verified": r.verified,
                "text": excerpt(r.text, quotes.get(r.ref, [])),
            }
            for r in rows
            if r.ref in quotes or r.record_id in related
        ]
        results = [
            {
                "id": c["id"],
                "method": "radar_arithmetic",
                "output": {"value": c["value"], "label": c["label"]},
                "calculation_basis": {
                    k: c[k] for k in ("expression", "operands", "unit", "period", "population", "denominator")
                },
                "limitations": ["程序按所列原文数值计算，口径以calculation_basis为准"],
            }
            for c in self.p.get("computations", [])
        ]
        mech = self.p.get("mechanism_result")
        if mech and mech.get("status") == "completed":
            results.append(
                {
                    "id": f"A{len(results) + 1}",
                    "method": "mechevidence",
                    # The writer copies values into Chinese prose, so the verdict goes in as words.
                    "output": {
                        "overall": OVERALL.get(mech.get("overall"), mech.get("overall")),
                        **{k: mech.get(k) for k in ("rationale", "gaps", "conflicts")},
                    },
                    "calculation_basis": {"mechanism": self.p["mechanism"]["statement"]},
                    "limitations": ["机制评价是专业判断，不是实验原件"],
                }
            )

        def bound(r):
            """The (old, new) evidence rows a relation links: same source and record, and the same
            version when Fabric names one; None when either end was not read."""
            old = refs.get((r.get("to_source", r["source"]), r["to"]))
            new = refs.get((r["source"], r["from"]))
            if old and new and (not r.get("to_version") or old.version_id == r["to_version"]):
                return old, new
            return None

        updates = [
            {
                "target": pair[0].ref,
                "replacement": pair[1].ref,
                "kind": "correction" if r["kind"] == "corrects" else "retraction",
            }
            for r in self.p["relations"]
            if r["kind"] in {"corrects", "retracts"} and (pair := bound(r))
        ]
        searched = {i for a in self.p["actions"] if a["status"] == "completed" for i in a["target_checks"]}
        failed = [
            f"{a['kind']} {a['source']} {a.get('error')}"
            for a in self.p["actions"]
            if a["status"] == "failed"
        ]
        body = {
            "question": self.input["question"],
            "evidence": evidence,
            "analysis_results": results,
            "source_updates": updates,
            "research_scope": {
                "checked": [
                    c["text"]
                    + (
                        "（已找到依据）"
                        if c["status"] != "open"
                        else "（已找到部分依据，未答全）"
                        if c["evidence"]
                        else "（已检索，未找到肯定或否定依据）"
                    )
                    for c in self.p["checks"]
                    if c["id"] in searched
                ],
                "not_checked": [c["text"] for c in self.p["checks"] if c["id"] not in searched],
                "failed": failed,
                "history_cutoff": self.input.get("cutoff"),
            },
        }
        if self.input.get("previous_report"):
            body["previous_report"] = self.input["previous_report"]
        if self.p.get("hypotheses"):
            # The hypotheses and the facts tagged to them go to BioCheck, which writes and checks the
            # 「情报假设」 section; assess only grades what survived that check.
            cited = {e["id"] for e in evidence}
            body["hypotheses"] = [
                dict(
                    h,
                    facts=[
                        {"ref": f["ref"], "text": f["text"], "stance": f["stance"]}
                        for f in self.p["facts"]
                        if f.get("hypothesis") == h["id"] and f["ref"] in cited
                    ],
                )
                for h in self.p["hypotheses"]
            ]
        return body

    def service(self, operation, body):
        body["max_llm_calls"] = max(1, self.calls_left() - 1)
        response = services.biocheck(self.s, operation, body)
        self.log(response.pop("llm_calls", []), "biocheck")
        return response

    def report(self):
        body = self.report_input()
        report = {"status": "NOT_RUN", "draft": "", "revised": False}
        self.p["report"] = report
        if not body["evidence"]:
            report["reason"] = "no_evidence"
            return
        try:
            report["draft"] = self.service("generate", dict(body))["draft"]
            review = self.service("verify", dict(body, draft=report["draft"]))
            for _ in range(REVISIONS):
                fixable = [
                    c
                    for c in review["claims"]
                    if c["verdict"] == "contradicted"
                    or c.get("scope_span")
                    or (c["verdict"] == "insufficient" and c["citations"] and c["labels"])
                    or (c["verdict"] == "insufficient" and not c["citations"])
                ]
                if review["status"] == "PASS" or not fixable or self.calls_left() <= 2:
                    break
                revised = self.service("revise", dict(body, draft=report["draft"], review=review))
                if not revised.get("changed"):
                    break
                report.update(draft=revised["draft"], revised=True, changed=revised["changed"])
                review = self.service("verify", dict(body, draft=report["draft"]))
        except ServiceError as exc:
            self.log(exc.calls, "biocheck")
            report["reason"] = f"biocheck_{exc.code}"
            self.note(report["reason"])
            return
        report.update(
            status=review["status"],
            claims=review["claims"],
            coverage=review.get("coverage"),
            read_requests=review.get("read_requests", []),
        )

    # ---- assess --------------------------------------------------------------------------------

    def assess(self):
        report = self.p["report"]
        if not report.get("draft"):
            self.p["assessment"] = None
            return
        rows = self.evidence_rows()
        result = self.ask(
            "assess",
            prompts.ASSESS,
            {
                "question": self.input["question"],
                "judgments": self.p.get("judgments", []),
                "report": report["draft"],
                "report_status": report["status"],
                "hypotheses": [
                    {"id": h["id"], "statement": h["statement"]} for h in self.p.get("hypotheses", [])
                ],
                "checks": [{k: c[k] for k in ("id", "text", "status", "evidence")} for c in self.p["checks"]],
                "evidence": [
                    {
                        "ref": r.ref,
                        "title": r.title,
                        "published_at": r.published_at,
                        "material_kind": r.material_kind,
                        "verified": r.verified,
                    }
                    for r in rows
                ],
            },
            schemas.Assessment,
        )
        if result is None:
            self.p["assessment"] = None
            return
        cited = set(re.findall(r"\[(E\d+)\]", report["draft"]))
        for judgment in result.judgments:
            judgment.grounds = [g for g in judgment.grounds if g in cited]
        known = {h["id"] for h in self.p.get("hypotheses", [])}
        result.hypotheses = [h for h in result.hypotheses if h.id in known]
        # A grade cannot go past the verified report. BioCheck writes hypotheses as 待验证 and never
        # as confirmed, so assess cannot confirm either; probable needs a tagged supporting fact whose
        # citing sentence BioCheck found supported in a PASS report and no such refutation; refuted
        # needs a verified refutation. Anything else is at most possible.
        verified = {
            e for c in report.get("claims", []) if c["verdict"] == "supported" for e in c["citations"]
        }
        order = ["insufficient", "possible", "probable", "confirmed"]
        for h in result.hypotheses:
            stances = {
                f["stance"]
                for f in self.p.get("facts", [])
                if f.get("hypothesis") == h.id and f["ref"] in verified
            }
            if h.level == "refuted":
                cap = "refuted" if "refutes" in stances else "possible"
            else:
                strong = report["status"] == "PASS" and "supports" in stances and "refutes" not in stances
                cap = "probable" if strong else "possible"
                cap = h.level if order.index(h.level) <= order.index(cap) else cap
            if cap != h.level:
                h.reason = f"评估给 {h.level}，已核验报告只撑得住 {cap}"
                h.level = cap
        self.p["assessment"] = result.model_dump()
        self.watch()

    def watch(self):
        """A topic analysis leaves its hypotheses' confirming and refuting signals on the topic, so the
        monitor can tell when a later record answers one (W1..; at most 5)."""
        if not self.p.get("hypotheses"):
            return
        with db.session() as s:
            topic_id = s.scalar(
                select(db.Investigation.topic_id).where(db.Investigation.id == self.row_investigation)
            )
            if not topic_id:
                return
            items = [
                {"id": "", "hypothesis": h["id"], "direction": direction, "text": text}
                for h in self.p["hypotheses"]
                for direction in ("confirm", "refute")
                for text in h.get(direction, [])[:1]
            ][:5]
            for n, item in enumerate(items, 1):
                item["id"] = f"W{n}"
            s.add(
                db.TopicEvent(
                    topic_id=topic_id,
                    at=db.now(),
                    kind="watch",
                    detail={"items": items, "analysis_id": self.id},
                )
            )

    # ---- result --------------------------------------------------------------------------------

    def result(self):
        with db.session() as s:
            usage = s.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(db.LlmCall.requests), 0),
                    func.coalesce(func.sum(db.LlmCall.total_tokens), 0),
                ).where(db.LlmCall.analysis_id == self.id)
            ).one()
        report = self.p.get("report") or {}
        return {
            "report_status": report.get("status", "NOT_RUN"),
            "draft": report.get("draft", ""),
            "review": {
                k: report.get(k) for k in ("claims", "coverage", "read_requests", "reason", "revised")
            },
            "assessment": self.p.get("assessment"),
            "checks": self.p.get("checks", []),
            "computations": self.p.get("computations", []),
            "mechanism": self.p.get("mechanism_result"),
            "stop": self.p.get("stop"),
            "reads": sum(1 for h in self.p.get("hits", []) if h["read"]),
            "notes": self.p.get("notes", []),
            # llm_calls: logical model calls; requests: HTTP requests behind them (what the budget counts)
            "usage": {"llm_calls": usage[0], "requests": int(usage[1]), "tokens": int(usage[2])},
        }
