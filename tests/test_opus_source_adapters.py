"""Synthetic HTTP and fixed original regressions; these tests never use a live model or source."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters import fabric_fetch
from bioradar.adapters.biocheck import build_packet, load_biocheck
from bioradar.adapters.fabric_document import context_at, project_signal_context, validate_document_evidence
from bioradar.adapters.fabric_history import FabricHistoryReader, validate_record
from bioradar.adapters.mech_evidence import _case, _source_kind
from bioradar.adapters.reasoning_model import ModelStage
from bioradar.adapters.weknora_kdd import features_for_snapshot, retrieval_scope
from bioradar.api.errors import DomainError
from bioradar.research.investigations import fetch_execution
from bioradar.research.investigations.analysis import source_excerpt
from bioradar.research.investigations.query_policy import build_read_manifest, semantic_key
from pydantic import SecretStr, TypeAdapter
from test_opus_research_inputs import evidence, fixed
from test_weknora_kdd import feature_response, fixed_snapshot
from test_weknora_kdd import settings as weknora_settings

pytestmark = pytest.mark.ENGINE
NOW = datetime(2026, 10, 31, tzinfo=UTC)


def original(source="pubmed", *, demo=False, text="SYNTHETIC original."):
    raw = json.dumps({"full_text": text}, ensure_ascii=False)
    return {
        "schema_version": "datafabric.history.record.v1",
        "record_kind": "document",
        "source_id": source,
        "source_record_id": "SYNTHETIC-1",
        "version_id": "1" * 64,
        "raw_content": raw,
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "raw_media_type": "application/json",
        "clean_text": text,
        "parser_version": "SYNTHETIC-v1",
        "facts": {},
        "field_locators": {},
        "is_demo": demo,
        "first_seen_at": {"value": NOW.isoformat(), "precision": "instant"},
        "received_at": {"value": NOW.isoformat(), "precision": "instant"},
    }


def doc_context(kind="social_media"):
    return {
        "schema_version": "datafabric.document.evidence.v1",
        "material_kind": kind,
        "text_level": "full_text",
        "statement_status": "unverified_report",
        "source_independence": {"status": "same_origin", "origin_group_id": "SYNTHETIC-origin"},
        "document_relations": [],
        "information_events": [],
    }


def test_document_contract_uses_unicode_and_never_invents_independence_or_target_version():
    record = original("external_archive", demo=True, text="原文声称有人删帖。尾段为待核更正。")
    record["document_evidence"] = doc_context()
    record["document_evidence"]["document_relations"] = [
        {
            "relation_type": "corrects",
            "target_source_id": "external_archive",
            "target_source_record_id": "SYNTHETIC-old",
            "target_version_id": None,
            "basis_locator": {"start": 10, "end": 17, "quote": record["clean_text"][10:17]},
        }
    ]
    validate_record(record, "external_archive", True, "current", NOW)
    reader = FabricHistoryReader(
        httpx.Client(base_url="http://synthetic.invalid"), authorization_ref="SYNTHETIC"
    )
    signal = project_signal_context(reader, record, known_records=[record])
    assert signal.verification_status == "unverified"
    assert signal.source_relations == ()
    assert signal.document_evidence["document_relations"][0]["target_version_id"] is None
    wrong = copy.deepcopy(record)
    wrong["document_evidence"]["document_relations"][0]["basis_locator"]["start"] += 1
    with pytest.raises(DomainError) as exc:
        validate_document_evidence(wrong)
    assert exc.value.code == "history_document_invalid"
    wrong = copy.deepcopy(record)
    wrong["document_evidence"]["source_independence"]["status"] = "independent_confirmed"
    with pytest.raises(DomainError):
        validate_document_evidence(wrong)
    wrong = copy.deepcopy(record)
    wrong["clean_text"] = "原文标题"
    with pytest.raises(DomainError):
        validate_document_evidence(wrong)
    reader.client.close()


def test_posthoc_censorship_label_is_not_historical_source_environment():
    text = "SYNTHETIC original reports an uncertain cluster."
    value = doc_context()
    value["information_events"] = [
        {
            "event_type": "reported_deletion",
            "description": "SYNTHETIC later dataset label",
            "reported_at": {"value": "2026-10-31", "precision": "day"},
            "basis_locator": {"json_pointer": "/censorship_type"},
        }
    ]
    filtered = context_at(
        value, mode="public_history_reconstruction", cutoff="2019-12-31T23:59:59+00:00", text=text
    )
    assert filtered["information_events"] == []
    assert value["information_events"]
    value["information_events"][0]["reported_at"] = None
    value["information_events"][0]["basis_locator"] = {"quote": "uncertain cluster"}
    assert context_at(
        value, mode="public_history_reconstruction", cutoff="2019-12-31T23:59:59+00:00", text=text
    )["information_events"]


def test_tail_read_is_exact_and_zero_window_and_record_order_have_same_identity():
    record = original("external_archive", demo=True, text="前" * 6500 + "待核更正段。")
    first = source_excerpt(record, 10000)
    tail = source_excerpt(record, 10000, start=first["end"])
    assert first["end"] == 6000 and tail["text"] == record["clean_text"][6000:]
    assert tail["text_sha256"] == first["text_sha256"]
    assert tail["source_length"] == len(record["clean_text"])
    with pytest.raises(DomainError):
        source_excerpt(record, 10000, start=len(record["clean_text"]) + 1)
    args = {
        "provider": "fabric",
        "source_id": "external_archive",
        "records": [
            {"source_record_id": "second", "version_id": "2" * 64},
            {"source_record_id": "first", "version_id": "1" * 64, "window_start": 0},
        ],
    }
    other = copy.deepcopy(args)
    other["records"].reverse()
    other["records"][0].pop("window_start")
    assert semantic_key("document_read", args) == semantic_key("document_read", other)
    other["records"][0]["window_start"] = 6000
    assert semantic_key("document_read", args) != semantic_key("document_read", other)
    e = evidence()
    e.update(source_length=8000, end=6000)
    p = {"evidence": [e], "queries": []}
    manifest = build_read_manifest(fixed(), p)
    assert manifest[0]["window_start"] == 6000 and manifest[0]["status"] == "unread"
    p["evidence"].append({**e, "start": 6000, "end": 8000})
    assert build_read_manifest(fixed(), p)[0]["status"] == "read"


@pytest.mark.parametrize("marked", [True, False, None])
def test_demo_weknora_uses_only_demo_kb_and_requires_true_per_feature(marked):
    snapshot = fixed_snapshot()
    record = snapshot.records[0]
    reference = record.record_ref.model_copy(
        update={
            "object_id": json.dumps(["synthetic_source", "synthetic-native-id", True]),
        }
    )
    record = record.model_copy(update={"record_ref": reference, "ingestion_mode": "fixture"})
    snapshot = snapshot.model_copy(update={"is_demo": True, "records": (record,)})
    cfg = weknora_settings()
    cfg.weknora_kdd_demo_knowledge_base_id = "SYNTHETIC-demo-kb"
    cfg.weknora_kdd_demo_source_scope = "simulation"
    reply = feature_response(snapshot)
    reply["features"][0]["source_scope"] = "simulation"
    if marked is not None:
        reply["features"][0]["is_demo"] = marked
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if marked is True:
            enriched, receipt = features_for_snapshot(cfg, snapshot, direction="business", client=client)
            assert receipt["adopted"] and enriched.records[0].embedding is not None
        else:
            with pytest.raises(DomainError) as exc:
                features_for_snapshot(cfg, snapshot, direction="business", client=client)
            assert exc.value.code == "weknora_feature_identity"
    assert requests[0]["knowledge_base_id"] == "SYNTHETIC-demo-kb"
    assert requests[0]["source_scope"] == "simulation" and requests[0]["is_demo"] is True
    cfg.weknora_kdd_demo_knowledge_base_id = None
    with pytest.raises(DomainError) as exc:
        retrieval_scope(cfg, direction="research", is_demo=True)
    assert exc.value.code == "weknora_kdd_not_configured"


def test_document_classification_and_pan_context_never_promote_news_or_registration_results():
    item = {"source_context": doc_context("news_report"), "source_kind": "primary"}
    assert _source_kind(item, "external_archive", "SYNTHETIC")[0] is None
    item["source_context"]["material_kind"] = "review"
    assert _source_kind(item, "pubmed", "SYNTHETIC")[0] == "review"
    item["source_context"]["material_kind"] = "trial_registry"
    packet = {
        "question": "SYNTHETIC",
        "mechanism": "待核机制",
        "evidence": [
            {
                **item,
                "id": "E1",
                "text": "SYNTHETIC registration, no results.",
                "version": "1" * 64,
                "is_demo": True,
            }
        ],
    }
    case, missing, _ = _case(packet)
    assert not missing
    assert case["materials"][0]["document_type"] == "trial_registration"
    assert case["materials"][0]["is_demo"] is True
    e = evidence()
    e["source_context"] = doc_context()
    built = build_packet("SYNTHETIC rumor", [e], assessment={"source_updates": []})
    assert built["evidence"][0]["source_kind"] == "social_media"
    assert built["evidence"][0]["verification_status"] == "unverified"
    assert built["evidence"][0]["text_level"] == "selected_passages"
    assert TypeAdapter(ModelStage).validate_python("read_plan_repair") == "read_plan_repair"
    assert TypeAdapter(ModelStage).validate_python("narrative_extract") == "narrative_extract"


def run(state="queued", versions=None):
    return {
        "id": "SYNTHETIC-run",
        "source_id": "pubmed",
        "is_demo": False,
        "kind": "fetch",
        "state": state,
        "version_ids": versions or [],
        "coverage": {"complete": state == "completed"},
    }


def fetch_fixed():
    f = fixed("research")
    f.update(
        is_demo=False,
        search_scope=["pubmed"],
        dependency_sources=[],
        providers={"fabric_fetch": {"available": True}},
        execution_version="query-step-v1",
    )
    f["budget"].update(max_queries=16, max_tool_calls=64)
    return f


def fetch_action(f):
    return {
        "id": "SYNTHETIC-fetch-action",
        "kind": "source_fetch",
        "arguments": {
            "capability_id": "fabric.history.fetch",
            "capability_version": fabric_fetch.FETCH_VERSION,
            "input_refs": [],
            "scope": {
                "direction": "research",
                "mode": "current",
                "as_of": f["evidence_time"]["cutoff"],
                "is_demo": False,
            },
            "parameters": {"source_id": "pubmed", "query": "SYNTHETIC"},
        },
    }


class Host:
    def __init__(self, client):
        self.settings = SimpleNamespace(
            data_fabric_url="http://synthetic.invalid", data_fabric_history_fetch_token=SecretStr("SYNTHETIC")
        )
        self.reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC-test")
        self.checkpoints = []
        self.expired = False
        self.revoke = False

    def _checkpoint(self, identifier, token, progress, **kwargs):
        self.checkpoints.append(copy.deepcopy(progress))

    def _authorize_query_sources(self, fixed, action):
        assert action["arguments"]["source_id"] in fixed["search_scope"]
        if self.revoke:
            raise DomainError("source_not_authorized", "SYNTHETIC revoked", 403)

    def _guard(self, identifier, token, **kwargs):
        if self.expired:
            raise DomainError("analysis_time_budget", "SYNTHETIC expired", 409)

    def _tool_start(self, fixed, progress):
        return self.reader.requests_made

    def _tool_finish(self, progress, before, *, extra=0):
        count = self.reader.requests_made - before + extra
        usage = progress.setdefault("tool_usage", {})
        usage["tool_calls"] = usage.get("tool_calls", 0) + count
        return count

    def _adopt_original(self, identifier, token, fixed, progress, original, tag):
        return {"key": "E-fetched", "record_ref": self.reader.ref(original).model_dump(mode="json")}

    def _source_instance(self, demo):
        return "SYNTHETIC-instance"


def test_fetch_known_pending_run_resumes_get_only_and_counts_actual_requests(monkeypatch):
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(202, json=run())
        if "/runs/" in request.url.path:
            return httpx.Response(200, json=run("completed", ["1" * 64]))
        return httpx.Response(200, json={"record": original()})

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        host = Host(client)
        original_submit = fabric_fetch.submit_fetch
        monkeypatch.setattr(
            fetch_execution, "submit_fetch", lambda *a, **kw: original_submit(*a, **kw, client=client)
        )
        monkeypatch.setattr(fetch_execution, "_seconds", lambda f, p: 60)
        f, p = fetch_fixed(), {"queries": [], "evidence": []}
        action = fetch_action(f)
        with pytest.raises(DomainError) as exc:
            fetch_execution.execute_source_fetch(
                host, "SYNTHETIC-analysis", "SYNTHETIC-lease", f, p, action, 0
            )
        assert exc.value.code == "analysis_fetch_pending"
        assert p["queries"][0]["usage"]["fetch"]["run_id"] == "SYNTHETIC-run"
        # Simulate a worker restart from the durable checkpoint, not shared Python objects.
        p = copy.deepcopy(host.checkpoints[-1])
        result = fetch_execution.execute_source_fetch(
            host, "SYNTHETIC-analysis", "SYNTHETIC-lease", f, p, action, 0
        )
        assert result["status"] == "read" and result["new_versions"] == 1
        assert result["cost"]["tool_calls"] == p["tool_usage"]["tool_calls"] == 3
        assert result["usage"]["fetch"]["run"]["coverage"]["complete"] is True
        fetch_execution.execute_source_fetch(host, "SYNTHETIC-analysis", "SYNTHETIC-lease", f, p, action, 0)
    assert [method for method, _ in calls] == ["POST", "GET", "GET"]


def test_fetch_unknown_post_is_never_resent_and_demo_or_history_never_post(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("SYNTHETIC timeout", request=request)

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        host = Host(client)
        original_submit = fabric_fetch.submit_fetch
        monkeypatch.setattr(
            fetch_execution, "submit_fetch", lambda *a, **kw: original_submit(*a, **kw, client=client)
        )
        monkeypatch.setattr(fetch_execution, "_seconds", lambda f, p: 60)
        f, p = fetch_fixed(), {"queries": [], "evidence": []}
        action = fetch_action(f)
        first = fetch_execution.execute_source_fetch(host, "a", "lease", f, p, action, 0)
        assert first["status"] == "unconfirmed" and first["cost"]["tool_calls"] == 1
        fetch_execution.execute_source_fetch(host, "a", "lease", f, p, action, 0)
        for demo, mode in [(True, "current"), (False, "public_history_reconstruction")]:
            value = fabric_fetch.submit_fetch(
                host.settings,
                {"source_id": "pubmed", "query": "SYNTHETIC"},
                call_key="never",
                is_demo=demo,
                mode=mode,
                client=client,
            )
            assert value["tool_requests"] == 0 and value["status"] == "unavailable"
    assert calls == ["POST"]


def test_fetch_checkpoint_crossing_deadline_persists_not_sent_and_restart_zero_post(monkeypatch):
    calls = []
    with httpx.Client(
        base_url="http://synthetic.invalid",
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(202, json=run())
        ),
    ) as client:
        host = Host(client)
        checkpoint = host._checkpoint

        def expire_on_intent(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            if progress["queries"] and progress["queries"][0]["status"] == "sent":
                host.expired = True

        host._checkpoint = expire_on_intent
        monkeypatch.setattr(fetch_execution, "_seconds", lambda f, p: 60)
        f, p = fetch_fixed(), {"queries": [], "evidence": []}
        action = fetch_action(f)
        with pytest.raises(DomainError) as exc:
            fetch_execution.execute_source_fetch(host, "a", "lease", f, p, action, 0)
        assert exc.value.code == "analysis_time_budget"
        assert p["queries"][0]["usage"]["request_status"] == "not_sent"
        assert p["queries"][0]["cost"]["tool_calls"] == 0
        fetch_execution.execute_source_fetch(host, "a", "lease", f, p, action, 0)
    assert calls == []


def test_local_biocheck_loader_includes_actual_optional_source_context_without_model_calls():
    from pathlib import Path

    directory = Path("/home/zk_test/zk_projects/.bioradar-kdd-integration/biocheck")
    if not (directory / "app/source_context.py").is_file():
        pytest.skip("native optional pure core is not installed")
    core, version = load_biocheck(SimpleNamespace(biocheck_project_dir=directory))
    assert core is not None and len(version) == 64
    packet = build_packet("SYNTHETIC claim", [evidence()])
    assert core.packet_input(packet)["evidence"][0]["text"] == evidence()["text"]


def test_topic_unsupported_demo_keeps_read_path_and_received_rpc_needs_input_replays(monkeypatch):
    from bioradar.adapters.professional_rpc import execute_professional

    sent = []
    with httpx.Client(
        base_url="http://synthetic.invalid",
        transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(500)),
    ) as client:
        reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC")
        hook = fetch_execution.make_topic_fetch_hook(SimpleNamespace(), reader)
        persisted = []
        result = hook(
            {
                "topic_id": "SYNTHETIC",
                "scope_revision": 1,
                "source_id": "external_archive",
                "is_demo": True,
                "terms_all": ["待核"],
                "terms_any": [],
                "may_send": True,
            },
            call_key="SYNTHETIC-slot",
            prior_receipt=None,
            checkpoint=persisted.append,
        )
        assert result["status"] == "not_sent" and result["tool_requests"] == 0
    assert sent == []
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            json={
                "schema_version": "bioradar.professional.v1",
                "provider": "mech_evidence",
                "provider_version": "SYNTHETIC-v1",
                "operation_id": "SYNTHETIC-op",
                "status": "needs_input",
                "missing_fields": ["materials"],
                "material_feedback": [{"evidence_id": "E1", "reason": "news_report_not_original_research"}],
            },
        )

    state = {}
    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        kwargs = dict(
            provider="mech_evidence",
            operation_id="SYNTHETIC-op",
            operation="evaluate",
            arguments={"case": {"question": "SYNTHETIC"}},
            call=lambda *a, **k: pytest.fail("model"),
            state=state,
            checkpoint=lambda: None,
            provider_version="SYNTHETIC-v1",
        )
        first = execute_professional(client, **kwargs)
        second = execute_professional(client, **kwargs)
    assert first == second and first["status"] == "needs_input"
    assert first["material_feedback"][0]["reason"] == "news_report_not_original_research"
    assert calls == ["POST"]


def test_fixed_graph_chinese_resolve_keeps_pending_identity_and_get_only():
    from bioradar.adapters.fixed_graph import (
        build_fixed_graph_candidates,
        execute_fixed_graph_action,
        remember_graph_package,
    )

    bundle = {
        "schema_version": "datafabric.graph.bundle.v1",
        "graph_kind": "reviewed",
        "namespace": "SYNTHETIC",
        "is_demo": True,
        "title": "SYNTHETIC pending graph",
        "origin": "explicit_demo",
        "scope_description": "SYNTHETIC fixed candidate only",
        "nodes": [
            {
                "namespace": "SYNTHETIC",
                "id": "drug-a",
                "entity_type": "Drug",
                "name": "示例药物A",
                "aliases": [],
                "identity_status": "unresolved",
                "review": {"status": "pending"},
            }
        ],
        "edges": [],
    }
    graph = original("reviewed_graph", demo=True)
    graph.update(
        record_kind="graph",
        source_record_id="SYNTHETIC-graph",
        raw_content=json.dumps(bundle, ensure_ascii=False),
    )
    graph["raw_sha256"] = hashlib.sha256(graph["raw_content"].encode()).hexdigest()
    f, p = fixed(), {"evidence": [], "graph_context": []}
    f.update(
        question="示例药物A是否已有确认关系",
        graph_scope=[],
        dependency_sources=[
            {"source_id": "reviewed_graph", "instance": "SYNTHETIC-demo", "history": True},
        ],
    )
    package = remember_graph_package(f, p, graph)
    cfg = SimpleNamespace(
        data_fabric_demo_source_instance="SYNTHETIC-demo",
        engine_source_id_allowlist={"SYNTHETIC-demo": ["reviewed_graph"]},
    )
    candidates = build_fixed_graph_candidates(cfg, f, p, None, checks=[{"id": "competing_hypotheses"}])
    action = next(item for item in candidates if item["arguments"]["name"] == "示例药物A")
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/resolve"):
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.graph.resolution.v1",
                    "graph_ref": package["graph_ref"],
                    "query": {"name": "示例药物A"},
                    "status": "resolved",
                    "candidates": [next(n for n in bundle["nodes"] if n["name"] == "示例药物A")],
                    "truncated": False,
                    "limitations": ["SYNTHETIC pending candidate only"],
                },
            )
        return httpx.Response(200, json={"record": graph})

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC")
        result = execute_fixed_graph_action(
            reader, action["arguments"], mode="current", cutoff=NOW, timeout_seconds=5
        )
    assert result["status"] == "read" and result["records"] == []
    resolved = result["graph_context"]["resolutions"][0]["candidates"][0]
    assert resolved["identity_status"] == "unresolved" and resolved["review"]["status"] == "pending"
    assert result["actual_cost"]["tool_calls"] == 2 and result["tool_requests"] == 0
    assert [method for method, _ in requests] == ["GET", "GET"]


@pytest.mark.parametrize("reply", ["invalid_json", "wrong_binding", "oversized"])
def test_fetch_accepted_but_unusable_receipt_stays_unknown_and_cannot_repost(monkeypatch, reply):
    requests = []

    def respond(request):
        requests.append(request)
        if reply == "invalid_json":
            return httpx.Response(202, text="SYNTHETIC invalid JSON")
        if reply == "wrong_binding":
            return httpx.Response(201, json={**run(), "source_id": "clinicaltrials"})
        return httpx.Response(200, content=b"S" * 8_000_001)

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        host = Host(client)
        original_submit = fabric_fetch.submit_fetch
        monkeypatch.setattr(
            fetch_execution, "submit_fetch", lambda *a, **kw: original_submit(*a, **kw, client=client)
        )
        monkeypatch.setattr(fetch_execution, "_seconds", lambda f, p: 60)
        f, p = fetch_fixed(), {"queries": [], "evidence": []}
        action = fetch_action(f)
        receipt = fetch_execution.execute_source_fetch(host, "a", "lease", f, p, action, 0)
        assert receipt["status"] == "unconfirmed"
        assert receipt["usage"]["fetch"]["submission_status"] == "unknown"
        assert receipt["usage"]["fetch"]["call_key"] == requests[0].headers["Idempotency-Key"]
        fetch_execution.execute_source_fetch(host, "a", "lease", f, p, action, 0)
    assert len(requests) == 1


def test_search_limit_eight_and_twenty_are_explicit_while_baseline_default_stays_three():
    from bioradar.adapters.fabric_monitor import search_analysis_records

    requests = []

    def respond(request):
        requests.append(int(request.url.params["limit"]))
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.history.collection.v1",
                "items": [],
                "coverage": {"complete": True},
                "next_cursor": None,
            },
        )

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC")
        for kwargs in ({}, {"limit": 8}, {"limit": 20}):
            page = search_analysis_records(
                reader, source_id="pubmed", is_demo=False, terms=["SYNTHETIC"], cutoff=NOW, **kwargs
            )
            assert page["coverage"]["complete"] is False
        with pytest.raises(DomainError):
            search_analysis_records(
                reader, source_id="pubmed", is_demo=False, terms=["SYNTHETIC"], cutoff=NOW, limit=21
            )
    assert requests == [3, 8, 20]


def test_fixed_version_reader_rejects_a_different_native_record_even_when_hashes_match():
    record = original()
    with httpx.Client(
        base_url="http://synthetic.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"record": record})),
    ) as client:
        reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC")
        with pytest.raises(DomainError):
            reader.read_version(
                {
                    "source_id": "pubmed",
                    "source_record_id": "different-fixed-native",
                    "version_id": record["version_id"],
                    "is_demo": False,
                },
                cutoff=NOW,
            )
        assert reader.requests_made == 1


def test_fetch_pending_poll_intent_preserves_known_requests_and_reserves_only_next_get(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(202 if request.method == "POST" else 200, json=run("running"))

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        host = Host(client)
        original_submit = fabric_fetch.submit_fetch
        monkeypatch.setattr(
            fetch_execution, "submit_fetch", lambda *a, **kw: original_submit(*a, **kw, client=client)
        )
        monkeypatch.setattr(fetch_execution, "_seconds", lambda f, p: 60)
        f, p = fetch_fixed(), {"queries": [], "evidence": []}
        for _ in range(3):
            with pytest.raises(DomainError) as exc:
                fetch_execution.execute_source_fetch(host, "a", "lease", f, p, fetch_action(f), 0)
            assert exc.value.code == "analysis_fetch_pending"
    intents = [
        snap["queries"][0]["cost"]
        for snap in host.checkpoints
        if snap["queries"] and snap["queries"][0]["status"] == "sent"
    ]
    assert any(cost.get("known_tool_calls") == 2 and cost["reserved_tool_calls"] == 1 for cost in intents)
    assert p["queries"][0]["cost"]["tool_calls"] == p["tool_usage"]["tool_calls"] == 3
    assert calls == ["POST", "GET", "GET"]


def test_new_mechanism_packet_receives_real_historical_cutoff_before_context_projection(monkeypatch):
    from bioradar.adapters import mech_evidence
    from bioradar.research.investigations import professional

    f = fixed()
    f["query_context_version"] = "semantic-v2"
    f["evidence_time"] = {"mode": "public_history_reconstruction", "cutoff": "2019-12-31T23:59:59+00:00"}
    f["providers"] = {"mech_evidence": {"available": False}}
    e = evidence()
    e["source_context"] = doc_context()
    e["source_context"]["information_events"] = [
        {
            "event_type": "reported_deletion",
            "description": "SYNTHETIC posthoc label",
            "detected_at": "2026-10-31T00:00:00+00:00",
            "basis_locator": {"json_pointer": "/censorship_type"},
        }
    ]
    p = {"evidence": [e], "calls": []}
    captured = []

    def evaluate(settings, packet, call, *, loaded):
        captured.append(packet)
        return {"status": "needs_input", "material_feedback": []}

    monkeypatch.setattr(mech_evidence, "evaluate_mechanism", evaluate)
    service = SimpleNamespace(
        settings=SimpleNamespace(),
        _guard=lambda *a, **k: None,
        _checkpoint=lambda *a, **k: None,
        _shared_callback=lambda *a: None,
    )
    professional.mechanism(
        service, "SYNTHETIC", "lease", f, p, SimpleNamespace(mechanism_candidate="待核机制")
    )
    assert captured[0]["assessment"]["research_scope"]["evidence_time"] == f["evidence_time"]
    assert captured[0]["evidence"][0]["source_context"]["information_events"] == []
    assert e["source_context"]["information_events"]


def test_snapshot_source_publication_axis_is_distinct_from_verified_public_availability():
    record = original("pubmed")
    record["source_published_at"] = {
        "value": "2019-12-31", "precision": "day", "meaning": "source_reported",
    }
    cutoff = datetime(2019, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    with httpx.Client(base_url="http://synthetic.invalid") as client:
        reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC")
        collection = {"items": [record], "source_id": "pubmed", "is_demo": False, "coverage": {}}
        item = reader.snapshot(collection).records[0]
        assert item.publication_time_verified and item.published_time_raw == "2019-12-31"
        assert item.observed_at == NOW and item.public_available_at is None
        with pytest.raises(DomainError):
            validate_record(record, "pubmed", False, "public_history_reconstruction", cutoff)
        record["availability_proof"] = {
            "verified_by": "SYNTHETIC-reviewer", "verified_at": NOW.isoformat(),
            "uri": "https://synthetic.invalid/archived-original", "sha256": record["raw_sha256"],
            "available_at": {"value": "2019-12-31", "precision": "day"},
        }
        validate_record(record, "pubmed", False, "public_history_reconstruction", cutoff)
        historical = reader.snapshot(collection).records[0]
        assert historical.public_available_at == cutoff and historical.observed_at == NOW
        with pytest.raises(DomainError):
            validate_record(record, "pubmed", False, "operational_replay", cutoff)
