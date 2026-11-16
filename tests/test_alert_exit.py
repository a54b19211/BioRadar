"""The monitor's exits (ADR 0005, section c): a received lead is shown at once as a program fact;
the triage model's verdict on a record never leaves, and a topic's level is shown with the mark of its latest
analysis (待核 until that analysis's report passed)."""

import pytest
from conftest import FakeFabric, record
from sqlalchemy import select
from test_access import admin_client, fabric_sources  # noqa: F401 - the fixture `admin` takes
from test_monitor import DAY

from bioradar import db
from bioradar.adapters import llm
from bioradar.config import settings
from bioradar.research import analyses, monitor, schemas


def urgent_outbreak(settings_, stage, system, payload, model_cls, **options):
    """The triage model calls the first record an urgent outbreak, with its own words, and leaves the
    second one out of its answer (the program records that as a failed triage)."""
    item = schemas.TriageItem(
        index=1,
        new_topic="临岑聚集",
        direction="public_health",
        alert="urgent",
        signal="outbreak",
        objects=[schemas.PlanObject(name="临岑医院", kind="place")],
        reason="模型判断：疑似院内聚集",
    )
    call = {"stage": stage, "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0}
    call["error"] = None
    return schemas.Triage(items=[item]), call


@pytest.fixture
def admin(monkeypatch, fabric_sources):  # noqa: F811
    """A signed-in client after one round over two records: a topic at the model's level urgent, whose
    analysis is queued and not run."""
    fabric_sources["value"] = ["external_archive"]
    monkeypatch.setattr(llm, "chat_json", urgent_outbreak)
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    monkeypatch.setattr(settings(), "auto_analysis", True)
    fabric = FakeFabric(
        [
            record("A21", "两家医院确认7例", "x", available=DAY.format(2)),
            record("F9", "另一份通报", "x", available=DAY.format(3)),
        ]
    )
    assert len(monitor.tick(is_demo=True, fabric=fabric)["analyses"]) == 1
    return admin_client()


def finish(analysis_id, result):
    with db.session() as s:
        row = s.get(db.Analysis, analysis_id)
        row.state, row.result = "completed", result


def test_a_lead_is_shown_at_once_as_a_program_fact_without_the_models_words(admin):
    rows = {o["record_id"]: o for o in admin.get("/api/monitor/observations?is_demo=true").json()}
    lead = rows["A21"]
    at = lead["available_at"]
    assert lead["lead"] == f"本系统于{at}收到来源external_archive的线索：两家医院确认7例（尚未核实）。"
    assert lead["alert"] is None and lead["reason"] == ""
    assert rows["F9"]["reason"] == "triage_failed"  # the program's own status stays
    with db.session() as s:  # the model's reason is kept, not shown
        stored = s.scalar(select(db.Observation.reason).where(db.Observation.record_id == "A21"))
    assert stored == "模型判断：疑似院内聚集"


def test_a_level_is_shown_marked_pending_until_the_analysis_that_read_the_topic_passed(admin):
    [topic] = admin.get("/api/topics?is_demo=true").json()
    assert topic["alert"] == "urgent" and topic["summary"] == ""
    gate = (topic["publishable"], topic["hold"], topic["hold_reason"])
    assert gate == (False, "pending_verification", "analysis_queued")
    detail = admin.get(f"/api/topics/{topic['id']}").json()
    observed = [e for e in detail["events"] if e["kind"] == "observed"]
    assert [(e["alert"], e["signal"], e["objects"]) for e in observed] == [(None, "", [])]
    assert [o["alert"] for o in detail["observations"]] == [None]
    with db.session() as s:  # what is stored, and what scheduling reads, is untouched
        assert s.get(db.Topic, topic["id"]).alert == "urgent"
    finish(topic["last_analysis_id"], {"report_status": "REVIEW"})  # a report that did not pass is pending
    held = admin.get(f"/api/topics/{topic['id']}").json()
    assert (held["alert"], held["publishable"], held["hold_reason"]) == ("urgent", False, "biocheck_review")


def test_a_passed_analysis_verifies_the_level_until_the_topic_is_owed_another(admin):
    [topic] = admin.get("/api/topics?is_demo=true").json()
    finish(topic["last_analysis_id"], {"report_status": "PASS"})
    shown = admin.get(f"/api/topics/{topic['id']}").json()
    assert shown["alert"] == "urgent" and shown["publishable"] is True and shown["hold"] is None
    # a single record's verdict is never shown, whatever the topic's gate says
    assert [e["signal"] for e in shown["events"] if e["kind"] == "observed"] == [""]
    with db.session() as s:  # a newer record owes the topic another analysis
        s.get(db.Topic, topic["id"]).pending_reason = "alert_urgent"
    owed = admin.get(f"/api/topics/{topic['id']}").json()
    assert (owed["alert"], owed["publishable"], owed["hold_reason"]) == ("urgent", False, "analysis_owed")
