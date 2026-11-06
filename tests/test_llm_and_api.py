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


def test_local_vllm_gets_the_per_call_thinking_switch_its_template_reads(monkeypatch):
    bodies = []
    monkeypatch.setattr(
        llm, "post", lambda s, body, call, limit=None: bodies.append(body) or reply('{"value": 1}')
    )
    local = settings().model_copy(update={"llm_provider": "vllm"})
    llm.chat_json(local, "t", "sys", {}, Answer)
    llm.chat_json(local, "t", "sys", {}, Answer, thinking=True)
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False} and "thinking" not in bodies[0]
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": True}


def test_invalid_json_is_retried_once_with_the_error(monkeypatch):
    bodies = []
    replies = iter([reply('{"value": "x"}'), reply('{"value": 2}')])
    monkeypatch.setattr(llm, "post", lambda s, body, call, limit=None: bodies.append(body) or next(replies))
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed.value == 2 and call["attempts"] == 2 and call["usage"]["total_tokens"] == 10
    assert "不合格" in bodies[1]["messages"][-1]["content"]
    assert bodies[0]["thinking"] == {"type": "disabled"} and bodies[0]["temperature"] == 0


def test_empty_output_at_limit_is_reported_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "post", lambda s, body, call, limit=None: reply("", "length"))
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed is None and call["error"] == "output_empty_after_limit" and call["attempts"] == 1


def test_timeout_is_not_resent(monkeypatch):
    def slow(s, body, call, limit=None):
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


def test_resends_stop_at_the_request_budget(monkeypatch):
    request = httpx.Request("POST", "http://model.invalid/chat/completions")
    sent = []
    monkeypatch.setattr(
        llm.httpx, "post", lambda url, **kwargs: sent.append(1) or httpx.Response(503, request=request)
    )
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer, max_requests=1)
    assert parsed is None and len(sent) == 1 and call["requests"] == 1


def test_a_choice_without_a_message_is_an_error_not_a_crash(monkeypatch):
    for reply_ in ({"choices": [None]}, {"choices": [{"message": None}]}):
        monkeypatch.setattr(llm, "post", lambda s, body, call, limit=None, reply_=reply_: reply_)
        parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
        assert parsed is None and call["error"] == "response_without_choices"


def test_a_200_without_choices_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(llm, "post", lambda s, body, call, limit=None: None)
    parsed, call = llm.chat_json(settings(), "t", "sys", {}, Answer)
    assert parsed is None and call["error"] == "response_without_choices"


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


ARTIFACT = {
    "artifact_key": "datafabric.clean_text.v8:" + "a" * 64,
    "processor_version": "datafabric.clean_text.v8",
    "text_sha256": "a" * 64,
    "handover": True,
}


def test_semantic_search_goes_through_fabric_with_the_analysis_cutoff():
    """v3 3.6: the radar never calls the bridge; Fabric recalls through WeKnora and verifies each hit at the
    cutoff, returning the version visible then, the fixed text and why others were excluded."""
    import json
    from datetime import UTC, datetime

    from bioradar.adapters.fabric import Fabric

    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((request.method, request.url.path, request.headers["authorization"], body))
        result = {
            "source_id": "who_don",
            "source_record_id": "D1",
            "version_id": "v-visible",
            "is_demo": True,
            "text": "布病 聚集 报告",
            "locators": [{"start": 0, "end": 7}],
            "rerank_score": 0.8,
            "indexed_as": {"version_id": "v-first"},
            "start": 10,
            "end": 17,
            "artifact": ARTIFACT,
        }
        execution = {
            "candidates": 3,
            "verified": 1,
            "returned": 1,
            "insufficient": True,
            "rerank": {"state": "succeeded"},
            "recall": [
                {
                    "source_scopes": ["public_health", "research"],
                    "execution": {"keyword": "succeeded", "vector": "succeeded", "degraded": False},
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "results": [result],
                "execution": execution,
                "excluded": {"counts": {"not_visible_at_cutoff": 2}, "items": []},
                "coverage": {"who_don": {"ready_records": 67}},
            },
        )

    s = settings().model_copy(update={"fabric_read_token": "read-token", "semantic_search": True})
    fabric = Fabric(
        s, client=httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    )
    cutoff = datetime(2026, 9, 30, tzinfo=UTC)
    hits, summary = fabric.semantic_search(
        "布病 聚集", scopes=("public_health", "research"), is_demo=True, cutoff=cutoff
    )
    method, path, auth, body = sent[0]
    assert (method, path, auth) == ("POST", "/api/history/search", "Bearer read-token")
    assert body["mode"] == "public_history_reconstruction" and body["cutoff"] == cutoff.isoformat()
    assert body["scopes"] == ["public_health", "research"] and body["is_demo"] is True
    assert hits == [
        {
            "source": "who_don",
            "record_id": "D1",
            "version_id": "v-visible",
            "score": 0.8,
            "text": "布病 聚集 报告",
            "locators": [{"start": 0, "end": 7}],
            "range": [10, 17],
            "artifact": ARTIFACT,
        }
    ]  # the hit keeps the artifact it was verified against
    assert summary["excluded"] == {"not_visible_at_cutoff": 2} and summary["insufficient"] is True
    assert summary["recall"] == [
        {
            "scopes": ["public_health", "research"],
            "keyword": "succeeded",
            "vector": "succeeded",
            "embedding": None,
            "degraded": False,
            "degraded_reasons": None,
        }
    ]
    assert fabric.requests["search"] == {"n": 1, "errors": 0}


def test_a_record_is_read_as_the_cleaning_layers_text_and_every_part_is_bound():
    """v3 3.3/6.1 (Codex 470): the radar reads Fabric's cleaned text of the version (for a trial its
    summary and every registry field, untruncated), not the version's projection text; a version not
    processed yet keeps its own text and says so. Each part of what is read is bound to its fixed version
    and processor, and a bound window reads back exactly."""
    from datetime import UTC, datetime

    import pytest

    from bioradar.adapters.fabric import Fabric, document
    from bioradar.material import MaterialError, reread

    v, previous = "v" * 64, "u" * 64
    clean = "Brief summary.\n状态 status: COMPLETED\n分期 phase: PHASE3\n入组人数 enrollment: 31"
    diff = {
        "state": "processed",
        "from_version_id": previous,
        "changes": [{"field": "status", "description": "状态 RECRUITING → COMPLETED", "material": True}],
    }
    processors = {"clean": "datafabric.clean_text.v11"}

    def handler(request):
        path, params = request.url.path, request.url.params
        ids = params.get_list("version_ids")
        if path.endswith("/clean-text"):
            named = params.get("processor_version")
            versions = {
                i: {
                    "state": "processed",
                    "clean": {"processor_version": processors["clean"], "status": "parsed", "text": clean},
                }
                for i in ids
                if i == v and named in (None, processors["clean"])
            }
            return httpx.Response(200, json={"versions": versions})
        if path.endswith("/registry-changes"):
            return httpx.Response(
                200,
                json={
                    "processor_version": "datafabric.registry_diff.v1",
                    "versions": {i: diff for i in ids if i == v},
                },
            )
        return httpx.Response(404, json={"detail": {"code": "version_not_found"}})

    fabric = Fabric(
        settings(),
        client=httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler)),
    )
    record = {
        "source_id": "clinicaltrials",
        "source_record_id": "NCT00073242",
        "version_id": v,
        "is_demo": False,
        "record_kind": "trial",
        "title": "Leptin",
        "clean_text": "Brief summary.",
        "parser_version": "p1",
        "facts": {"status": "COMPLETED"},
    }
    doc = document(record)
    assert doc["text"] == "Brief summary." and doc["material"].parts()[0]["kind"] == "projection_text"
    fabric.complete([doc], is_demo=False, mode="current", cutoff=datetime.now(UTC).isoformat())
    assert doc["clean_text_state"] == "processed" and doc["text"].endswith(clean)
    assert doc["text"].startswith("【与上一版相比的登记变化") and "入组人数 enrollment: 31" in doc["text"]
    parts = doc["material"].parts()
    assert [p["kind"] for p in parts] == ["registry_diff", "clean_text"]
    assert parts[1]["processor_version"] == "datafabric.clean_text.v11" and parts[1]["version_id"] == v
    assert parts[0]["from_version_id"] == previous and parts[0]["renderer"].startswith("bioradar.")
    window = doc["material"].slice(len(doc["text"]) - len(clean) + 15, len(doc["text"]))
    assert window.text == clean[15:] and window.binding()["segments"][0]["start"] == 15
    assert reread(doc["material"].binding(), fabric.part_texts) == doc["text"]
    # after a cleaner upgrade the bound processor's text is asked for by name, never the new one in its place
    processors["clean"] = "datafabric.clean_text.v12"
    with pytest.raises(MaterialError) as error:
        reread(window.binding(), fabric.part_texts)
    assert error.value.code == "part_unreadable"
    # not processed yet: the version's own text stays, marked
    other = document(dict(record, version_id="w" * 64))
    fabric.complete([other], is_demo=False, mode="current", cutoff=datetime.now(UTC).isoformat())
    assert other["clean_text_state"] == "unavailable" and other["text_level"] == "uncleaned_projection"
    notice = "【清洗层正文不可用（unavailable）：以下是该版本自带的投影正文，未经清洗，可能不完整】"
    assert other["text"] == notice + "\n\nBrief summary."
    assert other["material"].segments[0][1] is None  # the notice is a fixed template, the text a bound part


def test_fabric_requests_are_counted_where_they_are_sent_and_kept_across_a_resumed_run():
    """Codex r44: a read served from the run's cache sends nothing, so request counts come from the
    client, not from the number of actions. Counts only: endpoint, requests, errors."""
    from bioradar.adapters.fabric import Fabric, FabricError
    from bioradar.research import analyses
    from bioradar.research.pipeline import Run

    replies = iter([httpx.Response(200, json={"items": []}), httpx.Response(503, json={})])
    transport = httpx.MockTransport(lambda request: next(replies))
    client = httpx.Client(base_url="http://fabric.invalid", transport=transport)
    fabric = Fabric(settings(), client=client)
    row = analyses.create(question="计数？", direction="pipeline", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=fabric)
    fabric.search("s", ["x"], is_demo=True)
    run.save()
    run.save()  # saving again adds nothing
    try:
        fabric.search("s", ["x"], is_demo=True)
    except FabricError:
        pass
    run.save()
    assert run.p["fabric_requests"] == {"records": {"n": 2, "errors": 1}}
    resumed = Run(analyses.get(row.id), settings(), fabric=Fabric(settings(), client=client))
    resumed.save()
    assert resumed.p["fabric_requests"] == {"records": {"n": 2, "errors": 1}}
    assert all(set(c) == {"n", "errors"} for c in resumed.p["fabric_requests"].values())


def test_a_search_hit_is_read_as_exactly_the_artifact_it_was_verified_against():
    """Codex 470: a semantic hit names the cleaned artifact it was verified against (processor version and
    full-text hash, an earlier processor's during a handover). Reading asks Fabric for exactly that one and
    binds it with Fabric's locators; an artifact that is not there any more is said so, never replaced by the
    current one, and the projection text read instead is not counted as cleaned or full text."""
    import hashlib
    import json

    import pytest

    from bioradar.adapters.fabric import Fabric
    from bioradar.material import Material, MaterialError, reread, restore

    v = "v" * 64
    old_text = "旧处理器正文 status: COMPLETED\n第二段"
    old = {
        "processor_version": "datafabric.clean_text.v8",
        "status": "parsed",
        "text": old_text,
        "text_sha256": hashlib.sha256(old_text.encode()).hexdigest(),
        "segments": [
            {"start": 0, "end": 7, "kind": "field", "source": {"json_pointer": "/briefSummary"}},
            {"start": 7, "end": len(old_text), "kind": "field", "source": {"json_pointer": "/status"}},
        ],
    }
    asked = []

    def handler(request):
        path, params = request.url.path, request.url.params
        if path.startswith("/api/history/versions/"):
            return httpx.Response(
                200,
                json={
                    "source_id": "clinicaltrials",
                    "source_record_id": "NCT1",
                    "version_id": v,
                    "is_demo": True,
                    "first_seen_at": "2026-01-01T00:00:00+00:00",  # always in a HistoryRecord (required)
                    "raw_sha256": "r" * 64,
                    "record_kind": "trial",
                    "title": "T",
                    "clean_text": "投影正文",
                    "parser_version": "p1",
                    "facts": {},
                },
            )
        if path.endswith("/clean-text"):
            asked.append(params.get("processor_version"))
            named = params.get("processor_version")
            versions = {v: {"state": "processed", "clean": old}} if named == old["processor_version"] else {}
            return httpx.Response(200, json={"versions": versions})
        if path.endswith("/registry-changes"):
            return httpx.Response(200, json={"versions": {}})
        return httpx.Response(404, json={"detail": {"code": "version_not_found"}})

    fabric = Fabric(
        settings(),
        client=httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler)),
    )
    wanted = {"processor_version": old["processor_version"], "text_sha256": old["text_sha256"]}
    docs, _ = fabric.read_versions("clinicaltrials", [("NCT1", v)], is_demo=True, artifacts={v: wanted})
    doc = docs[0]
    assert asked == ["datafabric.clean_text.v8"]  # the hit's processor, not the current one
    assert doc["text"] == old_text
    assert (doc["clean_text_state"], doc["clean_text_choice"]) == ("processed", "search_hit")
    p = doc["material"].parts()[0]
    assert (p["processor_version"], p["raw_sha256"]) == (old["processor_version"], "r" * 64)
    assert p["text_sha256"] == old["text_sha256"]
    # a window names Fabric's locators of what it covers, and keeps them through storage and a further cut
    window = doc["material"].slice(9, len(old_text))
    stored = json.loads(json.dumps(window.binding(), ensure_ascii=False))
    assert [loc["source"]["json_pointer"] for loc in stored["segments"][0]["locators"]] == ["/status"]
    again = restore(window.text, stored).slice(0, 3).binding()
    assert again["segments"][0]["locators"] == stored["segments"][0]["locators"]
    assert reread(stored, fabric.part_texts) == window.text
    # the artifact the hit named changed (another hash): not read in its place, and not counted as cleaned
    docs, _ = fabric.read_versions(
        "clinicaltrials", [("NCT1", v)], is_demo=True, artifacts={v: dict(wanted, text_sha256="0" * 64)}
    )
    assert docs[0]["text"].endswith("投影正文") and "artifact_changed" in docs[0]["text"]
    assert docs[0]["clean_text_state"] == "artifact_changed"
    assert docs[0]["text_level"] == "uncleaned_projection"
    # text without known provenance never reads back as if it were bound
    with pytest.raises(MaterialError):
        reread(Material.unbound("旧证据", "read_before_binding").binding(), fabric.part_texts)


def test_an_attachment_artifact_a_hit_named_is_never_replaced_by_the_current_one():
    """Codex r86: the hit named attachment_parse.v1 and Fabric no longer has it; the current v2 text must not
    take its place (it stays uncleaned, said so). When the named artifact is there, it is read and keeps its
    inputs: the artifact and the parent page it belongs to."""
    import hashlib

    from bioradar.adapters.fabric import Fabric

    v, v1_text, v2_text = "a" * 64, "附件 v1 正文", "附件 v2 正文"
    v1 = {
        "processor_version": "datafabric.attachment_parse.v1",
        "status": "parsed",
        "text": v1_text,
        "basis": "mineru",
        "text_sha256": hashlib.sha256(v1_text.encode()).hexdigest(),
    }
    have_v1 = {"on": False}

    def handler(request):
        path, params = request.url.path, request.url.params
        if path.startswith("/api/history/versions/"):
            return httpx.Response(
                200,
                json={
                    "source_id": "nmpa",
                    "source_record_id": "P1#attachment=x.xls",
                    "version_id": v,
                    "is_demo": True,
                    "first_seen_at": "2026-01-01T00:00:00+00:00",  # always in a HistoryRecord (required)
                    "raw_sha256": "r" * 64,
                    "record_kind": "document",
                    "title": "附件",
                    "clean_text": "",
                    "parser_version": "p1",
                    "facts": {"role": "attachment", "parent_record_id": "P1"},
                },
            )
        if path.endswith("/clean-text"):
            named = params.get("processor_version")
            ok = have_v1["on"] and named == v1["processor_version"]
            versions = {v: {"state": "processed", "clean": v1}} if ok else {}
            return httpx.Response(200, json={"versions": versions})
        if path.endswith("/derived-text"):
            current = "datafabric.attachment_parse.v2"
            return httpx.Response(
                200,
                json={
                    "processor_version": current,
                    "versions": {
                        v: {
                            "state": "processed",
                            "status": "parsed",
                            "text": v2_text,
                            "parse_method": "mineru",
                            "attachment_of": [{"parent_record_id": "P1", "parent_version_id": "p" * 64}],
                        }
                    },
                },
            )
        return httpx.Response(404, json={"detail": {"code": "version_not_found"}})

    fabric = Fabric(
        settings(),
        client=httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler)),
    )
    wanted = {"processor_version": v1["processor_version"], "text_sha256": v1["text_sha256"]}
    docs, _ = fabric.read_versions("nmpa", [("P1#attachment=x.xls", v)], is_demo=True, artifacts={v: wanted})
    assert v2_text not in docs[0]["text"] and docs[0]["clean_text_state"] != "processed"
    assert "清洗层正文不可用" in docs[0]["text"]
    have_v1["on"] = True
    docs, _ = fabric.read_versions("nmpa", [("P1#attachment=x.xls", v)], is_demo=True, artifacts={v: wanted})
    doc = docs[0]
    assert doc["text"].endswith(v1_text) and v2_text not in doc["text"]
    assert doc["clean_text_choice"] == "search_hit"
    assert doc["material"].parts()[-1]["processor_version"] == "datafabric.attachment_parse.v1"
    derived = doc["derived"][-1]
    assert derived["processor_version"] == v1["processor_version"]
    assert derived["text_sha256"] == v1["text_sha256"]
    assert derived["attachment_of"] == [{"parent_record_id": "P1", "parent_version_id": "p" * 64}]
