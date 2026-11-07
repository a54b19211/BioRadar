"""series_sync on a real PostgreSQL (the migrations' tables, JSONB, the unique links, savepoints, row locks):
both arrival orders end with the same links once, a page and its cursor commit together, two workers linking
the same objects at the same moment write each link once, and withheld events come back after a re-grant.
Fabric is the contract stand-in of tests/fabric_measurements.py (shaped by Fabric's fixture)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fabric_measurements import Measured, version_id
from sqlalchemy import func, select, text

from bioradar import db
from bioradar.config import settings
from bioradar.research import series_sync

SOURCE = "who_don"
V = version_id("who-a")
AT = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
TABLES = "series_points, measurement_inbox, measurement_withheld, monitor_cursors, topic_events, observations"
TABLES += ", topics"


class Fabric:
    def __init__(self):
        self.measured = Measured()

    def measurement_changes(self, **read):
        return self.measured.changes(**read)

    def measurements(self, source, version_ids, **read):
        return self.measured.measurements(source, version_ids, **read)


@pytest.fixture(autouse=True)
def empty_series_tables(verified_database):
    with db.engine().begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield


def place(version=V, record="who-a", topic="T1"):
    """Triage's outcome: the version observed and placed in the topic."""
    with db.session() as s:
        if s.get(db.Topic, topic) is None:
            s.add(
                db.Topic(
                    id=topic,
                    workspace_id="default",
                    title=topic,
                    direction="public_health",
                    is_demo=True,
                    first_seen_at=AT,
                    last_activity_at=AT,
                )
            )
            s.flush()
        s.add(
            db.Observation(
                workspace_id="default",
                source=SOURCE,
                record_id=record,
                version_id=version,
                is_demo=True,
                title=record,
                available_at=AT,
                topic_id=topic,
                alert="normal",
            )
        )


def marburg(fabric):
    return [
        fabric.measured.add(V, source=SOURCE, record="who-a", value=v, metric=f"报告马尔堡病毒病{what}")
        for what, v in (("确诊病例", "66"), ("死亡病例", "15"), ("治愈病例", "40"))
    ]


def links():
    with db.session() as s:
        return sorted(
            (p.topic_id, p.measurement_id, p.object_key, p.series_key)
            for p in s.scalars(select(db.SeriesPoint))
        )


def count(model):
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def sync(fabric):
    return series_sync.sync(settings(), fabric, is_demo=True)


def linked_versions(fabric):
    return series_sync.link_versions(settings(), fabric, [{"source": SOURCE, "version_id": V}], is_demo=True)


def test_both_arrival_orders_end_with_the_same_links_once_and_a_replay_adds_nothing():
    topic_first = Fabric()
    place()
    linked_versions(topic_first)  # triage placed it: nothing to pull yet
    marburg(topic_first)
    sync(topic_first)
    one = links()
    assert len(one) == 3
    with db.engine().begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    measurement_first = Fabric()
    marburg(measurement_first)
    sync(measurement_first)  # the objects before the topic: in the inbox, linked to nothing
    assert count(db.MeasurementInbox) == 3 and links() == []
    place()
    linked_versions(measurement_first)
    assert links() == one
    with db.session() as s:
        s.get(db.Cursor, (series_sync.FEED, True)).after_seq = 0
    sync(measurement_first)
    linked_versions(measurement_first)
    assert links() == one and count(db.MeasurementInbox) == 3


def test_a_page_and_its_cursor_commit_together(monkeypatch):
    fabric = Fabric()
    place()
    marburg(fabric)
    monkeypatch.setattr(series_sync, "PAGE", 2)
    real, calls = series_sync.note, []

    def dies_on_the_second_page(session, *args, **kwargs):
        calls.append(args)
        if len(calls) == 3:
            raise RuntimeError("killed inside page 2")
        return real(session, *args, **kwargs)

    monkeypatch.setattr(series_sync, "note", dies_on_the_second_page)
    with pytest.raises(RuntimeError):
        series_sync.read_feed(settings(), fabric, is_demo=True)
    with db.session() as s:
        assert s.get(db.Cursor, (series_sync.FEED, True)).after_seq == 2
    assert count(db.MeasurementInbox) == 2
    monkeypatch.setattr(series_sync, "note", real)
    sync(fabric)
    assert count(db.MeasurementInbox) == 3 and len(links()) == 3


def test_two_workers_linking_the_same_objects_at_the_same_moment_write_each_link_once():
    fabric = Fabric()
    place()
    marburg(fabric)
    series_sync.read_feed(settings(), fabric, is_demo=True)
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(10)
        return series_sync.link_due(settings(), fabric, is_demo=True)

    with ThreadPoolExecutor(2) as pool:
        outcomes = [f.result(60) for f in [pool.submit(worker) for _ in range(2)]]
    assert len(links()) == 3  # the unique link and the savepoint: nobody wrote one twice, nobody failed
    assert sum(o.get("linked", 0) for o in outcomes) == 3
    with db.session() as s:
        assert (
            s.scalar(select(func.count()).select_from(db.TopicEvent)) == 0
        )  # no record contradiction made up


def test_withheld_events_come_back_by_version_after_the_source_is_re_granted():
    fabric = Fabric()
    place()
    fabric.measured.revoked.add(SOURCE)
    marburg(fabric)
    assert sync(fabric)["withheld"] == 3 and links() == []
    fabric.measured.revoked.clear()
    sync(fabric)
    sync(fabric)
    assert len(links()) == 3
    with db.session() as s:
        assert s.get(db.MeasurementWithheld, ("default", True, SOURCE)).state == "released"
