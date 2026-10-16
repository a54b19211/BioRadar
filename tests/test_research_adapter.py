"""Synthetic HTTP boundary cases; none are real research evidence."""

import copy
import hashlib
import json

import httpx
import pytest
from bioradar.adapters.data_fabric_research import PROTOCOL_PATHS, DataFabricResearchReader
from bioradar.api.errors import DomainError
from bioradar.research.mining.methods_text import is_structured_json

pytestmark = pytest.mark.BOOT
IDENTIFIER = "00000000-0000-0000-0000-000000000001"


def document():
    protocol = ('{"identificationModule": {"nctId": "NCT00000001"}, '
                '"descriptionModule": {"briefSummary": "测试"}}')
    return {
        "schema_version": "datafabric.research.document.v1", "uuid": IDENTIFIER,
        "source_site": "clinicaltrials", "title": "Synthetic BCMA myeloma study",
        "source_url": "https://clinicaltrials.gov/study/NCT00000001",
        "publish_date_raw": "2020-01", "observed_at": "2026-10-16T00:00:00Z",
        "document_sha256": hashlib.sha256(("prefix" + protocol).encode()).hexdigest(),
        "projection": {
            "version": "clinicaltrials.protocol.v1", "status": "available", "reason": None,
            "text": protocol, "locator": {"field": "raw_content", "offset_unit": "unicode_codepoint",
                                          "start": 6, "end": 6 + len(protocol)},
            "facts": {"nct_id": "NCT00000001", "brief_summary": "测试"},
            "field_paths": {key: "/protocolSection/" + path for key, path in PROTOCOL_PATHS.items()},
        },
    }


def reader(handler):
    return DataFabricResearchReader(
        httpx.Client(base_url="http://data-fabric.test", transport=httpx.MockTransport(handler)),
        source_instance="datafabric-test", authorization_ref="development:development:sources",
        allowed_source_sites={"clinicaltrials"},
    )


def test_selected_protocol_can_be_read_back_without_becoming_text_mining_input():
    source = document()
    source_reader = reader(lambda request: httpx.Response(200, json=source))
    selected = source_reader.read_document(IDENTIFIER)
    first = source_reader.snapshot_from_documents([selected]).records[0]
    second = source_reader.snapshot_from_documents([selected]).records[0]
    assert first.record_ref == second.record_ref
    assert first.normalized_text == source["projection"]["text"]
    assert is_structured_json(first.normalized_text)
    assert first.locator.end - first.locator.start == len(first.normalized_text)
    assert first.published_at is None and first.available_at is None
    assert not first.publication_time_verified
    changed = copy.deepcopy(source)
    changed["projection"]["text"] += " "
    changed["projection"]["locator"]["end"] += 1
    assert source_reader.snapshot_from_documents([changed]).records[0].record_ref != first.record_ref


def test_empty_search_is_a_successful_bounded_search_not_unavailable():
    def handle(request):
        assert request.method == "GET"
        assert request.url.params.get_list("terms") == ["BCMA", "myeloma"]
        return httpx.Response(200, json={
            "schema_version": "datafabric.research.search.v1", "source_site": "clinicaltrials",
            "scope": "title_literal_and_current_db", "terms": ["BCMA", "myeloma"],
            "limit": 6, "has_more": False, "items": [],
        })
    assert reader(handle).search(("BCMA", "myeloma"))["items"] == []
    for code in (302, 404, 503):
        with pytest.raises(DomainError):
            reader(lambda request: httpx.Response(code)).search(("BCMA",))


@pytest.mark.parametrize("change", [
    lambda value: value.update(source_site="other"),
    lambda value: value["projection"]["facts"].update(nct_id="NCT99999999"),
    lambda value: value["projection"]["facts"].update(overall_status="TERMINATED"),
    lambda value: value["projection"]["locator"].update(end=5),
    lambda value: value["projection"].update(text="[]"),
])
def test_source_identity_and_literal_proof_must_agree(change):
    source = document()
    change(source)
    with pytest.raises(DomainError, match="返回格式"):
        reader(lambda request: httpx.Response(200, json=source)).read_document(IDENTIFIER)


def test_unavailable_projection_is_not_adopted_as_empty_evidence():
    source = document()
    source["projection"] = {"version": "clinicaltrials.protocol.v1", "status": "unavailable",
                            "reason": "protocol_json_missing"}
    source_reader = reader(lambda request: httpx.Response(200, json=source))
    result = source_reader.read_document(IDENTIFIER)
    with pytest.raises(DomainError, match="不能采用"):
        source_reader.snapshot_from_documents([result])


def test_permission_limits_and_response_budget_apply_before_adoption():
    with pytest.raises(DomainError, match="许可"):
        DataFabricResearchReader(httpx.Client(base_url="http://data-fabric.test", trust_env=False),
                                 source_instance="datafabric-test", authorization_ref="grant",
                                 allowed_source_sites={"other"})
    source_reader = reader(lambda request: httpx.Response(200, content=b" " * 3_000_001))
    with pytest.raises(DomainError, match="大小上限"):
        source_reader.search(("BCMA",))
    source_reader.calls = 21
    with pytest.raises(DomainError, match="次数上限"):
        source_reader.search(("BCMA",))
    with pytest.raises(DomainError):
        source_reader.read_document("../sites")


def test_search_protocol_cannot_silently_ignore_the_requested_terms():
    result = {"schema_version": "datafabric.research.search.v1", "source_site": "clinicaltrials",
              "scope": "title_literal_and_current_db", "terms": [], "limit": 6,
              "has_more": False, "items": []}
    with pytest.raises(DomainError, match="返回格式"):
        reader(lambda request: httpx.Response(200, content=json.dumps(result))).search(("BCMA",))


def test_changed_document_is_rechecked_at_adoption():
    source_reader = reader(lambda request: httpx.Response(200, json=document()))
    changed = source_reader.read_document(IDENTIFIER)
    changed["source_site"] = "unlicensed"
    with pytest.raises(DomainError):
        source_reader.snapshot_from_documents([changed])
