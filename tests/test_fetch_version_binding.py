"""SYNTHETIC fixed-run binding regressions; no live source, database or model."""

import copy
from datetime import UTC, datetime

import httpx
import pytest
from bioradar.adapters import fabric_fetch
from bioradar.api.errors import DomainError
from bioradar.research.investigations import fetch_execution
from bioradar.research.investigations.analysis_service import AnalysisService
from test_opus_source_adapters import Host, fetch_action, fetch_fixed, original, run

pytestmark = pytest.mark.ENGINE
CUTOFF = datetime(2026, 10, 31, 12, tzinfo=UTC)


def records(*, demo=False):
    source = "external_archive" if demo else "pubmed"
    first, second = (
        original(source, demo=demo),
        original(source, demo=demo, text="SYNTHETIC second fixed original."),
    )
    first.update(source_record_id="SYNTHETIC-native-9", version_id="a" * 64)
    second.update(source_record_id="SYNTHETIC-native-2", version_id="b" * 64)
    return [first, second]


class NativeContract:
    def __init__(self, *, paged=False, items=None, demo=False):
        self.originals = records(demo=demo)
        self.items = copy.deepcopy(self.originals) if items is None else items
        self.actual_run = run("completed", [item["version_id"] for item in self.originals])
        if demo:
            self.actual_run.update(
                source_id="external_archive",
                is_demo=True,
                fetch_mode="simulated",
                demo_instance_id="SYNTHETIC-instance",
                input_sha256="d" * 64,
            )
        self.paged = paged
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/api/history/fetch"
            return httpx.Response(200, json=self.actual_run)
        assert request.headers["authorization"] == "Bearer SYNTHETIC-read"
        assert request.url.params["source_id"] == self.actual_run["source_id"]
        assert request.url.params["is_demo"] == str(self.actual_run["is_demo"]).lower()
        assert request.url.params["mode"] == "current"
        assert request.url.params["cutoff"]
        if request.url.path == "/api/history/records":
            assert request.url.params["run_id"] == self.actual_run["id"]
            ordered = list(reversed(self.items))
            cursor = request.url.params.get("cursor")
            page = ordered[:1] if self.paged and not cursor else ordered[1:] if cursor else ordered
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.history.collection.v1",
                    "items": page,
                    "next_cursor": "SYNTHETIC-next" if self.paged and not cursor else None,
                    "coverage": {
                        "run_id": self.actual_run["id"],
                        "as_of": request.url.params["cutoff"],
                        "max_seq": 2,
                        "complete": True,
                    },
                    "excluded_counts": {},
                },
            )
        version = request.url.path.rsplit("/", 1)[-1]
        item = next(item for item in self.originals if item["version_id"] == version)
        if "source_record_id" not in request.url.params:
            return httpx.Response(
                422, json={"detail": [{"loc": ["query", "source_record_id"], "type": "missing"}]}
            )
        assert request.url.params["source_record_id"] == item["source_record_id"]
        return httpx.Response(200, json=item)


class BindingHost(Host):
    _policy_budget = staticmethod(AnalysisService._policy_budget)
    _tool_start = AnalysisService._tool_start
    _tool_finish = AnalysisService._tool_finish

    def __init__(self, client, *, stop_after_binding=False, demo=False):
        super().__init__(client)
        self.stop_after_binding = stop_after_binding
        self.demo = demo
        if demo:
            self.settings.environment = "test"
            self.settings.data_fabric_demo_fetch_url = self.settings.data_fabric_url
            self.settings.data_fabric_demo_fetch_instance_id = "SYNTHETIC-instance"
            self.reader.demo_source_instance = "SYNTHETIC-instance"

    def _checkpoint(self, identifier, token, progress, **kwargs):
        super()._checkpoint(identifier, token, progress, **kwargs)
        if self.stop_after_binding and progress["queries"]:
            state = progress["queries"][-1].get("usage", {}).get("fetch", {})
            if state.get("binding_intent", {}).get("status") == "received":
                self.stop_after_binding = False
                raise SystemExit("SYNTHETIC exit after received binding checkpoint")

    def _adopt_original(self, identifier, token, fixed, progress, item, tag):
        return {
            "key": "E-" + item["source_record_id"],
            "record_ref": self.reader.ref(item).model_dump(mode="json"),
        }


def execute(host, monkeypatch, client, progress=None):
    submit = fabric_fetch.submit_fetch
    monkeypatch.setattr(fetch_execution, "submit_fetch", lambda *a, **kw: submit(*a, **kw, client=client))
    monkeypatch.setattr(fetch_execution, "_seconds", lambda f, p: 60)
    fixed = fetch_fixed()
    progress = {"queries": [], "evidence": [], "calls": []} if progress is None else progress
    if host.demo:
        fixed.update(is_demo=True, search_scope=["external_archive"])
        fixed["providers"]["fabric_fetch"]["demo_instance_id"] = "SYNTHETIC-instance"
    action = fetch_action(fixed)
    action["arguments"]["scope"]["is_demo"] = host.demo
    action["arguments"]["parameters"]["source_id"] = fixed["search_scope"][0]
    receipt = fetch_execution.execute_source_fetch(
        host, "SYNTHETIC-analysis", "SYNTHETIC-lease", fixed, progress, action, 0
    )
    return fixed, progress, receipt


def client_for(contract):
    return httpx.Client(
        base_url="http://synthetic.invalid",
        headers={"Authorization": "Bearer SYNTHETIC-read"},
        transport=httpx.MockTransport(contract),
    )


@pytest.mark.parametrize("demo", [False, True])
def test_completed_run_binds_native_id_once_and_charges_each_actual_page(monkeypatch, demo):
    contract = NativeContract(paged=True, demo=demo)
    with client_for(contract) as client:
        host = BindingHost(client, demo=demo)
        _, progress, receipt = execute(host, monkeypatch, client)
        assert receipt["status"] == "read"
        assert receipt["returned"] == receipt["new_versions"] == 2
        assert receipt["records"] == records(demo=demo)
        assert receipt["cost"]["tool_calls"] == progress["tool_usage"]["tool_calls"] == 5
        state = receipt["usage"]["fetch"]
        assert state["binding_intent"]["status"] == "received"
        assert state["version_bindings"]["bindings"]["a" * 64]["source_record_id"] == "SYNTHETIC-native-9"
        assert len([r for r in contract.requests if r.url.path == "/api/history/records"]) == 2
        before = len(contract.requests)
        execute(host, monkeypatch, client, progress)
        assert len(contract.requests) == before


def test_received_binding_checkpoint_resumes_without_collect_or_fetch(monkeypatch):
    contract = NativeContract(paged=True)
    with client_for(contract) as client:
        host = BindingHost(client, stop_after_binding=True)
        with pytest.raises(SystemExit, match="received binding"):
            execute(host, monkeypatch, client)
        saved = copy.deepcopy(host.checkpoints[-1])
        assert saved["queries"][0]["usage"]["fetch"]["binding_intent"]["status"] == "received"
        before = len(contract.requests)
        resumed = BindingHost(client)
        _, progress, receipt = execute(resumed, monkeypatch, client, saved)
        calls = contract.requests[before:]
        assert len(calls) == 2 and all("/versions/" in r.url.path for r in calls)
        assert all(r.method == "GET" for r in calls)
        assert receipt["returned"] == 2
        assert progress["tool_usage"]["tool_calls"] == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "OTHER"),
        ("source_id", "europe_pmc"),
        ("is_demo", True),
        ("mode", "operational_replay"),
        ("cutoff", "2026-10-31T11:00:00+00:00"),
    ],
)
def test_saved_binding_scope_cannot_change(field, value):
    contract = NativeContract()
    with client_for(contract) as client:
        host = BindingHost(client)
        bindings = fabric_fetch.collect_fetch_version_bindings(
            host.reader, contract.actual_run, cutoff=CUTOFF
        )
        bindings[field] = value
        count = len(contract.requests)
        with pytest.raises(DomainError) as caught:
            fabric_fetch.validate_fetch_version_bindings(contract.actual_run, bindings, cutoff=CUTOFF)
        assert caught.value.code == "history_fetch_binding_invalid"
        assert len(contract.requests) == count


@pytest.mark.parametrize("case", ["new_head", "ambiguous"])
def test_missing_or_ambiguous_exact_version_is_not_rebound(monkeypatch, case):
    items = records()
    if case == "new_head":
        items[0]["version_id"] = "c" * 64
    else:
        duplicate = copy.deepcopy(items[0])
        duplicate["source_record_id"] = "SYNTHETIC-other-native"
        items.append(duplicate)
    contract = NativeContract(items=items)
    with client_for(contract) as client:
        host = BindingHost(client)
        with pytest.raises(DomainError) as caught:
            execute(host, monkeypatch, client)
        assert caught.value.code == (
            "history_fetch_binding_missing" if case == "new_head" else "history_fetch_binding_ambiguous"
        )
        assert not any("/versions/" in r.url.path for r in contract.requests)
        saved = host.checkpoints[-1]["queries"][0]
        assert saved["status"] == "failed"
        if case == "new_head":
            assert saved["usage"]["fetch"]["version_bindings"]["missing_version_ids"] == ["a" * 64]


def test_revoked_source_is_rejected_before_binding_http(monkeypatch):
    contract = NativeContract()
    with client_for(contract) as client:
        host = BindingHost(client)
        host.revoke = True
        with pytest.raises(DomainError) as caught:
            execute(host, monkeypatch, client)
        assert caught.value.code == "source_not_authorized"
        assert contract.requests == []


def test_legacy_received_original_does_not_need_to_remain_current_head(monkeypatch):
    contract = NativeContract()
    with client_for(contract) as client:
        host = BindingHost(client)
        checkpoint = host._checkpoint

        def exit_after_first_original(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            if progress["queries"] and len(progress["queries"][0]["records"]) == 1:
                raise SystemExit("SYNTHETIC received original checkpoint")

        host._checkpoint = exit_after_first_original
        with pytest.raises(SystemExit, match="received original"):
            execute(host, monkeypatch, client)
        saved = copy.deepcopy(host.checkpoints[-1])
        prior = copy.deepcopy(saved["queries"][0]["records"][0])
        state = saved["queries"][0]["usage"]["fetch"]
        state.pop("version_bindings")
        state.pop("binding_intent")
        contract.items[0]["version_id"] = "c" * 64
        before = len(contract.requests)
        _, progress, receipt = execute(BindingHost(client), monkeypatch, client, saved)
        new_calls = contract.requests[before:]
        assert len(new_calls) == 2
        assert new_calls[0].url.path == "/api/history/records"
        assert new_calls[1].url.path.endswith("b" * 64)
        assert receipt["records"][0] == prior
        assert receipt["usage"]["fetch"]["version_bindings"]["requested_version_ids"] == ["b" * 64]
        assert receipt["returned"] == 2
        assert progress["tool_usage"]["tool_calls"] == 5


def test_unknown_binding_intent_never_recollects_or_resubmits(monkeypatch):
    contract = NativeContract()
    with client_for(contract) as client:
        host = BindingHost(client)
        checkpoint = host._checkpoint

        def exit_on_binding_intent(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            if progress["queries"]:
                state = progress["queries"][0]["usage"]["fetch"]
                if state.get("binding_intent", {}).get("status") == "sent":
                    raise SystemExit("SYNTHETIC binding outcome unknown")

        host._checkpoint = exit_on_binding_intent
        with pytest.raises(SystemExit, match="binding outcome unknown"):
            execute(host, monkeypatch, client)
        saved = copy.deepcopy(host.checkpoints[-1])
        assert saved["queries"][0]["cost"]["tool_calls"] is None
        assert saved["queries"][0]["cost"]["known_tool_calls"] == 1
        assert saved["queries"][0]["cost"]["reserved_tool_calls"] == 63
        before = len(contract.requests)
        _, _, receipt = execute(BindingHost(client), monkeypatch, client, saved)
        assert receipt["status"] == "unconfirmed"
        assert len(contract.requests) == before


def test_received_binding_does_not_bypass_later_revocation(monkeypatch):
    contract = NativeContract()
    with client_for(contract) as client:
        host = BindingHost(client, stop_after_binding=True)
        with pytest.raises(SystemExit):
            execute(host, monkeypatch, client)
        saved = copy.deepcopy(host.checkpoints[-1])
        resumed = BindingHost(client)
        resumed.revoke = True
        before = len(contract.requests)
        with pytest.raises(DomainError) as caught:
            execute(resumed, monkeypatch, client, saved)
        assert caught.value.code == "source_not_authorized"
        assert len(contract.requests) == before


def test_point_read_rejects_different_native_identity_even_with_same_version():
    item = records()[0]
    binding = {k: item[k] for k in ("source_id", "source_record_id", "version_id", "is_demo")}
    changed = {**item, "source_record_id": "SYNTHETIC-foreign-native"}
    calls = []
    with httpx.Client(
        base_url="http://synthetic.invalid",
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=changed)),
    ) as client:
        host = BindingHost(client)
        with pytest.raises(DomainError):
            fabric_fetch.read_fetch_version(
                host.reader,
                run("completed", [item["version_id"]]),
                item["version_id"],
                cutoff=CUTOFF,
                binding=binding,
            )
        assert len(calls) == 1
        assert calls[0].url.params["source_record_id"] == binding["source_record_id"]


@pytest.mark.parametrize(
    "field,value", [("run_id", "SYNTHETIC-other-run"), ("as_of", "2026-10-31T11:00:00+00:00")]
)
def test_collection_coverage_must_match_requested_run_and_cutoff(monkeypatch, field, value):
    contract = NativeContract()

    def respond(request):
        response = contract(request)
        if request.url.path == "/api/history/records":
            body = response.json()
            body["coverage"][field] = value
            return httpx.Response(200, json=body)
        return response

    with client_for(respond) as client:
        host = BindingHost(client)
        with pytest.raises(DomainError) as caught:
            execute(host, monkeypatch, client)
        assert caught.value.code == "history_fetch_binding_invalid"
        assert not any("/versions/" in r.url.path for r in contract.requests)
