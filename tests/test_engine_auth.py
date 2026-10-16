"""Real HTTP authentication behavior, independent of research fixture outputs."""

import pytest
from bioradar.api.app import create_app
from bioradar.api.config import Settings
from bioradar.research.engine.auth import COOKIE_NAME, DevelopmentAccess
from fastapi.testclient import TestClient
from pydantic import ValidationError

pytestmark = pytest.mark.ENGINE
CREDENTIAL = "test-development-user-credential-only"
ORIGIN = "http://127.0.0.1:15176"


@pytest.fixture
def client():
    settings = Settings(
        environment="test", engine_dev_credential=CREDENTIAL, database_url=None, redis_url=None
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def login(client):
    return client.post("/api/engine/session", json={"credential": CREDENTIAL}, headers={"Origin": ORIGIN})


def test_unconfigured_engine_cannot_be_used_anonymously():
    settings = Settings(environment="test", engine_dev_credential=None, database_url=None, redis_url=None)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert client.get("/api/engine/session").json() == {
            "enabled": False,
            "authenticated": False,
            "actor_id": None,
            "workspace_id": None,
        }
        assert client.get("/api/engine/methods").status_code == 401
        assert login(client).status_code == 503
        assert client.post("/api/v1/investigations", json={}).status_code == 503


def test_login_only_returns_short_lived_http_only_session(client):
    response = login(client)
    assert response.status_code == 200
    assert response.json()["workspace_id"] == "development"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=1800" in cookie
    assert CREDENTIAL not in response.text and CREDENTIAL not in response.headers["set-cookie"]
    assert client.get("/api/engine/session").json()["authenticated"] is True
    methods = client.get("/api/engine/methods")
    assert methods.status_code == 200 and len(methods.json()) > 0
    assert all(m["version"] and m["input_groups"] for m in methods.json())
    assert client.get("/api/engine/snapshots").status_code == 503


@pytest.mark.parametrize("origin", [None, "http://evil.example", "http://localhost.evil.example:15176"])
def test_login_requires_exact_approved_origin(client, origin):
    headers = {"Origin": origin} if origin else {}
    response = client.post("/api/engine/session", json={"credential": CREDENTIAL}, headers=headers)
    assert response.status_code == 403
    assert COOKIE_NAME not in client.cookies


def test_bad_credentials_and_client_claimed_workspace_are_rejected_without_echo(client):
    response = client.post(
        "/api/engine/session", json={"credential": "not-the-user"}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 401
    assert "not-the-user" not in response.text
    response = client.post(
        "/api/engine/session",
        json={"credential": CREDENTIAL, "workspace_id": "other"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 422
    assert CREDENTIAL not in response.text


def test_cookie_write_without_origin_is_rejected_and_logout_clears_session(client):
    assert login(client).status_code == 200
    assert client.delete("/api/engine/session").status_code == 403
    assert client.delete("/api/engine/session", headers={"Origin": ORIGIN}).status_code == 200
    assert client.get("/api/engine/methods").status_code == 401


def test_expired_and_tampered_sessions_cannot_read_methods(client, monkeypatch):
    assert login(client).status_code == 200
    token = client.cookies.get(COOKIE_NAME)
    monkeypatch.setattr("bioradar.research.engine.auth.time.time", lambda: 10**12)
    assert client.get("/api/engine/methods").status_code == 401
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token + "changed", path="/api/engine")
    assert client.get("/api/engine/methods").status_code == 401


def test_credential_rotation_revokes_previous_signature():
    old_access = DevelopmentAccess(Settings(environment="test", engine_dev_credential=CREDENTIAL))
    token = old_access.create_session()
    rotated = DevelopmentAccess(Settings(environment="test", engine_dev_credential=CREDENTIAL + "-rotated"))
    assert old_access._session_valid(token)
    assert not rotated._session_valid(token)


def test_cli_bearer_is_local_and_not_a_browser_origin_bypass(client):
    headers = {"Authorization": "Bearer " + CREDENTIAL}
    assert client.get("/api/engine/methods", headers=headers).status_code == 200
    assert (
        client.get("/api/engine/methods", headers={**headers, "Origin": "http://evil.example"}).status_code
        == 403
    )
    assert client.get("/api/engine/methods", headers={**headers, "Host": "public.example"}).status_code == 403
    assert client.get("/api/engine/methods", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_runtime_configuration_cannot_silently_expand_network_or_credential_scope():
    with pytest.raises(ValidationError):
        Settings(engine_dev_credential="short")
    with pytest.raises(ValidationError):
        Settings(engine_allowed_origins=["https://public.example"])
    with pytest.raises(ValidationError):
        Settings(engine_source_allowlist=[])


def test_runtime_openapi_declares_actual_cookie_or_cli_authentication(client):
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["DevelopmentUserCredential"]["scheme"] == "bearer"
    assert schemes["DevelopmentUserSession"]["in"] == "cookie"
    assert schema["paths"]["/api/engine/runs"]["post"]["security"] == [
        {"DevelopmentUserCredential": []},
        {"DevelopmentUserSession": []},
    ]
    assert "security" not in schema["paths"]["/api/engine/session"]["post"]
