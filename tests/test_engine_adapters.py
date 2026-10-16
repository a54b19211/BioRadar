"""Synthetic boundary probes; no provider network or real intelligence fixtures."""

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from bioradar.adapters.data_fabric_snapshot import (
    OBSERVATION_FORMAT,
    DataFabricSnapshotReader,
    SnapshotReadLimits,
    load_data_fabric_observation,
    publication_precision,
)
from bioradar.adapters.provider_batch import normalize_provider_batch
from bioradar.api.errors import DomainError
from bioradar.research.mining.models import MiningPolicy, canonical_hash
from bioradar.research.mining.prepare import prepare, publication_interval, snapshot_hash

pytestmark = pytest.mark.ENGINE
ROOT = Path(__file__).resolve().parents[1]
UUID = "c96c1323-8e06-54ec-bdc5-37d9970f9d7f"
OTHER_UUID = "94e5822d-0722-5574-89ef-3b8dd0a60ce5"
GRANT = "development:development:sources"
OBSERVED_AT = "2026-10-14T01:00:00+00:00"
NARRATIVE = "仅用于测试的文本；Ａb。"


def _detail(doc_uuid=UUID, publish_date="2004-10"):
    return {
        "uuid": doc_uuid,
        "title": "TEST FIXTURE: not a real trial",
        "source_site": "clinicaltrials",
        "raw_content": "---\nsource_site: clinicaltrials\n---\n## 研究摘要\n\n"
        + NARRATIVE
        + '\n\n---\n\n## 原始数据\n{"designModule": {"country": "TEST", "facility": "TEST"}}',
        "publish_date": publish_date,
        "doc_status": "PENDING_REVIEW",
        "extraction_status": "pending",
        "doc_type": "clinical_trial",
        "parse_method": "api",
        "language": "zh",
        "attachments": [{"local_path": "/TEST-DO-NOT-READ", "file_url": "http://TEST-DO-NOT-FETCH"}],
    }


def _observation(detail=None):
    detail = detail or _detail()
    path = "/api/documents/" + detail["uuid"]
    call = {
        "call_id": "TEST-CALL",
        "method": "GET",
        "path": path,
        "input_hash": canonical_hash({"path": path, "params": {}}),
        "status_code": 200,
        "outcome": "available",
        "observed_at": OBSERVED_AT,
        "canonical_payload_sha256": canonical_hash(detail),
    }
    return {
        "sample_format": OBSERVATION_FORMAT,
        "source_instance": "UNTRUSTED-BODY-ALIAS",
        "finished_at": OBSERVED_AT,
        "documents": [
            {
                "document_uuid": detail["uuid"],
                "detail": detail,
                "detail_observation": call,
                "list_record": {
                    "uuid": detail["uuid"],
                    "source_site": "clinicaltrials",
                    "updated_at": OBSERVED_AT,
                },
            }
        ],
        "calls": [call],
    }


def _load(document, **overrides):
    kwargs = {
        "source_instance": "datafabric-test",
        "authorization_ref": GRANT,
        "allowed_source_sites": {"clinicaltrials"},
    }
    kwargs.update(overrides)
    return load_data_fabric_observation(document, **kwargs)


def _reader(client, **overrides):
    kwargs = {
        "source_instance": "datafabric-test",
        "authorization_ref": GRANT,
        "allowed_source_sites": {"clinicaltrials"},
    }
    kwargs.update(overrides)
    return DataFabricSnapshotReader(client, **kwargs)


def test_pending_summary_projection_preserves_raw_hash_and_distinct_offset_spaces():
    raw = _detail()
    snapshot = _load(_observation(raw))
    row = snapshot.records[0]
    assert row.normalized_text == NARRATIVE
    assert row.document_hash == hashlib.sha256(raw["raw_content"].encode()).hexdigest()
    assert row.normalized_text_hash == hashlib.sha256(NARRATIVE.encode()).hexdigest()
    assert row.normalized_text_hash != row.document_hash
    assert row.record_ref.content_hash == canonical_hash(raw)
    assert row.record_ref.version_source == "export_hash"
    assert row.record_ref.adopted_version != row.document_hash
    assert row.record_ref.authorization_ref == GRANT
    assert row.source_instance == snapshot.source_instance == "datafabric-test"
    assert raw["raw_content"][row.locator.start : row.locator.end] == row.normalized_text
    assert row.locator.start > 0
    assert row.normalization_version == "data-fabric.clinicaltrials.upstream-summary.slice.v1"
    assert row.upstream_status["extraction_status"] == "pending"
    assert row.upstream_status["provider_call_id"] == "TEST-CALL"
    assert row.available_at is None and row.first_seen_at is None
    assert row.published_at is None and row.published_time_raw == "2004-10"
    assert row.time_precision == "month" and not row.publication_time_verified
    assert publication_interval(row) is None
    assert row.canonical_entity_refs == () and row.study_cluster_ref is None
    assert row.source_independence_status == "unresolved"
    assert {r.rule: r.state for r in row.rule_readiness} == {
        "R1": "unsupported",
        "R2": "unsupported",
        "R3": "unsupported",
        "R4": "exploratory_only",
    }
    assert snapshot.coverage_mode == "bounded_sample"
    assert not snapshot.coverage[0].is_consistent_snapshot
    assert snapshot.coverage[0].total_reported is None
    assert "clinicaltrials_summary_only_not_full_report" in snapshot.metadata_gaps
    assert "designModule" not in row.normalized_text and "country" not in row.normalized_text
    # Body alias, attachments and local paths cannot influence the adopted scope.
    assert "TEST-DO-NOT" not in snapshot.model_dump_json()
    assert "UNTRUSTED-BODY-ALIAS" not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    "raw_text",
    [
        '{"descriptionModule":{"briefSummary":"TEST"},"designModule":{}}',
        "TEST prose without a verified upstream section",
        '## 研究摘要\n\n{"designModule": {}}\n\n## 原始数据\n{}',
    ],
)
def test_raw_json_or_unrecognized_wrapper_never_becomes_cleaned_text(raw_text):
    detail = _detail()
    detail["raw_content"] = raw_text
    row = _load(_observation(detail)).records[0]
    assert row.text_status == "unavailable" and row.normalized_text is None
    assert "cleaned_text" in row.missing_fields
    assert all(h.state == "unsupported" for h in row.rule_readiness)


@pytest.mark.parametrize(
    "raw,precision",
    [
        ("2026-10-14", "day"),
        ("2004-10", "month"),
        ("2004", "year"),
        ("2026-02-31", "unknown"),
        ("2026-13", "unknown"),
        ("", "unknown"),
        ("2026-10-14T01:00:00+00:00", "instant"),
        ("2026-10-14T01:00:00", "unknown"),
    ],
)
def test_date_syntax_does_not_establish_publication_time(raw, precision):
    assert publication_precision(raw) == precision
    row = _load(_observation(_detail(publish_date=raw))).records[0]
    assert row.time_precision == precision
    assert row.published_time_raw == raw
    assert row.published_at is None and not row.publication_time_verified
    assert publication_interval(row) is None


def test_export_tamper_and_old_call_are_not_adopted():
    package = _observation()
    package["documents"][0]["detail"]["raw_content"] = "TAMPERED"
    with pytest.raises(DomainError) as caught:
        _load(package)
    assert caught.value.code == "provider_content_mismatch"
    package = _observation()
    package["documents"][0]["detail_observation"]["input_hash"] = "OLD-CALL"
    with pytest.raises(DomainError) as caught:
        _load(package)
    assert caught.value.code == "provider_call_mismatch"


def test_scoped_export_refuses_different_document_and_source():
    package = _observation(_detail())
    package["documents"][0]["list_record"]["uuid"] = OTHER_UUID
    with pytest.raises(DomainError):
        _load(package)
    package = _observation(_detail())
    with pytest.raises(DomainError) as caught:
        _load(package, allowed_source_sites={"nmpa"})
    assert caught.value.code == "source_not_authorized"


def test_export_cannot_hide_failed_or_executing_get():
    package = _observation()
    package["calls"].append(
        {
            "method": "GET",
            "path": "/api/agent/merge/entity/1/stream",
            "outcome": "available",
            "status_code": 200,
        }
    )
    with pytest.raises(DomainError):
        _load(package)
    package = _observation()
    package["calls"].append(
        {
            "method": "GET",
            "path": "/api/documents/" + OTHER_UUID,
            "outcome": "provider_unavailable",
            "status_code": 503,
        }
    )
    with pytest.raises(DomainError) as caught:
        _load(package)
    assert caught.value.code == "provider_incomplete"


def test_reader_only_gets_explicit_docs_without_redirects_or_attachments():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=_detail())

    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(handler)) as client:
        snapshot = _reader(client).read_snapshot([UUID])
    assert len(snapshot.records) == 1
    assert requests == [("GET", "/api/documents/" + UUID)]


def test_reader_overrides_client_redirect_setting():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://external/agent/stream"})

    with httpx.Client(
        base_url="http://fixture", follow_redirects=True, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(DomainError) as caught:
            _reader(client).read_snapshot([UUID])
    assert caught.value.code == "provider_unavailable"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "base_url", ["http://fixture/prefix", "http://fixture/?x=1", "http://fixture/#fragment"]
)
def test_reader_refuses_base_url_rewriting_of_allowlisted_paths(base_url):
    with httpx.Client(base_url=base_url, trust_env=False) as client:
        with pytest.raises(DomainError) as caught:
            _reader(client)
    assert caught.value.code == "invalid_provider_configuration"


@pytest.mark.parametrize("status", [404, 429, 503])
def test_http_failures_are_not_empty_success_or_retraction(status):
    with httpx.Client(
        base_url="http://fixture",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"secret": "DO-NOT-ECHO"})),
    ) as client:
        with pytest.raises(DomainError) as caught:
            _reader(client).read_snapshot([UUID])
    assert caught.value.code == "provider_unavailable"
    assert "DO-NOT-ECHO" not in str(caught.value)


def test_timeout_and_size_limits_are_explicit_failure():
    def timed_out(request):
        raise httpx.ReadTimeout("DO-NOT-ECHO", request=request)

    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(timed_out)) as client:
        with pytest.raises(DomainError) as caught:
            _reader(client).read_snapshot([UUID])
    assert caught.value.code == "provider_timeout"
    with httpx.Client(
        base_url="http://fixture",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_detail())),
    ) as client:
        with pytest.raises(DomainError) as caught:
            _reader(client, limits=SnapshotReadLimits(max_response_bytes=20)).read_snapshot([UUID])
    assert caught.value.code == "provider_response_too_large"


@pytest.mark.parametrize("manifest", [[], ["sites"], ["../agent/merge/entity/1/stream"], [UUID, UUID]])
def test_manifest_is_validated_before_network(manifest):
    requests = []
    with httpx.Client(
        base_url="http://fixture", transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as client:
        with pytest.raises(DomainError):
            _reader(client).read_snapshot(manifest)
    assert requests == []


def _provider_packet():
    # Reuse the frozen *shape*, never the conflicting fictional data.batch rows.
    examples = json.loads((ROOT / "contracts/provider-exchange.examples.json").read_text())["examples"]
    packet = copy.deepcopy(next(e for e in examples if e["capability"] == "data.batch"))
    payload = packet["result"]["payload"]
    row = payload["analysis_records"][0]
    text = "TEST FIXTURE: only text for boundary validation."
    quote = "only text for boundary validation"
    document_ref = dict(
        row["record_ref"],
        object_type="document",
        object_id="TEST-DOCUMENT",
        adopted_version="TEST-VERSION",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    evidence_ref = dict(document_ref, object_type="evidence", object_id="TEST-EVIDENCE")
    row.update(
        {
            "record_ref": copy.deepcopy(document_ref),
            "evidence_refs": [copy.deepcopy(evidence_ref)],
            "source_id": "fixture-public",
            "document_hash": hashlib.sha256(text.encode()).hexdigest(),
            "normalized_text": text,
            "text_status": "inline_authorized",
            "locator": {"type": "offset", "start": 0, "end": len(text)},
            "published_at": "2026-10-01T00:00:00Z",
            "published_time_raw": "2026-10-01",
            "time_precision": "day",
            "available_at": None,
            "first_seen_at": None,
        }
    )
    evidence = payload["records"][0]
    evidence.update(
        {
            "ref": copy.deepcopy(evidence_ref),
            "source_record": copy.deepcopy(document_ref),
            "content_hash": row["document_hash"],
            "quote": quote,
            "quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
            "locator": {"type": "paragraph", "paragraph_id": "TEST-PARAGRAPH"},
        }
    )
    payload["analysis_records"] = [row]
    payload["change_events"] = []
    return packet


def _batch(packet, **overrides):
    kwargs = {
        "expected_call_id": "fictional-data.batch",
        "expected_input_hash": "fictional-input",
        "authorization": packet["request"]["authorization"],
        "source_instance": "fixture",
        "allowed_source_ids": {"fixture-public"},
        "contract_dir": ROOT / "contracts",
        "allow_demo": True,
        "received_at": datetime.fromisoformat(OBSERVED_AT),
    }
    kwargs.update(overrides)
    return normalize_provider_batch(packet, **kwargs)


def test_provider_batch_retains_contract_refs_and_verified_publication_semantics():
    packet = _provider_packet()
    snapshot = _batch(packet)
    row = snapshot.records[0]
    assert row.record_ref.model_dump() == packet["result"]["payload"]["analysis_records"][0]["record_ref"]
    assert row.evidence_refs[0].object_id == "TEST-EVIDENCE"
    assert row.publication_time_verified and publication_interval(row) is not None
    assert row.normalized_text_hash == hashlib.sha256(row.normalized_text.encode()).hexdigest()
    assert row.available_at is None
    assert snapshot.is_demo and row.ingestion_mode == "fixture"
    assert snapshot.coverage_mode == "manual_package"


def test_provider_batch_checks_call_and_server_grant_and_fixture_permission():
    packet = _provider_packet()
    for overrides, code in [
        ({"expected_input_hash": "OLD-INPUT"}, "provider_call_mismatch"),
        (
            {"authorization": dict(packet["request"]["authorization"], workspace_id="OTHER")},
            "provider_authorization_mismatch",
        ),
        ({"allow_demo": False}, "fixture_not_allowed"),
        ({"allowed_source_ids": {"not-authorized"}}, "source_not_authorized"),
    ]:
        with pytest.raises(DomainError) as caught:
            _batch(packet, **overrides)
        assert caught.value.code == code


@pytest.mark.parametrize("status", ["partial", "unavailable", "failed", "unknown"])
def test_provider_noncompletion_cannot_be_empty_success(status):
    packet = _provider_packet()
    packet["execution"]["status"] = status
    if status != "partial":
        packet["result"] = {"outcome": "not_evaluated", "payload": None}
    with pytest.raises(DomainError) as caught:
        _batch(packet)
    assert caught.value.code == "provider_incomplete"


def test_provider_duplicate_conflict_and_quote_hash_are_semantic_failures():
    packet = _provider_packet()
    rows = packet["result"]["payload"]["analysis_records"]
    rows.append(dict(rows[0], normalized_text="DIFFERENT CONTENT SAME VERSION"))
    with pytest.raises(DomainError) as caught:
        _batch(packet)
    assert caught.value.code == "provider_version_conflict"
    packet = _provider_packet()
    packet["result"]["payload"]["records"][0]["quote"] += " TAMPERED"
    with pytest.raises(DomainError) as caught:
        _batch(packet)
    assert caught.value.code == "provider_quote_hash_mismatch"


def test_provider_join_checks_evidence_and_source_versions_not_titles_or_order():
    for field, code in [
        ("ref", "provider_evidence_unavailable"),
        ("source_record", "provider_evidence_mismatch"),
    ]:
        packet = _provider_packet()
        packet["result"]["payload"]["records"][0][field]["adopted_version"] = "OLD-VERSION"
        with pytest.raises(DomainError) as caught:
            _batch(packet)
        assert caught.value.code == code


def test_provider_duplicate_identical_row_does_not_add_independent_units():
    packet = _provider_packet()
    original = _batch(packet)
    rows = packet["result"]["payload"]["analysis_records"]
    rows.append(copy.deepcopy(rows[0]))
    repeated = _batch(packet)
    assert len(repeated.records) == 1
    assert snapshot_hash(original) == snapshot_hash(repeated)


def test_frozen_contract_example_conflict_is_not_silently_accepted():
    examples = json.loads((ROOT / "contracts/provider-exchange.examples.json").read_text())["examples"]
    packet = copy.deepcopy(next(e for e in examples if e["capability"] == "data.batch"))
    with pytest.raises(DomainError):
        _batch(packet, allowed_source_ids={"fictional-source"})


def _change(packet, kind):
    payload = packet["result"]["payload"]
    payload["change_events"] = [
        {
            "event_id": "TEST-CHANGE",
            "kind": kind,
            "subject": copy.deepcopy(payload["analysis_records"][0]["record_ref"]),
            "previous_ref": None,
            "effective_at": None,
            "recorded_at": "2026-09-30T00:00:00Z",
            "reason": "TEST FIXTURE",
        }
    ]


@pytest.mark.parametrize("kind", ["permission_changed", "identity_corrected", "withdrawn", "deleted"])
def test_source_changes_are_not_silently_ignored(kind):
    packet = _provider_packet()
    _change(packet, kind)
    with pytest.raises(DomainError) as caught:
        _batch(packet)
    assert caught.value.code == "provider_change_requires_reconciliation"


@pytest.mark.parametrize("kind,flag", [("withdrawn", "retracted"), ("deleted", "deleted")])
def test_bound_tombstone_requires_visibility_and_preserves_impact_gap(kind, flag):
    packet = _provider_packet()
    _change(packet, kind)
    row = packet["result"]["payload"]["analysis_records"][0]
    row["record_flags"] = [flag]
    with pytest.raises(DomainError) as caught:
        _batch(packet)
    assert caught.value.code == "provider_change_requires_reconciliation"
    row["available_at"] = "2026-10-01T00:00:00Z"
    frozen = _batch(packet)
    assert frozen.records[0].record_flags == (flag,)
    assert "provider_source_change_requires_business_impact_reconciliation" in frozen.metadata_gaps
    assert "provider_change_events_not_consumed_as_cdc" in frozen.metadata_gaps


@pytest.mark.parametrize("kind", ["created", "revised"])
def test_created_and_revised_import_do_not_claim_cdc_consumption(kind):
    packet = _provider_packet()
    _change(packet, kind)
    frozen = _batch(packet)
    assert "provider_change_events_not_consumed_as_cdc" in frozen.metadata_gaps
    assert frozen.records[0].upstream_status["provider_change_event_count"] == "1"


def test_provider_receipt_without_evidence_observation_can_be_frozen_for_replay():
    packet = _provider_packet()
    packet["result"]["payload"]["records"] = []
    packet["result"]["payload"]["analysis_records"][0]["evidence_refs"] = []
    receipt = datetime.fromisoformat(OBSERVED_AT)
    first = _batch(packet, received_at=receipt)
    second = _batch(packet, received_at=receipt)
    assert snapshot_hash(first) == snapshot_hash(second)
    assert first.records[0].observed_at == receipt
    assert first.records[0].available_at is None
    assert "provider_observation_missing_using_local_received_at" in first.metadata_gaps


def test_provider_early_observation_never_backdates_local_receipt_for_replay():
    packet = _provider_packet()
    payload = packet["result"]["payload"]
    evidence = payload["records"][0]
    evidence["observed_at"] = "2019-10-01T00:00:00Z"
    row = payload["analysis_records"][0]
    row.update(
        published_at="2019-09-01T00:00:00Z",
        published_time_raw="2019-09-01",
        available_at="2019-10-01T00:00:00Z",
    )
    receipt = datetime.fromisoformat(OBSERVED_AT)
    captured = _batch(packet, received_at=receipt)
    adopted = captured.records[0]
    assert captured.observed_at == adopted.observed_at == receipt
    assert json.loads(adopted.upstream_status["provider_evidence_observed_at_by_ref"]) == {
        canonical_hash(evidence["ref"]): evidence["observed_at"]
    }
    replay = prepare(
        captured,
        MiningPolicy(
            as_of=datetime.fromisoformat("2020-01-01T00:00:00+00:00"), time_mode="availability_replay"
        ),
    )
    assert replay.records == ()
    assert [excluded.reason for excluded in replay.excluded_records] == [
        "version_not_observed_by_replay_cutoff"
    ]


def test_provider_snapshot_identity_includes_trusted_local_receipt():
    packet = _provider_packet()
    captured = _batch(packet, received_at=datetime.fromisoformat(OBSERVED_AT))
    recaptured = _batch(packet, received_at=datetime.fromisoformat("2026-10-15T01:00:00+00:00"))
    assert captured.snapshot_id != recaptured.snapshot_id
    assert snapshot_hash(captured) != snapshot_hash(recaptured)
    assert recaptured.observed_at == recaptured.records[0].observed_at
