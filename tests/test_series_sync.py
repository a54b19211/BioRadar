"""Fabric's measurement objects into the radar's topics (research/series_sync.py, docs/decisions/0010): the
feed and the pull by version, both arrival orders, the cursor, withheld events, series keys, the domain gate
on Fabric's objects, corrections and supersessions, and what an analysis is handed and reads."""

import hashlib
import json
import os
import pathlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import FakeFabric, record
from fabric_measurements import CHECKER, version_id
from sqlalchemy import func, select
from test_access import admin_client, fabric_sources  # noqa: F401 - the fixture the API test takes

from bioradar import app, db
from bioradar.adapters import llm
from bioradar.config import settings
from bioradar.research import domain, monitor, scheduling, schemas, series_sync, topics
from bioradar.research.run import investigate

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "measurement_feed_contract.json"
# a byte copy of clean-fabric tests/fixtures/measurement_feed_contract.json (b2fa2e6, unchanged at 0faf8e9)
FIXTURE_SHA256 = "0bd804870dae1e8d0ceb74db8fb1eb15fc6fa56f72758b92818c02a26708b8af"
SOURCE = "external_archive"  # the demo monitoring source
TOPIC = "卢旺达马尔堡疫情"
V = version_id("who-a")
CALL = {"stage": "triage", "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0, "error": None}
PROVEN = [
    {"kind": "disease", "value": "马尔堡病毒病", "check": "match"},
    {"kind": "population", "value": "全人群", "check": "match"},
    {"kind": "date_basis", "value": "按报告日", "check": "match"},
]


def contract():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    return json.loads(FIXTURE.read_text("utf-8"))


@pytest.fixture
def placing(monkeypatch):
    """Triage puts every record into one topic (a record titled 噪声 into none)."""

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        index = {t["title"]: t["index"] for t in payload["topics"]}
        items = [
            schemas.TriageItem(index=r["index"], direction="public_health", alert="none")
            if "噪声" in r["title"]
            else schemas.TriageItem(
                index=r["index"],
                direction="public_health",
                topic=index.get(TOPIC),
                new_topic="" if TOPIC in index else TOPIC,
                alert="normal",
            )
            for r in payload["records"]
        ]
        return schemas.Triage(items=items), dict(CALL, stage=stage)

    monkeypatch.setattr(llm, "chat_json", chat_json)


def who(name="who-a", title="马尔堡病毒病 - 卢旺达", available="2026-10-29T08:00:00+00:00"):
    return record(
        name, title, "截至2026年10月28日……", available=available, version=version_id(name), source=SOURCE
    )


def marburg(fabric, version=V, record_id="who-a"):
    """The three objects of Fabric's WHO Marburg slice: confirmed, deaths, recovered."""
    m = fabric.measured
    return [
        m.add(version, source=SOURCE, record=record_id, value=v, metric=f"报告马尔堡病毒病{what}")
        for what, v in (("确诊病例", "66"), ("死亡病例", "15"), ("治愈病例", "40"))
    ]


def tick(fabric):
    return monitor.tick(is_demo=True, fabric=fabric)


def links():
    """(topic title, object, series identity apart from the topic's id, gaps) of every link."""
    with db.session() as s:
        return sorted(
            (
                s.get(db.Topic, p.topic_id).title,
                p.measurement_id,
                p.object_key,
                p.dims_key,
                tuple(p.gate_reasons),
            )
            for p in s.scalars(select(db.SeriesPoint))
        )


def count(model):
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def topic_events(kind):
    with db.session() as s:
        return [e.detail for e in s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == kind))]


def reset():
    db.Base.metadata.drop_all(db.engine())
    db.Base.metadata.create_all(db.engine())


# ---- the linkage contract: both arrival orders --------------------------------------------------------


def topic_first():
    fabric = FakeFabric([who()])
    tick(fabric)  # the topic exists before any measurement
    assert links() == []
    marburg(fabric)
    tick(fabric)  # the objects arrive through the feed
    return fabric


def measurement_first():
    fabric = FakeFabric([who()])
    fabric.docs, held = [], fabric.docs  # the record is not in Fabric's record feed yet
    marburg(fabric)
    tick(fabric)  # the measurements arrive before the topic: kept, linked to nothing
    with db.session() as s:
        assert {r.state for r in s.scalars(select(db.MeasurementInbox))} == {"pending_topic"}
    assert links() == []
    fabric.docs = held
    tick(fabric)
    return fabric


def test_both_arrival_orders_end_with_the_same_links_each_once_and_replaying_adds_nothing(placing):
    expect = contract()["linkage_expectation"]["who_marburg_fixture"]
    first = topic_first()
    one = links()
    reset()
    second = measurement_first()
    assert links() == one and len(one) == expect["links"] == expect["objects"]
    assert first.measured.objects.keys() == second.measured.objects.keys()
    with db.session() as s:
        s.get(db.Cursor, (series_sync.FEED, True)).after_seq = 0  # replay the feed from 0, and pull again
    tick(second)
    series_sync.link_versions(settings(), second, [{"source": SOURCE, "version_id": V}], is_demo=True)
    assert links() == one and count(db.MeasurementInbox) == expect["objects"]
    feed = second.measured.changes(is_demo=True, after_seq=0, limit=200)["items"]
    assert sum(i["kind"] == "created" for i in feed) == expect["created_events"]


def test_the_stand_in_speaks_fabrics_contract():
    feed_contract, pull_contract = contract()["feed"], contract()["pull"]
    fabric = FakeFabric([who()])
    [mid, *_] = marburg(fabric)
    for item in fabric.measurement_changes(is_demo=True)["items"]:
        assert set(feed_contract["item_keys"]) <= set(item) and item["kind"] in feed_contract["kinds"]
        assert set(item["versions"]) == set(feed_contract["version_keys"])
    receipt = fabric.measurements(SOURCE, [V], is_demo=True, mode="current", cutoff="x")["measurements"][0]
    assert set(pull_contract["receipt_keys"]) <= set(receipt) and receipt["measurement_id"] == mid


def test_the_cursor_moves_only_after_a_whole_page_is_written(placing, monkeypatch):
    fabric = FakeFabric([who()])
    tick(fabric)
    marburg(fabric)
    monkeypatch.setattr(series_sync, "PAGE", 2)
    real, calls = series_sync.note, []

    def dies_on_the_second_page(session, *args, **kwargs):
        calls.append(args)
        if len(calls) == 3:
            raise RuntimeError("worker killed in the middle of page 2")
        return real(session, *args, **kwargs)

    monkeypatch.setattr(series_sync, "note", dies_on_the_second_page)
    with pytest.raises(RuntimeError):
        series_sync.read_feed(settings(), fabric, is_demo=True)
    with db.session() as s:
        assert s.get(db.Cursor, (series_sync.FEED, True)).after_seq == 2  # page 1 stored, page 2 not
    assert count(db.MeasurementInbox) == 2
    monkeypatch.setattr(series_sync, "note", real)
    tick(fabric)
    with db.session() as s:
        assert s.get(db.Cursor, (series_sync.FEED, True)).after_seq == 3
    assert count(db.MeasurementInbox) == 3 and len(links()) == 3


def test_a_feed_page_that_is_not_the_contract_moves_no_cursor(placing):
    fabric = FakeFabric([who()])
    marburg(fabric)
    fabric.measured.events[0]["kind"] = "renamed"
    assert series_sync.read_feed(settings(), fabric, is_demo=True)["pages"] == 0
    with db.session() as s:
        assert s.get(db.Cursor, (series_sync.FEED, True)) is None
    assert count(db.MeasurementInbox) == 0


# ---- withheld events -------------------------------------------------------------------------------------


def test_events_withheld_while_a_source_is_revoked_are_pulled_again_by_version_once_it_is_back(placing):
    fabric = FakeFabric([who()])
    tick(fabric)
    fabric.measured.revoked.add(SOURCE)
    marburg(fabric)
    out = tick(fabric)["measurements"]
    assert out["withheld"] == 3 and links() == []  # counted, never shown; the cursor moves past them
    with db.session() as s:
        assert s.get(db.Cursor, (series_sync.FEED, True)).after_seq == 3
        assert s.get(db.MeasurementWithheld, ("default", True, SOURCE)).state == "withheld"
    tick(fabric)  # still revoked: still withheld
    assert links() == []
    fabric.measured.revoked.clear()  # re-granted
    tick(fabric)  # one page of the placed versions pulled again
    tick(fabric)  # nothing left: released
    assert len(links()) == 3
    with db.session() as s:
        assert s.get(db.MeasurementWithheld, ("default", True, SOURCE)).state == "released"


def test_a_correction_withheld_for_its_cause_source_applies_once_that_source_is_back(placing):
    fabric = FakeFabric([who()])
    tick(fabric)
    [cases, *_] = marburg(fabric)
    tick(fabric)
    fabric.measured.revoked.add("who_corrections")
    fabric.measured.correct(cases, by_source="who_corrections")
    tick(fabric)
    assert topic_events("correction") == []  # Fabric did not apply it either
    fabric.measured.revoked.clear()
    tick(fabric)  # the source answers again: released, every linked object read again
    tick(fabric)
    [corrected] = topic_events("correction")
    assert corrected["withdrawn"] == [cases] and corrected["by_source"] == "who_corrections"
    with db.session() as s:
        point = s.scalar(select(db.SeriesPoint).filter_by(measurement_id=cases))
        assert (
            point.left_reason == "corrected"
            and "source_corrected" in s.get(db.Topic, point.topic_id).pending_reason
        )


# ---- series keys and the comparable domain ----------------------------------------------------------------


def monthly(fabric, record_id, month, value, identity=None, obj="卢旺达卫生部"):
    return fabric.measured.add(
        version_id(record_id),
        source=SOURCE,
        record=record_id,
        value=str(value),
        metric="新增确诊病例",
        obj=obj,
        period={"type": "interval", "precision": "month", "value": f"2026-{month:02d}"},
        kind="flow",
        dims=PROVEN,
        identity=identity,
    )


CONFIRMED = {
    "state": "confirmed",
    "key": "MESH|D008381",
    "graph": {"source_id": "g", "record_id": "b", "version_id": "c" * 64},
}


def bulletins(fabric, identity=None):
    for n, (month, value) in enumerate(((7, 10), (8, 14), (9, 20)), 1):
        rid = f"who-m{n}"
        fabric.docs += FakeFabric([who(rid, available=f"2026-{month + 1:02d}-03T08:00:00+00:00")]).docs
        monthly(fabric, rid, month, value, identity)
        tick(fabric)


def test_records_with_a_confirmed_identity_make_one_series_and_a_rise_owes_the_topic(placing):
    fabric = FakeFabric([])
    bulletins(fabric, CONFIRMED)
    [*_, last] = topic_events("series")
    assert last["authority"] == "fabric" and last["object_key"] == "MESH|D008381"
    assert sorted(c["kind"] for c in last["changes"]) == ["new_high", "step", "streak_up"]
    with db.session() as s:
        topic = s.scalar(select(db.Topic))
        assert "series_change" in topic.pending_reason
        assert "程序按已入账观测算出" in scheduling.series_basis(s, topic.id)
        assert {p["current"] for p in scheduling.series_basis_points(s, topic.id)} == {True}


def test_a_provisional_identity_never_merges_two_records_on_a_name(placing):
    fabric = FakeFabric([])
    bulletins(fabric)  # the same literal object in three records: three series of one point each
    assert topic_events("series") == []
    with db.session() as s:
        keys = {p.object_key for p in s.scalars(select(db.SeriesPoint))}
    assert keys == {f"literal:{SOURCE}|who-m{n}|卢旺达卫生部" for n in (1, 2, 3)}


def gate(**over):
    obj = {
        "family": "case_count",
        "metric": "新增确诊病例",
        "scope": "湖北省",
        "scope_mode": "stated",
        "object": "医院A",
        "measure_kind": "flow",
        "dims": PROVEN,
        "dims_unlocated": [],
        "check": {"status": "consistent", "run_id": "r-1"},
    }
    return domain.series_gate(obj | over)


def test_the_gate_reads_fabrics_object_and_its_per_dimension_check():
    assert gate()["reasons"] == []
    carried = {d["kind"]: d for d in gate()["dims"] if d["state"] == "carried"}
    assert (
        carried["case_definition"]["proof"]["phrase"] == "新增确诊病例"
        and carried["coverage"]["norm"] == "湖北省"
    )
    unchecked = [dict(d, check="unsupported") if d["kind"] == "population" else d for d in PROVEN]
    assert gate(dims=unchecked)["reasons"] == ["dim_unchecked:population"]
    assert gate(dims=PROVEN[:1])["reasons"] == ["dim_unknown:population", "dim_unknown:date_basis"]
    located = gate(
        dims=PROVEN[:2], dims_unlocated=[{"kind": "date_basis", "text": "按报告日", "reason": "ambiguous"}]
    )
    assert located["reasons"] == ["dim_unchecked:date_basis"]
    assert gate(family="")["reasons"] == ["family_not_determined"]
    assert gate(metric="批准注册医疗器械产品")["reasons"] == ["family_not_determined"]  # not a case count
    assert gate(check={"status": "inconsistent", "run_id": "r-1"})["reasons"] == ["family_not_determined"]
    assert gate(family="efficacy")["reasons"] == ["efficacy_not_compared"]
    assert "dim_unknown:denominator_definition" in gate(measure_kind="rate")["reasons"]
    assert gate(scope=None, scope_mode="unknown")["reasons"] == ["dim_unknown:coverage"]
    assert gate(scope=None, scope_mode="object_metric_total")["reasons"] == []  # the object's own total
    other = [dict(d, value="成人") if d["kind"] == "population" else d for d in PROVEN]
    assert gate(dims=other)["dims_key"] != gate()["dims_key"]  # another cohort is another group
    long = [
        dict(d, value="符合本研究纳入条件的成年受试者" * 40) if d["kind"] == "population" else d
        for d in PROVEN
    ]
    assert len(gate(dims=long)["dims_key"]) == 71  # a lossless identity, never truncated


def test_a_milestone_and_a_capacity_need_their_own_dimensions():
    milestone = {"family": "milestone_count", "metric": "甲适应症获批项目", "object": "某药"}
    g = gate(**milestone, dims=[{"kind": "date_basis", "value": "按批准日", "check": "match"}])
    assert "dim_unknown:product_or_class" in g["reasons"]  # an object proves no product class
    assert next(d for d in g["dims"] if d["kind"] == "indication")["state"] == "carried"
    plan = gate(family="capacity", metric="计划新建灌装线", dims=[])
    run = gate(family="capacity", metric="投运灌装线", dims=[])
    assert plan["dims_key"] != run["dims_key"]  # a plan and an operating line are never one group


# ---- corrections, supersessions, contradictions -----------------------------------------------------


def test_a_superseded_object_leaves_and_its_successor_joins_in_its_place(placing):
    fabric = FakeFabric([who()])
    tick(fabric)
    [cases, *_] = marburg(fabric)
    tick(fabric)
    newer = fabric.measured.add(
        V, source=SOURCE, record="who-a", value="66", metric="报告马尔堡病毒病确诊病例"
    )
    fabric.measured.supersede(cases, newer)
    tick(fabric)
    [left] = topic_events("measurement_left")
    assert (left["measurement"], left["by"], left["reason"]) == (cases, newer, "superseded")
    with db.session() as s:
        current = {p.measurement_id for p in s.scalars(select(db.SeriesPoint)) if p.left_at is None}
    assert newer in current and cases not in current and len(current) == 3


def test_a_withdrawn_correction_brings_the_point_back(placing):
    fabric = FakeFabric([who()])
    tick(fabric)
    [cases, *_] = marburg(fabric)
    tick(fabric)
    fabric.measured.correct(cases)
    tick(fabric)
    fabric.measured.withdraw_correction(cases)
    tick(fabric)
    assert [e["measurement"] for e in topic_events("measurement_back")] == [cases]
    with db.session() as s:
        assert s.scalar(select(db.SeriesPoint).filter_by(measurement_id=cases)).left_at is None


def test_one_record_stating_two_values_for_one_period_keeps_both_and_owes_the_topic(placing):
    fabric = FakeFabric([who()])
    tick(fabric)
    for value in ("66", "68"):
        fabric.measured.add(V, source=SOURCE, record="who-a", value=value, metric="报告马尔堡病毒病确诊病例")
    tick(fabric)
    [conflict] = topic_events("record_conflict")
    assert sorted(p["value"] for p in conflict["points"]) == ["66", "68"]
    with db.session() as s:
        topic = s.scalar(select(db.Topic))
        assert all(series_sync.RECORD_CONFLICT in p.gate_reasons for p in s.scalars(select(db.SeriesPoint)))
        text, seeds, ids, _, _ = scheduling.conflict_basis(s, topic.id, datetime(2026, 1, 1, tzinfo=UTC))
    assert "66例" in text and "68例" in text and seeds[0]["version_id"] == V and len(ids) == 2


# ---- a reading is a point, marked by its check ----------------------------------------------------------


def reading(fabric, check, **over):
    """One reading of the Marburg record as Fabric publishes it: marked by its check. A reading no check has
    cleared says "unknown" for its unit, as BioCheck's verdict on a unit-less reading does."""
    over = dict(unit="例" if check == "consistent" else "unknown", dims=PROVEN) | over
    return fabric.measured.add(V, source=SOURCE, record="who-a", value="66", check=check, **over)


def readings():
    with db.session() as s:
        return topics.readings(s, s.scalar(select(db.Topic.id)))


@pytest.mark.parametrize("check", ["unchecked", "inconsistent"])
def test_a_reading_not_cleared_by_its_check_is_a_point_marked_by_it_and_never_compared(placing, check):
    fabric = FakeFabric([who()])
    tick(fabric)
    mid = reading(fabric, check)
    assert tick(fabric)["measurements"]["linked"] == 1
    with db.session() as s:
        point = s.scalar(select(db.SeriesPoint))
        assert (point.measurement_id, point.check_status, point.left_at) == (mid, check, None)
        assert point.unit == "unknown"
        assert point.checker_version == (CHECKER if check == "inconsistent" else "unchecked")
        assert point.gate_reasons == ["family_not_determined"] and not series_sync.proven(point)
        assert s.scalar(select(db.MeasurementInbox.state)) == "linked"
        [handed] = scheduling.described(s, point.topic_id, [mid])
    assert handed["extraction_check"]["status"] == check
    assert handed["comparability"]["state"] == "not_comparable"
    assert topic_events("series") == []  # nothing is compared, so nothing is owed for it
    [shown] = readings()
    assert (shown["check"], shown["comparable"]) == (check, False)
    assert (shown["value"], shown["unit"]) == ("66", "unknown")  # as stated: no unit is made up


def test_the_checks_answer_replaces_the_unchecked_point_and_the_facts_are_the_current_objects(placing):
    fabric = FakeFabric([who()])
    tick(fabric)
    waiting = reading(fabric, "unchecked")
    tick(fabric)
    assert [r["check"] for r in readings()] == ["unchecked"]
    answer = reading(fabric, "consistent")  # the same reading, answered: Fabric publishes a second object
    fabric.measured.recheck(waiting, answer)
    tick(fabric)
    with db.session() as s:
        old = s.scalar(select(db.SeriesPoint).filter_by(measurement_id=waiting))
        new = s.scalar(select(db.SeriesPoint).filter_by(measurement_id=answer))
        assert (old.left_at is not None, old.left_by, old.check_status) == (True, answer, "unchecked")
        assert (new.left_at, new.check_status, new.checker_version) == (None, "consistent", CHECKER)
        assert new.gate_reasons == [] and series_sync.proven(new) and not series_sync.proven(old)
    [left] = topic_events("measurement_left")
    assert (left["measurement"], left["by"]) == (waiting, answer)
    [shown] = readings()  # one fact, though Fabric created two objects for it
    assert (shown["id"], shown["check"], shown["comparable"]) == (answer, "consistent", True)
    assert count(db.SeriesPoint) == 2


def test_a_topic_lists_its_readings_by_check_and_a_revoked_source_takes_their_content(
    placing,
    fabric_sources,  # noqa: F811
):
    fabric_sources["value"] = [SOURCE]
    fabric = FakeFabric([who()])
    tick(fabric)
    for check in ("consistent", "inconsistent", "unchecked"):
        reading(fabric, check)
    tick(fabric)
    admin = admin_client()
    [listed] = admin.get("/api/topics?is_demo=true").json()
    shown = admin.get(f"/api/topics/{listed['id']}").json()["readings"]
    comparable = {r["check"]: r["comparable"] for r in shown}
    assert comparable == {"consistent": True, "inconsistent": False, "unchecked": False}
    assert all(r["quote"] and r["object"] and "withheld" not in r for r in shown)
    fabric_sources["value"] = []  # the source is revoked: the content goes, the ids and the check stay
    app._readable.update(at=None, sources=None)
    hidden = admin.get(f"/api/topics/{listed['id']}").json()["readings"]
    assert all(r["withheld"] == "来源已撤权" and r["comparable"] is False for r in hidden)
    for key in ("object", "metric", "scope", "value", "unit", "period", "quote"):
        assert {r[key] for r in hidden} == {""}
    assert sorted(r["check"] for r in hidden) == ["consistent", "inconsistent", "unchecked"]
    assert {(r["source"], r["record_id"]) for r in hidden} == {(SOURCE, "who-a")}


# ---- what an analysis is handed and reads ------------------------------------------------------------


def test_an_analysis_reads_each_points_state_from_fabric_when_it_runs(placing):
    fabric = FakeFabric([])
    bulletins(fabric, CONFIRMED)
    with db.session() as s:
        topic = s.scalar(select(db.Topic))
        handed = scheduling.series_basis_points(s, topic.id)
    run = SimpleNamespace(
        s=settings(), fabric=fabric, input={"series_points": handed, "is_demo": True}, cutoff=None
    )
    assert [p["current"] for p in investigate.series_state(run)] == [True, True, True]
    first = handed[0]["measurement_id"]
    fabric.measured.correct(first)  # Fabric corrects it before the radar's next round
    state = {p["record_id"]: p for p in investigate.series_state(run)}
    assert state["who-m1"]["current"] is False
    assert state["who-m1"]["comparability"]["reasons"] == ["withdrawn_by_correction"]
    fabric.measured.revoked.add(SOURCE)  # and then the source is revoked: nothing is current
    state = investigate.series_state(run)
    assert {p["current"] for p in state} == {False}
    assert {p["extraction_check"]["reason"] for p in state} == {"source_not_allowed"}


PLANNER = os.environ.get("BIORADAR_TEST_PLANNER_URL")


@pytest.mark.skipif(not PLANNER, reason="BIORADAR_TEST_PLANNER_URL (a running query-planner) is not set")
def test_a_real_planner_takes_fabric_points_and_asks_to_verify_one_that_is_no_longer_current(placing):
    """Radar's decide request over HTTP to a running query-planner (no stand-in), with the series state read
    from Fabric: accepted; a corrected value makes a read of its record the verification read."""
    from bioradar.adapters import services

    fabric = FakeFabric([])
    bulletins(fabric, CONFIRMED)
    with db.session() as s:
        handed = scheduling.series_basis_points(s, s.scalar(select(db.Topic.id)))
    fabric.measured.correct(handed[-1]["measurement_id"])
    run = SimpleNamespace(
        s=settings(), fabric=fabric, input={"series_points": handed, "is_demo": True}, cutoff=None
    )
    series = investigate.series_state(run)
    assert [p["current"] for p in series] == [True, True, False]
    s = settings().model_copy(
        update={"planner_url": PLANNER, "planner_token": os.environ["BIORADAR_TEST_PLANNER_TOKEN"]}
    )
    body = {
        "question": "卢旺达马尔堡疫情是否在扩大？",
        "checks": [{"id": "c1", "text": "最近三个月的新增确诊病例是否上升", "required": True}],
        "candidates": [
            {"kind": "search", "source": SOURCE, "target_checks": ["c1"], "args": {"terms": ["马尔堡"]}},
            {"kind": "read", "source": SOURCE, "target_checks": [], "args": {"record_id": "who-m3"}},
        ],
        "history": [],
        "budget": {"steps_left": 5, "reads_left": 3},
        "policy": "planner",
        "series": series,
    }
    answer = services.decide(s, body)
    assert answer["reason"] == "verify_series_value" and answer["candidate"] == 1


def test_an_object_whose_period_has_only_a_start_is_no_point_of_a_series_and_is_recorded(placing):
    """An object that begins at "since September 2012" and never ends: no end is made up, nothing raises, it
    joins no series, and its inbox row and the round's counts say why. The other objects link as before."""
    fabric = FakeFabric([who()])
    tick(fabric)
    ordinary = fabric.measured.add(
        V, source=SOURCE, record="who-a", value="66", metric="报告马尔堡病毒病确诊病例"
    )
    since = fabric.measured.add(
        V,
        source=SOURCE,
        record="who-a",
        value="1599",
        metric="报告马尔堡病毒病累计病例",
        period={"type": "interval", "precision": "month", "value": None, "start": "2012-09", "end": None},
    )
    measured = tick(fabric)["measurements"]
    assert measured["open_ended_period"] == 1 and measured["linked"] == 1
    assert [link[1] for link in links()] == [ordinary]
    with db.session() as s:
        row = s.scalar(select(db.MeasurementInbox).filter_by(measurement_id=since))
        assert (row.state, row.due) == ("open_ended_period", False)
