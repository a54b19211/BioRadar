"""Every material the radar sent to a model or a service is linked to the model call row it belongs to:
its own reads and calls, and the requests it made of BioCheck and MechEvidence (a service request is
linked to the first call row that request logged; the service's internal calls beyond that are rows of
their own)."""

import pytest
from conftest import FakeFabric, record
from sqlalchemy import select
from test_pipeline import fake_model, first_policy

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, schemas
from bioradar.research.pipeline import Run

RECORDS = [
    record("P1", "XQ-17 试验", "随机试验纳入240例，PFS的HR为0.69。", kind="primary_research"),
    record("P2", "XQ-17 另一项", "另一项研究报告PFS的HR为0.62。", kind="primary_research"),
]


def with_mechanism(chat):
    def wrapped(settings_, stage, system, payload, model_cls, **options):
        value, call = chat(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.mechanism = schemas.PlanMechanism(statement="X 抑制 Y", species="mouse")
        return value, call

    return wrapped


@pytest.fixture
def analysis(monkeypatch):
    """A whole analysis (plan, reads, calculation, mechanism, report, assessment) with stand-ins."""
    monkeypatch.setattr(llm, "chat_json", with_mechanism(fake_model([])))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))

    def biocheck(s, operation, body):
        calls = [{"stage": operation, "model": "fake", "usage": {"total_tokens": 7}, "requests": 2}]
        if operation == "generate":
            return {"draft": "同一试验PFS的HR为0.69[E1]。", "llm_calls": calls}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": calls}

    def mechevidence(s, body):
        return {"overall": "supported", "llm_calls": [{"stage": "review", "model": "fake", "requests": 1}]}

    monkeypatch.setattr(services, "biocheck", biocheck)
    monkeypatch.setattr(services, "mechevidence", mechevidence)
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    s = settings().model_copy(update={"mechevidence_url": "http://mech.invalid"})
    return Run(row, s, fabric=FakeFabric(RECORDS)).run(), row.id


def reads_and_calls(analysis_id):
    with db.session() as s:
        reads = list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.analysis_id == analysis_id)))
        calls = {c.id: c for c in s.scalars(select(db.LlmCall).where(db.LlmCall.analysis_id == analysis_id))}
    return reads, calls


def test_no_sent_material_read_of_a_full_analysis_is_left_unlinked(analysis):
    result, analysis_id = analysis
    assert result["report_status"] == "PASS" and result["mechanism"]["overall"] == "supported"
    reads, calls = reads_and_calls(analysis_id)
    sent = [r for r in reads if r.status == "sent"]
    assert {r.service for r in sent} == {"radar", "biocheck", "mechevidence"}
    assert [(r.id, r.stage, r.service, r.ref) for r in sent if r.llm_call_id is None] == []


def test_a_material_is_linked_to_a_call_of_the_receiver_it_was_sent_to(analysis):
    _, analysis_id = analysis
    reads, calls = reads_and_calls(analysis_id)
    for read in reads:
        if read.llm_call_id is not None:
            call = calls[read.llm_call_id]
            assert (call.service, call.analysis_id) == (read.service, analysis_id), read.id
            assert call.stage.split(":")[0] in {
                read.stage.split(":")[0],
                "generate",
                "verify",
                "revise",
                "review",
            }


def test_a_request_with_no_logged_call_has_nothing_to_link_to(monkeypatch):
    """A service that is not configured logs no call: its material is not_sent / withheld, not linked."""
    monkeypatch.setattr(llm, "chat_json", with_mechanism(fake_model([])))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(
        services,
        "biocheck",
        lambda s, op, body: {
            "draft": "x[E1]。",
            "status": "PASS",
            "claims": [],
            "coverage": {},
            "llm_calls": [],
        },
    )
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    reads, _ = reads_and_calls(row.id)
    assert any(r.service == "biocheck" and r.llm_call_id is None for r in reads)
