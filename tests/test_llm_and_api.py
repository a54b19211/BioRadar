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
    monkeypatch.setattr(llm, "post", lambda s, body, call: bodies.append(body) or next(replies))
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed.value == 2 and call["attempts"] == 2 and call["usage"]["total_tokens"] == 10
    assert "不合格" in bodies[1]["messages"][-1]["content"]
    assert bodies[0]["thinking"] == {"type": "disabled"} and bodies[0]["temperature"] == 0


def test_empty_output_at_limit_is_reported_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "post", lambda s, body, call: reply("", "length"))
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed is None and call["error"] == "output_empty_after_limit" and call["attempts"] == 1


def test_timeout_is_not_resent(monkeypatch):
    def slow(s, body, call):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(llm, "post", slow)
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed is None and call["error"] == "timeout" and call["attempts"] == 1


def test_a_refused_request_is_resent_after_a_pause(monkeypatch):
    """2026-11-03: DeepSeek answered 503 for ~25 minutes; each call failed on the first refusal."""
    statuses, waits = iter([503, 429, 200]), []
    request = httpx.Request("POST", "http://model.invalid/chat/completions")

    def fake_post(url, **kwargs):
        status = next(statuses)
        return httpx.Response(status, json=reply('{"value": 3}') if status == 200 else {}, request=request)

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed.value == 3 and call["requests"] == 3 and waits == [5, 20]


def test_a_refusal_that_outlasts_the_pauses_is_reported(monkeypatch):
    request = httpx.Request("POST", "http://model.invalid/chat/completions")
    monkeypatch.setattr(llm.httpx, "post", lambda url, **kwargs: httpx.Response(503, request=request))
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed is None and call["error"] == "http_error:503" and call["requests"] == 4


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


def test_a_literature_record_without_document_evidence_keeps_its_own_publication_type():
    import json

    from bioradar.adapters.fabric import document

    def epmc(types, evidence=None):
        raw = {"pubTypeList": {"pubType": types}, "abstractText": "We performed ..."}
        rec = {
            "source_id": "europe_pmc",
            "source_record_id": "MED:1",
            "version_id": "v",
            "is_demo": False,
            "record_kind": "document",
            "raw_content": json.dumps(raw),
        }
        if evidence:
            rec["document_evidence"] = evidence
        return document(rec)

    research = epmc(["research-article", "Journal Article"])
    assert (research["material_kind"], research["text_level"]) == ("primary_research", "abstract")
    assert epmc(["Editorial", "Journal Article"])["material_kind"] == "document"  # not research
    assert epmc(["Review", "Journal Article"])["material_kind"] == "review"
    stated = epmc(["research-article"], {"material_kind": "news_report", "text_level": "full_text"})
    assert (stated["material_kind"], stated["text_level"]) == ("news_report", "full_text")  # Fabric wins


def test_weknora_hits_point_back_to_fabric_only_with_a_fabric_identity():
    import json

    from bioradar.adapters.weknora import identity

    locator = {"source_instance": "x", "source_id": "clinicaltrials", "source_record_id": "NCT07500001"}
    assert identity({"metadata": {"raw_locator": json.dumps(locator)}}) == ("clinicaltrials", "NCT07500001")
    assert identity({"metadata": {"source_id": "who_don", "source_record_id": "D1"}}) == ("who_don", "D1")
    assert identity({"metadata": {"content": "---\nsource_site: chictr\n---"}}) is None  # crawler upload
