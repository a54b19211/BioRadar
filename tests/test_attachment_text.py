"""v3 3.3a/3.7: an attachment version is read with its parsed text from Fabric's processing layer; one not
parsed yet waits (retried later), one that could not be parsed is a recorded gap, never triaged empty."""

import httpx
from fabric_receipts import sources_receipt
from sqlalchemy import select

from bioradar import db
from bioradar.adapters.fabric import Fabric, document
from bioradar.config import settings
from bioradar.research import monitor
from bioradar.research.monitor import route

PARENT = "https://www.nmpa.gov.cn/xxgk/ggtg/1.html"
URL = "https://www.nmpa.gov.cn/directory/web/nmpa/images/a.doc"
V = "c" * 64


def attachment(version=V, source="nmpa"):
    return {
        "source_id": source,
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


def fabric_with(state, source="nmpa", known=True):
    """Fabric with its source receipt; known=False: the capabilities could not be read (an older Fabric)."""
    sent = []

    def handler(request):
        sent.append(request.url.path)
        if request.url.path == "/api/fabric/sources":
            return httpx.Response(200, json=sources_receipt({source}))
        if request.url.path == "/api/fabric/records":
            return httpx.Response(200, json={"items": [attachment(source=source)], "next_cursor": None})
        if request.url.path == "/api/fabric/derived-text":
            return httpx.Response(200, json={"versions": {V: state}})
        return httpx.Response(404, json={})

    client = httpx.Client(base_url="http://fabric.invalid", transport=httpx.MockTransport(handler))
    fabric = Fabric(settings(), client=client)
    if known:
        fabric.load_capabilities()
    return fabric, sent


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
    assert doc["text"].startswith("【附件正文（附件版本") and "注销药品注册证书目录" in doc["text"]
    # the heading is a fixed template with identifiers only; the attachment text is a bound part (v3 6.1)
    heading, body = doc["material"].segments[0], doc["material"].segments[1]
    assert heading[1] is None and doc["title"] not in heading[0] and body[1]["kind"] == "clean_text"
    assert PARENT in doc["text"]
    assert route(doc) is None
    assert "/api/fabric/derived-text" in sent


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
                if r.url.path == "/api/fabric/records"
                else httpx.Response(404, json={})
            )
        ),
    )
    [doc], _ = fabric.search("nmpa", (), is_demo=False)
    assert doc["attachment_text"] == {"state": "unavailable"} and route(doc) == "attachment_pending"


def test_every_source_fabric_declares_attachment_text_for_is_read_that_way():
    """The radar keeps no list of attachment sources: a PubMed full-text attachment or an HKEX filing's
    attachment gets its parsed text from Fabric's processing layer like an NMPA one (before, only nmpa, cde
    and chictr did; the other 17 sources Fabric declares were read as plain clean text)."""
    for source in ("hkex", "pubmed", "fda_press", "local_files"):
        fabric, sent = fabric_with(PARSED, source=source)
        [doc], _ = fabric.search(source, (), is_demo=False)
        assert (
            doc["text"].startswith("【附件正文（附件版本") and doc["attachment_text"]["status"] == "parsed"
        ), source
        assert "/api/fabric/derived-text" in sent and "/api/fabric/clean-text" not in sent
        assert route(doc) is None


def test_an_attachment_of_a_source_without_attachment_text_is_read_as_clean_text():
    fabric, sent = fabric_with(PARSED, source="who_don")
    fabric.search("who_don", (), is_demo=False)
    assert "/api/fabric/derived-text" not in sent and "/api/fabric/clean-text" in sent


def test_with_the_capabilities_unknown_an_attachment_waits_and_is_not_called_empty():
    fabric, sent = fabric_with(PARSED, known=False)
    [doc], _ = fabric.search("nmpa", (), is_demo=False)
    assert doc["attachment_text"] == {"state": "capability_unknown"} and doc["text"] == ""
    assert route(doc) == "attachment_pending"  # kept for the retry path, not a gap
    assert "/api/fabric/derived-text" not in sent and "/api/fabric/clean-text" not in sent


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
