import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from bioradar.adapters import llm
from bioradar.app import app
from bioradar.config import settings


class Answer(BaseModel):
    value: int


def reply(content, finish="stop"):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def test_invalid_json_is_retried_once_with_the_error(monkeypatch):
    bodies = []
    replies = iter([reply('{"value": "x"}'), reply('{"value": 2}')])
    monkeypatch.setattr(llm, "post", lambda s, body: bodies.append(body) or next(replies))
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed.value == 2 and call["attempts"] == 2 and call["usage"]["total_tokens"] == 10
    assert "不合格" in bodies[1]["messages"][-1]["content"]
    assert bodies[0]["thinking"] == {"type": "disabled"} and bodies[0]["temperature"] == 0


def test_empty_output_at_limit_is_reported_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "post", lambda s, body: reply("", "length"))
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed is None and call["error"] == "output_empty_after_limit" and call["attempts"] == 1


def test_timeout_is_not_resent(monkeypatch):
    def slow(s, body):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(llm, "post", slow)
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed is None and call["error"] == "timeout" and call["attempts"] == 1


def test_api_requires_login_and_validates_cutoff():
    client = TestClient(app)
    assert client.get("/api/options").status_code == 401
    assert client.post("/api/session", json={"credential": "wrong"}).status_code == 401
    assert client.post("/api/session", json={"credential": settings().dev_credential}).status_code == 200
    assert client.get("/api/options").json()["configured"]["planner"] is True
    response = client.post(
        "/api/analyses",
        json={"question": "历史回放问题", "direction": "public_health", "cutoff": "2019-12-31T00:00:00"},
    )
    assert response.status_code == 422


def test_a_failed_service_call_keeps_the_model_calls_it_paid_for(monkeypatch):
    import httpx

    from bioradar.adapters import services
    from bioradar.config import settings

    calls = [{"stage": "generate", "model": "m", "usage": {"total_tokens": 900}}]
    response = httpx.Response(502, json={"error_code": "model_failed", "llm_calls": calls})
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    s = settings()
    monkeypatch.setattr(s, "biocheck_url", "http://biocheck.invalid")
    try:
        services.biocheck(s, "generate", {})
    except services.ServiceError as exc:
        assert exc.code == "model_failed" and exc.calls == calls
    else:
        raise AssertionError("expected ServiceError")


def test_source_status_keeps_collection_state_new_records_and_publication_apart(monkeypatch):
    from bioradar import app as api_module

    class Fake:
        def __init__(self, s):
            pass

        def source_health(self, source):
            failed = {"watch_id": "w1", "state": "failed", "scope": "BCMA"}
            watches = [failed] if source == "europe_pmc" else []
            return {"collection": {"state": "unknown", "scope": "recorded_crawl_task"}, "watches": watches}

        def latest(self, source, *, is_demo):
            return {"published_at": "2026-10-19"}

        def head(self, source, *, is_demo):
            return 5

    monkeypatch.setattr(api_module, "Fabric", Fake)
    s = settings()
    monkeypatch.setattr(s, "monitor_sources", ["europe_pmc", "who_don"])
    client = TestClient(app)
    client.post("/api/session", json={"credential": s.dev_credential})
    body = client.get("/api/sources").json()
    rows = {r["source"]: r for r in body["sources"]}
    assert rows["europe_pmc"]["note"].startswith("采集任务失败")
    assert rows["who_don"]["note"] == "长时间无新增，采集状态待核"
    assert rows["who_don"]["latest_published_at"] == "2026-10-19"
    assert rows["who_don"]["collection"]["state"] == "unknown"
    assert body["daily_auto_tokens"] == s.daily_auto_tokens
