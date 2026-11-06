"""A follow-up hands on its earlier report with that report's verification: the plan model and BioCheck see
whether it passed (PASS) or is pending (any other status, or a record from before the status was kept), so a
pending report can only steer the follow-up as leads to verify. No real model: the outbound payloads are
captured."""

import pytest
from conftest import FakeFabric
from test_pipeline import RECORDS, fake_model, first_policy

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, prompts
from bioradar.research.pipeline import Run

DRAFT = "上一轮稿件：XQ-17 的PFS的HR为0.62[E1]。"


def follow_up(monkeypatch, parent_status, *, old_record=False):
    """(plan payload, BioCheck generate request) of a follow-up whose parent ended with parent_status."""
    sent, biocheck = [], []
    monkeypatch.setattr(llm, "chat_json", fake_model(sent))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))

    def fake_biocheck(s, operation, body):
        biocheck.append((operation, body))
        if operation == "generate":
            return {"draft": "本轮稿件[E1]。", "llm_calls": []}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": []}

    monkeypatch.setattr(services, "biocheck", fake_biocheck)
    parent = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        row = s.get(db.Analysis, parent.id)
        row.state, row.completed_at = "completed", db.now()
        row.result = {"draft": DRAFT} | ({"report_status": parent_status} if parent_status else {})
        s.add(
            db.Evidence(
                analysis_id=parent.id,
                ref="E1",
                source="external_archive",
                record_id="B31",
                version_id="v" * 64,
                title="XQ-17 随机试验初步结果",
                text="随机试验纳入240例，PFS的HR为0.62。",
                meta={},
            )
        )
    child = analyses.create(
        question="XQ-17 的PFS现在如何？",
        direction="pipeline",
        is_demo=True,
        parent_id=parent.id,
        enqueue=False,
    )
    if old_record:  # stored before the earlier report's status travelled with it
        with db.session() as s:
            row = s.get(db.Analysis, child.id)
            row.input = {**row.input, "previous_report": {"draft": DRAFT, "date": None}}
    Run(analyses.get(child.id), settings(), fabric=FakeFabric(RECORDS)).run()
    plan = next(payload for stage, payload, _ in sent if stage == "plan")
    generate = next(body for operation, body in biocheck if operation == "generate")
    return plan, generate["previous_report"]


@pytest.mark.parametrize(
    "status,old,check",
    [
        ("PASS", False, {"report_status": "PASS", "pending": False}),
        ("REVIEW", False, {"report_status": "REVIEW", "pending": True}),
        ("NOT_RUN", False, {"report_status": "NOT_RUN", "pending": True}),
        (None, True, {"report_status": "unknown", "pending": True}),  # an old record: unverified
    ],
)
def test_the_earlier_reports_verification_reaches_the_plan_and_biocheck(monkeypatch, status, old, check):
    plan, previous = follow_up(monkeypatch, status, old_record=old)
    # the follow-up still gets the earlier draft: a pending report may steer the investigation
    assert plan["previous_report"] == DRAFT and plan["previous_report_check"] == check
    assert {k: previous[k] for k in ("report_status", "pending")} == check
    if check["pending"]:
        assert previous["draft"].startswith("【上一轮报告未通过 BioCheck 核验") and previous[
            "draft"
        ].endswith(DRAFT)
    else:
        assert previous["draft"] == DRAFT


def test_the_plan_rules_say_a_pending_report_only_gives_leads_to_verify():
    assert "previous_report_check" in prompts.PLAN and "不能当作已核实的依据" in prompts.PLAN
