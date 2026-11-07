"""GDELT leads in the candidate-group view (task book 05, step 1; docs/decisions/0016): the program observes
them, the view lists them apart and counts none as a member, a backfill's are left out, the gaps of the day's
windows are registered."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from conftest import FakeFabric, record
from sqlalchemy import select
from test_groups import EBOLA, day, mat, members, observation
from test_monitor import DAY, START, _topics, observed, quiet_week, triage_by_title
from test_propositions import analysis_row, topic_row

from bioradar import db, ops
from bioradar.adapters import llm
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import groups, monitor, topics
from bioradar.research.pipeline import Run

CUTOFF = day(3) + timedelta(hours=6)  # 2026-10-04 14:00 UTC


def lead(name, **fields):
    """A GDELT record whose title would raise an alert if a model triaged it."""
    return record(name, "两家医院确认7例", "两家医院确认7例", source="gdelt_doc", **fields)


def test_a_lead_is_observed_by_the_program_whatever_its_title_says_and_a_backfilled_one_stays_background(
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(llm, "chat_json", triage_by_title(sent))
    monkeypatch.setattr(settings(), "demo_sources", ["gdelt_doc"])
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    news, backfilled = lead("G1", available=DAY.format(3)), lead("G2", available=DAY.format(1))
    fabric = FakeFabric([news, backfilled])
    fabric.background_version_ids = lambda run_ids: ({backfilled["version_id"]}, [])

    ran = monitor.tick(is_demo=True, fabric=fabric)

    assert ran["routed"] == {"news_index_lead": 1, "background_backfill": 1}
    assert sent == [] and _topics() == []  # no model call; a triage would have opened a topic on the alert
    with db.session() as session:
        seen = {o.record_id: (o.topic_id, o.alert, o.reason) for o in session.scalars(select(db.Observation))}
    assert seen == {"G1": (None, "none", "news_index_lead"), "G2": (None, "none", "background_backfill")}


def test_a_lead_is_listed_apart_and_no_count_start_or_first_seen_of_the_display_group_takes_it_in():
    s = settings()
    with db.session() as session:
        topic = topic_row(session)
        session.add_all(
            [
                observation("v1", "D1", day(1), topic.id),
                observation("L1", "G1", day(3), None, source="gdelt_doc"),
                observation("L2", "G2", day(3), None, source="gdelt_doc"),  # created by a backfill run
                observation("L0", "G0", day(2), None, source="gdelt_doc"),  # the day before
            ]
        )
        session.flush()
        before = groups.for_topic(session, s, topic, cutoff=CUTOFF, background={"L2"})  # source not read yet
        session.add(db.Cursor(source="gdelt_doc", is_demo=True, after_seq=7))
        session.flush()
        found = groups.for_topic(session, s, topic, cutoff=CUTOFF, background={"L2"})
        ranked = groups.for_selection(session, s, is_demo=True, cutoff=CUTOFF, background={"L2"})["selection"]

    # the news group first; each lead alone in a group of its own, 0 confirmed, after it; the background
    # one has no qualifying material and no rank
    standing = [(g["confirmed_groups"], g["rank"]) for g in ranked["ranking"]]
    assert standing == [(1, 1), (0, 2), (0, 3), (0, None)]
    assert ranked["chosen"] == found["display"]
    assert before["leads"] == {}
    assert found["leads"] == {
        "gdelt_doc": {
            "day": "2026-10-04",
            "count": 1,
            "background": 1,
            "listed": [
                {
                    "version_id": "L1",
                    "source": "gdelt_doc",
                    "record_id": "G1",
                    "available_at": day(3).isoformat(),  # when the radar could first read it
                }
            ],
        }
    }
    shown = found["group"]
    assert list(members(found)) == ["v1"] and shown["candidates"] == [] and found["others"] == []
    assert (shown["confirmed_groups"], shown["start"], shown["end"], shown["first_seen_at"]) == (
        1,
        day(1).isoformat(),
        day(1).isoformat(),
        day(1).isoformat(),
    )


def test_a_group_of_leads_alone_confirms_nothing_yet_takes_part_in_the_ranking():
    news = mat("a", day(1), **EBOLA)
    alone = mat("g1", day(2), source="gdelt_doc")
    twice = [mat("g2", day(2), source="gdelt_doc"), mat("g2b", day(3), record="g2", source="gdelt_doc")]
    stated = (("reposts", "who_don", "a"),)  # an explicit basis: Fabric states this lead reposts the record
    joined = mat("g3", day(3), source="gdelt_doc", activity_key="record:who_don:a", relations=stated)

    out = groups.select_display([news, alone, *twice, joined], cutoff=day(4))

    owner = {m["version_id"]: g["id"] for g in out["groups"] for m in g["members"]}
    standing = {g["id"]: g for g in out["selection"]["ranking"]}
    solo, same_record, news_group = (standing[owner[v]] for v in ("g1", "g2", "a"))
    assert owner["g3"] == owner["a"]  # joined by its explicit basis, so it counts with the record it names
    assert (news_group["rank"], news_group["confirmed_groups"]) == (1, 1)
    assert news_group["end"] == day(3).isoformat()
    # no source states them: 0 confirmed, no start or end - and they are still ranked, after the news group
    for row in (solo, same_record):
        assert (row["confirmed_groups"], row["start"], row["end"]) == (0, None, None)
        assert (row["kind"], row["first_seen_at"]) == ("unknown_cause", day(2).isoformat())
    assert sorted([solo["rank"], same_record["rank"]]) == [2, 3]
    assert out["selection"]["chosen"] == owner["a"]

    only = groups.select_display([alone], cutoff=day(4))["selection"]  # nothing else is visible
    [ranked] = only["ranking"]
    assert (only["outcome"], ranked["confirmed_groups"], ranked["rank"]) == ("selected", 0, 1)


def test_the_gaps_of_the_days_windows_are_registered_and_an_unread_day_is_not_an_empty_one():
    def at(hour, minute=0):
        return datetime(2026, 10, 4, hour, minute, tzinfo=UTC).isoformat()

    def receipt(start, end, status="read", **fields):
        return {"start": start, "end": end, "status": status, **fields}

    cut = receipt(at(1, 0), at(1, 15), full=True, possibly_truncated=True)
    receipts = [
        receipt(at(0), at(1), returned=12),
        receipt(at(1), at(2), "split", full=True),  # its halves follow: not a gap
        cut,
        receipt(at(2), at(3), "failed", reason="timeout"),
        receipt(at(3), at(4), "not_read"),
        cut,  # another query's read of the same window
    ]
    asked = []

    def handler(request):
        asked.append(dict(request.url.params))
        if request.url.params["window_start"] == at(0):  # a day Fabric answers for
            return httpx.Response(200, json={"items": [], "coverage": {"window_coverage": receipts}})
        if request.url.params["window_start"] == at(5):  # Fabric has no receipt of this day
            return httpx.Response(200, json={"items": [], "coverage": {}})
        return httpx.Response(503, json={})

    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    fabric = Fabric(settings(), client=client)
    read = {"is_demo": True, "mode": "operational_replay", "cutoff": CUTOFF}

    def window(first, last):
        return datetime.fromisoformat(at(first)), datetime.fromisoformat(at(last))

    day_read = fabric.lead_coverage("gdelt_doc", *window(0, 4), **read)
    no_receipt = fabric.lead_coverage("gdelt_doc", *window(5, 6), **read)
    down = fabric.lead_coverage("gdelt_doc", *window(7, 8), **read)

    assert asked[0] == {
        "source_id": "gdelt_doc",
        "is_demo": "true",
        "mode": "operational_replay",
        "cutoff": CUTOFF.isoformat(),
        "window_start": at(0),  # the windows are asked for by the source's observation time, with a time zone
        "window_end": at(4),
        "limit": "1",
    }
    gaps = [(g["start"], g["end"], g["kind"]) for g in day_read["gaps"]]
    assert day_read["receipts"] == 6 and gaps == [
        (at(1, 0), at(1, 15), "possibly_truncated"),
        (at(2), at(3), "failed"),
        (at(3), at(4), "not_read"),
    ]
    assert (no_receipt["receipts"], no_receipt["gaps"], "error" in no_receipt) == (0, [], False)
    assert (down["receipts"], down["gaps"], down["error"]) == (0, [], "fabric_http_503")


def test_a_run_registers_the_days_coverage_beside_the_days_leads():
    with db.session() as session:
        topic = topic_row(session)
        row = analysis_row(session, topic)
        row.input = {**row.input, "cutoff": CUTOFF.isoformat(), "replay_mode": "operational_replay"}
        session.add_all(
            [
                observation("v1", "D1", day(1), topic.id),
                observation("L1", "G1", day(3), None, source="gdelt_doc"),
                db.Cursor(source="gdelt_doc", is_demo=True, after_seq=1),
            ]
        )
        session.flush()
        session.expunge(row)
    asked = []
    gap = {"start": "2026-10-04T01:00:00+00:00", "end": "2026-10-04T01:15:00+00:00", "kind": "failed"}

    def lead_coverage(source, start, end, **read):
        asked.append((source, start, end, read))
        return {"window": {}, "receipts": 3, "gaps": [gap]}

    fabric = FakeFabric([])
    fabric.lead_coverage = lead_coverage

    found = Run(row, settings(), fabric=fabric).display_group()

    json.dumps(found)  # it is kept in analyses.result
    assert asked == [
        (
            "gdelt_doc",
            datetime(2026, 10, 4, tzinfo=UTC),
            datetime(2026, 10, 5, tzinfo=UTC),
            {"is_demo": True, "mode": "operational_replay", "cutoff": CUTOFF},
        )
    ]
    assert found["leads"]["gdelt_doc"]["count"] == 1
    assert found["leads"]["gdelt_doc"]["coverage"]["gaps"] == [gap]


def test_the_t1_selection_registers_the_days_leads_and_their_window_gaps_and_keeps_its_outcome(
    monkeypatch, capsys
):
    with db.session() as session:
        news = observation("v1", "D1", day(1), None)
        lead_row = observation("L1", "G1", day(3), None, source="gdelt_doc")
        news.is_demo = lead_row.is_demo = False  # the selection reads the real data
        session.add_all([news, lead_row, db.Cursor(source="gdelt_doc", is_demo=False, after_seq=1)])
    asked = []
    gap = {
        "start": "2026-10-04T01:00:00+00:00",
        "end": "2026-10-04T01:15:00+00:00",
        "kind": "possibly_truncated",
    }

    class SomeFabric:
        def __init__(self, s):
            pass

        def background_version_ids(self, run_ids):
            return set(), []

        def lead_coverage(self, source, start, end, **read):
            asked.append((source, start, end, read))
            return {"window": {}, "receipts": 3, "gaps": [gap]}

    monkeypatch.setattr(ops, "Fabric", SomeFabric)

    ops.select_display(["--cutoff", CUTOFF.isoformat()])

    found = json.loads(capsys.readouterr().out)
    assert asked == [
        (
            "gdelt_doc",
            datetime(2026, 10, 4, tzinfo=UTC),
            datetime(2026, 10, 5, tzinfo=UTC),
            {"is_demo": False, "mode": "operational_replay", "cutoff": CUTOFF},
        )
    ]
    registered = found["selection"]["coverage"]["leads"]["gdelt_doc"]
    assert (registered["count"], registered["coverage"]["gaps"]) == (1, [gap])
    assert found["selection"]["outcome"] == "selected"  # a gap is registered, not a verdict


def test_a_day_with_only_leads_is_not_a_day_the_monitor_received_news():
    s, today = settings(), START + timedelta(days=7)
    with db.session() as session:
        topic, bare = quiet_week(session, bare_day=3)  # on day 3 only three records
        for i in range(6):
            observed(session, topic, f"v7-{i}", today)
        session.flush()
        bare_day = list(session.scalars(select(db.Observation).where(db.Observation.version_id.in_(bare))))

        for o in bare_day:  # they are leads
            o.source, o.topic_id = "gdelt_doc", None
        session.flush()
        # 6 received days before today, not 7: today's 6 are not judged
        assert topics.activity(session, s, topic, today, True) is False

        for o in bare_day:  # the same records from a source that is news: day 3 was a received day
            o.source = "who_don"
        session.flush()
        assert topics.activity(session, s, topic, today, True) is True
