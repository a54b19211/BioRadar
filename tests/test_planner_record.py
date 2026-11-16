"""What the radar asks Bio-Planner (sources, channels) and what it keeps of every answer (task book 04, 2.2
and 2.5): a record per call that rebuilds the request, within a size limit, and a planner too old to take the
request is refused."""

import copy
import os
from types import SimpleNamespace

import pytest
from conftest import FakeFabric
from test_pipeline import RECORDS, empty_report, fake_model, first_policy

from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, schemas
from bioradar.research.pipeline import Run
from bioradar.research.run import Failed, decisions, investigate

ASK = {
    "question": "XQ-17 的PFS结果现在应怎样判断？",
    "direction": "pipeline",
    "is_demo": True,
    "enqueue": False,
}
SEMANTIC = "weknora_semantic"


class SemanticFabric(FakeFabric):
    """A Fabric whose semantic search finds nothing, noting what it was asked."""

    asked = []

    def semantic_search(
        self, query, *, scopes, is_demo, sources=(), cutoff=None, now=None, top_k=10, mode=None
    ):
        self.asked.append((query, tuple(sources)))
        return [], {"mode": "current", "cutoff": "now", "excluded": {}, "recall": [], "coverage": {}}


def planned(monkeypatch, policy=first_policy):
    """Stand in for the models and services; returns the requests as sent and the answers."""
    sent, answers = [], []

    def decide(s, body):
        sent.append(copy.deepcopy(body))
        answers.append(policy(body))
        return answers[-1]

    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", decide)
    monkeypatch.setattr(services, "biocheck", empty_report)
    return sent, answers


def run_one(fabric=None, **options):
    row = analyses.create(**ASK)
    run = Run(row, settings().model_copy(update=options), fabric=fabric or FakeFabric(RECORDS))
    return row, run, run.run()


def test_the_request_names_the_sources_declares_the_channels_and_tags_every_search_with_its_channel(
    monkeypatch,
):
    sent, _ = planned(monkeypatch)
    fabric = SemanticFabric(RECORDS)
    fabric.asked = []
    run_one(fabric, semantic_search=True)
    assert all(body["sources"] == ["external_archive"] for body in sent)
    declared = {c["channel"]: c for c in sent[0]["channels"]}
    assert [c["available"] for c in declared.values()] == [True, True, False, False]
    assert declared["graph"]["reason"] == "03_not_run"
    for body in sent:
        for c in body["candidates"]:
            if c["kind"] == "read":
                assert "channel" not in c  # a read is Fabric's read of a record already found
            else:
                assert c["channel"] in {"fabric_literal", SEMANTIC}
            assert c["source"] != "weknora"  # a channel, not a source: the real source is named
    # the same words through the other channel are another action, still offered once the first is done
    second = {(c["source"], c["channel"]) for c in sent[1]["candidates"] if c["kind"] == "search"}
    assert second == {("external_archive", SEMANTIC)}
    # and history carries the channel the candidates carry
    done = [h for body in sent for h in body["history"] if h["kind"] == "search"]
    assert {h["channel"] for h in done} == {"fabric_literal", SEMANTIC}
    assert fabric.asked == [("PFS", ("external_archive",))]  # the recall is limited to the candidate's source


def test_every_round_leaves_a_record_that_rebuilds_the_request_it_sent(monkeypatch):
    sent, answers = planned(monkeypatch)
    row, _, result = run_one()
    saved = analyses.get(row.id).progress
    kept = saved["decisions"]
    assert [d["step"] for d in kept] == list(range(1, len(sent) + 1))
    for entry, body, answer in zip(kept, sent, answers):
        rebuilt = decisions.rebuilt_request(entry, saved["actions"])
        assert rebuilt == body and decisions.digest(rebuilt) == entry["request_sha256"]
        assert entry["response"] == answer and entry["planner"]["version"] == "0.2.0"
        assert entry["history_ref"]["actions"] == len(body["history"])
    for entry, answer in zip(kept[:-1], answers):
        action = saved["actions"][entry["action_index"]]  # the action the chosen candidate became
        assert action["args"] == sent[entry["step"] - 1]["candidates"][answer["candidate"]]["args"]
    final = kept[-1]  # the stop is a record too: no action follows, the reason is kept
    assert (final["action_index"], final["stop"], final["response"]["decision"]) == (None, "test", "complete")
    assert result["stop"] == {"decision": "complete", "reason": "test"}
    # what the planner read of the checks: the answer the read stage grounded, with its evidence
    assert [(c["status"], c["evidence"]) for c in sent[-1]["checks"]] == [("answered", ["E1", "E2"])]


def test_a_failed_planner_call_is_a_record_too_after_the_first_action_and_before_it(monkeypatch):
    sent, _ = planned(monkeypatch)

    def down_after_one(s, body):
        if body["history"]:
            raise services.ServiceError("planner", "unreachable")
        return first_policy(body)

    monkeypatch.setattr(services, "decide", down_after_one)
    row, _, result = run_one()
    failed = analyses.get(row.id).progress["decisions"][-1]
    assert (failed["stop"], failed["action_index"], "response" in failed) == (
        "planner_unreachable",
        None,
        False,
    )
    assert result["stop"]["reason"] == "planner_unreachable"

    def down(s, body):
        raise services.ServiceError("planner", "unreachable")

    monkeypatch.setattr(services, "decide", down)
    row = analyses.create(**{**ASK, "question": "XQ-17 的另一个问题怎样判断？"})
    with pytest.raises(Failed) as stopped:
        Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert stopped.value.code == "planner_unreachable"
    [only] = analyses.get(row.id).progress["decisions"]  # saved before the run failed
    assert only["stop"] == "planner_unreachable" and only["history_ref"]["actions"] == 0


def test_a_round_that_does_not_fit_the_size_limit_keeps_only_its_hashes():
    run = SimpleNamespace(p={})
    planner = {"version": "0.2.0", "kinds": [], "channels": []}

    def request(word):
        return {"question": "q", "candidates": [{"args": {"terms": [word * 600_000]}}], "history": []}

    big = request("a")
    decisions.keep(run, big, planner, response={"decision": "continue", "candidate": 0, "reason": "x"})
    decisions.keep(run, request("b"), planner, response={"decision": "complete", "reason": "x"})
    first, second = run.p["decisions"]
    assert "request" in first and "response" in first and "snapshot_truncated" not in first
    assert second["snapshot_truncated"] is True and not {"request", "response"} & second.keys()
    assert (
        second["request_sha256"] == decisions.digest(request("b")) and second["history_ref"]["actions"] == 0
    )
    assert decisions.rebuilt_request(second, []) is None  # a cut round cannot be rebuilt
    assert sum(len(decisions.canonical(e).encode()) for e in run.p["decisions"]) <= decisions.MAX_BYTES
    assert decisions.rebuilt_request(first, []) == big


def test_a_planner_that_does_not_list_its_channels_is_refused_with_a_clear_error(monkeypatch):
    sent, _ = planned(monkeypatch)
    monkeypatch.setattr(services, "planner_health", lambda s: {"status": "ok"})  # planner before 0.2.0
    row = analyses.create(**ASK)
    with pytest.raises(Failed) as refused:
        Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert refused.value.code == "planner_outdated" and sent == []  # nothing was sent, nothing sent less
    assert "planner_outdated" in [n["code"] for n in analyses.get(row.id).progress["notes"]]


def test_conflict_and_open_reach_the_planner_as_they_are_and_complete_does_not_make_the_report_pass(
    monkeypatch,
):
    def reading_for_and_against(s, stage, system, payload, model_cls, **options):
        value, call = fake_model([])(s, stage, system, payload, model_cls, **options)
        if stage == "read":
            corrected = "更正" in payload["material"]["text"]
            for_it, against = ("HR为0.69", "95%CI 0.51-0.93") if corrected else ("HR为0.62", "纳入240例")
            value.facts = [
                schemas.ReadFact(text="PFS风险比", quote=for_it, checks=["Q1"], stance="supports"),
                schemas.ReadFact(text="区间或人数", quote=against, checks=["Q1"], stance="refutes"),
            ]
        return value, call

    def stop_after_a_read(body):
        done = any(h["kind"] == "read" for h in body["history"])
        return {"decision": "complete", "candidate": None, "reason": "test"} if done else first_policy(body)

    sent, _ = planned(monkeypatch, stop_after_a_read)
    monkeypatch.setattr(llm, "chat_json", reading_for_and_against)
    _, _, result = run_one()
    assert sent[0]["checks"][0]["status"] == "open" and sent[0]["checks"][0]["evidence"] == []
    assert sent[-1]["checks"][0]["status"] == "conflict"  # not turned into anything else on the way
    assert result["stop"]["decision"] == "complete" and result["checks"][0]["status"] == "conflict"
    assert result["report_status"] != "PASS" and result["publishable"] is False


PLANNER = os.environ.get("BIORADAR_TEST_PLANNER_URL")


@pytest.mark.skipif(not PLANNER, reason="BIORADAR_TEST_PLANNER_URL (a running Bio-Planner) is not set")
def test_a_real_planner_takes_the_request_and_answers_with_its_record(monkeypatch):
    monkeypatch.undo()  # the real /health, not the stand-in
    s = settings().model_copy(
        update={
            "planner_url": PLANNER,
            "planner_token": os.environ["BIORADAR_TEST_PLANNER_TOKEN"],
            "semantic_search": True,
        }
    )
    run = Run(analyses.create(**ASK), s, fabric=FakeFabric([]))
    run.p.update(queries=[{"terms": ["PFS"], "checks": ["Q1"]}], actions=[], pages={}, hits=[], objects=[])
    run.p["checks"] = [
        {"id": "Q1", "text": "PFS", "required": True, "importance": 1.0, "status": "open", "evidence": []}
    ]
    planner = decisions.planner_snapshot(run)
    request = investigate.planner_request(run, investigate.candidates(run))
    answer = services.decide(s, request)  # a 422 here means the request is not the planner's contract
    assert answer["planner_version"] == planner["version"]
    assert len(answer["reasons"]) == len(answer["scores"]) == len(request["candidates"]) == 2
    states = {c["channel"]: c for c in answer["channels"]}
    assert states["graph"] == {"channel": "graph", "status": "unavailable", "reason": "03_not_run"}
    assert states["fabric_literal"]["status"] == "chosen" or states[SEMANTIC]["status"] == "chosen"
    assert [c["state"] for c in answer["checks"]] == ["open"]
