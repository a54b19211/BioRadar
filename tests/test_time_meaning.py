"""Source times keep their meaning on the way into the radar (task book 01 2.4): a record without a published
time is not given its event time, and a GDELT observation time is not a modification of the source."""

from conftest import FakeFabric, record
from test_pipeline import RECORDS, empty_report, fake_model, first_policy
from test_registry_changes import trial

from bioradar.adapters import llm, services
from bioradar.adapters.fabric import document
from bioradar.config import settings
from bioradar.research import analyses
from bioradar.research.monitor import route
from bioradar.research.pipeline import Run


def test_a_missing_published_time_is_not_replaced_by_the_event_time():
    notice = record("W1", "WHO notice", "text")
    notice.pop("source_published_at")
    notice["event_time"] = {"value": "2026-10-02", "precision": "day", "meaning": "who_notice_publication"}

    doc = document(notice)

    assert doc["published_at"] is None
    assert doc["event_time"] == {"value": "2026-10-02", "meaning": "who_notice_publication"}


def test_the_published_time_and_the_event_time_each_carry_their_own_meaning():
    notice = record("W2", "WHO notice", "text")
    notice["source_published_at"] = {
        "value": "2026-10-01",
        "precision": "day",
        "meaning": "who_notice_publication",
    }
    notice["event_time"] = {"value": "2026-10-03", "precision": "day"}  # Fabric named no meaning

    doc = document(notice)

    assert doc["published_at"] == {"value": "2026-10-01", "meaning": "who_notice_publication"}
    assert doc["event_time"] == {"value": "2026-10-03", "meaning": None}


def test_a_gdelt_observation_time_is_not_a_recent_update_of_the_source():
    def routed(meaning):
        stopped = trial(status="TERMINATED")  # updated 2 days before it was first seen, registered long ago
        stopped["source_updated_at"]["meaning"] = meaning
        doc = document(stopped)
        doc["registry_diff"] = {"state": "first_adoption", "changes": []}
        return route(doc)

    assert routed("source_reported") is None  # stopped and updated within 7 days: worth triage
    assert routed("gdelt_first_observed") == "inventory_first_adoption"  # the time says nothing of the source


def test_no_proof_is_not_the_first_sight_and_the_two_times_stay_apart():
    proven = record("P1", "archive copy", "text", available="2026-09-20T08:00:00+08:00")
    proven["first_seen_at"] = "2026-11-06T03:10:00+00:00"  # archived long before this system saw it
    unproven = record("P2", "feed item", "text", available="2026-09-30T08:00:00+08:00")
    unproven["availability_proof"] = {"kind": "unverified"}

    archived, fed = document(proven), document(unproven)

    assert (archived["first_seen_at"], archived["proof_available_at"]) == (
        "2026-11-06T03:10:00+00:00",
        "2026-09-20T08:00:00+08:00",
    )
    assert (fed["first_seen_at"], fed["proof_available_at"]) == ("2026-09-30T08:00:00+08:00", None)
    # the radar's own clock is one of the two, never a third time
    assert archived["available_at"] == archived["proof_available_at"]
    assert fed["available_at"] == fed["first_seen_at"]


def test_the_three_time_facts_reach_biocheck_as_stated_and_the_merged_time_does_not(monkeypatch):
    """Task book 06 2.4: availability is first_seen_at or proof_available_at, which BioCheck tells apart with
    the mode the version was read in; the merged available_at of the radar is not part of the request."""
    sent = []
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(
        services, "biocheck", lambda s, op, body: sent.append((op, body)) or empty_report(s, op, body)
    )
    archived = dict(
        RECORDS[0],
        first_seen_at="2026-09-29T08:00:00+08:00",
        availability_proof={
            "kind": "reviewed_archive",
            "available_at": {"value": "2026-09-20T08:00:00+08:00", "precision": "instant"},
        },
    )
    fed = dict(RECORDS[1], availability_proof={"kind": "unverified"})
    fabric = FakeFabric([archived, fed])
    for doc in fabric.docs:  # as Fabric.complete leaves a document read in this mode
        doc["read_at"] = {"mode": "operational_replay", "cutoff": "2026-10-05T00:00:00+08:00"}
    row = analyses.create(
        question="XQ-17 的PFS？",
        direction="public_health",
        is_demo=True,
        cutoff="2026-10-05T00:00:00+08:00",
        replay_mode="operational_replay",
        scope_record_ids=["B31", "B41"],
        enqueue=False,
    )
    Run(row, settings(), fabric=fabric).run()

    evidence = {e["title"]: e for e in next(body for op, body in sent if op == "generate")["evidence"]}
    times = ("first_seen_at", "proof_available_at", "replay_mode")
    assert {title: [e[k] for k in times] for title, e in evidence.items()} == {
        "XQ-17 随机试验初步结果": [
            "2026-09-29T08:00:00+08:00",
            "2026-09-20T08:00:00+08:00",
            "operational_replay",
        ],
        "XQ-17 试验结果更正": ["2026-09-30T08:00:00+08:00", None, "operational_replay"],
    }
    assert not any("available_at" in e for e in evidence.values())
