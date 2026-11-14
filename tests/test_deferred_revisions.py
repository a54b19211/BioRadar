"""Owed triage is done on the fixed version that was deferred (adapted from a reviewer's counterexample):
one record revised twice while the budget was used up,
plus an unrelated record, plus a version that can no longer be read."""

from conftest import FakeFabric, record
from sqlalchemy import select
from test_monitor import triage_by_title

from bioradar import db
from bioradar.adapters import llm
from bioradar.config import settings
from bioradar.research import analyses, monitor, triage


class LatestOnlyFabric(FakeFabric):
    """Like the real Fabric: a records search returns each record's visible head, not every version."""

    def search(self, source, terms, *, is_demo, cutoff=None, cursor=None, limit=10, record_ids=(), now=None):
        docs = [
            d for d in self.docs if d["source"] == source and (not record_ids or d["record_id"] in record_ids)
        ]
        latest = {}
        for d in sorted(docs, key=lambda d: d["seq"]):
            latest[d["record_id"]] = d
        return list(latest.values())[:limit], None


def setup(monkeypatch):
    s = settings()
    for k, v in {"monitor_sources": ["who_don"], "monitor_max_records": 2, "auto_analysis": False}.items():
        monkeypatch.setattr(s, k, v)
    paused = [False]
    monkeypatch.setattr(analyses, "over_auto_budget", lambda: paused[0])
    sent = []
    monkeypatch.setattr(llm, "chat_json", triage_by_title(sent))
    fabric = LatestOnlyFabric([record("seed", "old", "x", source="who_don")])
    monitor.tick(is_demo=False, fabric=fabric)
    return fabric, paused, sent


def reasons():
    with db.session() as session:
        return {(o.record_id, o.version_id[:1]): o.reason for o in session.scalars(select(db.Observation))}


def test_every_deferred_revision_is_triaged_on_its_own_version_and_nobody_starves(monkeypatch):
    fabric, paused, sent = setup(monkeypatch)
    paused[0] = True  # day 1: budget used up
    fabric.docs += FakeFabric([record("A", "first", "x", source="who_don", version="a" * 64)]).docs
    monitor.tick(is_demo=False, fabric=fabric)
    fabric.docs += FakeFabric(
        [
            record("A", "updated", "x", source="who_don", version="b" * 64),
            record("B", "unrelated", "x", source="who_don", version="c" * 64),
        ]
    ).docs
    monitor.tick(is_demo=False, fabric=fabric)
    assert {r for r in reasons().values() if r == monitor.DEFERRED} == {monitor.DEFERRED}
    paused[0] = False  # next day: budget back
    rounds = [monitor.tick(is_demo=False, fabric=fabric)["records"] for _ in range(4)]
    left = [k for k, r in reasons().items() if r in triage.RETRYABLE]
    assert left == [] and sum(rounds) == 3  # A/v1, A/v2 and B, each once
    assert ("A", "a") in reasons() and ("A", "b") in reasons() and ("B", "c") in reasons()


def test_a_deferred_version_that_can_no_longer_be_read_becomes_a_recorded_gap(monkeypatch):
    fabric, paused, sent = setup(monkeypatch)
    paused[0] = True
    fabric.docs += FakeFabric([record("R", "revoked later", "x", source="who_don", version="d" * 64)]).docs
    monitor.tick(is_demo=False, fabric=fabric)
    fabric.docs = [d for d in fabric.docs if d["record_id"] != "R"]  # the source was revoked
    paused[0] = False
    monitor.tick(is_demo=False, fabric=fabric)
    assert reasons()[("R", "d")].startswith("triage_unavailable")
    assert monitor.tick(is_demo=False, fabric=fabric)["records"] == 0  # no longer retried


def test_the_adapter_reads_the_fixed_version_and_only_not_visible_is_a_gap():
    import httpx
    import pytest

    from bioradar.adapters.fabric import Fabric, FabricError

    record = {
        "source_id": "who_don",
        "source_record_id": "A",
        "version_id": "a" * 64,
        "is_demo": False,
        "record_kind": "document",
        "title": "first",
        "clean_text": "x",
        "facts": {},
        "first_seen_at": "2026-11-04T01:00:00+00:00",
        "published_seq": 2,
    }
    answers = {
        "a" * 64: httpx.Response(200, json=record),
        "b" * 64: httpx.Response(404, json={"detail": {"code": "version_not_found", "message": "-"}}),
        "c" * 64: httpx.Response(503, json={"detail": {"code": "fabric_service_failed", "message": "-"}}),
    }
    asked = []

    def handler(request):
        asked.append((request.url.path, request.url.params.get("source_record_id")))
        version = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith("/api/fabric/versions/"):
            return answers[version]
        return httpx.Response(404, json={})

    fabric = Fabric(
        settings(), client=httpx.Client(base_url="http://f.invalid", transport=httpx.MockTransport(handler))
    )
    docs, missing = fabric.read_versions("who_don", [("A", "a" * 64), ("A", "b" * 64)], is_demo=False)
    assert [d["version_id"] for d in docs] == ["a" * 64] and missing == ["b" * 64]
    assert asked[0] == ("/api/fabric/versions/" + "a" * 64, "A")
    with pytest.raises(FabricError):  # a transient failure is not turned into a permanent gap
        fabric.read_versions("who_don", [("A", "c" * 64)], is_demo=False)
