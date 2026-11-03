"""Topic lifecycle on the source clock: emerging -> rising -> cooling -> dormant (closure) -> awakened."""

from datetime import datetime

from conftest import FakeFabric, record

from bioradar.adapters import llm
from bioradar.research import monitor, schemas

DAY = "2026-09-{:02d}T08:00:00+08:00"


def triage_by_title(sent):
    def chat_json(settings_, stage, system, payload, model_cls, **options):
        sent.append(payload)
        items = []
        for r in payload["records"]:
            known = 1 if payload["topics"] else None
            if "噪声" in r["title"]:
                items.append(schemas.TriageItem(index=r["index"], alert="none"))
            else:
                items.append(
                    schemas.TriageItem(
                        index=r["index"],
                        topic=known,
                        new_topic="" if known else "临岑聚集",
                        alert="important" if "确认" in r["title"] else "normal",
                        closure="结案" in r["title"],
                    )
                )
        return schemas.Triage(items=items), {
            "stage": stage,
            "model": "fake",
            "finish_reason": "stop",
            "usage": {},
            "seconds": 0,
            "error": None,
        }

    return chat_json


def test_topic_lifecycle(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    records = [
        record("A11", "门诊记录5人", "x", available=DAY.format(1)),
        record("A13", "噪声", "x", available=DAY.format(1)),
        record("A21", "两家医院确认7例", "x", available=DAY.format(2)),
    ]
    fabric = FakeFabric(records)
    first = monitor.tick(is_demo=True, fabric=fabric)
    assert first["records"] == 3
    [topic] = [t for t in _topics() if t["title"] == "临岑聚集"]
    assert topic["state"] == "rising" and topic["alert"] == "important"

    # Blank rounds: no new records, the clock moves on. Cooling after 7 days, not yet dormant.
    monitor.tick(is_demo=True, fabric=fabric, clock=datetime.fromisoformat(DAY.format(12)))
    assert _topics()[0]["state"] == "cooling"

    # A closure notice puts the topic to sleep.
    fabric.docs += FakeFabric([record("L1", "调查结案为季节性流感", "x", available=DAY.format(20))]).docs
    monitor.tick(is_demo=True, fabric=fabric)
    assert _topics()[0]["state"] == "dormant"

    # An independent new cluster wakes it up again.
    fabric.docs += FakeFabric([record("L4", "另一医院新聚集4人", "x", available=DAY.format(28))]).docs
    monitor.tick(is_demo=True, fabric=fabric)
    topic = _topics()[0]
    assert topic["state"] == "rising" and topic["awakenings"] == 1


def test_replay_reads_only_records_visible_at_cutoff(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    fabric = FakeFabric(
        [
            record("A11", "门诊记录5人", "x", available=DAY.format(1)),
            record("A21", "两家医院确认7例", "x", available=DAY.format(5)),
        ]
    )
    first = monitor.tick(is_demo=True, fabric=fabric, cutoff=datetime.fromisoformat(DAY.format(3)))
    assert first["records"] == 1
    second = monitor.tick(is_demo=True, fabric=fabric, cutoff=datetime.fromisoformat(DAY.format(6)))
    assert second["records"] == 1


def _topics():
    from sqlalchemy import select

    from bioradar import db

    with db.session() as s:
        return [monitor.topic_view(t) for t in s.scalars(select(db.Topic))]


def test_records_whose_triage_failed_are_triaged_again(monkeypatch):
    from sqlalchemy import select

    from bioradar import db

    def down(settings_, stage, system, payload, model_cls, **options):
        call = {"stage": stage, "model": "fake", "finish_reason": None, "usage": {}, "seconds": 0}
        return None, {**call, "error": "timeout"}

    fabric = FakeFabric([record("T1", "两家医院确认7例", "x", available=DAY.format(3))])
    monkeypatch.setattr(llm, "chat_json", down)
    monitor.tick(is_demo=True, fabric=fabric)
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    assert monitor.tick(is_demo=True, fabric=fabric)["records"] == 1
    with db.session() as s:
        rows = list(s.scalars(select(db.Observation).where(db.Observation.record_id == "T1")))
    assert len(rows) == 1 and rows[0].reason != "triage_failed" and rows[0].topic_id


def test_a_real_feed_is_monitored_from_its_current_end(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    old = [record(f"H{i}", "历史记录", "x", source="who_don") for i in range(3)]
    fabric = FakeFabric(old)
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 0
    fabric.docs += FakeFabric([record("N1", "两家医院确认7例", "x", source="who_don")]).docs
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 1


def test_a_round_that_dies_before_triage_reads_the_same_records_again(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    fabric = FakeFabric([record("D1", "两家医院确认7例", "x", available=DAY.format(3))])
    real = monitor.observe

    def dies(*args, **kwargs):
        raise RuntimeError("worker killed")

    monkeypatch.setattr(monitor, "observe", dies)
    try:
        monitor.tick(is_demo=True, fabric=fabric)
    except RuntimeError:
        pass
    monkeypatch.setattr(monitor, "observe", real)
    assert monitor.tick(is_demo=True, fabric=fabric)["records"] == 1
    assert monitor.tick(is_demo=True, fabric=fabric)["records"] == 0


def test_an_analysis_held_back_by_the_hourly_cap_is_created_later_and_only_once(monkeypatch):
    from bioradar.config import settings
    from bioradar.research import analyses

    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    s = settings()
    monkeypatch.setattr(s, "auto_analysis", True)
    monkeypatch.setattr(s, "auto_analyses_per_hour", 0)
    fabric = FakeFabric([record("P1", "两家医院确认7例", "x", available=DAY.format(3))])
    assert monitor.tick(is_demo=True, fabric=fabric)["analyses"] == []
    assert _topics()[0]["pending_reason"]
    monkeypatch.setattr(s, "auto_analyses_per_hour", 6)
    assert len(monitor.tick(is_demo=True, fabric=fabric)["analyses"]) == 1  # no new record needed
    assert monitor.tick(is_demo=True, fabric=fabric)["analyses"] == []
    assert _topics()[0]["pending_reason"] is None


def test_a_round_that_dies_after_creating_the_analysis_does_not_create_another(monkeypatch):
    from sqlalchemy import func, select

    from bioradar import db
    from bioradar.config import settings

    def killed(analysis_id):
        raise RuntimeError("killed")

    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    monkeypatch.setattr(monitor.analyses, "dispatch", killed)
    s = settings()
    monkeypatch.setattr(s, "auto_analysis", True)
    fabric = FakeFabric([record("K1", "两家医院确认7例", "x", available=DAY.format(3))])
    try:
        # the analysis and the cleared debt are committed, then the process dies before dispatch
        monitor.tick(is_demo=True, fabric=fabric)
    except RuntimeError:
        pass
    monkeypatch.setattr(monitor.analyses, "dispatch", lambda analysis_id: None)
    assert monitor.tick(is_demo=True, fabric=fabric)["analyses"] == []
    with db.session() as session:
        # one queued row, which analyses.recover() dispatches later
        assert session.scalar(select(func.count()).select_from(db.Analysis)) == 1
