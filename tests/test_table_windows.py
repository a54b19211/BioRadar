"""A table row keeps its column header in every window that goes out - read, BioCheck (report) and
MechEvidence - as real slices of the same part with their own ranges, also after the window is stored and cut
again; when the header does not fit, the window says so; a search hit is placed by its bound artifact range,
not by the first matching text."""

from types import SimpleNamespace

from fabric_receipts import sources_receipt
from sqlalchemy import select

from bioradar import db
from bioradar.adapters import services
from bioradar.config import settings
from bioradar.material import Material, part_key, reread, restore, sha256, tables_of
from bioradar.research import analyses
from bioradar.research.pipeline import Run
from bioradar.research.run import submit
from bioradar.research.run.windows import adopt, excerpt, hit_spans, report_text, table_header_omitted

HEADER = (
    "| 序号 | 药品品名 | 标示生产企业名称 | 检验结果 | 不合格项目 |\n| ---- | ---- | ---- | ---- | ---- |\n"
)
HEADER_B = "| 编号 | 品名 | 企业 | 结论 | 原因 |\n| --- | --- | --- | --- | --- |\n"
CLEAN = {
    "kind": "clean_text",
    "source": "nmpa",
    "record_id": "R1",
    "version_id": "v1",
    "raw_sha256": "a" * 64,
    "is_demo": True,
    "processor_version": "clean_text.v11",
}
HEADING = (
    "【附件正文（附件版本 31b259997b89ba4c，所属页面 https://example.invalid/page；由 excel 从原件解析，"
    "表格和扫描件的识别可能有误，以原件为准）】\n\n"
)


def rows(n, start=1, result="合格"):
    return "".join(
        f"| {i} | 阿莫西林胶囊{i:04d} | 某某药业有限公司第{i:04d}厂 | {result} | |\n"
        for i in range(start, start + n)
    )


def doc_material(text):
    return Material.literal(HEADING) + Material.part(text, CLEAN)


def resolve(text):
    return lambda parts: {part_key(p): text for p in parts}


def row_of(text, i):
    a = text.index(f"| {i} | ")
    return text[a : text.index("\n", a)]


def test_tables_are_found_per_table_even_back_to_back():
    text = "说明\n" + HEADER + rows(3) + HEADER_B + rows(2, 10) + "\n结尾\n" + HEADER + rows(1, 20)
    tables = tables_of(text)
    assert [text[t["header_start"] : t["header_end"]] for t in tables] == [HEADER, HEADER_B, HEADER]
    assert text[tables[0]["start"] : tables[0]["end"]] == rows(3).rstrip("\n")
    assert text[tables[1]["start"] : tables[1]["end"]] == rows(2, 10).rstrip("\n")
    assert tables_of("| 只有一行 |\n普通文字\n") == []


def test_read_window_brings_the_header_of_a_selected_row():
    text = "检验结果公告\n" + HEADER + rows(400)
    material = doc_material(text)
    read, window = adopt(material, ["阿莫西林胶囊0350"], 3000)
    assert window == "selected" and len(read) <= 3000 + 200
    assert HEADER.rstrip("\n") in read.text and row_of(text, 350) in read.text
    assert read.text.index(HEADER.splitlines()[0]) < read.text.index(row_of(text, 350))  # original order
    assert table_header_omitted(read.text) == 0
    assert reread(read.binding(), resolve(text)) == read.text
    # the header is a slice of the same part at its own range, not text written by the radar
    header_start = text.index(HEADER)
    assert any(s.get("start") == header_start for s in read.binding()["segments"])


def test_stored_window_cut_again_for_biocheck_still_has_the_header():
    text = "检验结果公告\n" + HEADER + rows(400)
    read, _ = adopt(doc_material(text), ["阿莫西林胶囊0350"], 3000)
    stored = restore(read.text, read.binding())
    report = report_text(stored, [row_of(text, 350)[:40]], 300)
    assert HEADER.splitlines()[0] in report.text and row_of(text, 350) in report.text
    assert table_header_omitted(report.text) == 0
    assert reread(report.binding(), resolve(text)) == report.text
    # an excerpt straight from the whole document does the same
    whole = excerpt(doc_material(text), [row_of(text, 120)[:40]], 300)
    assert HEADER.splitlines()[0] in whole.text and row_of(text, 120) in whole.text


def test_a_header_that_does_not_fit_is_said_in_the_window_sent():
    long_header = "| " + " | ".join(f"第{i}列名称" for i in range(200)) + " |\n|" + " --- |" * 200 + "\n"
    text = long_header + rows(300)
    material = doc_material(text)
    target = text.index(row_of(text, 250))
    spans = [(len(HEADING) + target, len(HEADING) + target + 10)]
    read, _ = adopt(material, [], 1500, hits=spans)
    assert row_of(text, 250) in read.text and "第0列名称" not in read.text
    assert table_header_omitted(read.text) == 1 and "不能按列名解读" in read.text.splitlines()[0]
    report = excerpt(doc_material(text), [row_of(text, 250)[:40]], 400)
    assert table_header_omitted(report.text) == 1 and row_of(text, 250) in report.text


def test_mechevidence_receives_the_header_or_the_notice(monkeypatch):
    long_header = "| " + " | ".join(f"第{i}列名称" for i in range(200)) + " |\n|" + " --- |" * 200 + "\n"
    for head, expect_header in ((HEADER, True), (long_header, False)):
        text = head + rows(300)
        material = Material.part(text, CLEAN)
        target = text.index(row_of(text, 250))
        read, window = adopt(material, ["抑制"], 1500, hits=[(target, target + 10)])
        row = analyses.create(question="是否不合格？", direction="science", is_demo=True, enqueue=False)
        listed = SimpleNamespace(sources=lambda: sources_receipt({"nmpa"}))  # nmpa is readable
        run = Run(
            row, settings().model_copy(update={"mechevidence_url": "http://mech.invalid"}), fabric=listed
        )
        run.p.update(facts=[{"ref": "E1"}], mechanism={"statement": "X 抑制 Y"})
        with db.session() as s:
            read_id = submit.sent(
                run, s, "read", "radar", "E1", {"source": "nmpa", "record_id": "R1", "version_id": "v1"}, read
            )
            s.add(
                db.Evidence(
                    analysis_id=run.id,
                    ref="E1",
                    source="nmpa",
                    record_id="R1",
                    version_id="v1",
                    title="抽检",
                    material_kind="primary_research",
                    verified=True,
                    text=read.text,
                    meta={"material_read_id": read_id, "window": window},
                )
            )
        bodies = []
        monkeypatch.setattr(
            services, "mechevidence", lambda s, body: bodies.append(body) or {"llm_calls": []}
        )
        run.mechanism()
        sent = bodies[-1]["materials"][0]["text"]
        assert row_of(text, 250) in sent
        assert (HEADER.splitlines()[0] in sent) is expect_header
        assert (table_header_omitted(sent) == 0) is expect_header
        with db.session() as s:
            mine = select(db.MaterialRead).where(db.MaterialRead.analysis_id == run.id)
            bound = {r.stage: r.binding for r in s.scalars(mine)}
        assert reread(bound["mechanism"], resolve(text)) == sent


def header_before(window, at):
    """Which table header is the nearest one above position `at` of a window: "A", "B" or None. Lines left
    in the budget are filled with other rows (and their own headers), so only the nearest one counts."""
    a = window.rfind(HEADER.splitlines()[0], 0, at)
    b = window.rfind(HEADER_B.splitlines()[0], 0, at)
    return None if a == b == -1 else "A" if a > b else "B"


def test_adjacent_tables_never_lend_each_other_a_header():
    text = HEADER + rows(200) + HEADER_B + rows(200, 1000)
    read, _ = adopt(doc_material(text), ["阿莫西林胶囊1150"], 2000)
    assert header_before(read.text, read.text.index(row_of(text, 1150))) == "B"
    read, _ = adopt(doc_material(text), ["阿莫西林胶囊0150"], 2000)
    assert header_before(read.text, read.text.index(row_of(text, 150))) == "A"
    # the rows of table B never sit under table A's header in the window
    for line in read.text.split("\n"):
        if "阿莫西林胶囊1" in line and line.startswith("| 1"):
            assert header_before(read.text, read.text.index(line)) == "B"


def test_hit_is_placed_by_its_artifact_range_not_the_first_matching_text():
    same = rows(5, 7)
    text = HEADER + rows(60) + same + "\n其他说明\n\n" + HEADER_B + rows(60, 500) + same
    material = doc_material(text)
    second = text.rindex(same)
    place = {
        "version_id": "v1",
        "processor_version": "clean_text.v11",
        "text_sha256": sha256(text),
        "range": [second, second + len(same)],
    }
    spans, unresolved = hit_spans(material, [place])
    assert unresolved == [] and spans == [(len(HEADING) + second, len(HEADING) + second + len(same))]
    assert material.text[slice(*spans[0])] == same  # the 171-char style heading only shifts the display
    read, _ = adopt(material, [], 1200, hits=spans)
    # the hit's rows are the second copy (table B's), under table B's header
    assert any(s.get("start") == second for s in read.binding()["segments"])
    assert header_before(read.text, read.text.rindex(same.splitlines()[0])) == "B"
    # without a range: only a unique text stands in; a repeated one is said, never guessed
    assert hit_spans(material, [{"version_id": "v1", "text": same}]) == ([], ["hit_position_ambiguous"])
    unique = row_of(text, 530)
    assert hit_spans(material, [{"version_id": "v1", "text": unique}])[0] == [
        (material.text.index(unique), material.text.index(unique) + len(unique))
    ]
    # another artifact than the one read: no position, and said
    other = dict(place, text_sha256="b" * 64)
    assert hit_spans(material, [other]) == ([], ["hit_artifact_not_in_material"])


def test_bindings_stored_before_tables_still_restore_and_read_back():
    text = "公告\n" + HEADER + rows(400)
    read, _ = adopt(doc_material(text), ["阿莫西林胶囊0350"], 3000)
    binding = read.binding()
    for s in binding["segments"]:
        s.pop("tables", None)
    old = restore(read.text, binding)
    assert reread(binding, resolve(text)) == old.text == read.text
    report = excerpt(old, [row_of(text, 350)[:40]], 300)  # no table knowledge: the old behaviour, no notice
    assert row_of(text, 350) in report.text and table_header_omitted(report.text) == 0
