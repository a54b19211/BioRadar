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
