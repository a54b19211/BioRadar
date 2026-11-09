"""The report as sections (research/run/sections.py): the program arranges the run's results into sections,
checks that the five items of design 1.3 are there, and holds a report that lacks one as 待核 even when
BioCheck passed it. No model: the pipeline cases use the fakes of test_pipeline.py."""

from types import SimpleNamespace

import pytest
from conftest import FakeFabric
from sqlalchemy import select
from test_access import admin_client, fabric_sources, topic_analysis  # noqa: F401 - the fixture a test here takes
from test_pipeline import RECORDS, fake_model, first_policy
from test_publication_gate_and_source_revocation import passing

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, schemas, views
from bioradar.research.pipeline import Run
from bioradar.research.run.sections import MECH_NOT_RUN_TEXT, build, missing_items
from bioradar.research.state import MECH_NOT_RUN_REASONS

INPUT = {"question": "XQ-17 的PFS结果如何？", "sources": ["external_archive"], "cutoff": None}
JUDGMENT = {
    "question": "是否为重要进展",
    "conclusion": "有限人群内的重要进展",
    "confidence": "probable",
    "credibility": "high",
    "consequence": "high",
    "priority": "high",
    "grounds": ["E1"],
    "unproven": "还缺总生存期（OS）数据",
    "check": "supported",
}
HYPOTHESIS = {"id": "H1", "level": "possible", "reason": "待验证", "grounds": ["E1"], "check": "supported"}


def run_state(**changes):
    """The state of a finished run that has all five items; `changes` replaces keys."""
    state = {
        "checks": [
            {"id": "Q1", "text": "当前PFS疗效数值", "status": "conflict", "evidence": ["E1", "E2"]},
            {"id": "Q2", "text": "是否获批", "status": "open", "evidence": []},
            {"id": "Q3", "text": "OS是否成熟", "status": "open", "evidence": []},
        ],
        "actions": [
            {"kind": "search", "status": "completed", "target_checks": ["Q1", "Q2"]},
            {"kind": "read", "status": "failed", "target_checks": ["Q2"]},
        ],
        "facts": [
            {"id": "F1", "ref": "E1", "stance": "supports", "checks": ["Q1"]},
            {"id": "F2", "ref": "E2", "stance": "refutes", "checks": ["Q1"]},
            {"id": "F3", "ref": "E2", "stance": "context", "checks": ["Q2"]},
        ],
        "mechanism_result": {"status": "not_run", "reason": "no_mechanism_question"},
        "assessment": {"alert": "important", "judgments": [JUDGMENT], "hypotheses": []},
    }
    return {**state, **changes}


def record(ref, **changes):
    fields = {
        "ref": ref,
        "source": "external_archive",
        "record_id": f"B{ref[1:]}",
        "version_id": "v" * 64,
        "published_at": "2026-09-27",
        "available_at": "2026-09-27T08:00:00+08:00",
        "material_kind": "official_notice",
        "verified": True,
        "meta": {},
    }
    return SimpleNamespace(**{**fields, **changes})


ROWS = [record("E1"), record("E2", verified=False, meta={"clean_text_state": "unavailable"})]


def by_kind(sections):
    return {s["kind"]: s for s in sections}


def test_a_run_with_all_five_items_has_every_section_and_is_published_on_a_pass():
    sections = build(run_state(), INPUT, ROWS)
    assert [s["kind"] for s in sections] == [
        "question",
        "judgment",
        "evidence",
        "gaps",
        "scenarios",
        "visibility",
        "recommendation",
    ]
    assert missing_items(sections) == []
    assert views.publication("PASS", None, missing_items(sections))["publishable"] is True
    kinds = by_kind(sections)
    assert kinds["judgment"]["items"][0]["consequence"] == "high"
    # a check in conflict lists both sides
    assert [(i["id"], i["stance"], i["grounds"]) for i in kinds["evidence"]["items"]] == [
        ("Q1", "supports", ["E1"]),
        ("Q1", "refutes", ["E2"]),
    ]
    assert kinds["scenarios"]["items"] == [{"question": "是否为重要进展", "text": "还缺总生存期（OS）数据"}]
    assert [(i.get("id"), i.get("status")) for i in kinds["gaps"]["items"][1:]] == [
        ("Q2", "not_found"),
        ("Q3", "not_searched"),
        (None, "failed"),
        (None, "not_run"),
    ]


@pytest.mark.parametrize(
    ("changes", "lacking"),
    [
        ({"assessment": None}, ["main_judgment", "review_condition"]),  # nothing was assessed
        (  # a judgment BioCheck found contradicted was withdrawn: it is not one the report makes
            {"assessment": {"judgments": [{**JUDGMENT, "check": "contradicted"}], "hypotheses": []}},
            ["main_judgment", "review_condition"],
        ),
        (  # a graded hypothesis is a judgment, but it says nothing of the consequence
            {
                "assessment": {"judgments": [], "hypotheses": [HYPOTHESIS]},
                "hypotheses": [{"id": "H1", "statement": "x", "confirm": ["出现扩产公告"], "refute": []}],
            },
            ["impact"],
        ),
        ({"facts": []}, ["evidence"]),
        (
            {"assessment": {"judgments": [{**JUDGMENT, "unproven": ""}], "hypotheses": []}},
            ["review_condition"],
        ),
    ],
)
def test_a_report_lacking_an_item_of_the_five_is_held_though_the_check_passed(changes, lacking):
    sections = build(run_state(**changes), INPUT, ROWS)
    assert missing_items(sections) == lacking
    gate = views.publication("PASS", None, missing_items(sections))
    assert (gate["publishable"], gate["hold"], gate["hold_reason"]) == (
        False,
        "pending_verification",
        "sections_missing:" + ",".join(lacking),
    )


def test_the_visibility_section_counts_the_key_gaps_and_lists_each_record_in_its_appendix():
    visibility = by_kind(build(run_state(), {**INPUT, "cutoff": "2026-09-28T00:00:00+08:00"}, ROWS))[
        "visibility"
    ]
    assert [i["text"] for i in visibility["items"]] == [
        "截止时间：2026-09-28T00:00:00+08:00",
        "读取材料 2 份，逐份清单见附录",
        "1 个待查问题没有检索",
        "1 个待查问题检索后仍未查清",
        "1 次检索或读取失败",
        "1 份材料未经核实",
        "1 份材料的正文是未清洗的原始文本",
    ]
    assert [(r["ref"], r["verified"]) for r in visibility["appendix"]] == [("E1", True), ("E2", False)]
    assert visibility["appendix"][0]["available_at"] == "2026-09-27T08:00:00+08:00"


def test_the_appendix_gives_each_record_its_three_times_and_says_unknown_for_what_a_row_does_not_hold():
    proven = {
        "first_seen_at": "2026-11-06T03:10:00+00:00",
        "proof_available_at": "2020-05-08T00:00:00+00:00",
        "replay_mode": "operational_replay",
    }
    rows = [
        record("E1", published_at="2020-05-07", meta=proven),
        record("E2", published_at=None),  # stored before the times were kept: its meta holds none of them
        record("E3", meta={**proven, "proof_available_at": None}),  # a proof it does not have is not listed
    ]

    appendix = by_kind(build(run_state(), INPUT, rows))["visibility"]["appendix"]

    def times(n):
        return [(t["kind"], t["value"]) for t in appendix[n]["times"]]

    assert times(0) == [
        ("published_at", "2020-05-07"),
        ("first_seen_at", "2026-11-06T03:10:00+00:00"),
        ("proof_available_at", "2020-05-08T00:00:00+00:00"),
        ("replay_mode", "operational_replay"),
    ]
    assert appendix[0]["times"][0]["label"] == "发布日期（来源自述）"
    # the merged available_at (2026-09-27T08:00:00+08:00 on every row here) never fills in for a time
    assert times(1) == [("published_at", "未知"), ("first_seen_at", "未知"), ("replay_mode", "未知")]
    assert [kind for kind, _ in times(2)] == ["published_at", "first_seen_at", "replay_mode"]


def test_the_recommendation_is_empty_and_says_why_when_the_requester_gave_no_objective():
    recommendation = by_kind(build(run_state(), INPUT, ROWS))["recommendation"]
    assert recommendation == {
        "kind": "recommendation",
        "status": "empty",
        "reason": "no_objectives",
        "items": [{"text": "任务方未给目标，不写建议", "reason": "no_objectives"}],
    }


@pytest.mark.parametrize("reason", MECH_NOT_RUN_REASONS)
def test_the_gaps_say_in_one_sentence_why_the_mechanism_did_not_run(reason):
    assert set(MECH_NOT_RUN_TEXT) == set(MECH_NOT_RUN_REASONS)
    state = run_state(mechanism_result={"status": "not_run", "reason": reason})
    assert by_kind(build(state, INPUT, ROWS))["gaps"]["items"][-1] == {
        "text": f"机制评价未运行：{MECH_NOT_RUN_TEXT[reason]}",
        "status": "not_run",
        "reason": reason,
    }
    done = run_state(mechanism_result={"status": "completed"})
    assert all(i.get("status") != "not_run" for i in by_kind(build(done, INPUT, ROWS))["gaps"]["items"])


def finished(monkeypatch, unproven, **grades):
    """A run to its result with fake models and a BioCheck that passes; the assessment's judgment says
    `unproven` and carries `grades` (put through the schema, as a model's answer is)."""
    model = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = model(settings_, stage, system, payload, model_cls, **options)
        if stage == "assess":
            value.judgments = [
                schemas.Judgment.model_validate({**j.model_dump(), "unproven": unproven, **grades})
                for j in value.judgments
            ]
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", passing([]))
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    return Run(row, settings(), fabric=FakeFabric(RECORDS)).run()


def test_a_finished_run_stores_its_sections_and_a_pass_with_all_five_items_is_publishable(monkeypatch):
    result = finished(monkeypatch, "还缺总生存期（OS）数据")
    assert (result["report_status"], result["publishable"]) == ("PASS", True)
    kinds = by_kind(result["sections"])
    assert missing_items(result["sections"]) == []
    assert kinds["recommendation"]["reason"] == "no_objectives"
    assert kinds["gaps"]["items"][-1]["reason"] == "no_mechanism_question"
    assert [r["ref"] for r in kinds["visibility"]["appendix"]] == ["E1", "E2"]
    assert views.result(result)["sections"] == result["sections"]  # a published report shows them all


def test_a_passed_run_without_the_evidence_that_would_change_the_judgment_is_marked_pending(monkeypatch):
    result = finished(monkeypatch, "")
    assert result["report_status"] == "PASS"
    assert (result["publishable"], result["hold_reason"]) == (False, "sections_missing:review_condition")
    shown = views.result(result)  # shown whole, with the mark: the draft, the assessment and every section
    assert (shown["publishable"], shown["hold"]) == (False, "pending_verification")
    assert shown["draft"] == result["draft"] != "" and shown["assessment"] == result["assessment"] is not None
    assert shown["sections"] == result["sections"]
    assert by_kind(shown["sections"])["scenarios"]["status"] == "missing"
    assert by_kind(shown["sections"])["scenarios"]["reason"] == "no_review_condition"


def test_a_judgment_the_model_cannot_grade_says_unknown_in_the_result_the_view_and_the_export(
    monkeypatch,
    fabric_sources,  # noqa: F811
):
    """Credibility and consequence may be answered unknown, instead of a guessed low, medium or high."""
    result = finished(monkeypatch, "还缺总生存期（OS）数据", credibility="unknown", consequence="unknown")
    judgment = result["assessment"]["judgments"][0]
    assert (judgment["credibility"], judgment["consequence"]) == ("unknown", "unknown")
    with db.session() as s:  # as analyses.execute stores it
        row = s.scalars(select(db.Analysis)).one()
        row.state, row.result, analysis_id = "completed", result, row.id
    fabric_sources["value"] = ["external_archive"]  # every source readable
    admin = admin_client()
    shown = admin.get(f"/api/analyses/{analysis_id}").json()["result"]
    assert shown["assessment"]["judgments"][0]["consequence"] == "unknown"
    assert by_kind(shown["sections"])["judgment"]["items"][0]["consequence"] == "unknown"
    assert "真实性 未知，后果 未知" in admin.get(f"/api/analyses/{analysis_id}/export").text


def test_the_words_of_the_sections_are_withheld_with_a_revoked_source_and_the_codes_stay(fabric_sources):  # noqa: F811
    admin = admin_client()
    analysis_id, _ = topic_analysis()  # cites gone_src, which Fabric no longer lists: its words are withheld
    stored = [
        {"kind": "judgment", "status": "ok", "items": [{"text": "据撤权原文判断", "grounds": ["E2"]}]},
        {"kind": "recommendation", "status": "empty", "reason": "no_objectives", "items": [{"text": "说明"}]},
    ]
    with db.session() as s:
        row = s.get(db.Analysis, analysis_id)
        row.result = {**row.result, "sections": stored}
    result = admin.get(f"/api/analyses/{analysis_id}").json()["result"]
    assert result["withheld"]
    assert result["sections"] == [
        {"kind": "judgment", "status": "ok", "items": [{"text": "", "grounds": ["E2"]}]},
        {"kind": "recommendation", "status": "empty", "reason": "no_objectives", "items": [{"text": ""}]},
    ]
    assert views.blank({"reason": "据撤权原文"}) == {"reason": ""}  # a reason that is not a code is text
