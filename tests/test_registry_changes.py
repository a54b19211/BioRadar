"""First vertical chain A: Fabric's registry diff reaches the radar text and decides what needs the model."""

import httpx

from bioradar.adapters.fabric import Fabric, document, registry_change_block
from bioradar.config import settings
from bioradar.research.monitor import route

V1, V2 = "a" * 64, "b" * 64


def trial(version=V2, published="2019-03-01", updated="2026-11-02", status="TERMINATED", results=None):
    facts = {"trial_id": "NCT01234567", "status": status}
    if results:
        facts["results_first_posted"] = results
    return {
        "source_id": "clinicaltrials",
        "source_record_id": "NCT01234567",
        "version_id": version,
        "is_demo": False,
        "record_kind": "trial",
        "title": "Example",
        "clean_text": "Summary.",
        "facts": facts,
        "field_locators": {},
        "first_seen_at": "2026-11-04T01:00:00+00:00",
        "source_published_at": {"value": published, "precision": "day"},
        "source_updated_at": {"value": updated, "precision": "day"},
        "published_seq": 7,
    }


STATUS = {
    "field": "status",
    "change_kind": "status_change",
    "material": True,
    "description": 'status（/protocolSection/statusModule/overallStatus）: "RECRUITING" → "TERMINATED"',
}
SITES = {
    "field": "locations",
    "change_kind": "location_change",
    "material": False,
    "description": "地点数 3 → 2",
}


def processed(*changes):
    return {
        "state": "processed",
        "from_version_id": V1,
        "change_count": len(changes),
        "material_count": sum(c["material"] for c in changes),
        "changes": list(changes),
    }


def fabric_answering(versions=None, status=200):
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.path == "/api/history/records":
            return httpx.Response(200, json={"items": [trial()], "next_cursor": None})
        if request.url.path == "/api/history/registry-changes":
            if status != 200:
                return httpx.Response(status, json={"detail": "Not Found"})
            return httpx.Response(200, json={"versions": versions or {}})
        return httpx.Response(404, json={})

    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    return Fabric(settings(), client=client), sent


def test_material_change_goes_on_top_of_the_text_with_both_versions_and_as_an_event():
    fabric, sent = fabric_answering({V2: processed(STATUS, SITES)})
    [doc], _ = fabric.search("clinicaltrials", (), is_demo=False)
    assert doc["text"].startswith("【与上一版相比的登记变化")
    assert V1[:16] in doc["text"] and V2[:16] in doc["text"]
    assert doc["text"].index("TERMINATED") < doc["text"].index("（小修订）")  # material first
    assert doc["events"] == [{"kind": "registry_change", "description": STATUS["description"]}]
    asked = sent[-1].url.params
    assert asked.get_list("version_ids") == [V2] and asked["mode"] == "current"
    assert route(doc) is None  # material: the model triages it


def test_only_minor_changes_are_settled_without_the_model():
    fabric, _ = fabric_answering({V2: processed(SITES)})
    [doc], _ = fabric.search("clinicaltrials", (), is_demo=False)
    assert route(doc) == "registry_minor_change"
    assert doc["events"] == []


def test_older_fabric_without_the_endpoint_leaves_reading_unchanged():
    fabric, _ = fabric_answering(status=404)
    [doc], _ = fabric.search("clinicaltrials", (), is_demo=False)
    assert doc["registry_diff"] == {"state": "unavailable"}
    assert doc["text"] == document(trial())["text"]
    assert route(doc) is None  # unknown: triaged as before


def first(**kw):
    doc = document(trial(**kw))
    doc["registry_diff"] = {"state": "first_adoption", "changes": []}
    return doc


def test_first_adoption_routing_follows_the_registrys_own_dates():
    # inventory: registered years ago, updated recently but still recruiting
    assert route(first(status="RECRUITING")) == "inventory_first_adoption"
    # a new registration (first posted within 30 days)
    assert route(first(published="2026-10-20", status="RECRUITING")) is None
    # stopped and updated within 7 days: worth checking even without a previous version
    assert route(first(status="TERMINATED")) is None
    # stopped long ago: inventory
    assert route(first(status="TERMINATED", updated="2025-01-01")) == "inventory_first_adoption"
    # results just posted
    assert route(first(status="COMPLETED", results="2026-10-30")) is None


def test_retries_and_other_sources_are_never_rerouted():
    doc = document(trial())
    doc["registry_diff"] = processed(SITES)
    assert route(dict(doc, retry=True)) is None
    assert route(dict(doc, source="who_don")) is None
    assert route(dict(doc, registry_diff={"state": "not_processed"})) is None


def test_block_is_empty_for_first_adoption_and_caps_lines():
    assert registry_change_block({"state": "first_adoption", "changes": []}, V2) == ""
    many = processed(*[dict(SITES, field=f"f{i}", description=f"d{i}") for i in range(20)])
    block = registry_change_block(many, V2)
    assert "另有 8 处字段变化" in block


def test_change_block_names_both_full_versions_and_is_marked_as_derived():
    """Codex r58 #3: the block is a program-derived comparison of two versions, not the record's own text;
    the document carries the full inputs so the evidence record can point back to both versions."""
    fabric, _ = fabric_answering({V2: processed(STATUS, SITES)})
    [doc], _ = fabric.search("clinicaltrials", (), is_demo=False)
    head = doc["text"].split("\n", 1)[0]
    assert V1 in head and V2 in head and "派生说明，不是本版原文" in head
    [derived] = doc["derived"]
    assert (
        derived["kind"] == "registry_diff"
        and derived["from_version_id"] == V1
        and derived["to_version_id"] == V2
    )
    assert {f["field"] for f in derived["fields"]} == {"status", "locations"}
