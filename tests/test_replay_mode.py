"""The replay mode runs from the task through every Fabric read to the receipts: a task that names
operational_replay is read in it by the five Fabric entries and by the mechanism attempts, a follow-up
inherits it, and a task that names none keeps what a cutoff always meant (the public history
reconstruction). No real model or Fabric: the requests are captured."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import FakeFabric
from mech_support import Fab, attempt_body, service_client, started
from test_access import admin_client, fabric_sources  # noqa: F401 - the fixture these tests take
from test_pipeline import RECORDS, empty_report, fake_model, first_policy

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import analyses
from bioradar.research.pipeline import Run
from bioradar.research.run import search

CUTOFF = datetime(2026, 9, 30, tzinfo=UTC)
ASK = {"question": "XQ-17 的进展如何？", "direction": "pipeline", "is_demo": True, "sources": ["pubmed"]}
WHEN = "2026-09-29T12:00:00+08:00"
GRAPH_RECORD = {"version_id": "v1", "source": "reviewed_graph", "record_id": "G1", "is_demo": True}


def fabric_asking():
    """(the Fabric adapter, [(path, mode)] it sent): every endpoint answers with nothing."""
    sent = []

    def handler(request):
        params = json.loads(request.content) if request.content else dict(request.url.params)
        sent.append((request.url.path, params["mode"]))
        if "/versions/" in request.url.path:
            return httpx.Response(404, json={"error": {"code": "version_not_found"}})
        answers = {
            "search": {"results": [], "execution": {}, "excluded": {}, "coverage": {}},
            "records": {"items": [], "next_cursor": None},
            "resolve": {"status": "resolved", "candidates": []},
        }
        return httpx.Response(200, json=answers[request.url.path.rsplit("/", 1)[-1]])

    s = settings().model_copy(update={"fabric_read_token": "read-token", "semantic_search": True})
    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    return Fabric(s, client=client), sent


def read_everywhere(fabric, **how):
    """One read through each of the five entries; the semantic search's summary."""
    _, summary = fabric.semantic_search("布病", scopes=("research",), is_demo=True, **how)
    fabric.search("who_don", ["布病"], is_demo=True, **how)
    fabric.read_versions("who_don", [("D1", "v1")], is_demo=True, **how)
    fabric.version_visible("who_don", "D1", "v1", is_demo=True, **how)
    fabric.graph_resolve(GRAPH_RECORD, "XQ-17", **how)
    return summary


def test_every_read_entry_sends_the_mode_it_was_given_and_the_search_receipt_says_it():
    fabric, sent = fabric_asking()
    summary = read_everywhere(fabric, cutoff=CUTOFF, mode="operational_replay")
    assert [mode for _, mode in sent] == ["operational_replay"] * 5
    assert len({path for path, _ in sent}) == 4  # search, records, versions (asked twice), graph resolve
    assert summary["mode"] == "operational_replay" and summary["cutoff"] == CUTOFF.isoformat()


def test_a_read_that_names_no_mode_keeps_what_a_cutoff_always_meant():
    for cutoff, mode in ((CUTOFF, "public_history_reconstruction"), (None, "current")):
        fabric, sent = fabric_asking()
        read_everywhere(fabric, cutoff=cutoff)
        assert [m for _, m in sent] == [mode] * 5


def run_scope_task(monkeypatch, replay_mode):
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(
        question="当前对异常聚集能作什么判断？",
        direction="public_health",
        is_demo=True,
        cutoff=WHEN,
        replay_mode=replay_mode,
        scope_record_ids=["B31", "B41"],
        enqueue=False,
    )
    fabric = FakeFabric(RECORDS)
    return fabric, Run(row, settings(), fabric=fabric).run()


def visibility_texts(result):
    section = next(s for s in result["sections"] if s["kind"] == "visibility")
    return [item["text"] for item in section["items"]]


def test_a_run_reads_in_the_mode_its_task_named_and_the_report_says_it(monkeypatch):
    fabric, result = run_scope_task(monkeypatch, "operational_replay")
    assert fabric.modes and {mode for _, mode in fabric.modes} == {"operational_replay"}
    assert "回放模式：operational_replay" in visibility_texts(result)


def test_a_run_whose_task_names_no_mode_reads_as_before(monkeypatch):
    fabric, result = run_scope_task(monkeypatch, None)
    assert fabric.modes and {mode for _, mode in fabric.modes} == {"public_history_reconstruction"}
    assert not any(text.startswith("回放模式") for text in visibility_texts(result))


def test_the_retrieval_receipt_keeps_the_mode_apart_from_the_run_kind():
    fabric, sent = fabric_asking()
    row = analyses.create(**ASK, cutoff=WHEN, replay_mode="operational_replay", enqueue=False)
    run = Run(row, settings().model_copy(update={"semantic_search": True}), fabric=fabric)
    search.semantic(run, ["布病"])
    assert sent == [("/api/history/search", "operational_replay")]
    receipt = run.p["weknora"][0]
    assert (receipt["mode"], receipt["cutoff"], receipt["run_kind"]) == (
        "operational_replay",
        run.cutoff.isoformat(),
        "online",
    )


@pytest.fixture
def client(fabric_sources, monkeypatch):  # noqa: F811
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    fabric_sources["value"] = ["pubmed", "who_don"]
    return admin_client()


def test_the_task_input_keeps_the_mode_and_a_follow_up_inherits_it(client):
    ask = {**ASK, "cutoff": WHEN, "replay_mode": "operational_replay"}
    first = client.post("/api/analyses", json=ask).json()
    assert analyses.get(first["id"]).input["replay_mode"] == "operational_replay"
    child = client.post(f"/api/analyses/{first['id']}/followup", json={"question": "后续进展如何？"}).json()
    assert analyses.get(child["id"]).input["replay_mode"] == "operational_replay"
    # a task that names none stores none: its input, and so its hash, is what it always was
    plain = client.post("/api/analyses", json={**ASK, "cutoff": WHEN, "question": "另一个问题？"}).json()
    again = client.post(f"/api/analyses/{plain['id']}/followup", json={"question": "再后续呢？"}).json()
    assert all("replay_mode" not in analyses.get(a["id"]).input for a in (plain, again))


def test_a_mode_that_contradicts_the_cutoff_is_refused(client):
    for body in (
        {"replay_mode": "operational_replay"},  # a replay of no moment
        {"replay_mode": "current", "cutoff": WHEN},  # the live state at a past moment
        {"replay_mode": "someday", "cutoff": WHEN},
    ):
        assert client.post("/api/analyses", json={**ASK, **body}).status_code == 422
    with db.session() as s:
        assert s.query(db.Analysis).count() == 0


def test_the_mechanism_attempts_are_authorized_in_the_mode_of_the_run(monkeypatch):
    api = service_client(monkeypatch)
    _, ref = started(cutoff=CUTOFF, replay_mode="operational_replay")
    with db.session() as s:
        assert s.get(db.ModelAttemptAuthorization, ref).mode == "operational_replay"
    answer = api.post("/internal/v1/model-attempts/authorize", json=attempt_body(ref, 1, stage="decompose"))
    assert answer.status_code == 200 and Fab.modes == ["operational_replay"]
