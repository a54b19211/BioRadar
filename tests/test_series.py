"""T-24: values accumulate per object and metric across records; the program finds what changed."""

from datetime import UTC, datetime

from conftest import FakeFabric, record
from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.research import monitor, schemas, series


def test_periods_are_read_or_refused():
    at = datetime(2026, 9, 3, tzinfo=UTC)
    assert series.period_bounds("2026Q2", at) == (
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 6, 30, tzinfo=UTC),
    )
    assert series.period_bounds("2026h1", at)[1] == datetime(2026, 6, 30, tzinfo=UTC)
    assert series.period_bounds("2026-02", at)[1] == datetime(2026, 2, 28, tzinfo=UTC)
    assert series.period_bounds("", at) == (at, at)  # a stock "as of" the record
    assert series.period_bounds("去年下半年", at) is None


def row(id_, quarter, value, status, available, superseded=None):
    start, end = series.period_bounds(quarter, None)
    return {
        "id": id_,
        "value": value,
        "status": status,
        "period": quarter,
        "period_start": start,
        "period_end": end,
        "available_at": datetime(2026, 1, available, tzinfo=UTC),
        "superseded_by": superseded,
    }


def kinds(found):
    return sorted(c["kind"] for c in found)


def test_a_plan_repeated_each_quarter_is_not_growth_and_a_late_value_is_put_in_order():
    rows = [row(1, "2026Q1", 2, "planned", 1), row(2, "2026Q2", 2, "planned", 2)]
    assert kinds(series.changes(rows, 2)) == []  # the same plan restated
    rows.append(row(3, "2026Q3", 2, "under_construction", 3))
    assert kinds(series.changes(rows, 3)) == ["status_advance"]
    rows.append(row(4, "2026Q4", 3, "operational", 4))
    assert kinds(series.changes(rows, 4)) == ["new_high", "step"]
    rows.append(row(5, "2026Q4", 3, "operational", 5))  # another record repeats Q4
    assert kinds(series.changes(rows, 5)) == ["repeat"]
    rows.append(row(6, "2027Q1", 4, "operational", 6))
    assert kinds(series.changes(rows, 6)) == ["new_high", "step", "streak_up"]
    # Q2 arrives late with a different value: the new point and its successor are both compared again
    late = series.changes(rows + [row(7, "2026Q2", 1, "planned", 7)], 7)
    assert {(c["kind"], c["late"]) for c in late} >= {("step", False), ("step", True)}


def test_a_stock_series_is_compared_not_summed_and_a_missing_quarter_breaks_the_trend():
    rows = [row(1, "2026Q1", 3, "operational", 1), row(2, "2026Q2", 4, "operational", 2)]
    rows.append(row(3, "2026Q3", 5, "operational", 3))
    found = series.changes(rows, 3)
    assert kinds(found) == ["new_high", "step", "streak_up"]
    assert [c.get("delta") for c in found if c["kind"] == "step"] == [1]  # 5 lines now, not 3+4+5
    gap = [row(1, "2026Q1", 3, "operational", 1), row(2, "2026Q3", 5, "operational", 2)]
    gap.append(row(3, "2026Q4", 6, "operational", 3))
    assert "streak_up" not in kinds(series.changes(gap, 3))  # Q2 is missing, so no three-quarter trend


def test_a_correction_counts_only_from_when_it_was_visible():
    early = [row(1, "2026Q1", 100, "reported", 1), row(2, "2026Q2", 5, "reported", 2, superseded=3)]
    corrected = early + [row(3, "2026Q2", 4, "reported", 5)]
    # as of day 3 the correction is not visible yet: Q2 is still 5
    assert [p["value"] for p in series.points(early)] == [100, 5]
    # as of day 6 it is: Q2 is 4, compared with Q1 and not with the replaced 5
    assert [p["value"] for p in series.points(corrected)] == [100, 4]
    assert [(c["kind"], c.get("delta")) for c in series.changes(corrected, 3)] == [("step", -96)]


QUARTERS = [
    ("R1", "2026-01-15", "甲公司一季报：计划新建2条灌装线", ("2026Q1", 2, "planned", "计划新建2条灌装线")),
    ("R2", "2026-04-15", "甲公司二季报：重申计划新建2条灌装线", ("2026Q2", 2, "planned", "计划新建2条灌装线")),
    ("R3", "2026-07-15", "甲公司三季报：2条灌装线已开工建设", ("2026Q3", 2, "under_construction", "2条灌装线已开工")),
    ("R4", "2026-10-15", "甲公司四季报：3条灌装线投运", ("2026Q4", 3, "operational", "3条灌装线投运")),
    ("R5", "2026-10-20", "甲公司更正：四季度投运2条灌装线", ("2026Q4", 2, "operational", "投运2条灌装线")),
    ("R6", "2026-10-21", "甲公司新闻稿：产能扩张", ("2026Q4", 9, "operational", "原文里没有的9条")),
    ("R7", "2026-10-22", "甲公司公告：产能扩张顺利", ("2026Q4", 9, "operational", "产能扩张顺利")),
]


def triage_measures(payloads):
    by_title = {title: m for _, _, title, m in QUARTERS}

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        payloads.append(payload)
        items = []
        for r in payload["records"]:
            period, value, status, quote = by_title[r["title"]]
            measure = schemas.Measure(
                object="甲公司苏州基地",
                metric="灌装线数量",
                value=value,
                unit="条",
                kind="stock",
                period=period,
                status=status,
                quote=quote,
            )
            items.append(
                schemas.TriageItem(
                    index=r["index"],
                    topic=1 if payload["topics"] else None,
                    new_topic="甲公司扩产",
                    direction="business",
                    alert="normal",
                    signal="capacity",
                    measures=[measure],
                )
            )
        call = {"stage": stage, "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0, "error": None}
        return schemas.Triage(items=items), call

    return chat_json


def test_quarterly_records_build_one_series_and_owe_the_topic_only_real_changes(monkeypatch):
    payloads = []
    monkeypatch.setattr(llm, "chat_json", triage_measures(payloads))
    fabric = FakeFabric([])
    for rid, day, title, _ in QUARTERS:
        relations = [("corrects", "R4")] if rid == "R5" else ()
        available = day + "T08:00:00+08:00"
        fabric.docs += FakeFabric([record(rid, title, title, available=available, relations=relations)]).docs
        monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        rows = list(s.scalars(select(db.Measurement).order_by(db.Measurement.id)))
        events = list(
            s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series").order_by(db.TopicEvent.id))
        )
        topic = s.scalars(select(db.Topic)).one()
    # R6's quote is not in its text and R7's quote does not contain its value: nothing stored for either
    assert [(r.record_id, r.value, r.status) for r in rows] == [
        ("R1", 2, "planned"),
        ("R2", 2, "planned"),
        ("R3", 2, "under_construction"),
        ("R4", 3, "operational"),
        ("R5", 2, "operational"),
    ]
    assert rows[3].superseded_by == rows[4].id  # the correction replaces Q4's 3
    assert [(e.detail["record"], [c["kind"] for c in e.detail["changes"]]) for e in events] == [
        ("R3", ["status_advance"]),
        ("R4", ["step", "new_high"]),
        ("R5", ["status_advance"]),  # after the correction Q4 is the same 2 lines, now operational
    ]
    assert {e.detail["algo"] for e in events} == {series.VERSION}
    assert "series_change" in (topic.pending_reason or "")
    # triage is shown the series already being tracked, so the names stay the same next quarter
    assert payloads[-1]["series"] == [
        {"object": "甲公司苏州基地", "metric": "灌装线数量", "unit": "条", "kind": "stock", "scope": ""}
    ]
    # the same correction triaged again (a retried round) is not measured or announced twice
    doc = next(d for d in fabric.docs if d["record_id"] == "R5")
    again = schemas.Measure(
        object="甲公司苏州基地", metric="灌装线数量", value=2, unit="条", kind="stock",
        period="2026Q4", status="operational", quote="投运2条灌装线",
    )
    with db.session() as s:
        assert monitor.measure(s, monitor.settings(), doc, [again], topic, rows[4].available_at, True) is False
        assert s.scalar(select(func.count()).select_from(db.Measurement)) == 5
