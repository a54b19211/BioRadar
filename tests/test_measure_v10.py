"""measure.v10 (Codex): the first real local-Qwen answer (qwen-run1 event 1, kept as given) keyed sources as
period_text / scope_text; the four aliases are mapped mechanically inside sources only and recorded, the
canonical key beside its alias, an unknown key or an unknown source still fail, and every answer is kept."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from bioradar import db
from bioradar.adapters import llm
from bioradar.config import settings
from bioradar.research import schemas

REAL = (Path(__file__).parent / "fixtures" / "qwen_run1_event1_content.json").read_text(encoding="utf-8")


def test_the_real_answer_validates_after_the_alias_mapping_and_says_what_was_mapped():
    raw = json.loads(REAL)
    with pytest.raises(ValidationError):
        schemas.Extraction.model_validate(copy.deepcopy(raw))  # as given: refused (measure.v9)
    data, mappings = schemas.map_source_aliases(copy.deepcopy(raw))
    got = schemas.Extraction.model_validate(data)
    assert sum(len(i.measures) for i in got.items) == 3
    assert mappings == [
        {"item": 0, "measure": m, "from": f"{k}_text", "to": k} for m in range(3) for k in ("period", "scope")
    ]
    assert [m.quote for m in got.items[0].measures] == [m["quote"] for m in raw["items"][0]["measures"]]


@pytest.mark.parametrize(
    "sources",
    [
        {"period": "quote", "period_text": "quote"},  # canonical and alias together, even with one value
        {"period": "quote", "period_text": "window"},
        {"periods": "quote"},  # an unknown key
        {"period_text": "somewhere"},  # an unknown source: the value is never guessed
        {"metric_text": "quote"},  # not one of the four
    ],
)
def test_anything_else_is_still_refused(sources):
    raw = json.loads(REAL)
    raw["items"][0]["measures"][0]["sources"] = sources
    data, _ = schemas.map_source_aliases(raw)
    with pytest.raises(ValidationError):
        schemas.Extraction.model_validate(data)


def test_chat_json_keeps_the_answer_and_its_mappings(monkeypatch):
    sent = []

    def post(settings_, body, call, limit=None):
        sent.append(body)
        call["requests"] = call.get("requests", 0) + 1
        return {
            "choices": [{"message": {"content": REAL}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1975, "completion_tokens": 848, "total_tokens": 2823},
        }

    monkeypatch.setattr(llm, "post", post)
    parsed, call = llm.chat_json(settings(), "extract", "s", {"windows": []}, schemas.Extraction)
    assert parsed is not None and len(sent) == 1 and call["error"] is None
    [out] = call["outputs"]
    assert out["sha256"] == hashlib.sha256(REAL.encode("utf-8")).hexdigest() and out["content"] == REAL
    assert len(out["mappings"]) == 6
    with db.session() as s:
        row = db.LlmCall(stage="extract", service="radar", model="m")
        s.add(row)
        s.flush()
        db.keep_outputs(s, row, call)
        rid = row.id
    with db.session() as s:
        [kept] = s.scalars(select(db.LlmOutput).where(db.LlmOutput.llm_call_id == rid))
        assert (kept.attempt, kept.content_sha256, kept.content) == (1, out["sha256"], REAL)
        assert kept.mappings[0] == {"item": 0, "measure": 0, "from": "period_text", "to": "period"}


def test_the_prompt_names_the_keys():
    text = llm.describe(schemas.Extraction)
    assert "键只能是 period、object、scope、denominator" in text


def test_a_format_retry_and_a_429_resend_are_each_counted_through_a_real_http_chain(monkeypatch):
    """Codex r192: the adapter's real HTTP path against a local server - an answer that fails validation, its
    format retry answered 429, the resend answered: 3 requests, each outcome kept, tokens of the 2 answers."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    bad = json.loads(REAL)
    bad["items"][0]["measures"][0]["sources"] = {"period": "quote", "period_text": "quote"}  # still refused
    script = [(200, json.dumps(bad, ensure_ascii=False), 100), (429, None, 0), (200, REAL, 200)]
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(len(body["messages"]))
            status, content, completion = script[len(seen) - 1]
            payload = (
                {}
                if content is None
                else {
                    "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": completion,
                        "total_tokens": 1000 + completion,
                    },
                }
            )
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(llm, "BACKOFF", (0, 0, 0))
        s = settings().model_copy(
            update={
                "llm_base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "llm_provider": "vllm",
                "llm_model": "m",
                "llm_api_key": "t",
            }
        )
        parsed, call = llm.chat_json(s, "extract", "s", {"windows": []}, schemas.Extraction)
    finally:
        server.shutdown()
    assert parsed is not None and seen == [2, 4, 4], (
        seen,
        call.get("error"),
        call.get("http"),
        call.get("detail"),
        s.llm_base_url,
    )
    assert call["requests"] == 3 and call["http"] == [200, 429, 200]
    assert call["usage"]["total_tokens"] == 1100 + 1200  # the two answers, never the refusal
    assert [o["attempt"] for o in call["outputs"]] == [1, 2] and call["outputs"][1]["content"] == REAL
    with db.session() as s_:
        row = db.LlmCall(
            stage="extract", service="radar", model="m", requests=call["requests"], http=call["http"]
        )
        s_.add(row)
        s_.flush()
        db.keep_outputs(s_, row, call)
        rid = row.id
    with db.session() as s_:
        assert (s_.get(db.LlmCall, rid).requests, s_.get(db.LlmCall, rid).http) == (3, [200, 429, 200])
        assert len(s_.scalars(select(db.LlmOutput).where(db.LlmOutput.llm_call_id == rid)).all()) == 2
