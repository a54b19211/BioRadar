"""GDELT leads in the candidate-group view (task book 05, step 1; docs/设计.md 决策 0016): the triage judges
them for relevance alone, the view lists them apart and counts none as a member, a backfill's are left out,
the gaps of the day's windows are registered."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from conftest import FakeFabric, record
from sqlalchemy import select
from test_groups import EBOLA, day, mat, members, observation
from test_monitor import DAY, START, _topics, observed, quiet_week, triage_by_title
from test_pipeline import empty_report, fake_model, first_policy
from test_propositions import analysis_row, topic_row

from bioradar import db, ops
from bioradar.adapters import llm, services
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import analyses, groups, monitor, topics
from bioradar.research.pipeline import Run

CUTOFF = day(3) + timedelta(hours=6)  # 2026-10-04 14:00 UTC


def lead(name, **fields):
    """A GDELT record whose title would raise an alert if a model triaged it."""
    return record(name, "两家医院确认7例", "两家医院确认7例", source="gdelt_doc", **fields)


def test_a_lead_is_triaged_for_relevance_alone_and_a_backfilled_one_stays_background(monkeypatch):
    sent = []
    base = triage_by_title(sent)

    def judged(settings_, stage, system, payload, model_cls, **options):
        triaged, call = base(settings_, stage, system, payload, model_cls, **options)
        for item, r in zip(triaged.items, payload["records"]):
            item.related = "unrelated" if "封锁" in r["title"] else "related"
            item.reason = "判断理由"
        return triaged, call

    monkeypatch.setattr(llm, "chat_json", judged)
    monkeypatch.setattr(settings(), "demo_sources", ["gdelt_doc"])
    monkeypatch.setattr(settings(), "backfill_run_ids", ["run-1"])
    news, backfilled = lead("G1", available=DAY.format(3)), lead("G2", available=DAY.format(1))
    politics = record(
        "G3", "某海峡封锁引发油价上涨", "某海峡封锁", source="gdelt_doc", available=DAY.format(4)
    )
    fabric = FakeFabric([news, backfilled, politics])
    fabric.background_version_ids = lambda run_ids: ({backfilled["version_id"]}, [])

    ran = monitor.tick(is_demo=True, fabric=fabric)

    assert ran["routed"] == {"triage": 2, "background_backfill": 1}
    assert [[r["title"] for r in payload["records"]] for payload in sent] == [
        ["两家医院确认7例", "某海峡封锁引发油价上涨"]
    ]
    # the model would have opened a topic on the alert of G1; a lead joins none and raises none
    assert _topics() == []
    with db.session() as session:
        seen = {
            o.record_id: (o.topic_id, o.alert, o.reason, o.relevance)
            for o in session.scalars(select(db.Observation))
        }
    assert seen == {
        "G1": (None, "none", "判断理由", "related"),
        "G3": (None, "none", "判断理由", "unrelated"),
        "G2": (None, "none", "background_backfill", None),  # settled by the program: judged by no model
    }


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
                    "relevance": "",  # judged by no one
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


def test_what_the_triage_judged_unrelated_is_in_no_group_and_what_it_was_unsure_of_stays_marked():
    unsure = observation("L1", "G1", day(3), None, source="gdelt_doc")
    off_topic = observation("L2", "G2", day(3), None, source="gdelt_doc")
    failed = observation("v1", "D1", day(1), None)  # its triage failed: judged by no one, never unrelated
    unsure.relevance, off_topic.relevance, failed.reason = "uncertain", "unrelated", "triage_failed"
    with db.session() as session:
        session.add_all([unsure, off_topic, failed, db.Cursor(source="gdelt_doc", is_demo=True, after_seq=2)])
        session.flush()
        found = groups.for_selection(session, settings(), is_demo=True, cutoff=CUTOFF, background=set())

    rows = {m["version_id"]: m for g in found["groups"] for m in g["members"]}
    assert {v: m["relevance"] for v, m in rows.items()} == {"v1": "", "L1": "uncertain"}  # no L2
    assert [(g["rank"]) for g in found["selection"]["ranking"]] == [1, 2]
    leads = found["selection"]["coverage"]["leads"]["gdelt_doc"]
    assert (leads["count"], [r["relevance"] for r in leads["listed"]]) == (1, ["uncertain"])


def test_retriage_judges_the_observations_no_model_judged_once_and_leaves_the_rest(monkeypatch, capsys):
    sent = []
    base = triage_by_title(sent)

    def judged(settings_, stage, system, payload, model_cls, **options):
        triaged, call = base(settings_, stage, system, payload, model_cls, **options)
        for item, r in zip(triaged.items, payload["records"]):
            item.related = "unrelated" if "封锁" in r["title"] else "related"
        return triaged, call

    monkeypatch.setattr(llm, "chat_json", judged)

    def old(version, record_id, reason, source="gdelt_doc", **fields):
        row = observation(version, record_id, day(1), None, source=source)
        row.is_demo, row.reason = False, reason  # real data, as the t1 selection reads it
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    rows = [
        old("L1", "G1", "news_index_lead"),  # a lead from before leads were triaged: to judge
        old(
            "W1", "D1", "老分诊的理由", source="who_don"
        ),  # judged by a model before relevance existed: to judge
        old("L3", "G3", "已判", relevance="related"),  # judged: left alone
        old("L4", "G4", "triage_failed"),  # owed to the monitor's retry path: left alone
        old("L5", "G5", "background_backfill"),  # settled by the program: left alone
    ]
    with db.session() as session:
        session.add_all(rows)
    title = {"G1": "某海峡封锁引发油价上涨", "D1": "两家医院确认7例", "G3": "x", "G4": "x", "G5": "x"}
    fabric = FakeFabric(
        [record(r.record_id, title[r.record_id], "", source=r.source, version=r.version_id) for r in rows]
    )
    monkeypatch.setattr(ops, "Fabric", lambda s: fabric)

    def retriage(*arguments):
        ops.retriage(list(arguments))
        return json.loads(capsys.readouterr().out)

    # a used-up daily automatic budget does not keep an old lead from being judged
    monkeypatch.setattr(analyses, "over_auto_budget", lambda: True)
    first = retriage("--source", "gdelt_doc")
    assert (first["candidates"], first["judged"], first["failed"]) == (1, 1, 0)
    second = retriage()
    assert (second["candidates"], second["judged"]) == (1, 1)  # the lead is judged: only the other is left
    assert retriage()["candidates"] == 0  # nothing is judged twice
    assert [[r["title"] for r in p["records"]] for p in sent] == [
        ["某海峡封锁引发油价上涨"],
        ["两家医院确认7例"],
    ]
    with db.session() as session:
        seen = {
            o.version_id: (o.relevance, o.reason, o.topic_id, o.alert)
            for o in session.scalars(select(db.Observation))
        }
    assert seen == {
        "L1": ("unrelated", "news_index_lead", None, "normal"),  # only relevance and direction are written
        "W1": ("related", "老分诊的理由", None, "normal"),
        "L3": ("related", "已判", None, "normal"),
        "L4": (None, "triage_failed", None, "normal"),
        "L5": (None, "background_backfill", None, "normal"),
    }
    assert _topics() == []  # nothing was placed in a topic a second time


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


def test_a_lead_group_chosen_at_t1_is_investigated_through_its_seed_and_stays_an_unverified_lead(monkeypatch):
    title = "两家医院确认7例"
    lead_record = record("G1", title, title, source="gdelt_doc", kind="news_report", version="L1")
    lead_record["first_seen_at"] = lead_record["availability_proof"]["available_at"]["value"] = day(
        3
    ).isoformat()
    with db.session() as session:
        session.add(observation("L1", "G1", day(3), None, source="gdelt_doc"))
        session.flush()
        selection = groups.for_selection(session, settings(), is_demo=True, cutoff=CUTOFF, background=set())[
            "selection"
        ]
    seeds = [{"source": "gdelt_doc", "record_id": "G1", "version_id": "L1"}]
    assert (selection["outcome"], selection["chosen_topics"], selection["lead_group"]) == (
        "selected",
        [],
        True,
    )
    assert selection["submit"] == {"seed_records": seeds}  # no topic holds it: its fixed version is the seed
    assert selection["ranking"][0]["confirmed_groups"] == 0

    asked = []
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: asked.append(body) or first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(
        question=f"{title}，是否属实？",
        direction="public_health",
        is_demo=True,
        sources=["gdelt_doc"],
        cutoff=CUTOFF.isoformat(),
        replay_mode="operational_replay",
        enqueue=False,
        **selection["submit"],
    )
    Run(row, settings(), fabric=FakeFabric([lead_record])).run()

    reads = [c for c in asked[0]["candidates"] if c["kind"] == "read"]
    assert [(c["source"], c["args"], c["critical"]) for c in reads] == [
        ("gdelt_doc", {"record_id": "G1", "version_id": "L1"}, True)
    ]
    with db.session() as session:
        [read] = session.scalars(select(db.Evidence))
        [seen] = session.scalars(select(db.Observation))
        opened = session.scalars(select(db.Topic)).all()
    assert (read.version_id, read.verified) == ("L1", False)  # read, and said to be unverified
    assert (seen.topic_id, opened) == (None, [])  # the investigation opened no topic and confirmed nothing


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
