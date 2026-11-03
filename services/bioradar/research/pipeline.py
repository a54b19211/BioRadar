"""One analysis, stage by stage: plan -> investigate -> compute -> mechanism -> report -> assess.

Progress is saved after every stage and after every investigation action. A restarted job continues
from the saved stage; a model call that was in flight when the worker died is sent again."""

import bisect
import json
import re
from datetime import UTC, datetime
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
DEMO_SETTING = (
    "\n（情景说明：这是演练用的模拟情景，材料里的地点、机构、人物和数字都是为演练编写的。请在这个情景内部、"
    "按材料本身的证据判断；不要因为材料是虚构的就认定没有事件，或因此降低可信度、后果和核实优先级。）"
)
MAX_PAGES = 3
MAX_QUERIES = 12
MAX_FOUND = 12  # objects found in read records, beyond those the question names
MAX_RETIRED = 6  # searches used up and replaced by new evidence's searches, per analysis
BROWSE_TRIES = 2  # attempts per page of a browsed material set
SUPPLY = 3  # usable unread records that make a plain new search worth less than reading them
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
    """Report input: windows around the quoted facts, so BioCheck sees the context of every cited fact.
    Every quote goes in first; the room left then widens each quote, shortest first, to its whole line
    (a registry field's title-only quote such as "Total Energy Expenditure" is useless without the values
    on that line, Codex r40), and what is still left widens the quotes not yet widened (first 100 before
    and 900 after, since values follow their titles). Nothing quoted is dropped for context (Codex r38)."""
    if len(text) <= limit:
        return text
    spans = []
    for quote in quotes:
        # Quotes were checked with whitespace removed, so locate them the same way.
        hit = re.search(r"\s*".join(map(re.escape, squash(quote))), text) if squash(quote) else None
        if hit:
            spans.append([hit.start(), hit.end()])

    def merged(windows):
        out = []
        for a, b in sorted(windows):
            if out and a <= out[-1][1]:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out

    def size(windows):
        return sum(b - a for a, b in merged(windows))

    def line(a, b):
        end = text.find("\n", b)
        return [text.rfind("\n", 0, a) + 1, len(text) if end < 0 else end]

    windows = [list(s) for s in spans]
    widened = set()

    def widen(i, a, b):  # never narrower than what window i already has
        return windows[:i] + [[min(a, windows[i][0]), max(b, windows[i][1])]] + windows[i + 1 :]

    # a quote with no number in it (a table or field title) gets the numbers after it first
    for i, (a, b) in enumerate(spans):
        if not re.search(r"\d", text[a:b]):
            trial = widen(i, a, min(line(a, b)[1], b + 900))
            if size(trial) <= limit:
                windows = trial
    for i in sorted(range(len(spans)), key=lambda i: line(*spans[i])[1] - line(*spans[i])[0]):
        trial = widen(i, *line(*spans[i]))
        if size(trial) <= limit:
            windows, _ = trial, widened.add(i)
    # values follow their titles in records and tables, so the first try reaches further forward
    for before, after in ((100, 900), (400, 400), (150, 150)):
        for i, (a, b) in enumerate(spans):
            if i in widened:
                continue
            trial = widen(i, max(0, a - before), min(len(text), b + after))
            if size(trial) <= limit:
                windows, _ = trial, widened.add(i)
    parts = [text[a:b] for a, b in merged(windows)]
    return "\n…\n".join(parts) or text[:limit]


# A trial registry's fields the writer gets whether or not a fact quoted them (Codex r41): what the trial
# is now and its primary results. Each line names its JSON pointer in the same fixed version.
REGISTRY_ESSENTIALS = (
    "status",
    "phase",
    "study_type",
    "enrollment",
    "enrollment_type",
    "start_date",
    "primary_completion_date",
    "primary_completion_date_type",
    "completion_date",
    "has_results",
)
ESSENTIALS_SHARE = 0.6  # of the record's report characters; the rest is for the quoted passages


def report_text(text, quotes, limit=EXCERPT_CHARACTERS):
    """A record's report input: for a trial registry record its essential fields and primary end points
    first (dropped from the end, with a note naming what was left out, when they do not fit), then the
    quoted passages in what is left; any other record goes to excerpt() unchanged."""
    if "【登记字段：" not in text or len(text) <= limit:
        return excerpt(text, quotes, limit)
    keys = tuple(f" {k}（" for k in REGISTRY_ESSENTIALS)
    lines = text.split("\n")
    wanted = [ln for ln in lines if any(k in ln for k in keys) or ln.startswith("  终点 PRIMARY")]
    kept, left_out, size = [], [], 0
    for ln in wanted:
        if size + len(ln) + 1 <= limit * ESSENTIALS_SHARE:
            kept.append(ln)
            size += len(ln) + 1
        else:
            left_out.append(ln.split("（")[0].strip())
    if left_out:
        kept.append(f"（写稿输入放不下、未提供：{'、'.join(left_out)}；原件里有）")
    head = "\n".join(kept)
    rest = "\n".join(ln for ln in lines if ln not in kept)
    return head + "\n…\n" + excerpt(rest, quotes, max(0, limit - len(head)))


LINEAGE_DEPTH = 500  # rounds followed back; a longer, looping or broken chain counts as unknown


class UnknownLineage(Exception):
    """The chain of earlier rounds could not be followed to its start."""


def lineage(session, analysis_id: str, depth=LINEAGE_DEPTH) -> list[str]:
    """The analysis's earlier rounds, nearest first (a follow-up's parent, its parent, ...). Raises
    UnknownLineage when the chain loops, breaks off at a missing row or is longer than `depth`: an
    unfinished walk must never pass for "no earlier sources" (Codex r40: round 21 lost its root's limits)."""
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


def hypothesis_stance(fact):
    """A fact's direction for its hypothesis, judged apart from its stance on the checks (an answer to a
    check can refute the hypothesis); facts saved before the field existed fall back to the check stance."""
    return fact.get("hypothesis_stance", fact["stance"])


def hypothesis_citations(draft, claims, ids):
    """{hypothesis id: citations of the supported sentences in its paragraph} for the hypotheses the
    draft writes up. A paragraph runs from the id (first one inside 「情报假设」 when that section
    exists) to the next hypothesis id or line break; ids the draft never names are left out."""
    section = max(draft.find("情报假设"), 0)
    starts = {}
    for h in ids:
        found = re.compile(re.escape(h) + r"(?!\d)").search(draft, section)
        if found:
            starts[h] = found.start()
    bounds = sorted(starts.values()) + [len(draft)]
    cited = {}
    for h, start in starts.items():
        end = bounds[bounds.index(start) + 1]
        if "\n" in draft[start:end]:
            end = draft.index("\n", start)
        cited[h] = set()
        for c in claims:
            at = c["start"] if "start" in c else draft.find(c.get("text", ""))
            if c["verdict"] == "supported" and start <= at < end:
                cited[h].update(c["citations"])
    return cited


class Run:
    def __init__(self, analysis: db.Analysis, settings: Settings, fabric: Fabric | None = None):
        self.found = 0.0  # what the current read found apart from answering (evidence_gain)
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
        options["max_requests"] = self.calls_left()
        parsed, call = llm.chat_json(self.s, stage, system, payload, model_cls, **options)
        self.log([call])
        if call["error"] == "output_invalid" and self.calls_left() > 0:  # one retry; it is usually a one-off
            options["max_requests"] = self.calls_left()
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
            # once the plan used the earlier report, every later stage carries what came from it (checks,
            # search terms, the report's research scope); if that report's sources can no longer be read,
            # the run stops with the reason instead of passing those on (Codex r41)
            if self.stage != "plan":
                self.upstream_still_readable()
            steps[self.stage]()
            self.save(STAGES[STAGES.index(self.stage) + 1])
        return self.result()

    def upstream_still_readable(self):
        """Raise when the plan used the earlier report and its sources can no longer be read: checked at
        each stage and again at the writer's service boundary."""
        if self.p.get("used_previous") and self.previous_report() is None:
            raise Failed("upstream_access_withdrawn")

    def previous_report(self):
        """The earlier round's report, only while every source it was written from (through all earlier
        rounds) can still be read. Checked at each use (plan, then report), since a source can be revoked
        while the run goes on (Codex r38, r40); not checked once and remembered."""
        if not self.input.get("previous_report"):
            return None
        sources = lineage_sources(self.id)
        readable = set()
        if sources:
            try:
                readable = {r["source_id"] for r in self.fabric.sources()["sources"]}
            except (FabricError, httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
                readable = None
        if sources is None or (sources and (readable is None or not sources <= readable)):
            if all(n["code"] != "previous_report_withheld" for n in self.p.get("notes", [])):
                self.note("previous_report_withheld", "上一轮引用的来源现在不可读、授权状态或上游链无法确认")
            return None
        return self.input["previous_report"]

    @property
    def asked(self):
        """The question as the models and services see it. Scenario material says it is invented; without
        this the models judged "fictional, so no real event, low priority" (PHX). Same for every method;
        real data gets the question unchanged."""
        if self.input["is_demo"]:
            return self.input["question"] + DEMO_SETTING
        return self.input["question"]

    # ---- plan ----------------------------------------------------------------------------------

    def plan(self):
        payload = {
            "question": self.asked,
            "direction": self.input["direction"],
            "history_cutoff": self.input.get("cutoff"),
        }
        previous = self.previous_report()
        if previous:
            payload["previous_report"] = previous["draft"][:3000]
            self.p["used_previous"] = True
        plan = self.ask("plan", prompts.PLAN, payload, schemas.Plan)
        if plan is None or not plan.checks:
            raise Failed("plan_failed")
        planned = [o.name.strip() for o in plan.objects[:8]]
        # T-22: where each planned name comes from, decided here and not reported by the model: the
        # question, the previous report (a draft, not an original record), or nowhere (exploratory). Only
        # names from the question become objects; the others may steer searching but never narrow what
        # counts as relevant. plan_basis records every planned name's basis and plan_removed the objects
        # left out with the checks they were attached to (the model's raw output is not kept in progress).
        shown = squash(self.input["question"]), squash(payload.get("previous_report", ""))

        def basis(name):
            name = squash(name)
            return "question" if name in shown[0] else "prior_report" if name in shown[1] else "exploratory"

        named = [n if n and basis(n) == "question" else "" for n in planned]  # check indexes
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
        for c, planned_check in zip(checks, plan.checks):
            # a check only about objects the question never named is a direction, never required
            if planned_check.objects and not c["objects"]:
                c["required"], c["importance"] = False, 0.5
        if not any(c["required"] for c in checks):
            anchor = next((c for c, pc in zip(checks, plan.checks) if not pc.objects or c["objects"]), None)
            if anchor is None:  # every check is about names the question never gave: ask the question itself
                anchor = {
                    "id": f"Q{len(checks) + 1}",
                    "text": self.input["question"][:300],
                    "importance": 1.0,
                    "status": "open",
                    "evidence": [],
                    "objects": [],
                }
                checks.append(anchor)
            anchor["required"] = True
        required = [c["id"] for c in checks if c["required"]]
        queries = []
        for q in plan.queries[:10]:
            terms = clean_terms(q.terms)
            if terms and all(terms != x["terms"] for x in queries):
                ids = [f"Q{n}" for n in q.checks if 1 <= n <= len(checks)]
                queries.append({"terms": terms, "checks": ids or required, "origin": "plan"})
        for root in code_roots([t for q in queries for t in q["terms"]]):
            if all(root != q["terms"] for q in queries):
                queries.append({"terms": root, "checks": required, "origin": "plan"})
        if not queries:
            raise Failed("plan_without_queries")
        terms = [t for q in queries for t in q["terms"]]
        plan_basis = {n: basis(n) for n in dict.fromkeys([n for n in planned if n] + terms)}
        plan_removed = [
            {
                "name": n,
                "basis": basis(n),
                "checks": [f"Q{i}" for i, c in enumerate(plan.checks[:8], 1) if k in c.objects],
            }
            for k, n in enumerate(planned, 1)
            if n and basis(n) != "question"
        ]
        exploratory = [n for n, b in plan_basis.items() if b != "question"]
        for c in checks:
            # shown to the reader as search directions that do not limit the question's scope
            c["exploratory"] = [n for n in exploratory if squash(n) in squash(c["text"])]
        if any(c["exploratory"] for c in checks):
            self.note(
                "plan_exploratory_scope",
                "；".join(f"{c['id']}:{'、'.join(c['exploratory'])}" for c in checks),
            )
        hypotheses = [
            {"id": f"H{n}", **h.model_dump()}
            for n, h in enumerate(plan.hypotheses[:3], 1)
            if h.statement.strip()
        ]
        self.p.update(
            hypotheses=hypotheses,
            objects=objects,
            plan_basis=plan_basis,
            plan_removed=plan_removed,
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
        self.graph(required)
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

    def browse(self, source, page, checks):
        """One page (FABRIC_PAGE ids) of the fixed material set: its records visible at the cutoff become
        reading candidates, read later like any other hit. One Fabric request per action, so an action's
        cost does not grow with the set; the pages done are kept in progress for a resumed run, and a
        page that failed is offered again (Codex r42)."""
        ids = self.scope[page * FABRIC_PAGE : (page + 1) * FABRIC_PAGE]
        docs, _ = self.fabric.search(
            source,
            (),
            is_demo=self.input["is_demo"],
            cutoff=self.cutoff,
            record_ids=ids,
            limit=len(ids),
            now=datetime.now(UTC),
        )
        new = 0
        for doc in docs:
            self.docs[(doc["source"], doc["record_id"])] = doc
            new += self.add_hit(doc["source"], doc["record_id"], doc["title"], checks)
        self.p.setdefault("browsed", {}).setdefault(source, []).append(page)
        pages = -(-len(self.scope) // FABRIC_PAGE)
        self.note(
            "scope_browsed",
            f"{source}: 第 {page + 1}/{pages} 页（{len(ids)} 个记录号），"
            f"截止时可见 {len(docs)} 条，新增候选 {new} 条",
        )
        return len(docs), new

    def graph(self, checks):
        """T-20: the question's own objects looked up in Tang's reviewed graph (read only, through Fabric).
        Graph records visible at the cutoff that mention the exact name are resolved; each candidate keeps
        its identity status (unresolved stays unresolved) and the records it was read from become reading
        candidates, re-read from Fabric at the cutoff like any other hit. Nothing is merged or inferred."""
        if not self.s.graph_lookup:
            return
        found, seen = self.p.setdefault("graph", []), set()
        for name in self.p["objects"][:3]:
            entry = {"object": name, "graph_records": [], "candidates": [], "refs": []}
            try:
                records, _ = self.fabric.search(
                    "reviewed_graph", [name], is_demo=self.input["is_demo"], cutoff=self.cutoff, limit=5
                )
                resolutions = [(r, self.fabric.graph_resolve(r, name, cutoff=self.cutoff)) for r in records]
            except (FabricError, httpx.HTTPError) as exc:
                self.note("graph_unavailable", f"{name}: {getattr(exc, 'code', type(exc).__name__)}")
                continue
            for record, resolution in resolutions:
                entry["graph_records"].append(record["record_id"])
                try:  # a graph record is one bundle; its document_refs are the documents it was built from
                    bundle_refs = json.loads(record["text"] or "{}").get("document_refs") or []
                except ValueError:
                    bundle_refs = []
                for node in resolution.get("candidates") or []:
                    entry["candidates"].append(
                        {
                            "id": node.get("id"),
                            "name": node.get("name"),
                            "identity": node.get("identity_status"),
                        }
                    )
                    # the node's own evidence, else the documents of the bundle it was found in
                    refs = [(ev, "node") for ev in node.get("evidence") or []] or [
                        (ev, "bundle") for ev in bundle_refs
                    ]
                    for ev, basis in refs:
                        key = (ev["source_id"], ev["source_record_id"])
                        if ev["source_id"] not in self.input["sources"] or key in seen:
                            continue
                        seen.add(key)
                        entry["refs"].append(
                            {
                                "source": key[0],
                                "record_id": key[1],
                                "version_id": ev["version_id"],
                                "basis": basis,
                            }
                        )
                        self.add_hit(*key, f"图谱：{node.get('name')}", checks)
            found.append(entry)

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
        # fresh collection needs Fabric's admin token; without it there is no fetch step, said once
        if live and not self.s.fabric_admin_token:
            live = False
            if all(n["code"] != "fetch_not_configured" for n in self.p.get("notes", [])):
                self.note("fetch_not_configured", "没有 BIORADAR_FABRIC_ADMIN_TOKEN，不发起新采集")
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
        # The bridge searches today's index, so a replay would choose records by text seen later.
        bridge = self.s.weknora_url and self.cutoff is None
        sources = list(self.input["sources"]) + (["weknora"] if bridge else [])
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
                            # an object a read record named with a decisive fact (query-planner ranks it
                            # with the question's named objects)
                            "follows_up": q.get("origin", "").startswith("object:"),
                            "_value": q.get("value", 1),
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
        if self.scope and len(self.scope) > FABRIC_PAGE:
            # a large fixed material set (the task's own scope): list what is visible at the cutoff, so
            # discovery does not depend on a search term happening to match (PHX attempt 4, W1: 17 of 20
            # searches found 0-1 records). An ordinary candidate, same budget; no ranking by importance.
            required = [c["id"] for c in self.p["checks"] if c.get("required")]
            pages = -(-len(self.scope) // FABRIC_PAGE)
            for source in self.input["sources"]:
                browsed = set(self.p.get("browsed", {}).get(source, []))
                failed = [
                    a["args"]["page"]
                    for a in self.p["actions"]
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

        # T-21 reading order (planner only; the baseline keeps its fixed order). Fabric returns literal hits
        # in record-id order, so without this a tie is read alphabetically. coverage: the share of this
        # plan's search terms found in the record's text, a cheap ordering feature, not relevance or truth.
        terms = list(dict.fromkeys(squash(t) for q in self.p["queries"] for t in q["terms"]))

        def body(hit):
            doc = self.docs.get((hit["source"], hit["record_id"]))
            return None if doc is None else squash((doc["title"] or "") + (doc["text"] or ""))

        # full texts of what was read, only for documents in memory; after a restart those not reloaded
        # are unknown, and a copy already recorded stays recorded
        read_bodies = {}
        for h in self.p["hits"]:
            if h["read"] and (b := body(h)):
                read_bodies.setdefault(b, f"{h['source']}:{h['record_id']}")

        def sketch(hit):
            """The few ordering features of a hit, kept in progress so a resumed run orders the same way
            without the texts: which search terms its text contains, its own time, and which read record
            its full text is identical to (recorded only when both texts were compared)."""
            doc = self.docs.get((hit["source"], hit["record_id"]))
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

        unread = [h for h in self.p["hits"] if not h["read"]]
        if self.input["method"] == "planner":
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
                    "args": {"record_id": hit["record_id"]},
                    "target_checks": hit["checks"],
                    "critical": hit["critical"],
                    # query-planner's 0.4-1.0 range: search-term coverage of the text and a title naming an
                    # object the question asks about
                    "scope_match": round(0.4 + 0.3 * coverage(hit) + (0.3 if named(hit["title"]) else 0), 3),
                    "_copy": bool(sketch(hit).get("duplicate_of")),
                }
            )
        return self.weigh(result)

    def weigh(self, result):
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
            # what a read found apart from answering checks (query-planner's evidence_gain)
            | ({"evidence_gain": a["evidence_gain"]} if "evidence_gain" in a else {})
            for a in self.p["actions"]
        ]

    def investigate(self):
        budget = self.input["budget"]
        while True:
            reads = sum(1 for a in self.p["actions"] if a["kind"] == "read" and a["status"] == "completed")
            candidates = self.candidates()
            request = {
                "question": self.asked,
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
        if candidate["kind"] in {"search", "fetch", "next_page"}:
            terms = candidate["args"].get("terms")
            action["origin"] = next(
                (q.get("origin", "") for q in self.p["queries"] if q["terms"] == terms), ""
            )
        try:
            if candidate["kind"] == "read":
                self.found = 0.0
                action["gain"], action["ref"] = self.read(candidate)
                action["evidence_gain"] = self.found
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
        if candidate["args"].get("browse"):
            return self.browse(source, candidate["args"]["page"], candidate["target_checks"])
        if source == "weknora":
            hits, scopes = weknora.search(
                self.s, " ".join(terms), direction=self.input["direction"], is_demo=self.input["is_demo"]
            )
            self.p.setdefault("weknora", []).append({"terms": terms, "scopes": scopes})
            for scope, state in scopes.items():
                if state.get("error") or state.get("degraded"):
                    self.note("weknora_scope", f"{scope}: {state.get('error') or 'degraded'}")
            docs = []
            for hit in hits:
                if hit["source"] in self.input["sources"]:
                    found, _ = self.fabric.search(
                        hit["source"],
                        (),
                        is_demo=self.input["is_demo"],
                        cutoff=self.cutoff,
                        record_ids=[hit["record_id"]],
                        limit=1,
                    )
                    if found and hit["version_id"] and found[0]["version_id"] != hit["version_id"]:
                        # the index holds another version; the current Fabric version is what is read
                        self.note("weknora_version_differs", f"{hit['record_id']}: {hit['version_id'][:12]}")
                    docs += found
            cursor = None
        elif kind == "fetch":
            docs, cursor = self.fabric.fetch(source, " ".join(terms)), None
        else:
            page = self.p["pages"].get(key, {})
            # current mode: a next page is read at the time of its first page (Fabric rejects the cursor
            # otherwise: invalid_cursor on live Europe PMC, T-19)
            at = page.get("at") if kind == "next_page" else None
            at = datetime.fromisoformat(at) if at else datetime.now(UTC)
            docs, cursor = self.fabric.search(
                source,
                terms,
                is_demo=self.input["is_demo"],
                cutoff=self.cutoff,
                cursor=page.get("cursor") if kind == "next_page" else None,
                record_ids=self.scope if len(self.scope) <= FABRIC_PAGE else (),
                limit=FABRIC_PAGE if len(self.scope) > FABRIC_PAGE else 10,
                now=at,
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
            if kind == "search" and self.cutoff is None:
                page["at"] = at.isoformat()
        if not docs and kind == "search" and len(terms) > 1 and source != "weknora":
            # Terms are ANDed: drop the last one, and also the first in case the anchor term is the miss.
            for shorter in (terms[:-1], terms[1:]):
                if self.p["retries"] < MAX_RETRIES and all(shorter != q["terms"] for q in self.p["queries"]):
                    self.p["retries"] += 1
                    origin = next((q.get("origin", "") for q in self.p["queries"] if q["terms"] == terms), "")
                    self.p["queries"].append(
                        {"terms": shorter, "checks": candidate["target_checks"], "origin": origin}
                    )
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
                self.found = 1.0  # a correction or retraction arrived
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
                "question": self.asked,
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
                        "exploratory": c.get("exploratory", []),
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
        facts_before = len(self.p["facts"])
        found_before_names = {o["name"] for o in self.p.get("found_objects", [])}
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
                    "object": self.object_of(fact, ref),
                    "hypothesis": fact.hypothesis
                    if fact.hypothesis in {h["id"] for h in self.p.get("hypotheses", [])}
                    else "",
                    "hypothesis_stance": fact.hypothesis_stance,
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
            # a check is answered by what records say, not by what one does not mention: facts that only
            # limit ("未提及更正") never close it (PHX-W4 stopped after two routine weekly reports).
            # Every object the check names needs a supporting or refuting fact of its own.
            decisive = [
                f for f in self.p["facts"] if i in f["checks"] and f["stance"] in {"supports", "refutes"}
            ]
            covered = {f.get("object") for f in decisive}
            if decisive and all(o in covered for o in check.get("objects", [])):
                check["status"] = "answered"
        open_ids = [c["id"] for c in self.p["checks"] if c["status"] != "answered"]
        new_facts = self.p["facts"][facts_before:]
        decisive = any(f["stance"] in {"supports", "refutes"} for f in new_facts)
        # a newly found object is followed up by name (later updates of that event); then the reader's terms,
        # worth more when this record said something decisive than when it was only background
        new_names = [
            o["name"] for o in self.p.get("found_objects", []) if o["name"] not in found_before_names
        ]
        self.found = max(self.found, 1.0 if new_names else 0.6 if decisive else 0.0)
        for name in new_names:
            self.add_query([name], hit["checks"] or open_ids, value=3, origin=f"object:{ref}")
        for terms in reading.new_terms[:2]:
            self.add_query(
                clean_terms(terms),
                hit["checks"] or open_ids,
                value=2 if decisive else 0,
                origin=f"read:{ref}",
            )
        after = {c["id"] for c in self.p["checks"] if c["status"] != "open"}
        return min(1.0, len(after - before) / max(1, len(candidate["target_checks"]))), ref

    def object_of(self, fact, ref):
        """The fact's object: one the question names, or one this fact's own quoted words name (every
        attribution needs its own quote, also for a name seen before). Only a fact that supports or refutes
        something enters a new name into found_objects, the names that are followed up; background facts
        keep their name but take no place there. Plan-stage guesses never get in; found objects do not
        narrow the question or its checks, and are never merged with each other."""
        name = " ".join((fact.object or "").split())[:80]
        if not name:
            return ""
        if name in self.p.get("objects", []):
            return name
        if len(name) < 2 or squash(name) not in squash(fact.quote):
            return ""
        found = self.p.setdefault("found_objects", [])
        known = any(o["name"] == name for o in found)
        if not known and fact.stance in {"supports", "refutes"} and len(found) < MAX_FOUND:
            found.append({"name": name, "ref": ref, "quote": fact.quote[:120]})
        return name

    def add_query(self, terms, checks, *, value, origin):
        """A bounded pool of active searches. When it is full, a search already run whose pages are used up
        is retired first (it stays in actions, and is never run again); otherwise a new search may take the
        place of one not run yet that is worth less (plan terms 1, terms from a background-only record 0),
        so early background records cannot fill the pool for good."""
        if not terms or any(terms == q["terms"] for q in self.p["queries"]):
            return
        query = {"terms": terms, "checks": checks, "value": value, "origin": origin}
        if len(self.p["queries"]) < MAX_QUERIES:
            self.p["queries"].append(query)
            return
        ran = {
            (a.get("source", ""), tuple(a["args"].get("terms", ())))
            for a in self.p["actions"]
            if a["kind"] in {"search", "fetch"}
        }
        paging = {
            tuple(key.split("|")[1:])
            for key, page in self.p.get("pages", {}).items()
            if page.get("cursor") and page["pages"] < MAX_PAGES and page["new"]
        }
        bridge = self.s.weknora_url and self.cutoff is None
        sources = list(self.input["sources"]) + (["weknora"] if bridge else [])
        # retired only once every source it applies to has been searched (or failed) and paged out
        done = [
            i
            for i, q in enumerate(self.p["queries"])
            if all((src, tuple(q["terms"])) in ran for src in sources) and tuple(q["terms"]) not in paging
        ]
        ran = {terms for _, terms in ran}
        waiting = [
            (q.get("value", 1), i)
            for i, q in enumerate(self.p["queries"])
            if tuple(q["terms"]) not in ran and q.get("value", 1) < value
        ]
        # only evidence (a found object, terms from a decisive record) may take a used-up search's place,
        # and only up to MAX_RETIRED times: PHX attempt 3 kept rotating background terms in, both methods
        # spent 19-25 of 30 steps searching and read 5-11 records
        retired = sum(1 for n in self.p.get("notes", []) if n["code"] == "query_retired")
        if done and value >= 2 and retired < MAX_RETIRED:
            i, code = done[0], "query_retired"
        elif waiting:
            i, code = min(waiting)[1], "query_replaced"
        else:
            return
        self.note(code, f"{' '.join(self.p['queries'][i]['terms'])} -> {' '.join(terms)}")
        self.p["queries"][i] = query

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
                "question": self.asked,
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
            share = calc.unit.strip() in {"%", "百分比"} or any(w in calc.label for w in ("比例", "占比"))
            if share and len(calc.operands) > 1:
                # a percentage needs every number's unit, read from that number's own quote, and one unit
                # for all (2 devices / 3 institutions is no proportion, C05); a unit the model only says
                # it saw does not count. The program does not check that the numerator is part of the
                # denominator's population: that stays with the reader and BioCheck.
                units = [o.unit.strip() for o in calc.operands]
                seen = all(u and u in facts[o.fact]["quote"] for u, o in zip(units, calc.operands))
                if not seen or len(set(units)) > 1:
                    code = "calculation_mixed_units" if seen else "calculation_unit_unverified"
                    self.note(code, f"{calc.label}: {'、'.join(u or '?' for u in units)}")
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
                    "question": self.asked,
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
        self.upstream_still_readable()  # the scope and checks below may carry the earlier report's words
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
                "text": report_text(r.text, quotes.get(r.ref, [])),
                # Fabric's explicit same-origin group; absent means independence is unknown, not proven
                "origin_group": (r.meta or {}).get("origin_group"),
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
        # T-23: reposts and supplements keep their own field (they do not supersede anything); the end not
        # read is named by record and marked unread instead of being guessed from a title or a site
        relations = []
        for r in self.p["relations"]:
            if r["kind"] not in {"reposts", "supplements"} or (r["source"], r["from"]) not in refs:
                continue
            pair = bound(r)
            relations.append(
                {
                    "kind": "repost" if r["kind"] == "reposts" else "supplement",
                    "source": refs[(r["source"], r["from"])].ref,
                    "target": pair[0].ref if pair else None,
                    "target_record": r["to"],
                    "read": bool(pair),
                }
            )
        searched = {i for a in self.p["actions"] if a["status"] == "completed" for i in a["target_checks"]}
        failed = [
            f"{a['kind']} {a['source']} {a.get('error')}"
            for a in self.p["actions"]
            if a["status"] == "failed"
        ]
        body = {
            "question": self.asked,
            "evidence": evidence,
            "analysis_results": results,
            "source_updates": updates,
            "source_relations": relations,
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
        previous = self.previous_report()
        if previous:
            body["previous_report"] = previous
        if self.p.get("hypotheses"):
            # The hypotheses and the facts tagged to them go to BioCheck, which writes and checks the
            # 「情报假设」 section; assess only grades what survived that check.
            cited = {e["id"] for e in evidence}
            body["hypotheses"] = [
                dict(
                    h,
                    facts=[
                        {
                            "ref": f["ref"],
                            "text": f["text"],
                            # BioCheck's contract: a fact with no direction for this hypothesis is context
                            "stance": hypothesis_stance(f)
                            if hypothesis_stance(f) in {"supports", "refutes"}
                            else "context",
                        }
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
        report = {"status": "NOT_RUN", "draft": "", "revised": False, "versions": []}
        self.p["report"] = report

        def keep(step):
            """Every draft as soon as it exists, checked afterwards by checked(); if the check fails, the
            draft stays with status "unchecked" and the failure code, so a wrong citation can be traced."""
            report["versions"].append({"step": step, "draft": report["draft"], "status": "unchecked"})

        def checked(review):
            report["versions"][-1].update(
                status=review["status"],
                claims=[
                    {k: c.get(k) for k in ("index", "start", "end", "verdict", "citations", "labels")}
                    for c in review["claims"]
                ],
            )

        if not body["evidence"]:
            report["reason"] = "no_evidence"
            return
        try:
            report["draft"] = self.service("generate", dict(body))["draft"]
            keep("generate")
            review = self.service("verify", dict(body, draft=report["draft"]))
            checked(review)
            for n in range(REVISIONS):
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
                keep(f"revise_{n + 1}")
                review = self.service("verify", dict(body, draft=report["draft"]))
                checked(review)
        except ServiceError as exc:
            self.log(exc.calls, "biocheck")
            report["reason"] = f"biocheck_{exc.code}"
            if report["versions"] and report["versions"][-1]["status"] == "unchecked":
                report["versions"][-1]["error"] = exc.code
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
                "question": self.asked,
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
        # Only hypotheses the report writes up are graded; their grounds are the tagged facts cited by
        # supported sentences in their own paragraph (supporting ones, or refuting ones for refuted).
        verified = hypothesis_citations(
            report["draft"], report.get("claims", []), [h["id"] for h in self.p.get("hypotheses", [])]
        )
        result.hypotheses = [h for h in result.hypotheses if h.id in verified]
        assessment = result.model_dump()
        facts = self.p.get("facts", [])
        for h in assessment["hypotheses"]:
            stance = "refutes" if h["level"] == "refuted" else "supports"
            h["grounds"] = sorted(
                {
                    f["ref"]
                    for f in facts
                    if f.get("hypothesis") == h["id"]
                    and hypothesis_stance(f) == stance
                    and f["ref"] in verified[h["id"]]
                }
            )
        self.check_assessment(assessment)
        order = ["insufficient", "possible", "probable", "confirmed"]
        for j in assessment["judgments"]:
            cap = {"supported": j["confidence"], "contradicted": "insufficient"}.get(j["check"], "possible")
            if order.index(j["confidence"]) > order.index(cap):
                j["confidence"] = cap
            if j["check"] == "contradicted":
                self.note("judgment_contradicted", j["conclusion"])
                j["conclusion"] = "原结论与已读原文矛盾，已撤下"
        # BioCheck writes hypotheses as 待验证 and never as confirmed, so the grade tops out at probable,
        # which also needs a PASS report, no verified refutation and a reason BioCheck supports;
        # refuted needs a supported reason on a verified refutation. Anything else is at most possible.
        for h in assessment["hypotheses"]:
            refuted = any(
                f.get("hypothesis") == h["id"]
                and hypothesis_stance(f) == "refutes"
                and f["ref"] in verified[h["id"]]
                for f in facts
            )
            backed = h["check"] == "supported" and h["grounds"]
            if h["level"] == "refuted":
                cap = "refuted" if backed else "possible"
            else:
                cap = "probable" if backed and report["status"] == "PASS" and not refuted else "possible"
                cap = h["level"] if order.index(h["level"]) <= order.index(cap) else cap
            if h["check"] == "contradicted":
                self.note("hypothesis_reason_contradicted", h["reason"])
                h["reason"] = "原理由与已读原文矛盾，已撤下"
            if cap != h["level"]:
                h["reason"] = f"评估给 {h['level']}，已核验报告只撑得住 {cap}"
                h["level"] = cap
        self.p["assessment"] = assessment
        self.watch()

    def check_assessment(self, assessment):
        """Judgment conclusions and hypothesis reasons are written after the report, so BioCheck
        verifies them, one line each with their grounds, in one call against the same evidence. Sets
        check: supported / contradicted / unverified (also when BioCheck fails or has no budget)."""
        items = assessment["judgments"] + assessment["hypotheses"]
        if not items:
            return
        lines, starts, at = [], [], 0
        for item in items:
            # Each clause is checked on its own: a long conclusion verified whole hid a reversed figure.
            refs = "".join(f"[{g}]" for g in item["grounds"])
            text = item.get("conclusion", item.get("reason", ""))
            line = "".join(p.strip() + refs + "。" for p in re.split(r"[；;。\n]", text) if p.strip())
            lines.append(line or refs + "。")
            starts.append(at)
            at += len(line) + 1
        verdicts = [[] for _ in items]
        try:
            if self.calls_left() <= 0:
                raise ServiceError("biocheck", "no_budget")
            review = self.service("verify", dict(self.report_input(), draft="\n".join(lines)))
            for c in review["claims"]:
                n = bisect.bisect_right(starts, c["start"]) - 1
                if n >= 0 and c["verdict"] != "no_claim":
                    verdicts[n].append(c["verdict"])
        except ServiceError as exc:
            self.log(exc.calls, "biocheck")
            self.note(f"assessment_biocheck_{exc.code}")
        for item, found in zip(items, verdicts):
            if "contradicted" in found:
                item["check"] = "contradicted"
            elif found and all(v == "supported" for v in found):
                item["check"] = "supported"
            else:
                item["check"] = "unverified"

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
                k: report.get(k)
                for k in ("claims", "coverage", "read_requests", "reason", "revised", "versions")
            },
            "assessment": self.p.get("assessment"),
            "checks": self.p.get("checks", []),
            "plan_basis": self.p.get("plan_basis", {}),  # name -> question / prior_report / exploratory
            "computations": self.p.get("computations", []),
            "mechanism": self.p.get("mechanism_result"),
            "stop": self.p.get("stop"),
            "reads": sum(1 for h in self.p.get("hits", []) if h["read"]),
            "notes": self.p.get("notes", []),
            # llm_calls: logical model calls; requests: HTTP requests behind them (what the budget counts)
            "usage": {"llm_calls": usage[0], "requests": int(usage[1]), "tokens": int(usage[2])},
        }
