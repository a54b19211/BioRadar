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


def test_live_monitoring_skips_sources_kept_for_search_only(monkeypatch):
    from bioradar.config import settings

    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    s = settings()
    monkeypatch.setattr(s, "monitor_sources", ["who_don"])
    who, legacy = {"source": "who_don"}, {"source": "legacy_documents"}
    fabric = FakeFabric([record("H0", "历史", "x", **who), record("L0", "旧文档", "x", **legacy)])
    monitor.tick(is_demo=False, fabric=fabric)  # both feeds start at their current end
    fabric.docs += FakeFabric([record("H1", "确认7例", "x", **who), record("L1", "补录", "x", **legacy)]).docs
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 1


def test_a_failed_triage_of_an_excluded_source_is_not_retried(monkeypatch):
    from bioradar import db
    from bioradar.config import settings

    s = settings()
    monkeypatch.setattr(s, "monitor_sources", ["who_don"])
    with db.session() as session:
        session.add(
            db.Observation(
                workspace_id="default",
                source="legacy_documents",
                record_id="L9",
                version_id="v-L9",
                is_demo=False,
                title="旧文档",
                available_at=datetime.fromisoformat(DAY.format(1)),
                alert="none",
                closure=False,
                reason="triage_failed",
            )
        )
    fabric = FakeFabric([record("L9", "旧文档", "x", source="legacy_documents", version="v-L9")])
    docs, _, _ = monitor.new_documents(fabric, s, is_demo=False, cutoff=None)
    assert docs == []


def test_a_flood_is_triaged_a_bounded_amount_per_round_without_loss(monkeypatch):
    from bioradar.config import settings

    sent = []
    monkeypatch.setattr(llm, "chat_json", triage_by_title(sent))
    monkeypatch.setattr(settings(), "monitor_max_records", 100)
    fabric = FakeFabric([record(f"F{i:03d}", f"噪声{i}", "x", available=DAY.format(3)) for i in range(250)])
    rounds = [monitor.tick(is_demo=True, fabric=fabric) for _ in range(4)]
    assert [r["records"] for r in rounds] == [100, 100, 50, 0]
    assert [r["backlog"] for r in rounds] == [True, True, False, False]
    assert len(sent) == 25  # 250 records in batches of 10, each sent once


def test_a_flooding_source_does_not_starve_a_quiet_one(monkeypatch):
    from bioradar.config import settings

    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    s = settings()
    monkeypatch.setattr(s, "monitor_max_records", 20)
    monkeypatch.setattr(s, "monitor_sources", ["legacy_documents", "who_don"])
    who, legacy = {"source": "who_don"}, {"source": "legacy_documents"}
    fabric = FakeFabric([record("Q0", "历史", "x", **who), record("Z0", "历史", "x", **legacy)])
    monitor.tick(is_demo=False, fabric=fabric)  # both feeds start at their current end
    flood = [record(f"Z{i:03d}", "噪声", "x", **legacy) for i in range(1, 60)]
    fabric.docs += FakeFabric(flood + [record("Q1", "两家医院确认7例", "x", **who)]).docs
    first = monitor.tick(is_demo=False, fabric=fabric)
    assert first["records"] == 20 and first["backlog"]
    from sqlalchemy import select

    from bioradar import db

    with db.session() as session:
        assert "Q1" in set(session.scalars(select(db.Observation.record_id)))


def test_live_monitoring_pauses_at_the_daily_automatic_token_ceiling(monkeypatch):
    from bioradar import db
    from bioradar.config import settings

    sent = []
    monkeypatch.setattr(llm, "chat_json", triage_by_title(sent))
    s = settings()
    monkeypatch.setattr(s, "daily_auto_tokens", 1000)
    monkeypatch.setattr(s, "monitor_sources", ["who_don"])
    fabric = FakeFabric([record("P0", "历史", "x", source="who_don")])
    monitor.tick(is_demo=False, fabric=fabric)
    with db.session() as session:  # today's automatic work already used the ceiling
        session.add(db.LlmCall(stage="triage", service="radar", total_tokens=1000))
    fabric.docs += FakeFabric([record("P1", "两家医院确认7例", "x", source="who_don")]).docs
    paused = monitor.tick(is_demo=False, fabric=fabric)
    assert paused["paused"] == "daily_auto_tokens" and paused["records"] == 0 and sent == []
    monkeypatch.setattr(s, "daily_auto_tokens", 10**9)
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 1  # the record waited in the feed


def test_a_correction_marks_the_analyses_that_cited_the_old_record(monkeypatch):
    from bioradar import db

    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    at = datetime.fromisoformat(DAY.format(1))
    with db.session() as session:
        topic = db.Topic(
            workspace_id="w",
            title="XQ-17",
            direction="pipeline",
            is_demo=True,
            first_seen_at=at,
            last_activity_at=at,
        )
        session.add(topic)
        session.flush()
        for name, topic_id, is_demo in (
            ("manual", None, True),
            ("topic", topic.id, True),
            ("real", None, False),
        ):
            inv = db.Investigation(
                workspace_id="w", question=name, direction="pipeline", is_demo=is_demo, topic_id=topic_id
            )
            session.add(inv)
            session.flush()
            row = db.Analysis(
                id=name,
                investigation_id=inv.id,
                trigger="manual",
                method="planner",
                input={},
                input_hash=name,
                progress={},
                stage="done",
                state="completed",
                result={"draft": "x"},
            )
            session.add(row)
            session.flush()
            session.add(
                db.Evidence(
                    analysis_id=name,
                    ref="E1",
                    source="external_archive",
                    record_id="B31",
                    version_id="v",
                    title="初报",
                    text="HR为0.62",
                )
            )
        topic_id = topic.id
    correction = record(
        "B41", "试验结果更正", "HR为0.69", available=DAY.format(2), relations=[("corrects", "B31")]
    )
    monitor.tick(is_demo=True, fabric=FakeFabric([correction]))
    with db.session() as session:
        manual, real = session.get(db.Analysis, "manual"), session.get(db.Analysis, "real")
        assert manual.result["source_updates"][0]["by_record"] == "B41"
        assert "source_updates" not in real.result  # a real analysis is not touched by a demo correction
        assert "source_corrected" in (session.get(db.Topic, topic_id).pending_reason or "")
    monitor.tick(is_demo=True, fabric=FakeFabric([correction]))  # seen again: not marked twice
    with db.session() as session:
        assert len(session.get(db.Analysis, "manual").result["source_updates"]) == 1
