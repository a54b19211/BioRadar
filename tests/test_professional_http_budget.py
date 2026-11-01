"""Professional HTTP attempts share the existing tool budget; all peers are synthetic."""
import copy
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations import analysis, professional
from pydantic import SecretStr

pytestmark = pytest.mark.ENGINE


class Harness:
    def __init__(self, monkeypatch, *, limit=2, capabilities=False, drop_first=False):
        importlib.import_module("bioradar.adapters.biocheck")
        importlib.import_module("bioradar.adapters.mech_evidence")
        self.settings = SimpleNamespace(
            biocheck_transport="http", biocheck_service_url="http://synthetic.invalid",
            biocheck_service_token=SecretStr("synthetic-service-token"),
            professional_http_timeout_seconds=5, professional_max_steps=8,
            professional_http_max_bytes=100000,
        )
        self.fixed = {
            "budget": {"max_tool_calls": limit},
            "providers": {"biocheck": {
                "transport": "http", "service_endpoint": self.settings.biocheck_service_url,
            }},
        }
        self.progress = {
            "queries": [], "calls": [], "active_professional_action": "synthetic-action",
            "provider_versions": {} if capabilities else {"biocheck": "v1"},
        }
        self.wire = []
        self.snapshots = []
        self.expired = False
        self.stop_on_sent = False
        self.crash_on_sent = False
        self.drop_first = drop_first
        original = httpx.Client

        def handler(request):
            self.wire.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json={
                    "schema_version": "bioradar.professional.v1", "provider": "biocheck",
                    "provider_version": "v1", "operations": ["quality_feedback", "revise"],
                })
            if self.drop_first and self.wire.count("POST") == 1:
                raise httpx.ReadTimeout("SYNTHETIC unknown HTTP response", request=request)
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "schema_version": "bioradar.professional.v1", "provider": "biocheck",
                "provider_version": "v1", "operation_id": body["operation_id"],
                "status": "completed", "result": {"qualification": "SYNTHETIC no-model result"},
            })

        monkeypatch.setattr(
            professional.httpx, "Client",
            lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
        )
        monkeypatch.setattr(
            analysis, "analysis_budget",
            lambda *args: {"usage": {"remaining_seconds": 0 if self.expired else 100}},
        )

    def _guard(self, *args, check_deadline=False):
        if check_deadline and self.expired:
            raise DomainError("analysis_time_budget", "SYNTHETIC deadline", 409)

    def _checkpoint(self, *args, **kwargs):
        self.snapshots.append(copy.deepcopy(self.progress))
        sent = any(
            x.get("status") == "sent"
            for state in self.progress.get("professional_operations", {}).values()
            for x in state.get("exchanges", [])
        )
        if sent and self.stop_on_sent:
            self.expired = True
        if sent and self.crash_on_sent:
            self.crash_on_sent = False
            raise RuntimeError("SYNTHETIC crash after durable send intent")

    def _use_provider(self, identifier, token, fixed, progress, name, version):
        progress["provider_versions"][name] = version

    def core(self):
        return professional.load_provider(
            self, "synthetic-analysis", "unused-lock-token", self.fixed, self.progress, "biocheck"
        )[0]

    def usage(self):
        return self.progress["tool_usage"]


def test_received_http_replay_has_no_new_attempt_or_budget_charge(monkeypatch):
    host = Harness(monkeypatch, limit=1)
    first = host.core().quality_feedback({})
    assert host.wire == ["POST"]
    assert host.usage()["tool_calls"] == 1
    assert host.usage()["professional_reserved_tool_calls"] == 0
    host.expired = True
    host.fixed["budget"]["max_tool_calls"] = 0
    assert host.core().quality_feedback({}) == first
    assert host.wire == ["POST"]
    assert host.usage()["tool_calls"] == 1
    state = next(iter(host.progress["professional_operations"].values()))
    assert state["action_id"] == "synthetic-action"
    assert state["actual_cost"] == {"tool_calls": 1, "known_tool_calls": 1, "reserved_tool_calls": 0}


def test_new_http_operation_cannot_exceed_shared_tool_budget(monkeypatch):
    host = Harness(monkeypatch, limit=1)
    core = host.core()
    core.quality_feedback({})
    with pytest.raises(DomainError, match="预算") as error:
        core.revise({}, {}, lambda *args: {})
    assert error.value.code == "analysis_tool_budget"
    assert host.wire == ["POST"]
    assert host.usage()["tool_calls"] == 1


def test_post_checkpoint_crossing_deadline_is_not_sent_and_costs_zero(monkeypatch):
    host = Harness(monkeypatch, limit=1)
    host.stop_on_sent = True
    with pytest.raises(DomainError) as error:
        host.core().quality_feedback({})
    assert error.value.code == "analysis_time_budget"
    assert host.wire == []
    assert host.usage()["tool_calls"] == 0
    assert host.usage()["professional_reserved_tool_calls"] == 0
    exchange = next(iter(host.progress["professional_operations"].values()))["exchanges"][0]
    assert exchange["status"] == "not_sent" and exchange["attempts"] == 0


def test_each_real_pure_http_retry_counts_once(monkeypatch):
    host = Harness(monkeypatch, limit=2, drop_first=True)
    host.core().quality_feedback({})
    assert host.wire == ["POST", "POST"]
    assert host.usage()["tool_calls"] == 2
    assert host.usage()["professional_reserved_tool_calls"] == 0
    host.core().quality_feedback({})
    assert host.usage()["tool_calls"] == 2 and len(host.wire) == 2


def test_retry_cannot_use_a_third_or_unbudgeted_request(monkeypatch):
    host = Harness(monkeypatch, limit=1, drop_first=True)
    with pytest.raises(DomainError) as error:
        host.core().quality_feedback({})
    assert error.value.code == "analysis_tool_budget"
    assert host.wire == ["POST"]
    assert host.usage()["tool_calls"] == 1


def test_crashed_send_intent_stays_reserved_on_restore(monkeypatch):
    host = Harness(monkeypatch, limit=1)
    host.crash_on_sent = True
    with pytest.raises(RuntimeError):
        host.core().quality_feedback({})
    assert host.wire == []
    host.progress = copy.deepcopy(host.snapshots[-1])
    with pytest.raises(DomainError) as error:
        host.core().quality_feedback({})
    assert error.value.code == "analysis_tool_budget"
    assert host.wire == []
    assert host.usage()["tool_calls"] == 0
    assert host.usage()["professional_reserved_tool_calls"] == 1
    state = next(iter(host.progress["professional_operations"].values()))
    assert state["actual_cost"]["tool_calls"] is None


def test_runtime_capability_get_is_counted_and_replayed(monkeypatch):
    host = Harness(monkeypatch, limit=2, capabilities=True)
    host.core().quality_feedback({})
    assert host.wire == ["GET", "POST"]
    assert host.usage()["tool_calls"] == 2
    host.core().quality_feedback({})
    assert host.wire == ["GET", "POST"]
    assert host.usage()["tool_calls"] == 2


def test_capability_get_checkpoint_deadline_is_not_sent(monkeypatch):
    host = Harness(monkeypatch, limit=1, capabilities=True)
    host.stop_on_sent = True
    with pytest.raises(DomainError) as error:
        host.core()
    assert error.value.code == "analysis_time_budget"
    assert host.wire == [] and host.usage()["tool_calls"] == 0
    assert host.usage()["professional_reserved_tool_calls"] == 0
