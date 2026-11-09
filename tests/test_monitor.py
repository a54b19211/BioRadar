"""Topic lifecycle on the source clock: emerging -> rising -> cooling -> dormant (closure) -> awakened."""

from datetime import UTC, datetime, timedelta

from conftest import FakeFabric, record
from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.config import settings
from bioradar.research import analyses, monitor, schemas, topics

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
    assert topic["state"] == "rising"
    with db.session() as s:  # the triage level is stored and drives the state; the exit shows it marked 待核
        assert s.get(db.Topic, topic["id"]).alert == "important" and topic["alert"] == "important"
    assert topic["publishable"] is False and topic["hold_reason"] == "no_analysis"

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
        rows = list(s.scalars(select(db.Topic)))
        gates = topics.level_gates(s, rows)
        return [topics.topic_view(t, gates[t.id]) for t in rows]


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
    monkeypatch.setattr(s, "monitor_sources", ["bulk_source", "who_don"])
    who, legacy = {"source": "who_don"}, {"source": "bulk_source"}
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
    from sqlalchemy import select

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
    # v3 3.7: the budget pauses the model only; the record is read and owed a triage, durably
    assert paused["paused"] == "daily_auto_tokens" and paused["records"] == 1 and sent == []
    assert paused["routed"]["triage_deferred"] == 1
    with db.session() as session:
        assert session.scalar(select(db.Observation.reason).where(db.Observation.record_id == "P1")) == (
            "triage_deferred"
        )
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 0  # no retry while still over budget
    monkeypatch.setattr(s, "daily_auto_tokens", 10**9)
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 1  # the owed triage, by the retry path
    with db.session() as session:
        assert session.scalar(select(db.Observation.reason).where(db.Observation.record_id == "P1")) not in (
            "triage_deferred",
            "triage_failed",
        )


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


def priced_triage(sent, tokens=10):
    """triage_by_title whose every call costs `tokens`."""
    base = triage_by_title(sent)

    def chat_json(*args, **kwargs):
        value, call = base(*args, **kwargs)
        return value, dict(call, usage={"total_tokens": tokens})

    return chat_json


def test_the_daily_ceiling_is_checked_before_every_batch_and_every_new_analysis(monkeypatch):
    from sqlalchemy import func, select

    from bioradar import db
    from bioradar.config import settings
    from bioradar.research import analyses

    sent = []
    monkeypatch.setattr(llm, "chat_json", priced_triage(sent))
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    s = settings()
    for k, v in {"daily_auto_tokens": 100, "monitor_sources": ["who_don"], "auto_analysis": True}.items():
        monkeypatch.setattr(s, k, v)
    fabric = FakeFabric([record("B0", "历史", "x", source="who_don")])
    monitor.tick(is_demo=False, fabric=fabric)
    with db.session() as session:
        session.add(db.LlmCall(stage="triage", service="radar", total_tokens=99))
    new = [record(f"B{i:02d}", "两家医院确认7例", "x", source="who_don") for i in range(1, 31)]
    fabric.docs += FakeFabric(new).docs
    first = monitor.tick(is_demo=False, fabric=fabric)
    assert len(sent) == 1 and first["paused"] and first["analyses"] == []  # one batch crossed the line
    monkeypatch.setattr(s, "daily_auto_tokens", 10**9)
    rest = monitor.tick(is_demo=False, fabric=fabric)
    assert (
        rest["records"] == 20
    )  # the 20 deferred ones come back by the retry path; the first 10 are not redone
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(db.Observation)) == 30


def test_a_retried_old_record_does_not_count_as_new(monkeypatch):
    from bioradar import db
    from bioradar.config import settings

    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    s = settings()
    monkeypatch.setattr(s, "monitor_sources", ["who_don"])
    fabric = FakeFabric([record("R0", "历史", "x", source="who_don")])
    monitor.tick(is_demo=False, fabric=fabric)
    with db.session() as session:
        session.add(
            db.Observation(
                workspace_id="w",
                source="who_don",
                record_id="R0",
                version_id=fabric.docs[0]["version_id"],
                is_demo=False,
                title="历史",
                available_at=datetime.fromisoformat(DAY.format(1)),
                alert="none",
                closure=False,
                reason="triage_failed",
            )
        )
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 1  # the retry
    with db.session() as session:
        assert session.get(db.Cursor, ("who_don", False)).last_new_at is None


def test_a_queued_live_topic_analysis_waits_once_the_daily_budget_is_used(monkeypatch):
    from bioradar import db
    from bioradar.config import settings
    from bioradar.research import analyses

    monkeypatch.setattr(settings(), "daily_auto_tokens", 100)
    row = analyses.create(question="专题新进展", direction="public_health", trigger="topic", enqueue=False)
    with db.session() as session:
        session.add(db.LlmCall(stage="triage", service="radar", total_tokens=100))
    analyses.execute(row.id)
    assert analyses.get(row.id).state == "queued"


def test_a_signal_picks_its_question_and_a_watched_signal_reopens_the_topic(monkeypatch):
    from bioradar import db
    from bioradar.config import settings
    from bioradar.research import analyses

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        items = []
        for r in payload["records"]:
            known = 1 if payload["topics"] else None
            watch = "W1" if "GMP" in r["title"] else ("W9" if "无关" in r["title"] else None)
            items.append(
                schemas.TriageItem(
                    index=r["index"],
                    topic=known,
                    new_topic="" if known else "启衡新厂房",
                    direction="business",
                    alert="normal" if known else "important",
                    signal="capacity",
                    watch=watch,
                )
            )
        call = {
            "stage": stage,
            "model": "f",
            "finish_reason": "stop",
            "usage": {},
            "seconds": 0,
            "error": None,
        }
        return schemas.Triage(items=items), call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    monkeypatch.setattr(settings(), "auto_analysis", True)
    fabric = FakeFabric([record("S1", "启衡生物抗体原液厂房环评公示", "x", available=DAY.format(1))])
    [first] = monitor.tick(is_demo=True, fabric=fabric)["analyses"]
    assert analyses.get(first).input["question"].startswith("专题“启衡新厂房”出现产能或厂房信号")
    with db.session() as session:
        topic = session.scalars(select_topics()).one()
        session.add(
            db.TopicEvent(
                topic_id=topic.id,
                at=topic.first_seen_at,
                kind="watch",
                detail={"items": [{"id": "W1", "text": "厂房通过 GMP 检查"}]},
            )
        )
    fabric.docs += FakeFabric([record("S2", "无关新闻", "x", available=DAY.format(2))]).docs
    monitor.tick(is_demo=True, fabric=fabric)
    assert _topics()[0]["pending_reason"] is None  # W9 is not on the topic's list
    fabric.docs += FakeFabric([record("S3", "启衡厂房通过GMP检查", "x", available=DAY.format(3))]).docs
    assert monitor.tick(is_demo=True, fabric=fabric)["analyses"]  # W1 answered: a new analysis


def select_topics():
    from sqlalchemy import select

    from bioradar import db

    return select(db.Topic)


def test_information_activity_above_a_seven_day_baseline_is_flagged_once(monkeypatch):
    """Activity, not incidence; days with nothing received are left out; reposts count once."""
    from datetime import UTC, timedelta

    from sqlalchemy import select

    from bioradar import db
    from bioradar.config import settings

    s = settings()
    start = datetime(2026, 10, 1, 4, tzinfo=UTC)
    with db.session() as session:
        topic = db.Topic(
            workspace_id=s.workspace_id,
            title="T",
            direction="public_health",
            is_demo=True,
            first_seen_at=start,
            last_activity_at=start,
        )
        session.add(topic)
        session.flush()

        def seen(day, n, *, topic_id=None, origin=None):
            for i in range(n):
                session.add(
                    db.Observation(
                        workspace_id=s.workspace_id,
                        source="x",
                        record_id=f"{day}-{topic_id}-{i}-{origin}",
                        version_id=f"v{day}-{topic_id}-{i}-{origin}",
                        is_demo=True,
                        title="t",
                        available_at=start + timedelta(days=day),
                        topic_id=topic_id,
                        alert="normal",
                        activity_key=origin,
                    )
                )

        for day in range(8):
            if day == 3:
                continue  # nothing received at all that day: not a zero in the baseline
            seen(day, 2)  # other records the feed delivered
            seen(day, 1, topic_id=topic.id)
        session.flush()
        at = start + timedelta(days=7)
        assert topics.activity(session, s, topic, at, True) is False  # only 6 received days before day 7
        seen(8, 2)
        seen(8, 4, topic_id=topic.id)
        seen(8, 2, topic_id=topic.id, origin="record:x:original")  # reposts of one original count once
        session.flush()
        at = start + timedelta(days=8)
        assert topics.activity(session, s, topic, at, True) is True  # 5 > mean 1 + 3 (all days equal)
        assert topics.activity(session, s, topic, at, True) is False  # once per day
        [event] = list(session.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "activity")))
        assert (event.detail["records"], event.detail["threshold"], len(event.detail["days"])) == (5, 4.0, 7)
        assert "2026-10-04" not in event.detail["days"]
        assert topic.pending_reason == "activity_anomaly"


START = datetime(2026, 10, 1, 4, tzinfo=UTC)


def observed(session, topic, version, at):
    session.add(
        db.Observation(
            workspace_id=settings().workspace_id,
            source="x",
            record_id=version,
            version_id=version,
            is_demo=True,
            title="t",
            available_at=at,
            topic_id=topic.id,
            alert="normal",
        )
    )


def topic_at_start(session, title="T", **fields):
    """A topic first seen, and last active, at START."""
    topic = db.Topic(
        workspace_id=settings().workspace_id,
        title=title,
        direction="public_health",
        is_demo=True,
        first_seen_at=START,
        last_activity_at=START,
        **fields,
    )
    session.add(topic)
    session.flush()
    return topic


def quiet_week(session, *, bare_day=None):
    """A topic with two observations on each of the 7 days from START; on `bare_day` only three backfilled
    ones (version ids bg-*). Returns (the topic, the backfilled version ids)."""
    topic = topic_at_start(session)
    backfilled = set()
    for day in range(7):
        if day == bare_day:
            versions = [f"bg-{day}-{i}" for i in range(3)]
            backfilled.update(versions)
        else:
            versions = [f"v{day}-{i}" for i in range(2)]
        for version in versions:
            observed(session, topic, version, START + timedelta(days=day))
    return topic, backfilled


def test_backfilled_observations_are_not_activity_and_do_not_trigger_the_anomaly():
    s, today = settings(), START + timedelta(days=7)
    with db.session() as session:
        topic, _ = quiet_week(session)
        backfilled = {f"bg-7-{i}" for i in range(5)}
        for version in ("v7-0", "v7-1", *sorted(backfilled)):  # two new records and five backfilled ones
            observed(session, topic, version, today)
        session.flush()

        assert topics.activity(session, s, topic, today, True, backfilled) is False  # 2 <= mean 2 + 3
        assert topic.pending_reason is None
        assert topics.activity(session, s, topic, today, True) is True  # counted, 7 > 5: what it would be


def test_a_day_with_only_backfilled_observations_is_not_a_day_the_monitor_received_anything():
    s, today = settings(), START + timedelta(days=7)
    with db.session() as session:
        topic, backfilled = quiet_week(session, bare_day=3)
        for i in range(6):
            observed(session, topic, f"v7-{i}", today)
        session.flush()

        # 6 received days before today, not 7: not judged
        assert topics.activity(session, s, topic, today, True, backfilled) is False
        assert topics.activity(session, s, topic, today, True) is True  # the backfilled day would count


def test_a_round_keeps_the_versions_of_the_backfill_receipts_out_of_the_activity_check(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    today = (START + timedelta(days=7)).isoformat()
    news = [record(f"N{i}", "门诊记录5人", "x", available=today, version=f"n-{i}") for i in range(6)]
    fabric = FakeFabric(news)
    with db.session() as session:
        backfilled = quiet_week(session, bare_day=3)[1]  # observed before their receipt could be read
    fabric.background_version_ids = lambda run_ids: (backfilled, [])

    assert monitor.tick(is_demo=True, fabric=fabric)["records"] == 6

    with db.session() as session:  # 6 days with news before today, not 7: today's 6 are not judged
        flagged = select(func.count()).select_from(db.TopicEvent).where(db.TopicEvent.kind == "activity")
        assert session.scalar(flagged) == 0


def test_a_backfilled_record_opens_no_topic_and_is_not_triaged(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "chat_json", triage_by_title(sent))
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    old = record("B1", "两家医院确认7例", "x", available=DAY.format(1))
    fabric = FakeFabric([old])
    fabric.background_version_ids = lambda run_ids: ({old["version_id"]}, [])

    ran = monitor.tick(is_demo=True, fabric=fabric)

    assert ran["routed"] == {"background_backfill": 1} and sent == []  # settled by the program: no model call
    assert _topics() == []  # the triage would have opened a topic on an alert
    with db.session() as session:
        [seen] = session.scalars(select(db.Observation))
    assert (seen.topic_id, seen.alert, seen.reason) == (None, "none", "background_backfill")
    assert seen.activity_key == "record:external_archive:B1"  # still a member for the candidate groups

    fabric.docs += FakeFabric([record("N1", "两家医院确认7例", "x", available=DAY.format(3))]).docs
    monitor.tick(is_demo=True, fabric=fabric)  # the same record as news
    [topic] = _topics()
    with db.session() as session:
        arrived = {o.record_id: o.available_at.isoformat() for o in session.scalars(select(db.Observation))}
    assert topic["first_seen_at"] == arrived["N1"] != arrived["B1"]  # first seen when the news came


def test_a_backfilled_record_does_not_warm_alert_or_wake_a_topic(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    with db.session() as session:
        topic_id = topic_at_start(session, state="dormant", alert="normal", heat=1).id

    def standing():
        with db.session() as session:
            t = session.get(db.Topic, topic_id)
            return t.state, t.alert, t.heat, t.awakenings, t.pending_reason, t.last_activity_at

    before = standing()
    old = record("B1", "两家医院确认7例", "x", available=(START + timedelta(days=20)).isoformat())
    fabric = FakeFabric([old])
    fabric.background_version_ids = lambda run_ids: ({old["version_id"]}, [])
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(db.TopicEvent)) == 0
    assert standing() == before  # not rising again, no heat, no alert, nothing owed, no new last activity

    news = record("N1", "两家医院确认7例", "x", available=(START + timedelta(days=21)).isoformat())
    fabric.docs += FakeFabric([news]).docs
    monitor.tick(is_demo=True, fabric=fabric)  # the same record as news
    state, alert, heat, awakenings, owed, last = standing()
    assert (state, alert, heat, awakenings) == ("rising", "important", 3, 1)
    assert owed == "awakened+alert_important" and last > before[-1]


def test_advance_leaves_backfilled_observations_out_of_the_records_that_make_a_topic_rise():
    with db.session() as session:
        mixed, plain = topic_at_start(session, "mixed"), topic_at_start(session, "plain")
        for version in ("m-0", "m-1", "bg-m"):
            observed(session, mixed, version, START)
        for version in ("p-0", "p-1", "p-2"):
            observed(session, plain, version, START)
        mixed_id, plain_id = mixed.id, plain.id

    assert topics.advance(settings(), START, is_demo=True, background={"bg-m"}) == {plain_id: "rising"}
    assert topics.advance(settings(), START, is_demo=True) == {mixed_id: "rising"}  # counted, it has 3


def test_a_version_owed_a_triage_is_settled_as_background_once_its_receipt_names_it(monkeypatch):
    def down(settings_, stage, system, payload, model_cls, **options):
        call = {"stage": stage, "model": "fake", "finish_reason": None, "usage": {}, "seconds": 0}
        return None, {**call, "error": "timeout"}

    fabric = FakeFabric([record("T1", "两家医院确认7例", "x", available=DAY.format(3))])
    monkeypatch.setattr(llm, "chat_json", down)
    monitor.tick(is_demo=True, fabric=fabric)  # its triage failed: owed
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    fabric.background_version_ids = lambda run_ids: ({fabric.docs[0]["version_id"]}, [])

    assert monitor.tick(is_demo=True, fabric=fabric)["routed"] == {"background_backfill": 1}

    with db.session() as session:
        [seen] = session.scalars(select(db.Observation))  # the owed mark is replaced, not doubled
    assert (seen.reason, seen.topic_id) == ("background_backfill", None)


def unread_receipt_round(monkeypatch):
    """A round whose backfill receipt cannot be read, over two WHO records (B1 is in fact a backfilled one,
    N1 is news) and one record of another source. Returns (Fabric, what was sent to triage, B1's version)."""
    sent = []
    monkeypatch.setattr(llm, "chat_json", triage_by_title(sent))
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    monkeypatch.setattr(settings(), "demo_sources", ["external_archive", "who_don"])
    old = record("B1", "两家医院确认7例", "x", source="who_don", available=DAY.format(1))
    fabric = FakeFabric(
        [
            old,
            record("E1", "门诊记录5人", "x", available=DAY.format(2)),
            record("N1", "另一医院新聚集4人", "x", source="who_don", available=DAY.format(3)),
        ]
    )
    fabric.background_version_ids = lambda run_ids: (set(), ["run-1"])
    monitor.tick(is_demo=True, fabric=fabric)
    return fabric, sent, old["version_id"]


def observed_by_record():
    with db.session() as session:
        return {o.record_id: (o.reason, o.topic_id) for o in session.scalars(select(db.Observation))}


def test_a_round_puts_off_what_it_cannot_tell_from_a_backfill_while_a_receipt_is_unreadable(monkeypatch):
    _, sent, _ = unread_receipt_round(monkeypatch)

    seen = observed_by_record()
    assert seen["B1"] == seen["N1"] == ("triage_deferred", None)  # the WHO versions wait, in no topic
    assert seen["E1"][1] is not None  # another source is no backfill: triaged as ever
    assert [r["title"] for payload in sent for r in payload["records"]] == ["门诊记录5人"]
    with db.session() as session:
        [topic] = session.scalars(select(db.Topic))
        assert topic.alert == "normal"  # B1's alert important never reached it


def test_the_put_off_versions_are_settled_the_round_after_the_receipt_reads(monkeypatch):
    fabric, sent, backfilled = unread_receipt_round(monkeypatch)
    sent.clear()
    fabric.background_version_ids = lambda run_ids: ({backfilled}, [])

    ran = monitor.tick(is_demo=True, fabric=fabric)

    assert ran["routed"] == {"background_backfill": 1, "triage": 1}
    assert [r["title"] for payload in sent for r in payload["records"]] == ["另一医院新聚集4人"]
    seen = observed_by_record()
    assert seen["B1"] == ("background_backfill", None)  # the owed mark is replaced, not doubled
    assert seen["N1"][0] != "triage_deferred" and seen["N1"][1] is not None
    assert len(seen) == 3


def test_a_backfilled_correction_still_marks_the_analyses_that_cited_the_old_record(monkeypatch):
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    with db.session() as session:
        inv = db.Investigation(workspace_id="w", question="q", direction="pipeline", is_demo=True)
        session.add(inv)
        session.flush()
        session.add(
            db.Analysis(
                id="a",
                investigation_id=inv.id,
                trigger="manual",
                method="planner",
                input={},
                input_hash="a",
                progress={},
                stage="done",
                state="completed",
                result={"draft": "x"},
            )
        )
        session.flush()
        cited = dict(analysis_id="a", ref="E1", source="external_archive", record_id="B31", version_id="v")
        session.add(db.Evidence(**cited, title="初报", text="HR为0.62"))
    correction = record(
        "B41", "试验结果更正", "HR为0.69", available=DAY.format(2), relations=[("corrects", "B31")]
    )
    fabric = FakeFabric([correction])
    fabric.background_version_ids = lambda run_ids: ({correction["version_id"]}, [])

    monitor.tick(is_demo=True, fabric=fabric)

    with db.session() as session:
        assert session.get(db.Analysis, "a").result["source_updates"][0]["by_record"] == "B41"


def test_activity_keys_keep_sources_apart_and_join_a_repost_to_its_original():
    original = {"source": "a", "record_id": "R1", "relations": [], "origin_group": None}
    repost = {
        "source": "a",
        "record_id": "R2",
        "relations": [{"kind": "reposts", "target_record_id": "R1"}],
        "origin_group": "opaque-7",
    }
    other_source = {"source": "b", "record_id": "R1", "relations": [], "origin_group": None}
    grouped = {"source": "b", "record_id": "R9", "relations": [], "origin_group": "R1"}
    keys = [topics.activity_key(d) for d in (original, repost, other_source, grouped)]
    assert keys[0] == keys[1]  # the repost is its original, whatever its own group is called
    assert len({keys[0], keys[2], keys[3]}) == 3  # same id in another source, and a group named like an id


def test_recovery_does_not_queue_again_an_analysis_whose_job_is_still_waiting(monkeypatch):
    """Live 2026-11-03: a queued topic analysis was dispatched again every 30 s while its job waited
    behind others, so the queue held 24 copies."""
    from datetime import timedelta

    sent = []
    monkeypatch.setattr(analyses, "_scan", {"offset": 0})
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: sent.append(analysis_id))
    row = analyses.create(question="等待中的分析？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        s.get(db.Analysis, row.id).created_at = db.now() - timedelta(minutes=30)
    waiting = {row.id}
    monkeypatch.setattr(analyses, "waiting_in_queue", lambda analysis_id: analysis_id in waiting)
    assert analyses.recover() == 0 and sent == []
    waiting.clear()  # the job is gone from Redis (lost before it reached it, or Redis was flushed)
    assert analyses.recover() == 1 and sent == [row.id]


def test_waiting_rows_at_the_front_do_not_hide_a_lost_one_and_redis_trouble_is_not_loss(monkeypatch):
    """Limit 4 was taken before the Redis check, so four waiting rows hid a lost fifth forever."""
    from datetime import timedelta

    sent, waiting = [], set()
    monkeypatch.setattr(analyses, "_scan", {"offset": 0})
    # a dispatched job is in Redis afterwards
    monkeypatch.setattr(
        analyses, "dispatch", lambda analysis_id: sent.append(analysis_id) or waiting.add(analysis_id)
    )
    rows = []
    for i in range(5):
        row = analyses.create(
            question=f"等待中的分析{i}？", direction="pipeline", is_demo=True, enqueue=False
        )
        with db.session() as s:
            s.get(db.Analysis, row.id).created_at = db.now() - timedelta(minutes=60 - i)
        rows.append(row.id)
    waiting.update(rows[:4])
    monkeypatch.setattr(analyses, "waiting_in_queue", lambda analysis_id: analysis_id in waiting)
    assert analyses.recover() == 1 and sent == [rows[4]]
    sent.clear()
    assert analyses.recover() == 0 and sent == []  # the next round does not queue it twice
    with db.session() as s:  # beyond one page: the scan moves on round by round and still finds it
        s.get(db.Analysis, rows[4]).state = "completed"
    monkeypatch.setattr(analyses, "RECOVER_SCAN", 2)
    waiting.discard(rows[3])
    found = [analyses.recover() for _ in range(3)]
    assert sum(found) == 1 and sent == [rows[3]]

    def down(analysis_id):
        raise ConnectionError("redis down")

    sent.clear()
    monkeypatch.setattr(analyses, "waiting_in_queue", down)
    assert all(analyses.recover() == 0 for _ in range(3)) and sent == []


def test_each_triaged_record_window_is_recorded_with_its_call_and_reads_back():
    """v3 6.1: triage sends the first TRIAGE_CHARACTERS of each record; each window is recorded with the
    model call it went into and its binding, and reads back from the record's fixed-version text."""
    from sqlalchemy import select

    from bioradar.config import settings
    from bioradar.material import part_key, reread
    from bioradar.research import triage

    docs = FakeFabric([record("D1", "长文", "长文" * 2000), record("D2", "短文", "短文")]).docs
    sent = []
    original = llm.chat_json
    try:
        llm.chat_json = triage_by_title(sent)
        triage.triage(settings(), docs, [])
    finally:
        llm.chat_json = original
    with db.session() as s:
        rows = list(s.scalars(select(db.MaterialRead).order_by(db.MaterialRead.id)))
        call = s.scalars(select(db.LlmCall).where(db.LlmCall.stage == "triage")).one()
    assert [(r.stage, r.ref, r.llm_call_id) for r in rows] == [
        ("triage", "R1", call.id),
        ("triage", "R2", call.id),
    ]
    assert [r["text"] for r in sent[0]["records"]] == [docs[0]["text"][:1500], docs[1]["text"]]
    texts = {d["version_id"]: d["text"] for d in docs}
    for r, sent_record in zip(rows, sent[0]["records"]):
        resolve = lambda parts: {part_key(q): texts[q["version_id"]] for q in parts}  # noqa: E731
        assert reread(r.binding, resolve) == sent_record["text"] and r.chars == len(sent_record["text"])
