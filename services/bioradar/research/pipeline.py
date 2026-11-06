"""One analysis, stage by stage: plan -> investigate -> compute -> mechanism -> report -> assess.

Progress is saved after every stage and after every investigation action. A restarted job continues
from the saved stage; a model call that was in flight when the worker died is sent again.

Run holds what one run needs across its stages - the analysis input, the saved state (RunState), the Fabric
client and the call budget - and the bookkeeping every stage shares (notes, model calls and their log,
saving). The stages themselves are in research/run/, one module each."""

from datetime import datetime

import httpx
from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import Fabric, FabricError
from bioradar.config import Settings
from bioradar.research import state
from bioradar.research.lineage import lineage_sources
from bioradar.research.run import Failed, assess, calculate, investigate, mechanism, plan, report
from bioradar.research.state import RunState

__all__ = ["DEMO_SETTING", "STAGES", "Failed", "Run"]

STAGES = ["plan", "investigate", "compute", "mechanism", "report", "assess", "done"]
DEMO_SETTING = (
    "\n（情景说明：这是演练用的模拟情景，材料里的地点、机构、人物和数字都是为演练编写的。请在这个情景内部、"
    "按材料本身的证据判断；不要因为材料是虚构的就认定没有事件，或因此降低可信度、后果和核实优先级。）"
)


class Run:
    def __init__(self, analysis: db.Analysis, settings: Settings, fabric: Fabric | None = None):
        self.found = 0.0  # what the current read found apart from answering (evidence_gain)
        self.id = analysis.id
        self.row_investigation = analysis.investigation_id
        self.input = analysis.input
        self.p: RunState = state.load(analysis.progress)
        self.stage = analysis.stage
        self.s = settings
        self.fabric = fabric or Fabric(settings)
        self.docs = {}  # (source, record_id) -> document read in this process
        self.counted = {}  # Fabric request counts already added to progress by this process
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
                row = db.LlmCall(
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
                    http=c.get("http"),
                )
                s.add(row)
                s.flush()
                db.keep_outputs(s, row, c)
        self.calls_used += sum(c.get("requests") or 1 for c in calls)

    def calls_left(self):
        return self.input["budget"]["max_llm_calls"] - self.calls_used

    def ask(self, stage, system, payload, model_cls, **options):
        self.upstream_still_readable()
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
        # what this process sent to Fabric since the last save, added to the totals of earlier processes;
        # an action's cache hit sends nothing and is not counted
        for op, c in getattr(self.fabric, "requests", {}).items():
            seen = self.counted.get(op, {"n": 0, "errors": 0})
            total = self.p.setdefault("fabric_requests", {}).setdefault(op, {"n": 0, "errors": 0})
            total["n"] += c["n"] - seen["n"]
            total["errors"] += c["errors"] - seen["errors"]
            self.counted[op] = dict(c)
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
            # the run stops with the reason instead of passing those on
            if self.stage != "plan":
                self.upstream_still_readable()
            steps[self.stage]()
            self.save(STAGES[STAGES.index(self.stage) + 1])
        return self.result()

    # ---- stages: each is its module's function over this run; a method here, so one stage of one run
    # can be replaced (tests, tools) and the stage table above stays the one place that orders them

    def plan(self):
        plan.plan(self)

    def investigate(self):
        investigate.investigate(self)

    def compute(self):
        calculate.compute(self)

    def mechanism(self):
        mechanism.mechanism(self)

    def report(self):
        report.report(self)

    def assess(self):
        assess.assess(self)

    def report_input(self):
        """The report input BioCheck writes and checks from; assess verifies its own text against it too."""
        return report.report_input(self)

    # ---- the earlier round's report ------------------------------------------------------------

    def upstream_still_readable(self):
        """Raise when the plan used the earlier report and its sources can no longer be read. Checked
        before every model call, Fabric action and professional-service request, not only at stage starts
        (revoked mid-investigation, the plan's terms still reached Fabric). Calls already sent
        are not taken back. Progress saved before used_previous existed, with an earlier report and past
        the plan, counts as having used it: a missing field is not "not used"."""
        if not self.input.get("previous_report") or self.stage == "plan":
            return
        used = self.p.get("used_previous", True)
        if used and self.previous_report() is None:
            raise Failed("upstream_access_withdrawn")

    def previous_report(self):
        """The earlier round's report, only while every source it was written from (through all earlier
        rounds) can still be read. Checked at each use (plan, then report), since a source can be revoked
        while the run goes on; not checked once and remembered."""
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

    # ---- the question and its scope ------------------------------------------------------------

    @property
    def asked(self):
        """The question as the models and services see it. Scenario material says it is invented; without
        this the models judged "fictional, so no real event, low priority" (PHX). Same for every method;
        real data gets the question unchanged."""
        if self.input["is_demo"]:
            return self.input["question"] + DEMO_SETTING
        return self.input["question"]

    @property
    def scope(self):
        return self.input.get("scope_record_ids") or []

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
