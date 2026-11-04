"""v3 3.3a/3.7: an attachment version is read with its parsed text from Fabric's processing layer; one not
parsed yet waits (retried later), one that could not be parsed is a recorded gap, never triaged empty."""

import httpx
from sqlalchemy import select

from bioradar import db
from bioradar.adapters.fabric import Fabric, document
from bioradar.config import settings
from bioradar.research import monitor
from bioradar.research.monitor import route

PARENT = "https://www.nmpa.gov.cn/xxgk/ggtg/1.html"
URL = "https://www.nmpa.gov.cn/directory/web/nmpa/images/a.doc"
V = "c" * 64


def attachment(version=V):
    return {
        "source_id": "nmpa",
        "source_record_id": PARENT + "#attachment=" + URL,
        "version_id": version,
        "is_demo": False,
        "record_kind": "document",
        "title": "附件1.doc",
        "source_url": URL,
        "clean_text": "",
        "facts": {"role": "attachment", "parent_record_id": PARENT, "sha256": "e" * 64},
        "first_seen_at": "2026-11-04T01:00:00+00:00",
        "published_seq": 3,
    }


def fabric_with(state):
    sent = []

    def handler(request):
        sent.append(request.url.path)
        if request.url.path == "/api/history/records":
            return httpx.Response(200, json={"items": [attachment()], "next_cursor": None})
        if request.url.path == "/api/history/derived-text":
            return httpx.Response(200, json={"versions": {V: state}})
        return httpx.Response(404, json={})

    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    return Fabric(settings(), client=client), sent


PARSED = {
    "state": "processed",
    "status": "parsed",
    "text": "# 注销药品注册证书目录\n\n| 序号 | 药品 |",
    "attempt": 1,
    "parse_method": "mineru",
    "processor_version": "datafabric.attachment_parse.v1",
}


def test_parsed_attachment_text_becomes_the_document_text_with_its_parent_named():
    fabric, sent = fabric_with(PARSED)
    [doc], _ = fabric.search("nmpa", (), is_demo=False)
    assert doc["role"] == "attachment" and doc["parent_record_id"] == PARENT
    assert doc["text"].startswith("【附件正文：") and "注销药品注册证书目录" in doc["text"]
    assert PARENT in doc["text"]
    assert route(doc) is None
    assert "/api/history/derived-text" in sent


def test_not_yet_parsed_attachment_waits_and_final_failure_is_a_gap():
    fabric, _ = fabric_with({"state": "not_visible_or_not_processed"})
    [doc], _ = fabric.search("nmpa", (), is_demo=False)
    assert doc["text"] == "" and route(doc) == "attachment_pending"
    assert route(dict(doc, retry=True)) == "attachment_pending"  # a retry before parsing keeps waiting
    fabric, _ = fabric_with(
        {"state": "processed", "status": "failed", "error": "parser_failed", "attempt": 3}
    )
    [doc], _ = fabric.search("nmpa", (), is_demo=False)
    assert route(doc) == "attachment_unparsed"
    fabric, _ = fabric_with(
        {"state": "processed", "status": "failed", "error": "parser_failed", "attempt": 1}
    )
    [doc], _ = fabric.search("nmpa", (), is_demo=False)
    assert route(doc) == "attachment_pending"  # Fabric retries up to 3 times


def test_older_fabric_without_the_endpoint_leaves_the_attachment_pending():
    fabric, _ = fabric_with(None)
    fabric.client = httpx.Client(
        base_url="http://fabric.invalid",
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json={"items": [attachment()], "next_cursor": None})
                if r.url.path == "/api/history/records"
                else httpx.Response(404, json={})
            )
        ),
    )
    [doc], _ = fabric.search("nmpa", (), is_demo=False)
    assert doc["attachment_text"] == {"state": "unavailable"} and route(doc) == "attachment_pending"


def test_pending_is_kept_for_retry_and_a_gap_is_recorded(monkeypatch):
    pending = document(attachment("1" * 64))
    pending["attachment_text"] = {"state": "not_visible_or_not_processed"}
    gap = document(attachment("2" * 64))
    gap["attachment_text"] = {
        "state": "processed",
        "status": "failed",
        "error": "original_digest_mismatch",
        "attempt": 3,
    }
    monitor.settle_attachments([pending, gap], is_demo=False)
    monitor.settle_attachments([pending, gap], is_demo=False)  # idempotent
    with db.session() as session:
        rows = {o.version_id: o for o in session.scalars(select(db.Observation))}
    assert rows["1" * 64].reason == "triage_failed"  # picked up again by the retry path once parsed
    assert (
        rows["2" * 64].reason.startswith("附件未能解析")
        and "original_digest_mismatch" in rows["2" * 64].reason
    )
    assert rows["2" * 64].alert == "none"
