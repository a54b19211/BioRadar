"""The report written by slot and checked by assertion unit (research/run/slots.py, ADR 0014; task book 06 and
08). BioCheck is a stand-in that keeps the contract the radar relies on and BioCheck does not have yet: the
writer answers a request with `slots` by `sections`, and the check takes `sections` (kind, start, end) and
does not let an inference be supported outside a judgment, scenarios or recommendation section."""

import itertools
import re
from datetime import datetime

from conftest import FakeFabric
from sqlalchemy import select
from test_pipeline import RECORDS, fake_model, first_policy

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, propositions, views
from bioradar.research.pipeline import Run
from bioradar.research.run import report as report_stage
from bioradar.research.run.sections import build

JUDGMENT = {
    "kind": "judgment",
    "slot": "J1",
    "status": "supported",
    "text": "更正后PFS的HR为0.69，是有限人群内的重要进展[E1]。",
}
EVIDENCE = {"kind": "evidence", "text": "更正后的试验PFS的HR为0.69[E1]。"}
SCENARIOS = {"kind": "scenarios", "text": "若总生存期（OS）数据成熟，判断可能改变[E1]。"}
INFERENCE = "该试验PFS的HR为0.69，因此疗效确切[E1]。"
NARROWED = "更正后PFS的HR为0.69，是否为重要进展尚待确认[E1]。"
ASKED = itertools.count(1)  # an analysis is told by its question: each run asks its own
RULES = "biocheck.segment_rules.v1"
JUDGMENT_SECTIONS = ("judgment", "scenarios", "recommendation")  # where an inference may be supported


def call(stage):
    return {"stage": stage, "model": "fake", "usage": {"total_tokens": 7}}


def claim_of(n, text, start, section):
    """A sentence check as /v2/verify answers it: the units, the form check, the model's own split report."""
    inference = "因此" in text
    flagged = inference and section is not None and section not in JUDGMENT_SECTIONS
    verdict = "insufficient" if flagged else "supported"
    unit = {
        "id": "U1",
        "segment": "S1",
        "fragment": text,
        "kind": "inference" if inference else "fact",
        "model_verdict": "supported",
        "verdict": verdict,
        "reason": "inference_in_fact_section" if flagged else "",
        "void_reason": None,
        "labels": [],
        "governed_by": [],
        "conditional": False,
        "field_basis": None,
        "quotes": [
            {"ref": "E1", "quote": "HR为0.69", "status": "found", "positions": [[0, 7]], "adopted": 1}
        ],
    }
    return {
        "index": n,
        "text": text,
        "start": start,
        "end": start + len(text),
        "citations": re.findall(r"E\d+", text),
        "verdict": verdict,
        "labels": ["STR"] if flagged else [],
        "reason": unit["reason"],
        "segments": [{"id": "S1", "text": text, "status": "handled", "units": ["U1"]}],
        "units": [unit],
        "form_check": {"status": "passed", "problems": [], "units": 1, "void_units": 0},
        "semantic_split": {"source": "model_self_assessment", "assessment": "complete", "reason": ""},
    }


class StandIn:
    """BioCheck as far as the radar relies on it. `revised` maps a sentence to the one /v1/revise puts in
    its place."""

    def __init__(self, sections, revised=None):
        self.sections, self.revised, self.requests = sections, revised or {}, []

    def __call__(self, s, operation, body):
        self.requests.append((operation, body))
        return getattr(self, operation)(body)

    def bodies(self, operation):
        return [body for op, body in self.requests if op == operation]

    def generate(self, body):
        return {"sections": self.sections, "llm_calls": [call("generate")]}

    def verify(self, body):
        spans = body.get("sections")
        claims = []
        for n, m in enumerate(re.finditer(r"[^。\n]+。", body["draft"]), 1):
            section = next((x["kind"] for x in spans or [] if x["start"] <= m.start() < x["end"]), None)
            claims.append(claim_of(n, m.group(), m.start(), section))
        status = "PASS" if all(c["verdict"] == "supported" for c in claims) else "REVIEW"
        return {
            "status": status,
            "claims": claims,
            "coverage": {"verdict": "complete"},
            "checker_version": "biocheck.unit_verify.v1",
            "rules_version": RULES,
            "llm_calls": [call("verify_claims"), call("verify_coverage")],
        }

    def revise(self, body):
        draft, changed = body["draft"], []
        for c in sorted(body["review"]["claims"], key=lambda c: -c["start"]):
            after = self.revised.get(c["text"])
            if c["verdict"] != "supported" and after:
                draft = draft[: c["start"]] + after + draft[c["end"] :]
                changed.insert(0, {"index": c["index"], "before": c["text"], "after": after})
        return {"draft": draft, "changed": changed, "llm_calls": [call("revise")]}


def run_report(monkeypatch, bio, confidence="probable", judgments=None, topic=None):
    """An analysis to its result with the fake models, the planner stand-in and `bio`; assess grades the
    judgment `confidence`."""
    base = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, made = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan" and judgments:
            value.judgments = judgments
        if stage == "assess":
            for j in value.judgments:
                j.confidence = confidence
        return value, made

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", bio)
    row = analyses.create(
        question=f"XQ-17 的PFS结果现在应怎样判断？（{next(ASKED)}）",
        direction="pipeline",
        is_demo=True,
        enqueue=False,
        **({"trigger": "topic", "topic_id": topic} if topic else {}),
    )
    return row, Run(row, settings(), fabric=FakeFabric(RECORDS)).run()


def by_kind(result):
    return {s["kind"]: s for s in result["sections"]}


def test_the_writer_is_asked_by_slot_and_each_paragraph_goes_to_the_check_with_its_section(monkeypatch):
    bio = StandIn([JUDGMENT, EVIDENCE, SCENARIOS])
    _, result = run_report(monkeypatch, bio)

    assert bio.bodies("generate")[0]["slots"] == [{"slot": "J1", "question": "是否为重要进展"}]
    request = bio.bodies("verify")[0]
    assert [(s["kind"], request["draft"][s["start"] : s["end"]]) for s in request["sections"]] == [
        (p["kind"], p["text"]) for p in (JUDGMENT, EVIDENCE, SCENARIOS)
    ]
    # the stored sections are the writer's paragraphs; the judgment says what it states and what assess graded
    judgment = by_kind(result)["judgment"]["items"][0]
    assert (judgment["slot"], judgment["text"], judgment["status"]) == ("J1", JUDGMENT["text"], "supported")
    assert (judgment["confidence"], judgment["check"]) == ("probable", "supported")
    assert [i["text"] for i in by_kind(result)["scenarios"]["items"]][0] == SCENARIOS["text"]
    assert result["draft"] == "\n\n".join(p["text"] for p in (JUDGMENT, EVIDENCE, SCENARIOS))
    assert (result["report_status"], result["publishable"]) == ("PASS", True)
    # the receipt of the unit check is kept as it came, and which checker made it
    kept = result["review"]["claims"][0]
    assert kept["units"][0]["quotes"][0]["adopted"] == 1
    assert kept["form_check"]["status"] == "passed" and kept["semantic_split"]["assessment"] == "complete"
    assert result["review"]["versions"][0]["checker"] == {
        "checker_version": "biocheck.unit_verify.v1",
        "rules_version": RULES,
    }
    assert views.result(result)["sections"] == result["sections"]  # a published report shows all of it


def test_an_inference_is_not_supported_in_the_evidence_section_but_is_in_a_judgment(monkeypatch):
    in_evidence = StandIn([JUDGMENT, {"kind": "evidence", "text": INFERENCE}, SCENARIOS])
    _, result = run_report(monkeypatch, in_evidence)
    assert result["report_status"] == "REVIEW"
    assert (result["publishable"], result["hold_reason"]) == (False, "biocheck_review")

    in_judgment = StandIn([dict(JUDGMENT, text=INFERENCE), EVIDENCE, SCENARIOS])
    _, result = run_report(monkeypatch, in_judgment)
    assert (result["report_status"], result["publishable"]) == ("PASS", True)


def test_a_slot_without_a_paragraph_cannot_be_judged_and_a_paragraph_for_no_slot_is_dropped(monkeypatch):
    stray = dict(JUDGMENT, slot="J9")
    advice = {"kind": "recommendation", "text": "建议立即扩产[E1]。"}
    bio = StandIn([JUDGMENT, stray, advice, EVIDENCE, SCENARIOS])
    _, result = run_report(monkeypatch, bio, judgments=["是否为重要进展", "是否需要扩大调查"])

    items = by_kind(result)["judgment"]["items"]
    assert [(i["slot"], i["status"]) for i in items] == [("J1", "supported"), ("J2", "cannot_judge")]
    assert items[1]["reason"] == "no_paragraph" and "text" not in items[1]
    assert {n["detail"] for n in result["notes"] if n["code"] == "writer_section_dropped"} == {
        "J9: not a slot",
        "recommendation: not a section the writer fills",
    }
    assert by_kind(result)["recommendation"]["reason"] == "no_objectives"  # the program's, as before


def test_assess_grading_a_slot_insufficient_revises_its_paragraph_and_checks_again(monkeypatch):
    bio = StandIn([JUDGMENT, EVIDENCE, SCENARIOS], revised={JUDGMENT["text"]: NARROWED})
    _, result = run_report(monkeypatch, bio, confidence="insufficient")

    # write, check, check the conclusions; then the revision and the check of the revised draft
    assert [op for op, _ in bio.requests] == ["generate", "verify", "verify", "revise", "verify"]
    flagged = [c for c in bio.bodies("revise")[0]["review"]["claims"] if c["verdict"] != "supported"]
    assert [c["text"] for c in flagged] == [JUDGMENT["text"]]
    assert flagged[0]["reason"].startswith("assess_insufficient") and "STR" in flagged[0]["labels"]
    judgment = by_kind(result)["judgment"]
    assert judgment["status"] == "ok"
    assert (judgment["items"][0]["text"], judgment["items"][0]["status"]) == (NARROWED, "insufficient")
    assert result["draft"].startswith(NARROWED) and result["review"]["revised"] is True
    assert (result["report_status"], result["publishable"]) == ("PASS", True)
    assert [v["step"] for v in result["review"]["versions"]] == ["generate", "revise_1"]


def test_a_paragraph_that_cannot_be_settled_holds_the_judgment_section_and_the_report(monkeypatch):
    bio = StandIn([JUDGMENT, EVIDENCE, SCENARIOS])  # the revision changes nothing
    _, result = run_report(monkeypatch, bio, confidence="insufficient")

    assert [op for op, _ in bio.requests] == ["generate", "verify", "verify", "revise"]
    assert (by_kind(result)["judgment"]["status"], by_kind(result)["judgment"]["reason"]) == (
        "missing",
        "slot_mismatch",
    )
    assert (result["report_status"], result["publishable"]) == ("PASS", False)
    assert result["hold_reason"] == "sections_missing:slot_mismatch"


def test_with_no_revision_left_a_mismatched_paragraph_is_held_without_another_request(monkeypatch):
    row = analyses.create(question="XQ-17 的PFS结果如何？", direction="pipeline", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=FakeFabric(RECORDS))
    written = [dict(JUDGMENT)]
    run.p.update(
        judgments=["是否为重要进展"],
        checks=[],
        facts=[],
        assessment={
            "judgments": [{"slot": "J1", "confidence": "insufficient", "consequence": "high", "grounds": []}]
        },
        report={
            "status": "PASS",
            "draft": JUDGMENT["text"],
            "written": written,
            "claims": [{"index": 1, "start": 0, "end": 5, "text": "x", "verdict": "supported"}],
            "versions": [{"step": "generate"}, {"step": "revise_1"}, {"step": "revise_2"}],
        },
    )
    monkeypatch.setattr(services, "biocheck", lambda *a: (_ for _ in ()).throw(AssertionError("sent")))

    report_stage.reconcile(run)

    assert written[0]["status"] == "supported"
    judgment = build(run.p, {"question": "q", "sources": []}, [])[1]
    assert (judgment["status"], judgment["reason"]) == ("missing", "slot_mismatch")


def test_the_verify_goes_to_v2_with_what_it_names_unless_the_switch_says_v1(monkeypatch):
    seen = []
    monkeypatch.setattr(services, "_post", lambda svc, url, token, path, body, *a: seen.append((path, body)))
    body = {
        "draft": "x[E1]。",
        "sections": [{"kind": "judgment", "start": 0, "end": 6}],
        "previous_report": {"draft": "d", "date": None, "report_status": "REVIEW", "pending": True},
        "research_scope": {"checked": [], "not_checked": [], "failed": [], "history_cutoff": None},
    }
    services.biocheck(settings(), "verify", body)
    services.biocheck(settings().model_copy(update={"biocheck_verify": "v1"}), "verify", body)

    (v2_path, v2), (v1_path, v1) = seen
    assert v2_path == "/v2/verify" and v2["sections"] == body["sections"]
    assert v2["previous_report"] == {"draft": "d", "date": ""}
    assert v2["research_scope"] == {"checked": [], "not_checked": [], "failed": []}
    assert v1_path == "/v1/verify" and v1 == {k: v for k, v in body.items() if k != "sections"}


def test_a_conclusion_is_checked_by_the_call_that_checked_it_and_its_slot_item_is_numbered(monkeypatch):
    at = datetime.fromisoformat("2026-09-30T08:00:00+08:00")
    with db.session() as s:
        topic = db.Topic(
            workspace_id="w",
            title="XQ-17",
            direction="pipeline",
            is_demo=True,
            first_seen_at=at,
            last_activity_at=at,
        )
        s.add(topic)
    row, result = run_report(monkeypatch, StandIn([JUDGMENT, EVIDENCE, SCENARIOS]), topic=topic.id)

    with db.session() as s:
        calls = list(
            s.scalars(
                select(db.LlmCall)
                .where(db.LlmCall.analysis_id == row.id, db.LlmCall.service == "biocheck")
                .order_by(db.LlmCall.id)
            )
        )
        propositions.number(s, s.get(db.Analysis, row.id), result)
    # generate, then the report's check and the conclusions' check, each a sentence check and a coverage check
    assert [c.stage for c in calls] == [
        "generate",
        "verify_claims",
        "verify_coverage",
        "verify_claims",
        "verify_coverage",
    ]
    judgment = result["assessment"]["judgments"][0]
    assert judgment["checked_by"] == calls[3].id  # not the last call, the coverage check
    assert judgment["depends_on"]["checked_by"] == calls[3].id
    item = by_kind(result)["judgment"]["items"][0]
    assert item["prop_id"] == judgment["prop_id"] and item["question_key"] == judgment["question_key"]
    shown = next(s for s in views.result(result)["sections"] if s["kind"] == "judgment")["items"][0]
    assert shown["prop_id"] == item["prop_id"] and views.blank(shown)["slot"] == "J1"
