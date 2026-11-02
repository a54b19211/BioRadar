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
