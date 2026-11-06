"""T-24 close-out (Codex 718, r168): a replay at a cutoff takes only the debt visible then and leaves the rest
owed; a correction notice is read as the version that corrected, not the record's head."""

from datetime import UTC, datetime

from conftest import FakeFabric, record
from sqlalchemy import select
from test_pipeline import fake_model, first_policy

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, monitor
from bioradar.research.pipeline import Run


def day(n):
    return datetime(2026, 6, n, 12, tzinfo=UTC)


def topic(*owed):
    with db.session() as s:
        t = db.Topic(
            workspace_id=monitor.settings().workspace_id,
            title="炭疽",
            direction="public_health",
            is_demo=True,
            first_seen_at=day(1),
            last_activity_at=day(1),
            alert="normal",
        )
        s.add(t)
        s.flush()
        for reason, at in owed:
            monitor.owe(t, reason, at)
        return t.id


def state(topic_id):
    with db.session() as s:
        t = s.get(db.Topic, topic_id)
        asked = [
            s.get(db.Analysis, e.detail["analysis_id"]).input["question"]
            for e in s.scalars(
                select(db.TopicEvent).where(
                    db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "analysis"
                )
            )
        ]
        since = monitor.parse_time(t.pending_since) if t.pending_since else None  # SQLite drops the zone
        return t.pending_reason, since, asked


def start(monkeypatch, cutoff):
    monkeypatch.setattr(monitor.analyses, "dispatch", lambda analysis_id: None)
    return monitor.start_analyses(monitor.settings(), is_demo=True, cutoff=cutoff)


def test_a_debt_owed_after_the_cutoff_starts_no_analysis_and_stays_owed(monkeypatch):
    t = topic(("rising", day(5)))
    assert start(monkeypatch, day(3)) == []  # the future was read first, an earlier cutoff asked after
    assert state(t)[:2] == ("rising", day(5))
    assert len(start(monkeypatch, day(6))) == 1 and state(t)[0] is None


def test_an_old_debt_with_a_later_reason_takes_only_the_reason_visible_then(monkeypatch):
    t = topic(("rising", day(2)), ("source_corrected", day(5)))
    assert len(start(monkeypatch, day(3))) == 1
    reason, since, asked = state(t)
    assert "rising" in asked[0] and "source_corrected" not in asked[0]
    assert (reason, since) == ("source_corrected", day(5))  # still owed, from its own time
    assert len(start(monkeypatch, day(6))) == 1
    assert state(t)[0] is None and "source_corrected" in state(t)[2][1]


def test_a_debt_that_cannot_be_placed_in_time_is_kept_by_a_replay(monkeypatch):
    t = topic()
    with db.session() as s:  # owed before dated reasons were kept
        row = s.get(db.Topic, t)
        row.pending_reason, row.pending_since = "rising", day(2)
    assert start(monkeypatch, day(3)) == [] and state(t)[0] == "rising"
    assert len(start(monkeypatch, None)) == 1 and state(t)[0] is None  # live monitoring still takes it


V_OLD, V_NEW = "c" * 64, "d" * 64
RECORDS = [
    record(
        "C1", "更正通知", "更正：本月新增病例为8例。", available="2026-06-02T08:00:00+08:00", version=V_OLD
    ),
    record(
        "C1",
        "更正通知（再次修订）",
        "再次修订：本月新增病例为7例。",
        available="2026-06-04T08:00:00+08:00",
        version=V_NEW,
    ),
]


def test_a_correction_notice_is_read_as_the_version_that_corrected(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(
        services,
        "biocheck",
        lambda s, op, body: (
            {"draft": "x[E1]。", "llm_calls": []}
            if op == "generate"
            else {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": []}
        ),
    )
    fabric = FakeFabric(RECORDS)
    row = analyses.create(
        question="更正后的数字是多少？",
        direction="public_health",
        is_demo=True,
        enqueue=False,
        seed_records=[
            {"source": "external_archive", "record_id": "C1", "version_id": V_OLD, "title": "更正通知"}
        ],
    )
    Run(row, settings(), fabric=fabric).run()
    assert ("read_versions", "external_archive", (("C1", V_OLD),)) in fabric.calls
    with db.session() as s:
        read = [e.version_id for e in s.scalars(select(db.Evidence).where(db.Evidence.record_id == "C1"))]
    assert V_OLD in read  # the version that corrected, not only the head


def test_a_fixed_version_read_accepts_only_exactly_the_named_version():
    from bioradar.adapters.fabric import fixed_version_is

    doc = {
        "source": "s",
        "record_id": "C1",
        "version_id": V_OLD,
        "is_demo": True,
        "available_at": "2026-06-02T08:00:00+08:00",
    }
    assert fixed_version_is(doc, "s", "C1", V_OLD, True, day(3))
    assert not fixed_version_is(doc, "s", "C1", V_NEW, True, day(3))  # another version
    assert not fixed_version_is(doc, "s", "C2", V_OLD, True, day(3))  # another record
    assert not fixed_version_is(doc, "t", "C1", V_OLD, True, day(3))  # another source
    assert not fixed_version_is(doc, "s", "C1", V_OLD, False, day(3))  # the other demo kind
    assert not fixed_version_is(doc, "s", "C1", V_OLD, True, day(1))  # not visible at that cutoff


def test_a_fixed_version_read_needs_explicit_well_formed_metadata():
    """Codex r170: a boolean demo kind and an aware time no later than the bound - never a parser error."""
    from bioradar.adapters.fabric import fixed_version_is

    base = {
        "source": "s",
        "record_id": "C1",
        "version_id": V_OLD,
        "is_demo": True,
        "available_at": "2026-06-02T08:00:00+08:00",
    }
    assert fixed_version_is(base, "s", "C1", V_OLD, True, day(3))
    for bad in (
        {"is_demo": None},
        {"is_demo": "true"},
        {"is_demo": 1},
        {"available_at": None},
        {"available_at": "2026-06-02"},
        {"available_at": "2026-06-02T08:00:00"},
        {"available_at": "x"},
        {"available_at": 1780000000},
    ):
        assert fixed_version_is(dict(base, **bad), "s", "C1", V_OLD, True, day(3)) is False, bad
    assert fixed_version_is(base, "s", "C1", V_OLD, True, "not-a-cutoff") is False
    assert fixed_version_is(base, "s", "C1", V_OLD, True, None)  # live: bounded by now
    assert (
        fixed_version_is(dict(base, available_at="2999-01-01T00:00:00+00:00"), "s", "C1", V_OLD, True, None)
        is False
    )


def test_a_reason_owed_many_times_after_the_cutoff_is_one_mark_and_settles_in_parts(monkeypatch):
    """Codex r172: 25 daily occurrences of one reason; partial settlements at successive cutoffs never repeat
    the mark (pending_reason stays one reason, well within its column) nor settle what comes later."""
    t = topic(*(("series_change", day(n)) for n in range(1, 26)))
    for n, cutoff in enumerate((2, 3, 10, 24), 1):
        assert len(start(monkeypatch, day(cutoff))) == 1
        reason, since, asked = state(t)
        assert (reason, since) == ("series_change", day(cutoff + 1)), (cutoff, reason, since)
        assert len(asked) == n
    assert len(start(monkeypatch, day(26))) == 1 and state(t)[0] is None
    with db.session() as s:
        owed = list(
            s.scalars(select(db.TopicEvent).where(db.TopicEvent.topic_id == t, db.TopicEvent.kind == "owed"))
        )
    assert len(owed) < 25 * 6  # carried once per remaining occurrence per settlement, not multiplied
