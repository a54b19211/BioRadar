"""T-24 DoD: two continuous inputs through the real checker v14 (BioCheck in-process, no model) -
public-health
bulletins whose later issue revises an earlier month (late reports), and quarterly industrial output with one
quarter restated later. Each: only the affected period is redone, a replay before the revision sees the old
value, and running again after a restart adds nothing."""

from datetime import UTC, datetime

import pytest
from conftest import FakeFabric, record
from sqlalchemy import func, select
from test_measure_checks import SRC, real_post

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import measure_checks, monitor, schemas

pytestmark = pytest.mark.skipif(not SRC, reason="BIORADAR_TEST_BIOCHECK_SRC (a BioCheck checkout) is not set")
CALL = {"stage": "triage", "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0, "error": None}


def run(monkeypatch, topic, direction, specs, ticks_after=1):
    """specs: [(record id, available, text, [measures], relations)], one tick per record in order (each tick
    sees the records available so far), then `ticks_after` more ticks over all of them (a restart: the same
    records again)."""
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: real_post(body))
    by_text = {t: ms for _, _, t, ms, _ in specs}
    records = [record(rid, topic, t, available=at, relations=rel) for rid, at, t, _, rel in specs]

    def model(settings_, stage, system, payload, model_cls, **options):
        if stage == "extract":
            return schemas.Extraction(
                items=[schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
            ), dict(CALL, stage=stage)
        index = {t["title"]: t["index"] for t in payload["topics"]}
        return schemas.Triage(
            items=[
                schemas.TriageItem(
                    index=r["index"],
                    topic=index.get(topic),
                    new_topic="" if topic in index else topic,
                    direction=direction,
                    alert="normal",
                    measures=by_text[r["text"]],
                )
                for r in payload["records"]
            ]
        ), CALL

    monkeypatch.setattr(llm, "chat_json", model)
    for n in range(1, len(records) + 1):
        monitor.tick(is_demo=True, fabric=FakeFabric(records[:n], clean=True))
    before = snapshot()
    for _ in range(ticks_after):
        monitor.tick(is_demo=True, fabric=FakeFabric(records, clean=True))
    return before, snapshot()


def snapshot():
    with db.session() as s:
        count = {
            t: s.scalar(select(func.count()).select_from(getattr(db, t)))
            for t in ("Measurement", "MeasurementCandidate", "MeasurementCheck", "Observation")
        }
        # the data a restart must not add to; a topic state event (no new records: it calms down) is the
        # topic living on, not a repeat
        count["events"] = sorted(
            (e.kind, e.detail.get("reason"), e.detail.get("record"))
            for e in s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind != "state"))
        )
        points = [
            (p.record_id, p.period, p.value_text, p.superseded_by is not None, p.dims_reasons)
            for p in s.scalars(select(db.Measurement).order_by(db.Measurement.id))
        ]
        series = [
            (e.detail.get("record"), sorted({c["kind"] for c in e.detail.get("changes") or []}))
            for e in s.scalars(
                select(db.TopicEvent).where(db.TopicEvent.kind == "series").order_by(db.TopicEvent.id)
            )
        ]
        checks = sorted({(c.checker_version, c.status) for c in s.scalars(select(db.MeasurementCheck))})
    return {"count": count, "points": points, "series": series, "checks": checks}


def comparability_at(record_id, period, at):
    with db.session() as s:
        [p] = s.scalars(select(db.Measurement).filter_by(record_id=record_id, period=period))
        return measure_checks.comparability(s, p, at)


# ---- public health: a later bulletin revises an earlier month (late reports) -----------------------------

HEAD = "## 按报告日统计\n\n"


def case_measure(text, month, value, lead=""):
    sentence = text[text.index(f"{lead}2026年{month}月") + len(lead) :]
    sentence = sentence[: sentence.index("。")]
    return schemas.Measure(
        object_text="医院A",
        metric_text="报告成人炭疽确诊病例",
        value_text=str(value),
        unit_text="例",
        period_text=f"2026年{month}月",
        period=f"2026-{month:02d}",
        period_type="interval",
        kind="flow",
        scope_text="在湖北省",
        qualifier="exact",
        modality="actual",
        status="reported",
        quote=sentence,
        family="case_count",
        dims=[
            schemas.Dim(kind="disease", text="炭疽"),
            schemas.Dim(kind="population", text="成人"),
            schemas.Dim(kind="date_basis", text="按报告日统计", source="window"),
        ],
    )


def line(month, value):
    return f"2026年{month}月，医院A在湖北省共报告成人炭疽确诊病例{value}例。"


def bulletins():
    t1 = HEAD + line(1, 10)
    lead = "经补报订正，"
    t2 = HEAD + line(2, 12) + "\n\n" + lead + line(1, 13)
    t3 = HEAD + line(3, 15)
    return [
        ("B1", "2026-02-03T08:00:00+08:00", t1, [case_measure(t1, 1, 10)], ()),
        (
            "B2",
            "2026-03-03T08:00:00+08:00",
            t2,
            [case_measure(t2, 2, 12), case_measure(t2, 1, 13, lead)],
            [("corrects", "B1")],
        ),
        ("B3", "2026-04-03T08:00:00+08:00", t3, [case_measure(t3, 3, 15)], ()),
    ]


def test_late_reports_revise_only_the_month_they_name_and_a_replay_before_them_sees_the_old_value(
    monkeypatch,
):
    before, after = run(monkeypatch, "炭疽", "public_health", bulletins())
    assert after == before  # a restart over the same bulletins adds nothing
    assert before["checks"] == [("biocheck.extraction_rules.v16", "consistent")]
    assert [(r, p, v, s) for r, p, v, s, _ in before["points"]] == [
        ("B1", "2026-01", "10", True),
        ("B2", "2026-02", "12", False),
        ("B2", "2026-01", "13", False),
        ("B3", "2026-03", "15", False),
    ]
    assert all(reasons == [] for *_, reasons in before["points"])  # every dimension proven: comparable
    # the revision is redone where it is: B2 revises January only; March compares with February and the
    # revised January is not redone again
    assert before["series"] == [("B2", ["revised"]), ("B3", ["new_high", "step"])]
    revised = datetime(2026, 3, 5, tzinfo=UTC)  # after B2 (sqlite in tests keeps no time zone)
    early = datetime(2026, 2, 10, tzinfo=UTC)
    assert "withdrawn_by_correction" in comparability_at("B1", "2026-01", revised)["reasons"]
    assert "withdrawn_by_correction" not in comparability_at("B1", "2026-01", early)["reasons"]


# ---- industrial: quarterly output, one quarter restated --------------------------------------------------

QHEAD = "## 甲公司\n\n### 按报告期统计\n\n"  # the company a section of its own, the basis its heading


def output_measure(text, q, value, lead="", obj="苏州工厂", scope="苏州工厂", entity=True):
    cn = "一二三四"[q - 1]
    sentence = text[text.index(f"{lead}2026年第{cn}季度") + len(lead) :]
    sentence = sentence[: sentence.index("。")]
    dims = [schemas.Dim(kind="date_basis", text="按报告期统计", source="window")]
    if entity:
        dims.insert(0, schemas.Dim(kind="entity", text="甲公司", source="window"))
    return schemas.Measure(
        object_text=obj,
        metric_text="实际产出的注射剂产量",
        value_text=str(value),
        unit_text="万支",
        period_text=f"2026年第{cn}季度",
        period=f"2026Q{q}",
        period_type="interval",
        kind="flow",
        scope_text=scope,
        qualifier="exact",
        modality="actual",
        status="reported",
        quote=sentence,
        family="capacity",
        dims=dims,
    )


def qline(q, value, site="苏州工厂"):
    cn = "一二三四"[q - 1]
    return f"2026年第{cn}季度，{site}实际产出的注射剂产量为{value}万支。"


def quarters():
    texts = [QHEAD + qline(q, v) for q, v in ((1, 120), (2, 130))]
    lead = "更正前期数据，"
    t3 = QHEAD + qline(3, 128) + "\n\n" + lead + qline(2, 125)
    t4 = QHEAD + qline(4, 140)
    return [
        ("Q1", "2026-04-20T08:00:00+08:00", texts[0], [output_measure(texts[0], 1, 120)], ()),
        ("Q2", "2026-07-20T08:00:00+08:00", texts[1], [output_measure(texts[1], 2, 130)], ()),
        (
            "Q3",
            "2026-10-20T08:00:00+08:00",
            t3,
            [output_measure(t3, 3, 128), output_measure(t3, 2, 125, lead)],
            [("corrects", "Q2")],
        ),
        ("Q4", "2027-01-20T08:00:00+08:00", t4, [output_measure(t4, 4, 140)], ()),
    ]


def test_a_restated_quarter_is_redone_alone_and_a_replay_before_the_restatement_sees_the_old_value(
    monkeypatch,
):
    before, after = run(monkeypatch, "甲公司产能", "business", quarters())
    assert after == before
    assert before["checks"] == [("biocheck.extraction_rules.v16", "consistent")]
    assert [(r, p, v, s) for r, p, v, s, _ in before["points"]] == [
        ("Q1", "2026Q1", "120", False),
        ("Q2", "2026Q2", "130", True),
        ("Q3", "2026Q3", "128", False),
        ("Q3", "2026Q2", "125", False),
        ("Q4", "2026Q4", "140", False),
    ]
    assert all(
        reasons == [] for *_, reasons in before["points"]
    )  # entity, stage, coverage, date basis proven
    # Q3 revises Q2 only; Q4 compares with Q3 and the revised Q2 is not redone again
    # Q3 brings its own quarter, then the revision of Q2 with Q3 compared again against the revised Q2; Q4
    # only compares forward - the revised quarter is not redone again
    assert before["series"] == [
        ("Q2", ["step"]),
        ("Q3", ["step"]),
        ("Q3", ["revised", "step"]),
        ("Q4", ["new_high", "step", "streak_up"]),
    ]
    restated = datetime(2026, 10, 22, tzinfo=UTC)  # after Q3 (sqlite in tests keeps no time zone)
    early = datetime(2026, 8, 1, tzinfo=UTC)
    assert "withdrawn_by_correction" in comparability_at("Q2", "2026Q2", restated)["reasons"]
    assert "withdrawn_by_correction" not in comparability_at("Q2", "2026Q2", early)["reasons"]


def test_the_company_written_fused_with_its_site_is_kept_but_never_compared(monkeypatch):
    """Codex 783: the original business sentence - 甲公司苏州工厂 - proves no entity under v15 either; its
    points are kept, checked, and out of comparison with the entity gap; no series step is computed from
    them."""
    head = "## 按报告期统计\n\n"
    specs = []
    for n, (q, v) in enumerate(((1, 120), (2, 130)), 1):
        text = head + qline(q, v, site="甲公司苏州工厂")
        m = output_measure(text, q, v, obj="甲公司苏州工厂", scope="甲公司苏州工厂", entity=False)
        m = m.model_copy(update={"dims": [schemas.Dim(kind="entity", text="甲公司")] + list(m.dims)})
        specs.append((f"F{n}", f"2026-0{3 * n + 1}-20T08:00:00+08:00", text, [m], ()))
    before, after = run(monkeypatch, "甲公司产能", "business", specs)
    assert after == before and len(before["points"]) == 2
    assert all(
        any(r.startswith("dim_") and "entity" in r for r in reasons) for *_, reasons in before["points"]
    )
    assert before["series"] == []
