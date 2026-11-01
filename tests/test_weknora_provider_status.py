"""SYNTHETIC configuration status only; no live connection, model, or database."""

import importlib
import json
from types import SimpleNamespace

import pytest
from bioradar.adapters.registry import provider_statuses
from bioradar.adapters.weknora_kdd import search_status
from bioradar.api.config import Settings
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.analysis_service import AnalysisService
from fastapi.testclient import TestClient
from pydantic import SecretStr

pytestmark = pytest.mark.ENGINE
CREDENTIAL = "SYNTHETIC-status-user-credential-123456"


def config(**changes):
    values = {
        "_env_file": None,
        "environment": "test",
        "engine_dev_credential": CREDENTIAL,
        "analysis_kdd_enabled": True,
        "analysis_query_policy": "query_planner",
        "weknora_kdd_url": "http://synthetic-kdd.invalid",
        "weknora_kdd_token": "SYNTHETIC-no-live-token",
        "weknora_kdd_knowledge_base_id": "SYNTHETIC-private-real-kb",
        "weknora_kdd_demo_knowledge_base_id": "SYNTHETIC-private-demo-kb",
        "weknora_kdd_source_scope": "SYNTHETIC-private-real-scope",
        "weknora_kdd_demo_source_scope": "SYNTHETIC-private-demo-scope",
    }
    values.update(changes)
    return Settings(**values)


@pytest.fixture(autouse=True)
def no_live_checks(monkeypatch):
    import bioradar.adapters.biocheck as biocheck
    import bioradar.adapters.mech_evidence as mech
    import bioradar.research.investigations.professional as professional

    def unavailable(*args, **kwargs):
        return {"available": False, "configured": False, "status": "not_configured"}

    monkeypatch.setattr(biocheck, "provider_status", unavailable)
    monkeypatch.setattr(mech, "provider_status", unavailable)
    monkeypatch.setattr(professional, "provider_status", unavailable)
    monkeypatch.setattr(AnalysisService, "_search_scope", lambda self, actor, is_demo: ["SYNTHETIC-source"])
    monkeypatch.setattr(AnalysisService, "_graph_scope", lambda *args: [])


@pytest.mark.parametrize("is_demo", [False, True])
def test_independent_retrieval_configuration_is_scoped_and_never_a_live_receipt(is_demo):
    value = search_status(config(), direction="research", is_demo=is_demo)
    assert value["configured"] is value["available"] is True
    assert value["status"] == "configured" and value["connection_checked"] is False
    assert value["is_demo"] is is_demo
    space = "demo" if is_demo else "real"
    assert value["knowledge_base_id"] == f"SYNTHETIC-private-{space}-kb"
    assert value["source_scope"] == f"SYNTHETIC-private-{space}-scope"
    assert "不代表" in value["qualification"]
    assert "SYNTHETIC-no-live-token" not in json.dumps(value)


def test_demo_only_and_ambiguous_namespaces_cannot_make_real_retrieval_available():
    settings = config(weknora_kdd_knowledge_base_id=None)
    real = search_status(settings, direction="research")
    demo = search_status(settings, direction="research", is_demo=True)
    assert real["configured"] is real["available"] is False
    assert real["error_code"] == "weknora_kdd_not_configured"
    assert "knowledge_base_id" not in real and "source_scope" not in real
    assert demo["configured"] is demo["available"] is True
    settings.weknora_kdd_knowledge_base_id = settings.weknora_kdd_demo_knowledge_base_id
    assert not search_status(settings, direction="research")["available"]
    assert not search_status(settings, direction="research", is_demo=True)["available"]


def test_options_uses_actual_kdd_scope_and_preserves_legacy_when_disabled():
    settings = config(weknora_kdd_knowledge_base_id=None)
    engine = EngineService(SimpleNamespace(url=SimpleNamespace(database="SYNTHETIC-not-a-store")), settings)
    service = AnalysisService(engine)
    actor = DevelopmentAccess(settings).actor()
    assert service.options(actor, is_demo=True)["providers"]["weknora"]["configured"] is True
    assert service.options(actor, is_demo=False)["providers"]["weknora"]["configured"] is False
    assert not service.options(actor, is_demo=True)["providers"]["weknora"]["configured"]
    settings.weknora_url = "http://synthetic-legacy.invalid"
    settings.weknora_api_key = SecretStr("SYNTHETIC-legacy-key")
    settings.weknora_knowledge_base_ids = ["SYNTHETIC-legacy-kb"]
    assert service.options(actor, is_demo=False)["providers"]["weknora"]["configured"] is True


@pytest.mark.parametrize("legacy_configured", [False, True])
def test_default_baseline_uses_legacy_status_even_when_kdd_feature_is_enabled(legacy_configured):
    settings = config(
        analysis_query_policy="baseline",
        weknora_kdd_url=None if legacy_configured else "http://synthetic-kdd.invalid",
        weknora_url="http://synthetic-legacy.invalid" if legacy_configured else None,
        weknora_api_key="SYNTHETIC-legacy-key" if legacy_configured else None,
        weknora_knowledge_base_ids=["SYNTHETIC-legacy-kb"] if legacy_configured else [],
    )
    engine = EngineService(SimpleNamespace(url=SimpleNamespace(database="SYNTHETIC-not-a-store")), settings)
    value = AnalysisService(engine).options(DevelopmentAccess(settings).actor())["providers"]["weknora"]
    assert value["configured"] is legacy_configured


def test_public_registry_has_configuration_summary_without_private_kb_or_scope():
    settings = config(weknora_kdd_knowledge_base_id=None)
    knowledge = next(item for item in provider_statuses(settings) if item["id"] == "knowledge")
    assert knowledge["state"] == "configured"
    assert knowledge["name"] == "WeKnora 知识检索"
    assert "雷达执行器" in knowledge["note"]
    encoded = json.dumps(knowledge, ensure_ascii=False)
    assert "SYNTHETIC-private" not in encoded
    assert "knowledge_base_id" not in encoded and "source_scope" not in encoded
    assert "SYNTHETIC-no-live-token" not in encoded
    assert knowledge["configuration"]["connection_checked"] is False
    assert "available" not in knowledge["configuration"]  # public summary has no selected namespace


def test_native_http_auth_and_system_summary_do_not_disclose_scoped_kb(monkeypatch):
    module = importlib.import_module("bioradar.api.app")
    settings = config(weknora_kdd_knowledge_base_id=None)
    monkeypatch.setattr(module, "create_database", lambda *args: None)

    async def dependencies(*args):
        return {"postgres": "not_configured", "redis": "not_configured", "worker": "not_configured"}

    monkeypatch.setattr(module, "dependencies", dependencies)
    app = module.create_app(settings)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        app.state.database = SimpleNamespace(
            url=SimpleNamespace(database="SYNTHETIC-not-a-store"), dispose=lambda: None
        )
        path = "/api/engine/investigations/research-options"
        denied = client.get(path, params={"is_demo": True})
        assert denied.status_code == 401
        system = client.get("/api/system/status")
        assert system.status_code == 200
        encoded = denied.text + system.text
        assert "SYNTHETIC-private" not in encoded
        assert "knowledge_base_id" not in encoded and "source_scope" not in encoded
        headers = {"Authorization": "Bearer " + CREDENTIAL}
        demo = client.get(path, params={"is_demo": True}, headers=headers)
        real = client.get(path, params={"is_demo": False}, headers=headers)
        assert demo.status_code == real.status_code == 200
        assert demo.json()["providers"]["weknora"]["configured"] is True
        assert real.json()["providers"]["weknora"]["configured"] is False
        assert demo.json()["providers"]["weknora"]["connection_checked"] is False
        app.state.database = None
