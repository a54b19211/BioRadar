"""The complete-domain chain with the real checker v11 (Codex 730/740): triage -> measure.v8 locates the
stated
dimensions in what was sent -> BioCheck v11 /v3 (real ASGI handler) judges each -> adopt groups by the proven
domain -> a step inside one complete group; no comparison for a dimension in another sentence, a date value
claimed as a date basis, or another population's group. No model, no external service."""

import pytest
from conftest import FakeFabric, record
from sqlalchemy import select
from test_measure_checks import SRC, real_post

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import measure_checks, monitor, schemas

pytestmark = pytest.mark.skipif(not SRC, reason="BIORADAR_TEST_BIOCHECK_SRC (a BioCheck checkout) is not set")
CALL = {"stage": "triage", "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0, "error": None}


def text_of(month, value, population="成人", lead=""):
    return f"## 按报告日统计\n\n{lead}2026年{month}月，医院A在湖北省共报告{population}炭疽确诊病例{value}例。"


def measure_of(text, month, value, population="成人", dims=None):
    sentence = text[text.index(f"2026年{month}月") : -1]
    stated = (
        dims
        if dims is not None
        else [
            schemas.Dim(kind="disease", text="炭疽"),
            schemas.Dim(kind="population", text=population),
            schemas.Dim(kind="date_basis", text="按报告日统计", source="window"),
        ]
    )
    return schemas.Measure(
        object_text="医院A",
        metric_text=f"报告{population}炭疽确诊病例",
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
        dims=stated,
    )


def feed(monkeypatch, specs):
    """specs: (record id, text, measure); one tick per record, all in topic 炭疽, checked by real v11."""
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: real_post(body))
    by_text = {t: m for _, t, m in specs}
    records = [
        record(rid, "炭疽", t, available=f"2026-06-{n:02d}T08:00:00+08:00")
        for n, (rid, t, _) in enumerate(specs, 1)
    ]

    def triage(settings_, stage, system, payload, model_cls, **options):
        if stage == "extract":
            return schemas.Extraction(
                items=[schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
            ), dict(CALL, stage=stage)
        index = {t["title"]: t["index"] for t in payload["topics"]}
        return schemas.Triage(
            items=[
                schemas.TriageItem(
                    index=r["index"],
                    topic=index.get("炭疽"),
                    new_topic="" if "炭疽" in index else "炭疽",
                    direction="public_health",
                    alert="normal",
                    measures=[by_text[r["text"]]],
                )
                for r in payload["records"]
            ]
        ), CALL

    monkeypatch.setattr(llm, "chat_json", triage)
    for n in range(1, len(records) + 1):
        monitor.tick(is_demo=True, fabric=FakeFabric(records[:n], clean=True))
    with db.session() as s:
        points = {p.record_id: p for p in s.scalars(select(db.Measurement))}
        checks = {c.candidate_id: c for c in s.scalars(select(db.MeasurementCheck))}
        events = [
            (e.detail["record"], sorted(c["kind"] for c in e.detail["changes"]))
            for e in s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series"))
        ]
        states = {r: measure_checks.comparability(s, p) for r, p in points.items()}
        dims = {r: {d["kind"]: d["state"] for d in p.dims} for r, p in points.items()}
    return points, checks, events, states, dims


def spec(rid, month, value, population="成人", lead="", dims=None):
    t = text_of(month, value, population, lead)
    return rid, t, measure_of(t, month, value, population, dims)


def test_a_complete_group_proven_by_v11_compares_across_periods(monkeypatch):
    points, checks, events, states, dims = feed(monkeypatch, [spec("A1", 1, 10), spec("A2", 2, 12)])
    assert {c.checker_version for c in checks.values()} == {"biocheck.extraction_rules.v11"}
    assert all(c.status == "consistent" for c in checks.values())
    assert dims["A2"] == {
        "disease": "checked",
        "population": "checked",
        "date_basis": "checked",
        "case_definition": "carried",
        "coverage": "carried",
    }
    assert states["A2"]["state"] == "comparable"
    assert "step" in dict(events)["A2"]


def test_another_population_is_another_group_never_compared(monkeypatch):
    points, checks, events, states, dims = feed(
        monkeypatch, [spec("A1", 1, 10), spec("K1", 2, 30, "儿童"), spec("A2", 2, 12)]
    )
    kinds = dict(events)
    assert "K1" not in kinds or "step" not in kinds["K1"]  # 儿童 has no earlier point of its own group
    assert "step" in kinds["A2"] and points["A2"].dims_key != points["K1"].dims_key


def test_a_dimension_in_another_sentence_keeps_the_point_out(monkeypatch):
    other = [
        schemas.Dim(kind="disease", text="炭疽"),
        schemas.Dim(kind="population", text="成人", source="window"),
        schemas.Dim(kind="date_basis", text="按报告日统计", source="window"),
    ]
    a1 = spec("A1", 1, 10, "", lead="本期人群：成人。", dims=other)
    a2 = spec("A2", 2, 12, "", lead="本期人群：成人。", dims=other)
    points, checks, events, states, dims = feed(monkeypatch, [a1, a2])
    assert all(c.status == "consistent" for c in checks.values())  # the numbers themselves are checked
    assert dims["A2"]["population"] == "stated_unchecked" and events == []
    assert "dim_unchecked:population" in states["A2"]["reasons"]


def test_a_date_value_claimed_as_the_date_basis_keeps_the_point_out(monkeypatch):
    wrong = [
        schemas.Dim(kind="disease", text="炭疽"),
        schemas.Dim(kind="population", text="成人"),
        schemas.Dim(kind="date_basis", text="2026年1月"),
    ]
    points, checks, events, states, dims = feed(monkeypatch, [spec("A1", 1, 10, dims=wrong)])
    [check] = checks.values()
    assert check.status == "consistent"
    assert check.response["items"][0]["dims"]["date_basis"]["reason"] == "dim_kind_not_proven"
    assert dims["A1"]["date_basis"] == "stated_unchecked" and states["A1"]["state"] == "not_comparable"


def test_an_institution_claimed_as_the_product_and_a_number_as_a_denominator_definition_prove_nothing(
    monkeypatch,
):
    """Codex 740: the numbers stay consistent; the agency and the bare number are no product and no
    denominator definition; the product class the text writes is checked."""
    from test_measure_checks import measure, one, rows

    one(
        monkeypatch,
        [
            measure(
                family="milestone_count",
                dims=[
                    {"kind": "product_or_class", "text": "国家药监局", "source": "quote"},
                    {"kind": "denominator_definition", "text": "318", "source": "quote"},
                ],
            )
        ],
        post=real_post,
    )
    [check] = rows(db.MeasurementCheck)
    [point] = rows(db.Measurement)
    got = check.response["items"][0]["dims"]
    assert check.status == "consistent" and check.checker_version == "biocheck.extraction_rules.v11"
    assert (
        got["product_or_class"]["reason"] == got["denominator_definition"]["reason"] == "dim_kind_not_proven"
    )
    assert "dim_unchecked:product_or_class" in point.dims_reasons


def test_a_product_class_the_text_writes_is_checked(monkeypatch):
    from test_measure_checks import measure, one, rows

    one(
        monkeypatch,
        [
            measure(
                family="milestone_count",
                dims=[
                    {"kind": "product_or_class", "text": "医疗器械产品", "source": "quote"},
                ],
            )
        ],
        post=real_post,
    )
    [check] = rows(db.MeasurementCheck)
    [point] = rows(db.Measurement)
    assert check.response["items"][0]["dims"]["product_or_class"]["result"] == "match"
    assert {d["kind"]: d["state"] for d in point.dims}["product_or_class"] == "checked"
    assert "dim_unknown:date_basis" in point.dims_reasons  # still not complete: no date basis written


def test_a_dimension_citing_a_source_never_sent_is_kept_unlocated_and_the_value_is_still_checked(monkeypatch):
    """Codex 740: triage sends no heading; a dimension citing one is not sent, kept with why - and the number
    is still built and checked."""
    unsent = [
        schemas.Dim(kind="disease", text="炭疽"),
        schemas.Dim(kind="population", text="成人"),
        schemas.Dim(kind="date_basis", text="按报告日统计", source="heading"),
    ]
    points, checks, events, states, dims = feed(monkeypatch, [spec("A1", 1, 10, dims=unsent)])
    [check] = checks.values()
    assert check.status == "consistent" and "date_basis" not in check.request["items"][0]["dims"][0]["kind"]
    assert {d["kind"] for d in check.request["items"][0]["dims"]} == {"disease", "population"}
    [d] = [d for d in points["A1"].dims if d["kind"] == "date_basis"]
    assert (d["state"], d["why"]) == ("stated_unchecked", "source_not_sent_to_model")
    assert "dim_unchecked:date_basis" in states["A1"]["reasons"]
