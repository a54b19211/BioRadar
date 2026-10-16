import copy
import json
from pathlib import Path

import httpx
import pytest
from bioradar.adapters.data_fabric import DataFabricReader
from bioradar.adapters.exchange import ExchangeValidator
from bioradar.api.app import create_app
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from fastapi.testclient import TestClient
from pydantic import ValidationError

pytestmark = pytest.mark.BOOT
ROOT = Path(__file__).resolve().parents[1]


def test_unconfigured_service_never_claims_research_ready():
    with TestClient(create_app(Settings(environment="test"))) as client:
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["intelligence_ready"] is False
        status = client.get("/api/system/status").json()
        assert all(p["state"] == "not_connected" for p in status["providers"])
        response = client.post("/api/v1/investigations", json={"question": "test"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "capability_not_implemented"
        assert "X-Request-ID" in response.headers


def test_boot_cannot_silently_be_deployed_as_production():
    with pytest.raises(ValidationError):
        Settings(environment="production")
    with pytest.raises(ValidationError):
        Settings(queue_prefix="extraction")


def _exchange():
    examples = json.loads((ROOT / "contracts/provider-exchange.examples.json").read_text())
    rows = examples["examples"] if isinstance(examples, dict) else examples
    return copy.deepcopy(next(x for x in rows if x["capability"] == "answer.check"))


def _validate(document, **overrides):
    args = {
        "call_id": document["call_id"],
        "capability": document["capability"],
        "input_hash": document["request"]["input_hash"],
    }
    args.update(overrides)
    return ExchangeValidator(ROOT / "contracts").validate(document, **args)


def test_provider_timeout_cannot_be_empty_success():
    document = _exchange()
    document["execution"]["status"] = "unavailable"
    document["result"] = {"outcome": "available", "payload": document["result"]["payload"]}
    with pytest.raises(DomainError, match="供应方结果不符合"):
        _validate(document)
    document["result"] = {"outcome": "not_evaluated", "payload": None}
    assert _validate(document)["result"]["outcome"] == "not_evaluated"


def test_old_provider_reply_cannot_be_adopted_for_new_input():
    document = _exchange()
    with pytest.raises(DomainError, match="本次调用不匹配"):
        _validate(document, input_hash="different-scope-hash")
    assert _validate(document) == document


def test_provider_validation_does_not_echo_secret_input():
    document = _exchange()
    document["unexpected_secret"] = "DO-NOT-ECHO-THIS"
    with pytest.raises(DomainError) as exc:
        _validate(document)
    assert "DO-NOT-ECHO-THIS" not in str(exc.value)


def test_readonly_data_fabric_probe_cannot_be_used_as_evidence():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "stages": [
                    {"key": "extraction", "label": "抽取", "total": 3, "by_status": {"pending": 2, "done": 1}}
                ]
            },
        )

    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(handler)) as client:
        result = DataFabricReader(client).inspect_pipeline()
    assert requests == [("GET", "/api/agent/pipeline/overview")]
    assert result["adoptable_as_evidence"] is False


@pytest.mark.parametrize(
    "behavior,code",
    [
        ("timeout", "provider_timeout"),
        ("503", "provider_unavailable"),
        ("bad-json", "provider_protocol_error"),
        ("empty", "provider_protocol_error"),
    ],
)
def test_read_failure_never_fabricates_zero_statistics(behavior, code):
    def handler(request):
        if behavior == "timeout":
            raise httpx.ReadTimeout("private endpoint details", request=request)
        if behavior == "503":
            return httpx.Response(503, text="private database details")
        if behavior == "empty":
            return httpx.Response(200, json={"stages": []})
        return httpx.Response(200, text="not json")

    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as exc:
            DataFabricReader(client).inspect_pipeline()
    assert exc.value.code == code
    assert "private" not in str(exc.value)
