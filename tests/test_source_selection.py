"""Which sources a new analysis or a monitoring round may use: only those Fabric offers for selection. A
retired source such as legacy_documents stays readable (old references resolve through Fabric) but is never
chosen."""

import pytest
from conftest import FakeFabric, record
from fabric_receipts import sources_receipt
from test_access import admin_client, fabric_sources  # noqa: F401 - the fixture these tests take
from test_monitor import DAY, triage_by_title

from bioradar import db
from bioradar.adapters import llm
from bioradar.config import settings
from bioradar.research import analyses, monitor
from bioradar.research.pipeline import Run

ASK = {"question": "XQ-17 的进展如何？", "direction": "pipeline"}


@pytest.fixture
def client(fabric_sources, monkeypatch):  # noqa: F811
    monkeypatch.setattr(analyses, "dispatch", lambda analysis_id: None)
    fabric_sources["value"] = ["pubmed", "who_don", "legacy_documents", "reviewed_graph"]
    return admin_client(), fabric_sources


def test_a_retired_source_is_readable_but_cannot_be_chosen_for_a_new_analysis(client):
    api, _ = client
    for named in (
        ["legacy_documents"],
        ["pubmed", "legacy_documents"],
        ["reviewed_graph"],
        ["no_such_source"],
    ):
        response = api.post("/api/analyses", json={**ASK, "sources": named})
        assert response.status_code == 422, named
        body = response.json()
        assert body["error_code"] == "source_not_selectable"
        assert set(body["sources"]) == set(named) - {"pubmed"}
    with db.session() as s:
        assert s.query(db.Analysis).count() == 0  # nothing was created


def test_a_source_fabric_offers_is_accepted(client):
    api, _ = client
    response = api.post("/api/analyses", json={**ASK, "sources": ["pubmed", "who_don"]})
    assert response.status_code == 200
    assert analyses.get(response.json()["id"]).input["sources"] == ["pubmed", "who_don"]


def test_the_configured_default_sources_are_filtered_and_an_empty_result_is_refused(client, monkeypatch):
    api, listed = client
    monkeypatch.setattr(settings(), "sources", ["legacy_documents", "pubmed", "nmpa"])
    response = api.post("/api/analyses", json=ASK)  # nmpa is not readable now, legacy is retired
    assert response.status_code == 200
    assert analyses.get(response.json()["id"]).input["sources"] == ["pubmed"]
    monkeypatch.setattr(settings(), "sources", ["legacy_documents"])
    refused = api.post("/api/analyses", json={**ASK, "question": "另一个问题？"})
    assert refused.status_code == 422 and refused.json()["sources"] == ["legacy_documents"]


def test_when_fabric_cannot_say_what_may_be_chosen_no_analysis_is_created(client):
    api, listed = client
    listed["value"] = None  # the source list cannot be read
    response = api.post("/api/analyses", json={**ASK, "sources": ["pubmed"]})
    assert response.status_code == 503 and response.json()["error_code"] == "source_capabilities_unavailable"
    with db.session() as s:
        assert s.query(db.Analysis).count() == 0


def test_a_running_analysis_leaves_out_a_source_that_is_no_longer_offered_and_says_so():
    row = analyses.create(
        question="旧分析还有别的来源？",
        direction="pipeline",
        sources=["legacy_documents", "pubmed"],
        enqueue=False,
    )
    run = Run(row, settings(), fabric=FakeFabric([], readable=["legacy_documents", "pubmed"]))
    run.plan_with_capabilities()
    assert run.input["sources"] == ["pubmed"]
    assert [n["code"] for n in run.p["notes"]] == ["source_not_selectable"]
    assert row.input["sources"] == ["legacy_documents", "pubmed"]  # the stored input is not rewritten


def test_monitoring_reads_only_offered_sources_and_reports_the_others(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    monkeypatch.setattr(settings(), "monitor_sources", ["who_don", "legacy_documents"])
    fabric = FakeFabric(
        [
            record("H0", "历史", "x", source="who_don", available=DAY.format(1)),
            record("L0", "旧文档", "x", source="legacy_documents", available=DAY.format(1)),
        ]
    )
    first = monitor.tick(is_demo=False, fabric=fabric)
    assert first["source_not_selectable"] == ["legacy_documents"]
    with db.session() as s:
        assert {c.source for c in s.query(db.Cursor)} == {"who_don"}  # legacy never got a cursor


def test_when_the_capabilities_cannot_be_read_the_round_reads_nothing_and_moves_no_cursor(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", triage_by_title([]))
    monkeypatch.setattr(settings(), "monitor_sources", ["who_don"])
    fabric = FakeFabric([record("H0", "历史", "x", source="who_don")])
    monitor.tick(is_demo=False, fabric=fabric)  # the cursor starts at the feed's end
    fabric.docs += FakeFabric([record("H1", "确认7例", "x", source="who_don")]).docs
    fabric.sources = lambda: {"sources": []}  # an older Fabric: no capabilities
    down = monitor.tick(is_demo=False, fabric=fabric)
    assert down["paused"] == "source_capabilities_unavailable" and down["records"] == 0
    with db.session() as s:
        assert s.get(db.Cursor, ("who_don", False)).after_seq == 0 + fabric.docs[0]["seq"]
    del fabric.sources
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 1  # the same record, once Fabric can say


def test_the_receipt_helper_lists_a_retired_source_apart_from_the_offered_ones():
    receipt = sources_receipt(["legacy_documents", "pubmed"])
    assert [e["source_id"] for e in receipt["sources"]] == ["pubmed"]
    assert [e["source_id"] for e in receipt["retired_sources"]] == ["legacy_documents"]
