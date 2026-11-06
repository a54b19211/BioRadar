"""T-24: values accumulate per object and metric across records; the program finds what changed."""

from datetime import UTC, datetime

import pytest
from conftest import FakeFabric, biocheck_fake, record
from sqlalchemy import func, select

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import measure_checks, monitor, schemas, series, triage
from bioradar.research.clock import utc

# the series kernel: families and dimensions count as proven here (the real gate: test_complete_domain)
pytestmark = pytest.mark.usefixtures("dims_verified")


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
    assert not series.quoted(1, "2026Q1有12条")  # the 1 of Q1 is not a value


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
        if stage == "extract":  # T-31 continuation windows: this stand-in states no further measures
            call = {
                "stage": stage,
                "model": "fake",
                "finish_reason": "stop",
                "usage": {},
                "seconds": 0,
                "error": None,
            }
            return schemas.Extraction(
                items=[schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
            ), call
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
    """Add the records one per round (clean artifacts); yields the feed before each round so a test can
    adjust a
    record. Each round's check worker answers with the v6 stand-in."""
    fabric = FakeFabric([], clean=True)
    for rid, day, title, relations in records:
        available = day + "T08:00:00+08:00"
        fabric.docs += FakeFabric(
            [record(rid, title, title, available=available, relations=relations)], clean=True
        ).docs
        yield fabric
        monitor.tick(is_demo=True, fabric=fabric)


def checked(monkeypatch):
    post = biocheck_fake()
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: post(body))


def lines(period, value, status, quote, obj="甲公司"):
    return schemas.Measure(
        object_text=obj,
        metric_text="灌装线",
        value_text=str(value),
        unit_text="条",
        kind="stock",
        period=period,
        period_type="interval",
        status=status,
        modality="planned" if status == "planned" else "actual",
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
    checked(monkeypatch)
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
        candidates = {c.record_id: c for c in s.scalars(select(db.MeasurementCandidate))}
    # planned values stay candidates (D4); R6's quote is not in its text, R7's 2 is only inside 2026 and 12:
    # neither can be built, nothing stored
    assert [(r.record_id, r.value_text, r.status) for r in rows] == [
        ("R3", "2", "under_construction"),
        ("R4", "3", "operational"),
        ("R5", "2", "operational"),
    ]
    assert {k: (c.status, c.reason) for k, c in candidates.items() if k in ("R1", "R2", "R6", "R7")} == {
        "R1": ("checked", None),
        "R2": ("checked", None),
        "R6": ("not_built", "quote_not_in_text"),
        "R7": ("not_built", "value_literal_not_located"),
    }
    assert all(r.check_id and r.numerator is None for r in rows)
    assert rows[1].superseded_by == rows[2].id  # the correction replaces Q4's 3
    assert [(e.detail["record"], [c["kind"] for c in e.detail["changes"]]) for e in events] == [
        ("R4", ["status_advance"]),
        ("R5", ["status_advance"]),  # after the correction Q4 is 2 lines operating
    ]
    assert {e.detail["algo"] for e in events} == {series.VERSION}
    assert "series_change" in (topic.pending_reason or "")
    assert payloads[-1]["series"] == [
        {"object": "甲公司", "metric": "灌装线", "unit": "条", "kind": "stock", "scope": ""}
    ]
    # the same correction triaged again (a retried round) is not enqueued, measured or announced twice
    doc = next(d for d in fabric.docs if d["record_id"] == "R5")
    again = schemas.TriageItem(
        index=1, alert="normal", measures=[lines("2026Q4", 2, "operational", "投运2条灌装线")]
    )
    with db.session() as s:
        assert (
            measure_checks.enqueue(s, monitor.settings(), doc, again, topic, rows[2].available_at, True) == 0
        )
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(db.Measurement)) == 3


def test_a_correction_of_another_version_does_not_replace_this_one(monkeypatch):
    checked(monkeypatch)
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
        object_text="澜津",
        metric_text="流感样病例比例",
        value_text=str(value),
        unit_text="%",
        kind="rate",
        scope_text=scope,
        period=period,
        period_type="interval",
        status="reported",
        quote=quote,
    )


WEEKS = [
    (
        "H1",
        "2026-10-12",
        "澜津5个哨点周报第41周流感样病例比例4.1%",
        [ili("2026-10-05/2026-10-11", 4.1, "流感样病例比例4.1%")],
    ),
    (
        "H2",
        "2026-10-19",
        "澜津5个哨点周报第42周流感样病例比例4.6%",
        [ili("2026-10-12/2026-10-18", 4.6, "流感样病例比例4.6%")],
    ),
    (
        "H3",
        "2026-10-26",
        "澜津5个哨点周报第43周流感样病例比例5.2%",
        [ili("2026-10-19/2026-10-25", 5.2, "流感样病例比例5.2%")],
    ),
    # a wider coverage and a yearly figure are other series, never compared with the weekly 5-sentinel one
    (
        "H4",
        "2026-10-27",
        "澜津新增哨点后的周报全部8个哨点流感样病例比例为3.0%",
        [ili("2026-10-19/2026-10-25", "3.0", "全部8个哨点流感样病例比例为3.0%", "8个哨点")],
    ),
    (
        "H5",
        "2026-10-28",
        "澜津年度监测综述全年流感样病例比例2.5%",
        [ili("2026", 2.5, "全年流感样病例比例2.5%", "")],
    ),
]


def test_a_rise_across_records_of_unknown_domain_is_kept_but_not_computed(monkeypatch):
    """Each weekly report alone is unremarkable (alert none, no topic). Until T-24 the three-week rise
    opened a topic named after object and metric; that grouped every topic-less point by name, which r160
    rules out as a business identity. The points are kept, effective and pending a domain; no change, event
    or topic."""
    checked(monkeypatch)
    payloads = []
    by_title = {title: (measures, "none") for _, _, title, measures in WEEKS}
    monkeypatch.setattr(llm, "chat_json", fake_triage(by_title, payloads))
    for _ in feed([(rid, day, title, ()) for rid, day, title, _ in WEEKS]):
        pass
    with db.session() as s:
        topics = list(s.scalars(select(db.Topic)))
        events = list(s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series")))
        rows = list(s.scalars(select(db.Measurement).order_by(db.Measurement.id)))
    assert topics == [] and events == []
    assert len(rows) == 5 and {r.scope for r in rows} == {"5个哨点", "8个哨点", ""}
    assert {(r.domain_basis, r.domain_id, r.topic_id) for r in rows} == {("unknown", None, None)}
    with db.session() as s:  # all five stay effective points, pending a known domain
        assert len(list(s.scalars(measure_checks.effective(s, monitor.settings(), True)))) == 5
    monkeypatch.setattr(monitor.analyses, "dispatch", lambda analysis_id: None)
    assert monitor.start_analyses(monitor.settings(), is_demo=True, cutoff=None) == []


def point(name, when, checked_=True):
    """A stored point, with (or without) the candidate and current check that make it effective."""
    with db.session() as s:
        cand = db.MeasurementCandidate(
            workspace_id=monitor.settings().workspace_id,
            is_demo=True,
            source="x",
            record_id=name,
            version_id=name,
            measure={"quote": "1"},
            status="checked",
            observed_at=when,
        )
        s.add(cand)
        s.flush()
        check_id = None
        if checked_:
            check = db.MeasurementCheck(
                candidate_id=cand.id,
                checker_version=measure_checks.CHECKER,
                contract_version=measure_checks.CONTRACT,
                status="consistent",
                gate="series",
            )
            s.add(check)
            s.flush()
            check_id = check.id
        s.add(
            db.Measurement(
                workspace_id=monitor.settings().workspace_id,
                is_demo=True,
                object=name,
                metric="m",
                value=1,
                value_text="1",
                period_start=when,
                period_end=when,
                source="x",
                record_id=name,
                version_id=name,
                quote="1",
                available_at=when,
                candidate_id=cand.id if checked_ else None,
                check_id=check_id,
            )
        )


def test_known_series_shown_to_a_replay_are_only_checked_ones_visible_then():
    point("早", datetime(2026, 1, 1, tzinfo=UTC))
    point("晚", datetime(2026, 1, 20, tzinfo=UTC))
    point("未核", datetime(2026, 1, 2, tzinfo=UTC), checked_=False)  # stored before checking: history only
    with db.session() as s:
        shown = triage.known_series(s, monitor.settings(), True, datetime(2026, 1, 10, tzinfo=UTC))
    assert [k["object"] for k in shown] == ["早"]


def test_a_second_notice_correcting_the_same_value_does_not_bring_the_old_value_back(monkeypatch):
    """Codex r15: 5, corrected to 4 on day 5, another notice corrects the same 5 to 3 on day 8. As of day 6
    the value is 4 before and after the second notice arrives; from day 8 the two notices conflict."""
    checked(monkeypatch)
    payloads = []
    quarters = [
        ("R4", "2026-10-01", "乙公司Q4投运5条灌装线", ("2026Q4", 5, "operational", "投运5条灌装线")),
        ("R5", "2026-10-05", "乙公司更正：Q4投运4条灌装线", ("2026Q4", 4, "operational", "投运4条灌装线")),
        (
            "R6",
            "2026-10-08",
            "乙公司另一通知：Q4投运3条灌装线",
            ("2026Q4", 3, "operational", "投运3条灌装线"),
        ),
    ]
    by_title = {title: ([lines(*m, obj="乙公司")], "normal") for _, _, title, m in quarters}
    monkeypatch.setattr(llm, "chat_json", fake_triage(by_title, payloads, topic_title="乙公司扩产"))
    records = [
        (rid, day, title, [("corrects", "R4")] if rid != "R4" else ()) for rid, day, title, _ in quarters
    ]
    for _ in feed(records):
        pass
    with db.session() as s:
        rows = list(s.scalars(select(db.Measurement).order_by(db.Measurement.id)))
    as_rows = [
        {
            "id": r.id,
            "value": r.value,
            "status": r.status,
            "period": r.period,
            "period_start": r.period_start,
            "period_end": r.period_end,
            "available_at": r.available_at,
            "superseded_by": r.superseded_by,
        }
        for r in rows
    ]
    day6 = [r for r in as_rows if utc(r["available_at"]) <= datetime(2026, 10, 6, tzinfo=UTC)]
    assert [p["value"] for p in series.points(day6)] == [4]  # the 5 stays replaced
    assert sorted(p["value"] for p in series.points(as_rows)) == [3, 4]  # two notices, no basis to choose
    assert [c["kind"] for c in series.changes(as_rows, rows[2].id)] == ["conflict"]
