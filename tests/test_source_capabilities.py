"""The radar reads what each source can do from Fabric's receipt and keeps no list of its own."""

import copy

import httpx
import pytest
from fabric_receipts import RECEIPT, entry, sources_receipt

from bioradar.adapters.capabilities import CapabilityError, SourceCapabilities
from bioradar.adapters.fabric import Fabric
from bioradar.config import settings


def test_the_real_receipt_gives_readable_from_sources_and_retired_sources_alike():
    caps = SourceCapabilities.parse(RECEIPT)
    assert "pubmed" in caps.readable and "external_archive" in caps.readable
    # a retired source is readable (old references still resolve through Fabric's alias) but never selectable
    assert "legacy_documents" in caps.readable and not caps.selectable("legacy_documents")
    assert caps.selectable("pubmed") and not caps.selectable("reviewed_graph")
    assert caps.snapshot()["receipt_sha256"] == RECEIPT["receipt_sha256"]


def test_unavailable_sources_are_not_readable_whatever_else_they_say():
    receipt = sources_receipt({"pubmed"}, known={"nmpa"})
    caps = SourceCapabilities.parse(receipt)
    assert caps.readable == {"pubmed"} and not caps.attachment_text("nmpa") and not caps.selectable("nmpa")
    receipt["unavailable_sources"][0]["capabilities"]["readable"] = True  # even a lying entry is not listed
    assert "nmpa" not in SourceCapabilities.parse(receipt).readable


def test_readable_is_not_derived_from_what_a_source_may_do():
    """A graph source is read but never offered; an archive that cannot be fetched is offered and read."""
    caps = SourceCapabilities.parse(sources_receipt({"reviewed_graph", "external_archive", "local_files"}))
    assert caps.readable == {"reviewed_graph", "external_archive", "local_files"}
    assert not caps.selectable("reviewed_graph") and caps.selectable("external_archive")
    assert caps.selectable("local_files") and not caps.query_fetch("local_files")  # its mode is an upload


def test_the_capabilities_that_replace_the_radars_own_lists():
    caps = SourceCapabilities.parse(sources_receipt({"pubmed", "nmpa", "clinicaltrials", "external_archive"}))
    assert (
        caps.query_fetch("pubmed")
        and not caps.query_fetch("nmpa")
        and not caps.query_fetch("external_archive")
    )
    assert (
        caps.attachment_text("nmpa")
        and caps.attachment_text("pubmed")
        and not caps.attachment_text("clinicaltrials")
    )
    assert caps.registry_diff("clinicaltrials") and not caps.registry_diff("pubmed")
    # a source Fabric does not list can do nothing
    assert not caps.query_fetch("who_don") and not caps.attachment_text("cde") and not caps.registry_diff("x")


@pytest.mark.parametrize(
    "damage",
    [
        lambda r: r.update(capabilities_version="datafabric.source_capabilities.v0"),
        lambda r: r.pop("capabilities_version"),
        lambda r: r.update(sources={"pubmed": {}}),
        lambda r: r["sources"].append({"source_id": "x"}),
        lambda r: r["sources"].append({"source_id": "x", "capabilities": {"readable": "true"}}),
        lambda r: r["sources"].append({"source_id": 7, "capabilities": {"readable": True}}),
        lambda r: r.update(retired_sources=None),
    ],
)
def test_a_receipt_that_is_not_understood_is_unusable_as_a_whole(damage):
    receipt = copy.deepcopy(RECEIPT)
    damage(receipt)
    with pytest.raises(CapabilityError):
        SourceCapabilities.parse(receipt)


def test_an_entry_that_contradicts_another_makes_the_source_unreadable():
    receipt = sources_receipt({"pubmed"})
    receipt["retired_sources"].append(entry("pubmed", readable=False))
    assert "pubmed" not in SourceCapabilities.parse(receipt).readable


def test_the_old_receipt_without_capabilities_is_unknown_not_everything_readable():
    with pytest.raises(CapabilityError):
        SourceCapabilities.parse({"sources": [{"source_id": "pubmed"}]})


def fabric_answering(handler):
    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    return Fabric(settings(), client=client)


def test_the_fabric_client_reads_the_capabilities_once_per_load_and_none_when_it_cannot():
    asked = []

    def handler(request):
        asked.append(request.url.path)
        return httpx.Response(200, json=sources_receipt({"pubmed"}))

    fabric = fabric_answering(handler)
    assert fabric.caps is None
    assert fabric.load_capabilities().readable == {"pubmed"} and fabric.caps.readable == {"pubmed"}
    assert asked == ["/api/history/sources"]
    for broken in (httpx.Response(503, json={}), httpx.Response(200, json={"sources": []})):
        again = fabric_answering(lambda request, broken=broken: broken)
        assert again.load_capabilities() is None and again.caps is None
