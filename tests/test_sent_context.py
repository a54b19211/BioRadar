"""B r2 section 0 (Codex 711.1): every context a continuation window sends - heading, table header, table
caption - is its own material_read of the same model call, its recorded text exactly the text sent; every
candidate names the material_read of the window it was read from, never another send of the same text."""

from sqlalchemy import select
from test_continuation import QUOTE, feed, model, stand_ins, text_with  # noqa: F401

from bioradar import db
from bioradar.adapters import llm
from bioradar.material import Material
from bioradar.research import continuation, monitor

CLEAN = {
    "kind": "clean_text",
    "source": "nmpa",
    "record_id": "R1",
    "version_id": "v1",
    "raw_sha256": "a" * 64,
    "is_demo": True,
    "processor_version": "clean_text.v12",
}
HEADER = "| 月份 | 注册数 |\n| --- | --- |\n"


def table_rows(n):
    return "".join(f"| 2026-{i % 12 + 1:02d} | {100 + i} |\n" for i in range(n))


def contexts(text, a, b):
    out = continuation._contexts(Material.part(text, CLEAN), a, b)
    return {k: (at, c.text if c else None) for k, (at, c) in out.items()}


def test_a_window_inside_a_table_sends_its_heading_header_and_caption_as_bound_slices():
    text = "# 年报\n\n正文。\n\nTable 1. 2026年各月注册数\n" + HEADER + table_rows(40)
    a = text.index("| 2026-05 |")
    got = contexts(text, a, a + 60)
    assert got["heading"] == (0, "# 年报")
    assert got["table_header"][1] == HEADER
    assert got["table_title"] == (text.index("Table 1."), "Table 1. 2026年各月注册数")
    c = continuation._contexts(Material.part(text, CLEAN), a, a + 60)["table_title"][1]
    assert c.binding()["segments"][0]["part"]["processor_version"] == "clean_text.v12"  # bound, not literal


def test_a_caption_is_only_the_line_right_above_its_own_table():
    text = "Table 1. 甲表\n" + HEADER + table_rows(3) + "\n中间一句说明。\n\n" + HEADER + table_rows(40)
    a = text.rindex("| 2026-05 |")
    got = contexts(text, a, a + 40)
    assert got["table_header"][1] == HEADER and got["table_title"] == (None, None)  # not table 1's caption
    assert contexts("正文。\n" + HEADER + table_rows(40), 60, 90)["table_title"] == (None, None)


def test_each_context_is_a_material_read_of_the_same_call_and_the_candidate_names_its_window(
    stand_ins,  # noqa: F811
):
    calls, _ = stand_ins
    text = "# 公告\n" + text_with(3000)[5:]
    monitor.tick(is_demo=True, fabric=feed(text))
    first = [p for st, p in calls if st == "extract"][0]["windows"]
    with db.session() as s:
        reads = list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.stage == "extract")))
        by_ref = {}
        for r in reads:
            by_ref.setdefault(r.llm_call_id, {})[r.ref] = r
        call = by_ref[min(by_ref)]
        assert {"W1", "W1.heading"} <= set(call)
        heading = call["W1.heading"]
        assert heading.status == "sent" and heading.chars == len(first[0]["heading"]["text"])
        assert all(seg["part"]["kind"] == "clean_text" for seg in heading.binding["segments"])
        cands = list(s.scalars(select(db.MeasurementCandidate).order_by(db.MeasurementCandidate.id)))
        [found] = [c for c in cands if c.quote_start and c.quote_start > 2000]
        window = s.get(db.MaterialRead, found.material_read_id)
        assert window.stage == "extract" and "." not in window.ref and window.status == "sent"
        seg = window.binding["segments"][0]
        assert seg["start"] <= found.quote_start and found.quote_end <= seg["end"]  # the window that read it


def test_a_triage_candidate_names_the_triage_send_of_its_record(stand_ins):  # noqa: F811
    text = "2026年4月，" + QUOTE + "。" + "本公告的其他内容说明如下，详见正文。" * 3
    monitor.tick(is_demo=True, fabric=feed(text))
    with db.session() as s:
        [cand] = list(s.scalars(select(db.MeasurementCandidate)))
        sent = s.get(db.MaterialRead, cand.material_read_id)
        assert (sent.stage, sent.ref, sent.status, sent.version_id) == (
            "triage",
            "R1",
            "sent",
            cand.version_id,
        )


def test_a_failed_call_marks_its_contexts_failed_too(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "chat_json", model(calls, error_for=lambda n: "model_error"))
    text = "# 公告\n" + text_with(3000)[5:]
    monitor.tick(is_demo=True, fabric=feed(text))
    with db.session() as s:
        reads = list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.stage == "extract")))
        assert reads and any("." in r.ref for r in reads)
        assert {r.status for r in reads} == {"failed"}
