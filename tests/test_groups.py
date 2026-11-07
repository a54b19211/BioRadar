"""Candidate groups (research/groups.py): who is a member, who is only a candidate, what is counted."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from conftest import FakeFabric
from test_propositions import analysis_row, topic_row

from bioradar import db
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import groups
from bioradar.research.groups import Material
from bioradar.research.pipeline import Run

T0 = datetime(2026, 10, 1, 8, tzinfo=UTC)
EBOLA = {"pathogen": "ebola", "region": "drc"}


def day(n):
    return T0 + timedelta(days=n)


def mat(name, at, *, record=None, source="who_don", **fields):
    record = record or name
    key = fields.pop("activity_key", f"record:{source}:{record}")
    return Material(
        version_id=name, source=source, record_id=record, available_at=at, activity_key=key, **fields
    )


def members(found):
    return {m["version_id"]: m for m in found["group"]["members"]}


def test_only_an_explicit_basis_confirms_a_member_and_a_key_match_is_a_candidate():
    original, newer = mat("a", day(0), **EBOLA), mat("a2", day(1), record="a", **EBOLA)
    repost = mat(
        "n", day(1), source="news", activity_key="record:who_don:a", relations=(("reposts", "who_don", "a"),)
    )
    other = mat("b", day(2), **EBOLA)  # the same pathogen and region, no explicit link to the others

    found = groups.view([original, newer, repost, other], cutoff=day(3), anchors={"a2"})

    joined = members(found)
    assert {k: (v["basis"], v["via"]) for k, v in joined.items()} == {
        "a": ("same_record", None),
        "a2": ("same_record", "a"),
        "n": ("fabric_relation", "a"),
    }
    assert joined["a2"]["confirmed_at"] == day(1).isoformat()  # as old as the later of its two ends
    [candidate] = found["group"]["candidates"]
    # the key made it a candidate, through the nearest member in time
    assert (candidate["version_id"], candidate["basis"], candidate["via"]) == ("b", "key_match", "a2")
    # the candidate does not extend the group, and the original and its repost are one deduplicated group
    assert (found["group"]["start"], found["group"]["end"]) == (day(0).isoformat(), day(1).isoformat())
    assert found["group"]["confirmed_groups"] == 1


def test_an_event_id_and_a_named_record_confirm_too():
    first, second = mat("x1", day(0), event_id="EV-1"), mat("x2", day(1), event_id="EV-1")
    quoted, alone = mat("q", day(2), mentions=(("who_don", "x1"),)), mat("o", day(0), record="o")

    found = groups.view([first, second, quoted, alone], cutoff=day(3), anchors={"x1"})

    assert {k: v["basis"] for k, v in members(found).items()} == {  # "o" has none: not a member
        "x1": "same_record",
        "x2": "event_id",
        "q": "text_reference",
    }


def test_a_material_that_is_a_candidate_of_two_groups_is_listed_with_both():
    x, y = mat("x", day(0), **EBOLA), mat("y", day(1), **EBOLA)
    m = mat("m", day(2), **EBOLA)

    found = groups.view([x, y, m], cutoff=day(3), anchors={"x"})

    assert {c["version_id"] for c in found["group"]["candidates"]} == {"y", "m"}
    in_both = {(r["group"], r["role"]) for r in found["shared"]["m"]}
    assert len(in_both) == 3  # its own group, and a candidate of the display group and of y's group
    listed = {r["group"] for r in found["shared"]["m"]}
    assert {g["id"] for g in found["others"]} == listed - {found["display"]}
    assert len(found["others"]) == 2


def test_a_replay_sees_no_link_that_a_later_material_made():
    a, later = mat("a", day(0), **EBOLA), mat("a2", day(5), record="a", **EBOLA)

    early = groups.view([a, later], cutoff=day(2), anchors={"a", "a2"})
    late = groups.view([a, later], cutoff=day(6), anchors={"a", "a2"})

    assert list(members(early)) == ["a"]
    assert members(late)["a2"]["confirmed_at"] == day(5).isoformat()
    assert groups.view([later], cutoff=day(2), anchors={"a2"}) is None  # nothing of the topic is visible yet


def test_background_versions_are_members_but_not_counted_or_first_seen():
    old = mat("old", day(0), record="r", background=True, **EBOLA)
    now = mat("new", day(20), record="r", **EBOLA)

    found = groups.view([old, now], cutoff=day(21), anchors={"new"})

    assert members(found)["old"]["background"] is True
    assert found["group"]["confirmed_groups"] == 1
    assert found["group"]["first_seen_at"] == found["group"]["start"] == day(20).isoformat()
    only_old = groups.view([old], cutoff=day(21), anchors={"old"})
    assert only_old is None  # a background material is never what a display group is chosen by


def test_activity_counts_each_origin_once_and_the_baseline_leaves_out_background_days():
    # one record seen every day but day 3; on day 3 only a backfilled record arrives, which is no received day
    seen = [mat(f"r{n}", day(n), record="r", **EBOLA) for n in range(8) if n != 3]
    seen.append(mat("bg", day(3), record="bg", background=True))
    # day 8: the original and four others, each a different origin, plus a repost of the original
    seen += [mat(f"x{n}", day(8), relations=(("supplements", "who_don", "r"),)) for n in range(4)]
    relation = (("reposts", "who_don", "r"),)
    repost = mat("p", day(8), source="news", activity_key="record:who_don:r", relations=relation)
    seen += [repost, mat("o", day(8), record="r", independence="independent")]

    found = groups.view(seen, cutoff=day(8) + timedelta(hours=8), anchors={"o"})
    activity = found["group"]["activity"]

    assert (activity["groups"], activity["independent"], activity["unknown"]) == (5, 1, 4)
    assert "2026-10-04" not in activity["baseline"]["days"] and len(activity["baseline"]["days"]) == 7
    assert (activity["baseline"]["threshold"], activity["baseline"]["above"]) == (4.0, True)


def test_a_place_without_a_pathogen_is_a_candidate_of_few_groups_and_only_with_a_located_sentence():
    ones = [mat(f"g{n}", day(n), pathogen=f"p{n}", region="drc") for n in range(7)]
    clue = mat("clue", day(8), region="drc", quote="two cases in the province")
    silent = mat("silent", day(8), region="drc")
    unknown = mat("lead", day(8))
    everything = [*ones, clue, silent, unknown]
    group_of = {m.version_id: g for g in groups.build_groups(everything, []) for m in g.members}

    found = groups.candidate_links(everything, group_of)

    nearest = [c for c in found if c.version_id == "clue"]
    assert [c.via for c in nearest] == ["g6", "g5", "g4", "g3", "g2"]  # five, the nearest in time first
    assert {c.basis for c in nearest} == {"region_clue"}
    assert not [c for c in found if c.version_id in ("silent", "lead")]  # no sentence, or no place
    assert group_of["lead"].kind == "unknown_cause" and group_of["g0"].kind == "event"


def test_a_key_match_only_reaches_materials_seen_within_thirty_days():
    far = [mat("a", day(0), **EBOLA), mat("b", day(40), **EBOLA)]
    group_of = {m.version_id: g for g in groups.build_groups(far, []) for m in g.members}

    assert groups.candidate_links(far, group_of) == []


# ---- from the rows ----------------------------------------------------------------------------------


def observation(version, record, at, topic_id, *, source="who_don", key=None):
    s = settings()
    return db.Observation(
        workspace_id=s.workspace_id,
        source=source,
        record_id=record,
        version_id=version,
        is_demo=True,
        title="t",
        available_at=at,
        topic_id=topic_id,
        alert="normal",
        activity_key=key,
    )


def point(topic_id, version, record):
    s = settings()
    dims = [
        {"kind": "disease", "norm": "ebola", "state": "checked"},
        {"kind": "coverage", "norm": "drc", "state": "carried"},
        {"kind": "population", "norm": "all", "state": "unknown"},
    ]
    return db.SeriesPoint(
        workspace_id=s.workspace_id,
        is_demo=True,
        topic_id=topic_id,
        measurement_id="m-" + version,
        source="who_don",
        record_id=record,
        version_id=version,
        object="cases",
        metric="cases",
        unit="",
        measure_kind="count",
        scope_mode="object_metric_total",
        period="2026-10",
        period_type="month",
        period_precision="month",
        period_start=T0,
        period_end=T0,
        value_text="1",
        quote="1 case reported",
        object_key="o",
        joined_by_rule="r",
        dims=dims,
        dims_key="k",
        gate_version="v",
        series_key="s",
        available_at=T0,
    )


def test_the_view_of_a_topic_is_built_from_observations_points_evidence_and_the_backfill():
    s = settings()
    with db.session() as session:
        topic = db.Topic(
            workspace_id=s.workspace_id,
            title="t",
            direction="public_health",
            is_demo=True,
            first_seen_at=day(0),
            last_activity_at=day(0),
        )
        session.add(topic)
        session.flush()
        investigation = db.Investigation(
            workspace_id=s.workspace_id, question="q", direction="public_health", is_demo=True
        )
        session.add(investigation)
        session.flush()
        analysis = db.Analysis(  # its reading is recorded before the cutoff of the view
            investigation_id=investigation.id,
            trigger="manual",
            method="planner",
            input={},
            input_hash="h",
            created_at=day(3),
        )
        session.add(analysis)
        session.flush()
        relation = {"kind": "supplements", "target_source": "who_don", "target_record_id": "D0"}
        session.add_all(
            [
                observation("v1", "D1", day(1), topic.id),
                observation("v2", "N1", day(2), topic.id, source="news", key="record:who_don:D1"),
                observation("v3", "D2", day(3), None),
                observation("v0", "D0", day(-5), None),  # read by the backfill
                point(topic.id, "v1", "D1"),
                point(topic.id, "v3", "D2"),
                db.Evidence(
                    analysis_id=analysis.id,
                    ref="E1",
                    source="who_don",
                    record_id="D2",
                    version_id="v3",
                    title="t",
                    text="x",
                    meta={"relations": [relation]},
                ),
            ]
        )
        session.flush()

        found = groups.for_topic(session, s, topic, cutoff=day(4), background={"v0"})
        # the relation was recorded by an analysis made at day 3: an earlier moment does not see it
        assert dict(groups._relations(session, ["v3"], day(2))) == {}
        assert dict(groups._relations(session, ["v3"], day(4))) == {"v3": {("supplements", "who_don", "D0")}}

    joined = members(found)
    assert {k: v["basis"] for k, v in joined.items()} == {"v1": "same_record", "v2": "fabric_relation"}
    [candidate] = found["group"]["candidates"]  # D2 has the same pathogen and region, and is no member
    assert (candidate["version_id"], candidate["basis"]) == ("v3", "key_match")
    [other] = found["others"]  # D2's own group: D2 and the background record the evidence relates it to
    # the background record is a member, not counted; D1's candidate D2 moves D2's first seen, not its start
    assert (other["confirmed_groups"], other["start"], other["first_seen_at"]) == (
        1,
        day(3).isoformat(),
        day(1).isoformat(),
    )
    assert found["truncated"] is False


def test_the_backfill_receipts_name_the_background_versions_and_an_unread_one_is_listed():
    def handler(request):
        if request.url.path.endswith("/runs/r1"):
            backfill = {"background_version_ids": ["v1", "v2"]}
            return httpx.Response(200, json={"coverage": {"backfill": backfill}})
        if request.url.path.endswith("/runs/r2"):
            return httpx.Response(200, json={"coverage": {}})  # not a backfill run
        return httpx.Response(404, json={"error": {"code": "run_not_found", "message": "x"}})

    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))

    found, unread = Fabric(settings(), client=client).background_version_ids(["r1", "r2", "r3"])

    assert (found, unread) == ({"v1", "v2"}, ["r3"])


def test_a_run_keeps_its_topics_display_group_with_its_mode_and_the_backfill_it_could_not_read(monkeypatch):
    with db.session() as session:
        topic = topic_row(session)
        row = analysis_row(session, topic)
        row.input = {**row.input, "cutoff": day(3).isoformat(), "replay_mode": "operational_replay"}
        for version, at in (("v0", day(0)), ("v1", day(1))):
            session.add(observation(version, "D1", at, topic.id))
        session.flush()
        session.expunge(row)
    fabric = FakeFabric([])
    fabric.background_version_ids = lambda run_ids: ({"v0"}, ["r9"])
    monkeypatch.setattr(settings(), "backfill_run_ids", ["r1", "r9"])

    found = Run(row, settings(), fabric=fabric).display_group()

    json.dumps(found)  # it is kept in analyses.result
    assert (found["replay_mode"], found["run_kind"], found["background_unread"]) == (
        "operational_replay",
        "online",
        ["r9"],
    )
    assert {m["version_id"]: m["background"] for m in found["group"]["members"]} == {"v0": True, "v1": False}
    assert found["cutoff"] == day(3).isoformat() and found["group"]["confirmed_groups"] == 1


def test_an_analysis_without_a_topic_or_without_a_material_of_it_has_no_display_group():
    with db.session() as session:
        topic = topic_row(session)
        bare, without = analysis_row(session, None), analysis_row(session, topic)
        session.flush()
        session.expunge_all()
    fabric = FakeFabric([])

    assert Run(bare, settings(), fabric=fabric).display_group() is None
    assert Run(without, settings(), fabric=fabric).display_group() is None
