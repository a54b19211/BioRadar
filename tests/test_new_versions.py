"""New versions of the records propositions rest on (research/revision.py, ADR 0015; task book 05 2.6 and 08
2.6). Fabric is a stand-in that answers the difference facts a test sets; no model is called."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeFabric, record
from sqlalchemy import func, select
from test_access import admin_client, fabric_sources  # noqa: F401 - the fixture these tests take
from test_monitor import DAY, triage_by_title
from test_propositions import analysis_row, judgment, result_of, topic_row

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import analyses, monitor, propositions, revision, scheduling, schemas, views
from bioradar.research.run import plan

V1, V2, V3 = "1" * 64, "2" * 64, "3" * 64
WHEN = "2026-09-29T12:00:00+08:00"


def compared(kind, **moved):
    """Fabric's difference facts for a version set against the one before it."""
    return {
        "state": "compared",
        "kind": kind,
        "from_version_id": V1,
        **dict.fromkeys(revision.FACTS, False),
        **moved,
    }


def read_of(session, row, ref, record_id, version, source="who_don"):
    """What the analysis read of a record: a material_reads row and the evidence row that points to it."""
    read = db.MaterialRead(
        analysis_id=row.id,
        stage="read",
        service="radar",
        ref=ref,
        source=source,
        record_id=record_id,
        version_id=version,
        sha256="s",
        chars=1,
        binding={},
    )
    session.add(read)
    session.flush()
    session.add(
        db.Evidence(
            analysis_id=row.id,
            ref=ref,
            source=source,
            record_id=record_id,
            version_id=version,
            title="t",
            text="x",
            meta={"material_read_id": read.id},
        )
    )
    session.flush()


def world():
    """A topic whose latest analysis judged two questions, each resting on its own who_don record read at V1:
    (topic id, analysis id, [prop ids])."""
    with db.session() as s:
        topic = topic_row(s)
        row = analysis_row(s, topic)
        judgments = []
        for n, (question, record_id) in enumerate((("是否人传人", "DON-1"), ("病原是什么", "DON-2")), 1):
            read_of(s, row, f"E{n}", record_id, V1)
            judgments.append(judgment(question, grounds=[f"E{n}"]))
        result = result_of(*judgments)
        propositions.number(s, row, result)
        row.state, row.result = "completed", result
        topic.last_analysis_id = row.id
        return topic.id, row.id, [j["prop_id"] for j in judgments]


def versions(topic_id):
    """The `version` events of the topic, as stored."""
    with db.session() as s:
        query = (
            select(db.TopicEvent)
            .where(db.TopicEvent.topic_id == topic_id, db.TopicEvent.kind == "version")
            .order_by(db.TopicEvent.id)
        )
        return [e.detail for e in s.scalars(query)]


def owed(topic_id):
    with db.session() as s:
        return s.get(db.Topic, topic_id).pending_reason


def stored(analysis_id):
    with db.session() as s:
        return copy.deepcopy(s.get(db.Analysis, analysis_id).result)


def arrival(record_id, version, difference, *, source="who_don", relations=()):
    """A version of a record arrives with the difference Fabric states for it: (the Fabric stand-in, how many
    events the round wrote)."""
    fabric = FakeFabric(
        [record(record_id, "新版本", "新的正文", source=source, version=version, relations=relations)]
    )
    fabric.differences[version] = difference
    return fabric, revision.arrive(fabric, fabric.docs, is_demo=True)


def test_each_kind_of_difference_goes_its_own_way():
    judge = revision.judge
    assert judge(compared("raw_changed", body_changed=True), False) == ("pending_review", True, "real_update")
    assert judge(compared("serialization_only"), False) == ("pending_review", True, "engineering_reuse")
    # a processing upgrade is no update of the source; it leaves a proposition standing only when nothing it
    # rests on moved
    parser = compared("projection_only", parser_changed=True)
    assert judge(parser, False) == ("processing_upgrade", False, None)
    assert judge({**parser, "structure_changed": True}, False) == ("processing_upgrade", True, None)
    # a correction or retraction first, whatever the text
    assert judge(compared("serialization_only"), True) == ("source_corrected", True, "engineering_reuse")
    # no answer, or nothing before it to compare with: never a reason to skip
    assert judge(None, False) == ("pending_review", True, None)
    assert judge({"state": "first_adoption"}, False) == ("pending_review", True, None)


def test_a_new_version_is_left_for_review_and_only_the_propositions_resting_on_it_are_named():
    topic_id, analysis_id, (first, _) = world()
    before = stored(analysis_id)

    fabric, written = arrival("DON-1", V2, compared("raw_changed", body_changed=True))

    assert written == 1
    [event] = versions(topic_id)
    assert (event["cause"], event["difference"], event["reason"], event["sample"]) == (
        "version",
        "raw_changed",
        "pending_review",
        "real_update",
    )
    assert event["affected"] == [first] and event["judge_again"]  # DON-2's proposition does not rest on it
    assert "new_version" in owed(topic_id)
    assert stored(analysis_id) == before  # the earlier judgment version is not written to
    # Fabric was asked at the mode and cutoff the version was read at; a second round adds nothing
    [(_, _, _, mode, cutoff)] = [c for c in fabric.calls if c[0] == "version_differences"]
    assert {"mode": mode, "cutoff": cutoff} == fabric.docs[0]["read_at"]
    assert revision.arrive(fabric, fabric.docs, is_demo=True) == 0 and len(versions(topic_id)) == 1


def test_a_who_don_version_is_never_skipped_not_even_when_only_its_serialization_differs():
    topic_id, _, (first, _) = world()

    arrival("DON-1", V2, compared("serialization_only"))
    arrival("DON-1", V3, None)  # Fabric gave no answer

    serialized, unanswered = versions(topic_id)
    facts = (serialized["reason"], serialized["sample"], serialized["affected"], serialized["judge_again"])
    assert facts == ("pending_review", "engineering_reuse", [first], True)
    assert (unanswered["difference"], unanswered["reason"], unanswered["sample"]) == (
        "unavailable",
        "pending_review",
        None,
    )


def test_a_processing_upgrade_is_no_source_update_and_judges_again_only_what_it_moved():
    topic_id, _, (first, _) = world()

    arrival("DON-1", V2, compared("projection_only", parser_changed=True))
    assert owed(topic_id) is None  # nothing the proposition rests on moved: it stands

    arrival("DON-1", V3, compared("projection_only", parser_changed=True, structure_changed=True))

    parser_only, moved = versions(topic_id)
    assert (parser_only["reason"], parser_only["sample"], parser_only["judge_again"]) == (
        "processing_upgrade",
        None,
        False,
    )
    assert (moved["sample"], moved["affected"], moved["judge_again"]) == (None, [first], True)
    assert "new_version" in owed(topic_id)


def test_a_correction_comes_first_and_names_what_rested_on_the_corrected_record():
    topic_id, _, (first, _) = world()

    arrival("DON-1-CORR", V3, {"state": "first_adoption"}, relations=[("corrects", "DON-1")])

    [event] = versions(topic_id)
    assert (event["cause"], event["reason"], event["affected"], event["judge_again"]) == (
        "correction",
        "source_corrected",
        [first],
        True,
    )


def test_the_owed_analysis_judges_only_the_affected_propositions_and_adds_a_revision_section(monkeypatch):
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    topic_id, first_id, (first, second) = world()
    arrival("DON-1", V2, compared("raw_changed", body_changed=True))
    before = stored(first_id)

    [made] = scheduling.start_analyses(settings(), is_demo=True, cutoff=None)

    row = analyses.get(made)
    assert row.parent_id == first_id
    assert [(a["prop_id"], a["reason"]) for a in row.input["reassess"]] == [(first, "pending_review")]
    assert row.input["reassess"][0]["triggers"][0]["sample"] == "real_update"
    pinned = {"source": "who_don", "record_id": "DON-1", "version_id": V2, "title": "新版本"}
    assert pinned in row.input["seed_records"]
    assert first in row.input["question"] and second not in row.input["question"]

    class StubRun:
        def __init__(self, analysis, settings_, lease_token=None):
            pass

        def run(self):
            changed = judgment("是否人传人", "有限的人传人", confidence="probable")
            return {"report_status": "PASS", **result_of(changed)}

    monkeypatch.setattr(analyses, "Run", StubRun)
    analyses.execute(made)

    result = stored(made)
    assert [j["prop_id"] for j in result["assessment"]["judgments"]] == [first]  # nothing else was judged
    [section] = [s for s in result["sections"] if s["kind"] == "revision"]
    lines = {i["prop_id"]: i for i in section["items"]}
    assert (lines[first]["status"], lines[first]["reason"]) == ("revised", "pending_review")
    assert (lines[first]["before"]["content_rev"], lines[first]["after"]["content_rev"]) == (1, 2)
    assert lines[first]["trigger"][0]["version_id"] == V2
    assert (lines[second]["status"], lines[second]["reason"]) == ("maintained", "unaffected")
    assert stored(first_id) == before  # the earlier judgment version stands as it was
    assert views.result(result)["sections"] == result["sections"]  # every field of the section is shown
    assert owed(topic_id) is None


def test_an_asked_proposition_the_analysis_did_not_judge_stays_open():
    old = {"prop_id": "T1-P1", "question": "是否人传人", "confidence": "possible", "content_rev": 3}
    item = revision.line(old, {"reason": "pending_review", "triggers": []}, [])
    assert (item["status"], item["before"]["content_rev"], item["after"]["status"]) == ("open", 3, "open")


def numbered_parent(source="open_src"):
    """A completed analysis of a topic that stands on one numbered proposition: (analysis id, prop id)."""
    with db.session() as s:
        topic_id = topic_row(s).id
    row = analyses.create(
        question="XQ-17 的进展？",
        direction="public_health",
        is_demo=True,
        sources=["open_src"],
        cutoff=WHEN,
        replay_mode="operational_replay",
        topic_id=topic_id,
        enqueue=False,
    )
    with db.session() as s:
        parent = s.get(db.Analysis, row.id)
        s.add(
            db.Evidence(
                analysis_id=parent.id,
                ref="E1",
                source=source,
                record_id="r",
                version_id=V1,
                title="t",
                text="x",
                meta={},
            )
        )
        result = {"report_status": "PASS", "draft": "稿", **result_of(judgment("是否人传人", grounds=["E1"]))}
        propositions.number(s, parent, result)
        parent.state, parent.result = "completed", result
        return parent.id, result["assessment"]["judgments"][0]["prop_id"]


@pytest.fixture
def client(fabric_sources, monkeypatch):  # noqa: F811
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    return admin_client()


def test_a_follow_up_about_a_proposition_judges_it_alone_inside_the_parents_reach(client, monkeypatch):
    parent_id, prop = numbered_parent()

    answer = client.post(
        f"/api/analyses/{parent_id}/followup", json={"question": "后续进展如何？", "about": prop}
    )

    assert answer.status_code == 200
    parent, child = analyses.get(parent_id), analyses.get(answer.json()["id"])
    assert child.input["reassess"] == [
        {"prop_id": prop, "question": "是否人传人", "reason": "followup", "triggers": []}
    ]
    for key in ("sources", "cutoff", "replay_mode", "scope_record_ids", "is_demo", "direction"):
        assert child.input[key] == parent.input[key]  # naming a proposition widens nothing it may read

    class StubRun:
        def __init__(self, analysis, settings_, lease_token=None):
            pass

        def run(self):
            return {"report_status": "PASS", **result_of(judgment("是否人传人", "有限的人传人"))}

    monkeypatch.setattr(analyses, "Run", StubRun)
    analyses.execute(child.id)
    [section] = [s for s in stored(child.id)["sections"] if s["kind"] == "revision"]
    [line] = section["items"]
    assert (line["prop_id"], line["status"], line["reason"]) == (prop, "revised", "followup")


def test_a_follow_up_about_a_proposition_it_does_not_stand_on_or_cannot_read_is_refused(client):
    parent_id, _ = numbered_parent()
    _, elsewhere = numbered_parent()  # a proposition of another analysis, in another topic
    for about in (elsewhere, "T0-P9"):
        refused = client.post(
            f"/api/analyses/{parent_id}/followup", json={"question": "后续进展如何？", "about": about}
        )
        assert refused.status_code == 422 and "about_not_in_analysis" in refused.json()["message"]
    hidden_id, hidden_prop = numbered_parent(source="gone_src")  # its source is not listed: unreadable now
    refused = client.post(
        f"/api/analyses/{hidden_id}/followup", json={"question": "后续进展如何？", "about": hidden_prop}
    )
    assert refused.status_code == 422 and "about_analysis_not_readable" in refused.json()["message"]
    with db.session() as s:
        assert s.scalar(select(func.count()).where(db.Analysis.trigger == "followup")) == 0


def test_an_analysis_asked_about_propositions_answers_those_whatever_the_plan_proposed():
    model = schemas.Plan(
        checks=[schemas.PlanCheck(text="c", required=True)],
        queries=[schemas.PlanQuery(terms=["x"], checks=[1])],
        judgments=["计划自己的问题"],
    )
    assert plan.judgment_questions(SimpleNamespace(input={}), model) == ["计划自己的问题"]
    asked = SimpleNamespace(input={"reassess": [{"question": "是否人传人"}]})
    assert plan.judgment_questions(asked, model) == ["是否人传人"]


def fabric_answering(status=200):
    """A Fabric whose /records gives one WHO record and whose version-differences answers with `status`."""
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.path == "/api/fabric/records":
            item = record("DON-1", "t", "正文", source="who_don", version=V2)
            return httpx.Response(200, json={"items": [item], "next_cursor": None})
        if request.url.path == "/api/fabric/version-differences" and status == 200:
            return httpx.Response(200, json={"versions": {V2: compared("raw_changed", body_changed=True)}})
        return httpx.Response(status if status != 200 else 404, json={"detail": "Not Found"})

    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    return Fabric(settings(), client=client), sent


def test_the_differences_are_asked_at_the_mode_and_cutoff_the_record_was_read_at():
    fabric, sent = fabric_answering()
    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    [doc], _ = fabric.search("who_don", (), is_demo=False, cutoff=cutoff, mode="operational_replay")

    found = revision.differences(fabric, [doc], is_demo=False)

    assert found == {V2: compared("raw_changed", body_changed=True)}
    [read] = [r for r in sent if r.url.path == "/api/fabric/records"]
    [asked] = [r for r in sent if r.url.path == "/api/fabric/version-differences"]
    pair = (asked.url.params["mode"], asked.url.params["cutoff"])
    assert pair == ("operational_replay", cutoff.isoformat())
    assert pair == (read.url.params["mode"], read.url.params["cutoff"])


def test_an_older_fabric_without_the_endpoint_leaves_the_version_for_review():
    fabric, _ = fabric_answering(status=404)
    [doc], _ = fabric.search("who_don", (), is_demo=False)
    assert revision.differences(fabric, [doc], is_demo=False) == {}


def test_a_monitoring_round_records_a_new_version_of_a_record_a_topic_holds(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    fabric = FakeFabric([record("DON-1", "两家医院确认7例", "旧正文", version=V1, available=DAY.format(1))])
    monitor.tick(is_demo=True, fabric=fabric)
    later = record("DON-1", "两家医院确认7例", "新正文", version=V2, available=DAY.format(2))
    fabric.docs += FakeFabric([later]).docs
    fabric.differences[V2] = compared("raw_changed", body_changed=True)

    monitor.tick(is_demo=True, fabric=fabric)

    with db.session() as s:
        [topic] = list(s.scalars(select(db.Topic)))
        topic_id = topic.id
    [event] = versions(topic_id)
    assert (event["version_id"], event["reason"], event["affected"]) == (V2, "pending_review", [])
