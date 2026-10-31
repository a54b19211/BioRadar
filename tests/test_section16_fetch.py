"""Isolated demo fetch uses the same fixed-run protocol and never targets live by default."""

import httpx
import pytest
from bioradar.adapters.fabric_fetch import normalize_fetch_request, submit_fetch
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError


def test_demo_fetch_requires_bound_isolated_reader_and_preserves_fixed_package():
    posted = []

    def transport(request):
        posted.append(request)
        return httpx.Response(
            202,
            json={
                "id": "SYNTHETIC-run",
                "source_id": "external_archive",
                "is_demo": True,
                "kind": "fetch",
                "state": "queued",
                "version_ids": [],
                "coverage": {},
                "fetch_mode": "simulated",
                "demo_instance_id": "SYNTHETIC-instance",
                "input_sha256": "a" * 64,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(transport))
    request = {"source_id": "external_archive", "query": "Orion"}
    settings = Settings(
        environment="test",
        data_fabric_url="http://127.0.0.1:19900",
        data_fabric_history_fetch_token="SYNTHETIC",
    )
    assert (
        submit_fetch(settings, request, call_key="SYNTHETIC", is_demo=True, client=client)["status"]
        == "unavailable"
    )
    assert not posted
    settings = settings.model_copy(
        update={
            "data_fabric_demo_fetch_url": settings.data_fabric_url,
            "data_fabric_demo_fetch_instance_id": "SYNTHETIC-instance",
        }
    )
    reply = submit_fetch(settings, request, call_key="SYNTHETIC", is_demo=True, client=client)
    assert reply["status"] == "pending" and reply["run"]["fetch_mode"] == "simulated"
    assert reply["run"]["input_sha256"] == "a" * 64 and len(posted) == 1
    assert b'"is_demo":true' in posted[0].content


def test_other_instance_receipt_is_unknown_not_success():
    def transport(request):
        return httpx.Response(
            202,
            json={
                "id": "SYNTHETIC-run",
                "source_id": "external_archive",
                "is_demo": True,
                "kind": "fetch",
                "state": "queued",
                "version_ids": [],
                "coverage": {},
                "fetch_mode": "simulated",
                "demo_instance_id": "wrong-instance",
                "input_sha256": "a" * 64,
            },
        )

    settings = Settings(
        environment="test",
        data_fabric_url="http://127.0.0.1:19900",
        data_fabric_demo_fetch_url="http://127.0.0.1:19900",
        data_fabric_demo_fetch_instance_id="SYNTHETIC-instance",
        data_fabric_history_fetch_token="SYNTHETIC",
    )
    reply = submit_fetch(
        settings,
        {"source_id": "external_archive", "query": "Orion"},
        call_key="SYNTHETIC",
        is_demo=True,
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    assert reply["status"] == "unconfirmed" and reply["usage"]["request_status"] == "unknown"


def test_demo_time_filters_are_rejected_and_live_wire_stays_unchanged():
    with pytest.raises(DomainError):
        normalize_fetch_request(
            {
                "source_id": "external_archive",
                "query": "*",
                "is_demo": True,
                "demo_instance_id": "SYNTHETIC-instance",
                "updated_since": "2026-01-01T00:00:00Z",
            }
        )
    assert "is_demo" not in normalize_fetch_request({"source_id": "pubmed", "query": "Orion"})
