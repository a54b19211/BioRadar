"""T-24: values accumulate per object and metric across records; the program finds what changed."""

from datetime import UTC, datetime

from conftest import FakeFabric, record
from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters import llm
from bioradar.research import monitor, schemas, series


def test_periods_and_numbers_are_read_strictly():
    at = datetime(2026, 9, 3, tzinfo=UTC)
    assert series.period_bounds("2026Q2", at) == (
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 6, 30, tzinfo=UTC),
    )
    assert series.period_bounds("2026h1", at)[1] == datetime(2026, 6, 30, tzinfo=UTC)
    assert series.period_bounds("2026-02", at)[1] == datetime(2026, 2, 28, tzinfo=UTC)
    assert series.period_bounds("", at) == (at, at)  # a stock "as of" the record
    assert series.period_bounds("去年下半年", at) is None
    periods = ("2026", "2026Q4", "2026H1", "2026-10", "2026-10-05/2026-10-11", "")
    assert [series.grain(p) for p in periods] == ["Y", "Q", "H", "M", "R", "A"]
    assert series.quoted(12, "2026Q1有12条投运产线") and not series.quoted(2, "2026Q1有12条投运产线")
    assert series.quoted(4.1, "比例为4.1%") and not series.quoted(4, "比例为4.1%")
    assert series.quoted(3, "8个哨点为3.0%") and series.quoted(1444, "已有1,444篇")
    assert not series.quoted(1, "2026Q1有12条")  # the 1 of Q1 is not a value
    assert series.quoted(-3, "净变化-3条") and not series.quoted(3, "净变化-3条")


def row(id_, period, value, status, available, superseded=None):
    start, end = series.period_bounds(period, None)
    return {
        "id": id_,
        "value": value,
        "status": status,
        "period": period,
        "period_start": start,
        "period_end": end,
        "available_at": datetime(2026, 1, available, tzinfo=UTC),
        "superseded_by": superseded,
    }


def kinds(found):
    return sorted(c["kind"] for c in found)


def test_a_restated_plan_is_not_growth_and_stages_are_not_compared_as_values():
    rows = [row(1, "2026Q1", 2, "planned", 1), row(2, "2026Q2", 2, "planned", 2)]
    assert kinds(series.changes(rows, 2)) == []  # the same plan restated
    rows.append(row(3, "2026Q3", 2, "under_construction", 3))
    assert kinds(series.changes(rows, 3)) == ["status_advance"]
    rows.append(row(4, "2026Q4", 3, "operational", 4))
    [advance] = series.changes(rows, 4)  # 2 being built -> 3 operating: a new stage, not "+50%"
    assert (advance["kind"], advance["from_value"], advance["to_value"]) == ("status_advance", 2, 3)
    rows.append(row(5, "2026Q4", 3, "operational", 5))  # another record repeats Q4
    assert kinds(series.changes(rows, 5)) == ["repeat"]
    rows.append(row(6, "2027Q1", 4, "operational", 6))
    assert kinds(series.changes(rows, 6)) == ["step"]
    plan_then_run = [row(1, "2026Q1", 10, "planned", 1), row(2, "2026Q2", 3, "operational", 2)]
    assert kinds(series.changes(plan_then_run, 2)) == ["status_advance"]  # not -70%


def test_a_stock_series_is_compared_not_summed_and_a_gap_breaks_the_trend():
    rows = [row(1, "2026Q1", 3, "operational", 1), row(2, "2026Q2", 4, "operational", 2)]
    rows.append(row(3, "2026Q3", 5, "operational", 3))
    found = series.changes(rows, 3)
    assert kinds(found) == ["new_high", "step", "streak_up"]
    assert [c.get("delta") for c in found if c["kind"] == "step"] == [1]  # 5 lines now, not 3+4+5
    gap = [row(1, "2026Q1", 3, "operational", 1), row(2, "2026Q3", 5, "operational", 2)]
    gap.append(row(3, "2026Q4", 6, "operational", 3))
    assert "streak_up" not in kinds(series.changes(gap, 3))  # Q2 is missing


def test_a_year_is_not_compared_with_a_quarter():
    rows = [row(1, "2026", 100, "operational", 1), row(2, "2026Q4", 25, "operational", 2)]
    assert series.changes(rows, 2) == []  # not -75%


def test_two_values_for_one_period_are_a_conflict_not_a_trend():
    rows = [row(1, "2026Q1", 1, "operational", 1), row(2, "2026Q2", 2, "operational", 2)]
    rows.append(row(3, "2026Q2", 3, "operational", 3))  # another source says 3 for the same Q2
    assert [c["kind"] for c in series.changes(rows, 3)] == ["conflict"]
    rows.append(row(4, "2026Q3", 4, "operational", 4))
    assert "streak_up" not in kinds(series.changes(rows, 4))  # Q2 is unresolved


def test_a_correction_counts_from_when_it_was_visible_and_revises_every_later_point():
    early = [row(1, "2026Q1", 1, "operational", 1), row(2, "2026Q2", 2, "operational", 2, superseded=5)]
    early += [row(3, "2026Q3", 3, "operational", 3), row(4, "2026Q4", 4, "operational", 4)]
    assert [p["value"] for p in series.points(early)] == [1, 2, 3, 4]  # as of day 4: Q2 is still 2
    corrected = early + [row(5, "2026Q2", 10, "operational", 5)]
    assert [p["value"] for p in series.points(corrected)] == [1, 10, 3, 4]
    found = series.changes(corrected, 5)
    # Q3 and Q4 are evaluated again: the rise to Q4 and its new high no longer hold
    assert [c["rows"] for c in found if c["kind"] == "revised"] == [[3], [4]]
    q4 = next(c for c in found if c["kind"] == "revised" and c["rows"] == [4])
    assert "streak_up" in q4["was"] and "streak_up" not in q4["now"] and "new_high" not in q4["now"]
    assert series.notable(found)


def fake_triage(by_title, payloads, *, topic_title=""):
    def chat_json(settings_, stage, system, payload, model_cls, **options):
        payloads.append(payload)
        items = []
        for r in payload["records"]:
            measures, alert = by_title[r["title"]]
            items.append(
                schemas.TriageItem(
                    index=r["index"],
                    topic=1 if payload["topics"] and topic_title else None,
                    new_topic=topic_title,
                    direction="business" if topic_title else "public_health",
                    alert=alert,
                    measures=measures,
                )
            )
        call = {
            "stage": stage,
            "model": "fake",
            "finish_reason": "stop",
            "usage": {},
            "seconds": 0,
            "error": None,
        }
        return schemas.Triage(items=items), call

    return chat_json


def feed(records):
    """Add the records one per round; yields the feed before each round so a test can adjust a record."""
    fabric = FakeFabric([])
    for rid, day, title, relations in records:
        available = day + "T08:00:00+08:00"
        fabric.docs += FakeFabric([record(rid, title, title, available=available, relations=relations)]).docs
        yield fabric
        monitor.tick(is_demo=True, fabric=fabric)


def lines(period, value, status, quote):
    return schemas.Measure(
        object="甲公司苏州基地",
        metric="灌装线数量",
        value=value,
        unit="条",
        kind="stock",
        period=period,
        status=status,
        quote=quote,
    )


QUARTERS = [
    ("R1", "2026-01-15", "甲公司一季报：计划新建2条灌装线", ("2026Q1", 2, "planned", "计划新建2条灌装线")),
    (
        "R2",
        "2026-04-15",
        "甲公司二季报：重申计划新建2条灌装线",
        ("2026Q2", 2, "planned", "计划新建2条灌装线"),
    ),
    (
        "R3",
        "2026-07-15",
        "甲公司三季报：2条灌装线已开工建设",
        ("2026Q3", 2, "under_construction", "2条灌装线已开工"),
    ),
    ("R4", "2026-10-15", "甲公司四季报：3条灌装线投运", ("2026Q4", 3, "operational", "3条灌装线投运")),
    ("R5", "2026-10-20", "甲公司更正：四季度投运2条灌装线", ("2026Q4", 2, "operational", "投运2条灌装线")),
    ("R6", "2026-10-21", "甲公司新闻稿：产能扩张", ("2026Q4", 9, "operational", "原文里没有的9条")),
    (
        "R7",
        "2026-10-22",
        "甲公司公告：2026Q4有12条线在调试",
        ("2026Q4", 2, "operational", "2026Q4有12条线在调试"),
    ),
]


def quarter_records(quarters):
    return [(rid, day, title, [("corrects", "R4")] if rid == "R5" else ()) for rid, day, title, _ in quarters]


def test_quarterly_records_build_one_series_and_owe_the_topic_only_real_changes(monkeypatch):
    payloads = []
    by_title = {title: ([lines(*m)], "normal") for _, _, title, m in QUARTERS}
    monkeypatch.setattr(llm, "chat_json", fake_triage(by_title, payloads, topic_title="甲公司扩产"))
    for fabric in feed(quarter_records(QUARTERS)):
        pass
    with db.session() as s:
        rows = list(s.scalars(select(db.Measurement).order_by(db.Measurement.id)))
        events = list(
            s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series").order_by(db.TopicEvent.id))
        )
        topic = s.scalars(select(db.Topic)).one()
    # R6's quote is not in its text; R7's 2 only appears inside 2026 and 12: nothing stored for either
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
        ("R4", ["status_advance"]),
        ("R5", ["status_advance"]),  # after the correction Q4 is 2 lines operating
    ]
    assert {e.detail["algo"] for e in events} == {series.VERSION}
    assert "series_change" in (topic.pending_reason or "")
    assert payloads[-1]["series"] == [
        {"object": "甲公司苏州基地", "metric": "灌装线数量", "unit": "条", "kind": "stock", "scope": ""}
    ]
    # the same correction triaged again (a retried round) is not measured or announced twice
    doc = next(d for d in fabric.docs if d["record_id"] == "R5")
    again = schemas.TriageItem(
        index=1, alert="normal", measures=[lines("2026Q4", 2, "operational", "投运2条灌装线")]
    )
    with db.session() as s:
        assert monitor.measure(s, monitor.settings(), doc, again, topic, rows[4].available_at, True, []) == []
        assert s.scalar(select(func.count()).select_from(db.Measurement)) == 5


def test_a_correction_of_another_version_does_not_replace_this_one(monkeypatch):
    payloads = []
    by_title = {title: ([lines(*m)], "normal") for _, _, title, m in QUARTERS[3:5]}
    monkeypatch.setattr(llm, "chat_json", fake_triage(by_title, payloads, topic_title="甲公司扩产"))
    rounds = feed(quarter_records(QUARTERS[3:5]))
    next(rounds)
    fabric = next(rounds)  # R5 is in the feed; aim its correction at a version of R4 that is not stored
    fabric.docs[-1]["relations"][0]["target_version_id"] = "another-version"
    for _ in rounds:
        pass
    with db.session() as s:
        r4 = s.scalars(select(db.Measurement).where(db.Measurement.record_id == "R4")).one()
    assert r4.superseded_by is None


def ili(period, value, quote, scope="5个哨点"):
    return schemas.Measure(
        object="澜津",
        metric="流感样病例比例",
        value=value,
        unit="%",
        kind="rate",
        scope=scope,
        period=period,
        status="reported",
        quote=quote,
    )


WEEKS = [
    (
        "H1",
        "2026-10-12",
        "澜津哨点周报第41周流感样病例比例4.1%",
        [ili("2026-10-05/2026-10-11", 4.1, "流感样病例比例4.1%")],
    ),
    (
        "H2",
        "2026-10-19",
        "澜津哨点周报第42周流感样病例比例4.6%",
        [ili("2026-10-12/2026-10-18", 4.6, "流感样病例比例4.6%")],
    ),
    (
        "H3",
        "2026-10-26",
        "澜津哨点周报第43周流感样病例比例5.2%",
        [ili("2026-10-19/2026-10-25", 5.2, "流感样病例比例5.2%")],
    ),
    # a wider coverage and a yearly figure are other series, never compared with the weekly 5-sentinel one
    (
        "H4",
        "2026-10-27",
        "澜津新增哨点后的周报全部8个哨点为3.0%",
        [ili("2026-10-19/2026-10-25", 3.0, "全部8个哨点为3.0%", "8个哨点")],
    ),
    (
        "H5",
        "2026-10-28",
        "澜津年度监测综述全年流感样病例比例2.5%",
        [ili("2026", 2.5, "全年流感样病例比例2.5%")],
    ),
]


def test_a_trend_no_single_record_shows_opens_a_topic_and_is_owed_an_analysis(monkeypatch):
    """Each weekly report alone is unremarkable (alert none, no topic); the three-week rise is the signal."""
    payloads = []
    by_title = {title: (measures, "none") for _, _, title, measures in WEEKS}
    monkeypatch.setattr(llm, "chat_json", fake_triage(by_title, payloads))
    for _ in feed([(rid, day, title, ()) for rid, day, title, _ in WEEKS]):
        pass
    with db.session() as s:
        topics = list(s.scalars(select(db.Topic)))
        events = list(s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series")))
        rows = list(s.scalars(select(db.Measurement).order_by(db.Measurement.id)))
    assert [t.title for t in topics] == ["澜津·流感样病例比例（5个哨点）"]
    # the three weekly records now belong to the topic, which also makes it rise
    assert "series_change" in topics[0].pending_reason.split("+") and topics[0].direction == "public_health"
    assert [[c["kind"] for c in e.detail["changes"]] for e in events] == [["step", "new_high", "streak_up"]]
    assert len(rows) == 5 and {r.scope for r in rows} == {
        "5个哨点",
        "8个哨点",
    }  # all kept, none compared across
    # the analysis it starts reads the three weekly reports and is told what the program computed
    monkeypatch.setattr(monitor.analyses, "dispatch", lambda analysis_id: None)
    [analysis_id] = monitor.start_analyses(monitor.settings(), is_demo=True, cutoff=None)
    with db.session() as s:
        analysis = s.get(db.Analysis, analysis_id)
    assert sorted(r["record_id"] for r in analysis.input["seed_records"]) == ["H1", "H2", "H3"]
    question = analysis.input["question"]
    assert "4.1%" in question and "5.2%" in question and "streak_up" in question


def test_known_series_shown_to_a_replay_are_only_those_visible_then():
    at = {"早": datetime(2026, 1, 1, tzinfo=UTC), "晚": datetime(2026, 1, 20, tzinfo=UTC)}
    with db.session() as s:
        for name, when in at.items():
            s.add(
                db.Measurement(
                    workspace_id=monitor.settings().workspace_id,
                    is_demo=True,
                    object=name,
                    metric="m",
                    value=1,
                    period_start=when,
                    period_end=when,
                    source="x",
                    record_id=name,
                    version_id=name,
                    quote="1",
                    available_at=when,
                )
            )
    with db.session() as s:
        shown = monitor.known_series(s, monitor.settings(), True, datetime(2026, 1, 10, tzinfo=UTC))
    assert [k["object"] for k in shown] == ["早"]
