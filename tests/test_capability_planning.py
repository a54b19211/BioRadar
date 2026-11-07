"""What an analysis plans from Fabric's declared capabilities, and what it does when they are unknown."""

import pytest
from conftest import FakeFabric
from test_pipeline import empty_report, fake_model, first_policy

from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses
from bioradar.research.pipeline import Run


@pytest.fixture
def offered(monkeypatch):
    """Every candidate action the planner is offered, by (kind, source), over one run."""
    seen = set()

    def decide(s, body):
        seen.update((c["kind"], c["source"]) for c in body["candidates"])
        return first_policy(body)

    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", decide)
    monkeypatch.setattr(services, "biocheck", empty_report)
    return seen


def run_over(fabric, sources, token="admin-token"):
    s = settings().model_copy(update={"fabric_admin_token": token})
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", sources=sources, enqueue=False)
    return Run(row, s, fabric=fabric)


def test_a_fresh_fetch_is_offered_for_the_sources_fabric_says_can_be_fetched_by_a_search_term(offered):
    sources = ["pubmed", "nmpa", "gdelt_doc", "external_archive"]
    run_over(FakeFabric([], readable=sources), sources).run()
    assert {s for kind, s in offered if kind == "fetch"} == {"pubmed", "gdelt_doc"}
    assert {s for kind, s in offered if kind == "search"} == set(sources)  # searching asks Fabric's index


def test_the_receipt_the_run_planned_with_is_recorded_for_audit(offered):
    fabric = FakeFabric([], readable=["pubmed"])
    run = run_over(fabric, ["pubmed"])
    run.run()
    [snapshot] = run.p["capability_snapshots"]
    assert snapshot["receipt_sha256"] == fabric.caps.receipt_sha256 and snapshot["readable"] == 1
    assert snapshot["capabilities_version"] == "datafabric.source_capabilities.v1"


def test_a_resumed_run_with_the_same_receipt_records_it_once(offered):
    fabric = FakeFabric([], readable=["pubmed"])
    run = run_over(fabric, ["pubmed"])
    run.plan_with_capabilities()
    run.plan_with_capabilities()
    assert len(run.p["capability_snapshots"]) == 1


def test_when_fabric_cannot_say_what_it_can_do_nothing_is_fetched_and_the_run_says_so(offered):
    class Unlisted(FakeFabric):
        def sources(self):
            return {"sources": [{"source_id": "pubmed"}]}  # an older Fabric: no capabilities at all

    result = run_over(Unlisted([]), ["pubmed"]).run()
    assert not {s for kind, s in offered if kind == "fetch"}
    assert [n["code"] for n in result["notes"]].count("capability_unknown") == 1
