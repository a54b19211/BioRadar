"""Producer measure.v6 (B r2 sections 2-3, Codex 691/711): a measure cites where its period / object / scope /
denominator literal is written; Radar looks only there, in the candidate's own send, bound to its artifact.
Success and failure samples S1-S3, F1-F6 of the design; the checker stays v10 (judged as it is)."""

import extraction_contract as ec
import pytest

from bioradar import db
from bioradar.material import Material, sha256
from bioradar.research import measure_checks as mc
from bioradar.research import schemas

TEXT = (
    "# 2026年4月监测周报\n\n"
    "本周通报如下。医院A共报告新增病例23例。另有说明。\n\n"
    "Table 2. 2026年4月各医院报告数\n"
    "| 医院 | 新增病例 |\n| --- | --- |\n"
    "| 医院B | 新增病例31例 |\n| 医院C | 新增病例17例 |\n"
)
PART = {
    "kind": "clean_text",
    "source": "who_don",
    "record_id": "R1",
    "version_id": "v1",
    "raw_sha256": "a" * 64,
    "is_demo": True,
    "processor_version": "clean_text.v12",
}
SENTENCE_QUOTE = "医院A共报告新增病例23例"
ROW_QUOTE = "| 医院B | 新增病例31例 |"


def test_the_producer_and_checker_versions():
    assert mc.CHECKER == ec.CHECKER_V10 and mc.PRODUCER == {
        "name": "bioradar.triage",
        "version": "measure.v7",
    }


def measure(quote=SENTENCE_QUOTE, **over):
    base = dict(
        object_text="医院A",
        metric_text="新增病例",
        value_text="23",
        unit_text="例",
        period_text="2026年4月",
        period="2026-04",
        period_type="interval",
        kind="flow",
        qualifier="exact",
        modality="actual",
        status="reported",
        quote=quote,
    )
    base.update(over)
    return schemas.Measure(**base).model_dump()


class Cand:
    def __init__(self, m, quote, material_read_id=None):
        self.id, self.measure, self.material_read_id = 1, m, material_read_id
        self.quote_start = TEXT.index(quote)
        self.quote_end = self.quote_start + len(quote)
        self.source, self.record_id, self.version_id, self.is_demo = "who_don", "R1", "v1", True
        self.raw_sha256, self.processor_version, self.text_sha256 = "a" * 64, "clean_text.v12", sha256(TEXT)


def ranges(*parts):
    return [(TEXT.index(p), TEXT.index(p) + len(p)) for p in parts]


HEADING = "# 2026年4月监测周报"
CAPTION = "Table 2. 2026年4月各医院报告数"
HEADER = "| 医院 | 新增病例 |\n| --- | --- |\n"


def located(m, quote, cited):
    c = Cand(m, quote)
    return mc.locate(TEXT, c.quote_start, c.quote_end, m, cited)


def test_s3_without_sources_it_reads_as_measure_v5():
    """No sources (a stored measure.v5 measure has no such key): the v5 locating rules, the same spans and the
    same request; a period written only in the heading is still not located."""
    legacy = measure(period_text="", period="", period_type="")
    legacy.pop("sources")
    new = measure(period_text="", period="", period_type="")
    assert located(legacy, SENTENCE_QUOTE, None) == located(new, SENTENCE_QUOTE, None)
    assert located(legacy, SENTENCE_QUOTE, None)[1] is None
    assert mc.build(Cand(legacy, SENTENCE_QUOTE), TEXT) == mc.build(Cand(new, SENTENCE_QUOTE), TEXT)
    heading_only = measure()
    heading_only.pop("sources")
    assert located(heading_only, SENTENCE_QUOTE, None) == (None, "period_literal_not_located")


def test_s1_a_period_cited_from_the_heading_is_located_there_and_built():
    m = measure(sources={"period": "heading"})
    spans, why = located(m, SENTENCE_QUOTE, {"heading": ranges(HEADING)})
    assert why is None and spans["period"] == (TEXT.index("2026年4月"), TEXT.index("2026年4月") + 7)
    (body, _), why = mc.build(Cand(m, SENTENCE_QUOTE), TEXT, {"heading": ranges(HEADING)})
    assert why is None
    period = body["items"][0]["fields"]["period"]
    # the heading is inside the primary passage (it extends to the ancestor heading): a span of p1, judged by
    # v10's own heading rule; nothing in v10 is changed for it
    assert period["span"]["passage"] == "p1" and [p["id"] for p in body["passages"]] == ["p1"]


def test_a_cited_context_outside_the_primary_passage_is_a_context_passage_of_its_own():
    far = (
        "# 2026年4月监测周报\n\n"
        + "其他内容不含数字。" * 60
        + "\n\n本周通报如下。医院A共报告新增病例23例。\n"
    )
    m = measure(sources={"period": "heading"})
    c = Cand(m, SENTENCE_QUOTE)
    c.quote_start = far.index(SENTENCE_QUOTE)
    c.quote_end = c.quote_start + len(SENTENCE_QUOTE)
    c.text_sha256 = sha256(far)
    (body, _), why = mc.build(c, far, {"heading": [(0, len("# 2026年4月监测周报"))]})
    assert why is None
    by_id = {p["id"]: p for p in body["passages"]}
    period = body["items"][0]["fields"]["period"]["span"]
    if period["passage"] != "p1":
        assert by_id[period["passage"]]["role"] == "context"
        spans = sorted((p["start"], p["end"]) for p in body["passages"])
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))  # never overlapping


def test_s2_a_period_cited_from_the_caption_of_the_value_table():
    m = measure(ROW_QUOTE, object_text="医院B", value_text="31", sources={"period": "table_title"})
    spans, why = located(m, ROW_QUOTE, {"table_title": ranges(CAPTION)})
    assert why is None and CAPTION.find("2026年4月") >= 0
    assert TEXT.index(CAPTION) <= spans["period"][0] < TEXT.index(CAPTION) + len(CAPTION)


def test_f2_a_cited_source_that_does_not_hold_the_literal_is_not_searched_further():
    m = measure(period_text="2026年5月", period="2026-05", sources={"period": "heading"})
    assert located(m, SENTENCE_QUOTE, {"heading": ranges(HEADING)}) == (None, "period_not_in_cited_source")


def test_f3_a_header_or_caption_of_another_table_is_refused():
    m = measure(sources={"period": "table_title"})  # the value is in a sentence, not in that table
    assert located(m, SENTENCE_QUOTE, {"table_title": ranges(CAPTION)}) == (None, "source_not_of_value_table")


def test_f4_a_literal_twice_in_the_cited_source_is_ambiguous():
    m = measure(sources={"period": "window"})
    assert located(m, SENTENCE_QUOTE, {"window": [(0, len(TEXT))]}) == (
        None,
        "period_ambiguous_in_cited_source",
    )


def test_f6_quote_cited_means_the_quote_only():
    m = measure(object_text="本周通报", sources={"object": "quote"})
    assert located(m, SENTENCE_QUOTE, {}) == (None, "object_not_in_cited_source")
    legacy = measure(object_text="本周通报")  # measure.v5 rules: the clause beyond the quote
    spans, why = located(legacy, SENTENCE_QUOTE, None)
    assert why is None or why == "object_literal_not_located"


def test_a_cited_source_without_its_resolution_is_not_sent():
    m = measure(sources={"period": "heading"})
    assert located(m, SENTENCE_QUOTE, None) == (None, "source_not_sent_to_model")


# ---- cited_sources: the candidate's own send only -------------------------------------------------------


def sends(window_ref="W1", status="sent", contexts=("heading",), part=None):
    material = Material.part(TEXT, part or PART)
    with db.session() as s:
        call = db.LlmCall(
            stage="extract",
            service="radar",
            model="fake",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            seconds=0,
        )
        s.add(call)
        s.flush()

        def row(mat, ref):
            r = db.material_read(
                mat,
                stage="extract",
                service="radar",
                ref=ref,
                source="who_don",
                record_id="R1",
                version_id="v1",
                llm_call_id=call.id,
            )
            r.status = status
            return r

        w = row(material.slice(0, len(TEXT)), window_ref)
        rows = [w] + [row(material.slice(*ranges(HEADING)[0]), f"{window_ref}.{c}") for c in contexts]
        s.add_all(rows)
        s.flush()
        return w.id


def cand(m, material_read_id):
    return Cand(m, SENTENCE_QUOTE, material_read_id)


def test_cited_sources_come_from_the_candidates_own_call():
    wid = sends()
    m = measure(sources={"period": "heading"})
    with db.session() as s:
        got, why = mc.cited_sources(s, cand(m, wid))
    assert why is None and got == {"heading": ranges(HEADING)}
    with db.session() as s:  # no sources cited: nothing to resolve
        assert mc.cited_sources(s, cand(measure(), None)) == ({}, None)


@pytest.mark.parametrize(
    "case,reason",
    [
        (
            "no_send",
            "source_not_sent_to_model",
        ),  # F1: e.g. triage, or a candidate from before its send was kept
        ("context_not_sent", "source_not_sent_to_model"),
        ("failed_call", "source_not_sent_to_model"),
        ("other_processor", "source_binding_stale"),  # F5: an older cleaned artifact's send
    ],
)
def test_f1_f5_a_source_not_sent_with_this_candidate_or_of_another_artifact_is_refused(case, reason):
    m = measure(sources={"period": "heading"})
    if case == "no_send":
        wid = None
    elif case == "context_not_sent":
        wid = sends(contexts=())
    elif case == "failed_call":
        wid = sends(status="failed")
    else:
        wid = sends(part=dict(PART, processor_version="clean_text.v11"))
    with db.session() as s:
        assert mc.cited_sources(s, cand(m, wid)) == (None, reason)


def test_another_send_of_the_same_text_is_never_borrowed():
    sends(window_ref="W1")  # an earlier call that did send the heading
    wid = sends(window_ref="W1", contexts=())  # this candidate's call did not
    with db.session() as s:
        assert mc.cited_sources(s, cand(measure(sources={"period": "heading"}), wid)) == (
            None,
            "source_not_sent_to_model",
        )
