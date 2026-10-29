"""SYNTHETIC identity fixtures; real HTTP encoding and Fabric reader boundaries."""

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters import weknora
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError

pytestmark = pytest.mark.ENGINE

KNOWLEDGE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
KB_ID = "synthetic-kb"
URL = "https://www.chictr.org.cn/showproj.html?proj=123456"
CUTOFF = datetime(2026, 10, 30, tzinfo=UTC)
PRIVATE = "PRIVATE SOURCE BODY AND CONTACT"


def settings():
    return Settings(
        weknora_url="http://127.0.0.1:8080",
        weknora_api_key="SYNTHETIC-NOT-A-REAL-KEY",
        weknora_knowledge_base_ids=[KB_ID],
    )


def original(*, version="a" * 64, identifier="fixed-uuid", url=URL):
    return {
        "source_id": "legacy_documents",
        "source_record_id": "legacy:chictr:" + identifier,
        "version_id": version,
        "raw_sha256": "b" * 64,
        "is_demo": False,
        "facts": {"source_site": "chictr"},
        "source_url": url,
        "raw_content": "FIXED FABRIC SOURCE",
    }


def selected(record=None):
    record = record or original()
    binding = {
        name: record[name]
        for name in ("source_id", "source_record_id", "version_id", "raw_sha256", "is_demo")
    }
    return {
        "source_id": record["source_id"],
        "source_url": record["source_url"],
        "title": "TITLE IS NEVER AN IDENTITY",
        "facts": copy.deepcopy(record["facts"]),
        "history_binding": binding,
        "record_ref": {
            "provider": "data_fabric_history",
            "object_id": json.dumps([record["source_id"], record["source_record_id"], record["is_demo"]]),
            "adopted_version": record["version_id"],
            "content_hash": record["raw_sha256"],
        },
        "text_sha256": "c" * 64,
        "text": "Selected normalized passage, not a raw hash",
    }


class Reader:
    def __init__(self, record=None):
        self.original = record or original()
        self.records_read = 0
        self.max_records = 10
        self.reads = []
        self.queries = []
        self.error = None

    def _get(self, path, params):
        self.queries.append((path, params))
        return {
            "schema_version": "datafabric.history.collection.v1",
            "items": [copy.deepcopy(self.original)],
        }

    def read_version(self, binding, *, mode, cutoff):
        self.reads.append((copy.deepcopy(binding), mode, cutoff))
        if self.error:
            raise self.error
        return copy.deepcopy(self.original)

    def ref(self, record):
        return SimpleNamespace(model_dump=lambda **_: selected(record)["record_ref"])


def candidate(*, metadata=None):
    return {
        "id": "synthetic-chunk",
        "knowledge_id": KNOWLEDGE_ID,
        "knowledge_base_id": KB_ID,
        "knowledge_title": "TITLE IS NEVER AN IDENTITY",
        "content": PRIVATE,
        "start_at": 9999,
        "metadata": metadata if metadata is not None else {"content": PRIVATE},
    }


def detail(*, header=None):
    header = header if header is not None else f"---\nsource_site: chictr\nsource_url: {URL}\n---\n"
    return {
        "success": True,
        "data": {
            "id": KNOWLEDGE_ID,
            "knowledge_base_id": KB_ID,
            "enable_status": "enabled",
            "deleted_at": None,
            "metadata": {"content": header + PRIVATE, "version": "1"},
            "custom_metadata": {},
        },
    }


def search(item=None, *, knowledge=None, evidence=None, reader=None, source_ids=None, handler=None):
    calls = []
    item = item if item is not None else candidate()
    knowledge = knowledge if knowledge is not None else detail()
    evidence = evidence if evidence is not None else [selected()]
    reader = reader or Reader()

    def transport(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.headers["X-API-Key"] == "SYNTHETIC-NOT-A-REAL-KEY"
        if handler:
            return handler(request)
        if request.method == "POST":
            return httpx.Response(200, json={"success": True, "data": [item]})
        assert request.method == "GET"
        assert request.url.path == "/api/v1/knowledge/" + KNOWLEDGE_ID
        return httpx.Response(200, json=knowledge)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = weknora.search_candidates(
            settings(),
            "SYNTHETIC QUERY",
            reader=reader,
            source_ids=source_ids if source_ids is not None else ["legacy_documents"],
            selected_evidence=evidence,
            cutoff=CUTOFF,
            client=client,
        )
    return result, reader, calls


def test_old_knowledge_header_selects_only_the_previously_fixed_fabric_original():
    evidence = selected()
    before = copy.deepcopy(evidence)
    result, reader, calls = search(evidence=[evidence])
    binding = result["candidates"][0]
    assert binding["binding_status"] == "bound"
    assert binding["binding_reason"] == "selected_original_by_url"
    assert binding["search_version_verified"] is False
    assert binding["history_binding"]["version_id"] == evidence["history_binding"]["version_id"]
    assert result["records"] == [reader.original]
    expected_request = {
        key: value for key, value in evidence["history_binding"].items() if key != "raw_sha256"
    }
    assert reader.reads == [(expected_request, "current", CUTOFF)]
    assert not reader.queries
    assert result["usage"]["knowledge_reads"] == 1
    assert result["usage"]["version_reads"] == 1
    assert result["usage"]["lookup_requests"] == 0
    assert reader.records_read == 1
    assert [r.method for r in calls] == ["POST", "GET"]
    assert evidence == before
    assert PRIVATE not in json.dumps(result)
    assert "start_at" not in binding and "content" not in binding


def test_fixed_original_multiple_passages_do_not_create_false_identity_ambiguity():
    evidence = selected()
    second = copy.deepcopy(evidence)
    second["text"] = "another passage"
    result, reader, _ = search(evidence=[evidence, second])
    assert result["candidates"][0]["binding_status"] == "bound"
    assert len(reader.reads) == 1


@pytest.mark.parametrize("evidence", [[], [selected(original(url=URL + "&extra=1"))]])
def test_no_selected_exact_full_url_never_uses_a_similar_title(evidence):
    result, reader, _ = search(evidence=evidence)
    assert result["candidates"][0]["binding_reason"] == "selected_original_not_found"
    assert not result["records"] and not reader.reads and not reader.queries


def test_two_fixed_versions_with_the_same_source_url_are_ambiguous():
    result, reader, _ = search(evidence=[selected(), selected(original(version="d" * 64))])
    assert result["candidates"][0]["binding_reason"] == "selected_original_not_unique"
    assert not result["records"] and not reader.reads


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e["history_binding"].pop("raw_sha256"),
        lambda e: e["record_ref"].update(adopted_version="d" * 64),
        lambda e: e["record_ref"].update(content_hash="c" * 64),
        lambda e: e["record_ref"].update(object_id='["legacy_documents","another",false]'),
        lambda e: e["history_binding"].update(is_demo=True),
        lambda e: e["history_binding"].update(source_record_id="legacy:pubmed:fixed-uuid"),
    ],
)
def test_malformed_or_conflicting_selected_identity_is_not_repaired_by_guessing(change):
    evidence = selected()
    change(evidence)
    result, reader, _ = search(evidence=[evidence])
    assert result["candidates"][0]["binding_reason"] == "selected_identity_invalid"
    assert not result["records"] and not reader.reads


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "external_archive"},
        {"source_record_id": "legacy:chictr:other"},
        {"version_id": "d" * 64},
        {"raw_sha256": "c" * 64},
        {"source_url": URL + "&different=1"},
        {"facts": {"source_site": "pubmed"}},
        {"facts": {}},
        {"is_demo": True},
    ],
)
def test_returned_fixed_version_must_match_identity_url_site_and_raw_hash(changes):
    reader = Reader()
    reader.original.update(changes)
    result, reader, _ = search(reader=reader)
    assert not result["records"]
    assert result["candidates"][0]["binding_status"] == "unbound"
    assert len(reader.reads) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_metadata": "{not JSON"},
        {"source_site": "chictr", "source_metadata": {"source_site": "pubmed"}},
        {"source_metadata": {"source_site": "chictr"}},
        {"version_id": ""},
        {"version_id": None},
        {"version_id": "invalid"},
        {"raw_sha256": "invalid"},
        {"source_id": "legacy_documents"},
        {"source_uuid": "declared-uuid"},
        {"source_id": "legacy_documents", "source_record_id": "legacy:chictr:one", "source_uuid": "two"},
    ],
)
def test_invalid_or_conflicting_declared_metadata_never_enters_url_fallback(metadata):
    item = candidate(metadata=metadata)
    if metadata == {"source_metadata": {"source_site": "chictr"}}:
        item["source_metadata"] = {"source_site": "pubmed"}
    result, reader, calls = search(item)
    assert result["candidates"][0]["binding_reason"] == "source_metadata_invalid"
    assert len(calls) == 1 and not reader.reads and not result["records"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_site": "pubmed"},
        {"source_url": URL + "&different=1"},
        {"version_id": "d" * 64},
        {"raw_sha256": "c" * 64},
    ],
)
def test_explicit_search_identity_cannot_be_overridden_by_the_detail(metadata):
    result, reader, calls = search(candidate(metadata=metadata))
    assert result["candidates"][0]["binding_status"] == "unbound"
    assert len(calls) == 2 and not reader.reads and not result["records"]


@pytest.mark.parametrize(
    "header",
    [
        f"source_site: chictr\nsource_url: {URL}\n",
        f"---\nsource_site: chictr\nsource_url: {URL}\n",
        f"---\nsource_site: chictr\nsource_site: chictr\nsource_url: {URL}\n---\n",
        f"---\nsource_site: [chictr]\nsource_url: {URL}\n---\n",
        "---\nsource_site: chictr\nsource_url: null\n---\n",
        f"---\nsource_site: chictr\nsource_url: {URL}\nversion_id: invalid\n---\n",
        "---\ntitle: ordinary document\n---\n" + f"source_site: chictr\nsource_url: {URL}\n",
        "---\n" + "title: " + "x" * 8192 + f"\nsource_site: chictr\nsource_url: {URL}\n---\n",
    ],
)
def test_only_bounded_complete_plain_frontmatter_can_declare_identity(header):
    result, reader, _ = search(knowledge=detail(header=header))
    assert result["candidates"][0]["binding_status"] == "unbound"
    assert not reader.reads and not result["records"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"),
        ("knowledge_base_id", "unselected-kb"),
        ("enable_status", "disabled"),
        ("deleted_at", "2026-10-01T00:00:00Z"),
    ],
)
def test_detail_must_be_the_requested_enabled_undeleted_knowledge_in_the_same_kb(field, value):
    knowledge = detail()
    knowledge["data"][field] = value
    result, reader, _ = search(knowledge=knowledge)
    assert result["candidates"][0]["binding_status"] == "unbound"
    assert not reader.reads and not result["records"]


@pytest.mark.parametrize(
    "changes",
    [
        {"knowledge_id": "../../private"},
        {"knowledge_id": "https://evil.invalid/doc"},
        {"knowledge_id": KNOWLEDGE_ID + "?redirect=evil"},
        {"knowledge_base_id": "unselected-kb"},
        {"knowledge_base_id": None},
    ],
)
def test_detail_address_is_never_taken_from_candidate_url_or_unsafe_id(changes):
    item = candidate()
    item.update(changes)
    item["url"] = "http://evil.invalid/private"
    result, reader, calls = search(item)
    assert len(calls) == 1
    assert not result["records"] and not reader.reads


def test_declared_custom_metadata_conflict_does_not_fall_back_to_the_header():
    knowledge = detail()
    knowledge["data"]["custom_metadata"] = {"source_site": "pubmed"}
    result, reader, _ = search(knowledge=knowledge)
    assert result["candidates"][0]["binding_status"] == "unbound"
    assert not result["records"] and not reader.reads


def test_legacy_wrapper_selection_is_sufficient_but_fixed_version_permission_is_still_required():
    reader = Reader()
    reader.error = DomainError("history_source_not_accessible", "Origin source revoked", 403)
    result, reader, _ = search(reader=reader)
    assert result["candidates"][0]["binding_reason"] == "history_source_not_accessible"
    assert not result["records"] and len(reader.reads) == 1


def test_missing_wrapper_selection_never_reads_an_original():
    result, reader, _ = search(source_ids=["chictr"])
    assert result["candidates"][0]["binding_status"] == "not_authorized"
    assert not result["records"] and not reader.reads


def test_url_binding_cannot_bypass_fabric_record_budget():
    reader = Reader()
    reader.max_records = 0
    result, reader, _ = search(reader=reader)
    assert result["candidates"][0]["binding_reason"] == "history_read_budget"
    assert not result["records"] and not reader.reads


def test_complete_direct_metadata_keeps_exact_version_path_without_detail_fallback():
    metadata = {
        "source_id": "legacy_documents",
        "source_record_id": original()["source_record_id"],
        "version_id": "a" * 64,
        "raw_sha256": "b" * 64,
    }
    result, reader, calls = search(candidate(metadata=metadata))
    assert result["candidates"][0]["binding_reason"] == "exact_fabric_version"
    assert result["candidates"][0]["search_version_verified"] is True
    assert len(calls) == 1 and len(reader.reads) == 1
    assert result["usage"]["knowledge_reads"] == 0


@pytest.mark.parametrize("kind", ["timeout", "redirect", "http", "invalid"])
def test_detail_failures_remain_visible_candidates_without_leaking_private_responses(kind):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"success": True, "data": [candidate()]})
        if kind == "timeout":
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "http://evil.invalid/private"})
        if kind == "http":
            return httpx.Response(403, text=PRIVATE)
        return httpx.Response(200, text=PRIVATE)

    result, reader, calls = search(handler=handler)
    assert result["status"] == "completed"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["binding_status"] in {"failed", "unbound"}
    assert result["usage"]["knowledge_reads"] == 1
    assert not result["records"] and not reader.reads and len(calls) == 2
    assert PRIVATE not in json.dumps(result)


def test_search_and_all_details_share_one_byte_budget(monkeypatch):
    first = candidate()
    one = json.dumps({"success": True, "data": [first]}).encode()
    two = json.dumps(detail()).encode()
    monkeypatch.setattr(weknora, "MAX_RESPONSE_BYTES", len(one) + len(two) - 1)

    def handler(request):
        return httpx.Response(200, content=one if request.method == "POST" else two)

    result, reader, calls = search(handler=handler)
    assert result["candidates"][0]["binding_reason"] == "weknora_response_budget"
    assert len(calls) == 2 and result["usage"]["knowledge_reads"] == 1
    assert not result["records"] and not reader.reads


def test_search_and_details_share_remaining_overall_seconds(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(weknora.time, "monotonic", lambda: now[0])

    def handler(request):
        if request.method == "POST":
            assert request.extensions["timeout"]["read"] == 20
            now[0] += 17
            return httpx.Response(200, json={"success": True, "data": [candidate()]})
        assert request.extensions["timeout"]["read"] == 3
        now[0] += 4
        return httpx.Response(200, json=detail())

    result, reader, _ = search(handler=handler)
    assert result["candidates"][0]["binding_reason"] == "weknora_response_budget"
    assert not result["records"] and not reader.reads


def test_at_most_three_returned_candidates_get_details_and_read_fixed_versions():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"success": True, "data": [candidate()] * 4})
        return httpx.Response(200, json=detail())

    result, reader, _ = search(handler=handler)
    assert result["returned"] == 4 and result["truncated"] is True
    assert result["usage"]["knowledge_reads"] == 3
    assert result["usage"]["version_reads"] == 3
    assert len(requests) == 4 and len(result["candidates"]) == 3
    assert len(result["records"]) == 1


def test_nonlegacy_url_binding_also_rechecks_the_actual_origin_site():
    record = original(url="https://clinicaltrials.gov/study/NCT00000001")
    record.update(
        source_id="clinicaltrials", source_record_id="NCT00000001", facts={"source_site": "clinicaltrials"}
    )
    evidence = selected(record)
    reader = Reader(copy.deepcopy(record))
    reader.original["facts"] = {"source_site": "pubmed"}
    header = f"---\nsource_site: clinicaltrials\nsource_url: {record['source_url']}\n---\n"
    result, reader, _ = search(
        evidence=[evidence], reader=reader, source_ids=["clinicaltrials"], knowledge=detail(header=header)
    )
    assert result["candidates"][0]["binding_reason"] == "source_identity_mismatch"
    assert not result["records"] and len(reader.reads) == 1


def test_direct_metadata_cannot_adopt_a_knowledge_base_outside_the_configuration():
    item = candidate(
        metadata={
            "source_id": "legacy_documents",
            "source_record_id": original()["source_record_id"],
            "version_id": "a" * 64,
        }
    )
    item["knowledge_base_id"] = "unselected-kb"
    result, reader, calls = search(item)
    assert result["candidates"][0]["binding_reason"] == "knowledge_base_not_selected"
    assert not result["records"] and not reader.reads and len(calls) == 1


@pytest.mark.parametrize(
    "declaration",
    [
        '{"source_site":"pubmed","source_site":"chictr"}',
        '{"source_site":"chictr","source_site":"chictr"}',
    ],
)
def test_source_metadata_duplicate_json_keys_are_not_silently_overwritten(declaration):
    result, reader, calls = search(candidate(metadata={"source_metadata": declaration}))
    assert result["candidates"][0]["binding_reason"] == "source_metadata_invalid"
    assert not result["records"] and not reader.reads and len(calls) == 1
