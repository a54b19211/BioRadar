"""T-29 P3: a measure becomes a series point only through its own check (contract v2 / checker v6).

The gate, the Fabric readback (binding and permission, before the call and again before adoption), the
append-only checks, lossless storage and the effective view (checker upgrade, revocation, recovery). The
stand-in answers all-match unless a test alters it; test_real_chain_* use the real BioCheck rules over the
real matrix clauses when BIORADAR_TEST_BIOCHECK_SRC names a BioCheck checkout (skipped, never passed,
otherwise)."""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import extraction_contract as ec
import pytest
from conftest import FakeFabric, biocheck_fake, record
from sqlalchemy import select, update
from test_series import fake_triage

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import measure_checks, monitor, schemas

TEXT = "2026年4月，国家药监局共批准注册医疗器械产品318个。"


def measure(**over):
    base = dict(
        object_text="国家药监局",
        metric_text="批准注册医疗器械产品",
        value_text="318",
        unit_text="个",
        period_text="2026年4月",
        period="2026-04",
        period_type="interval",
        kind="flow",
        status="reported",
        quote="国家药监局共批准注册医疗器械产品318个",
    )
    base.update(over)
    return schemas.Measure(**base)


def one(monkeypatch, measures, *, text=TEXT, post=None, rid="R1", day="2026-05-08"):
    """One record triaged with `measures`; the check worker answers with `post` (default: all-match by the
    current checker)."""
    calls = []
    post = post or biocheck_fake(calls=calls)
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    monkeypatch.setattr(llm, "chat_json", fake_triage({text: (measures, "normal")}, [], topic_title="批准"))
    fabric = FakeFabric([record(rid, text, text, available=day + "T08:00:00+08:00")], clean=True)
    monitor.tick(is_demo=True, fabric=fabric)
    return fabric, calls


def rows(model):
    with db.session() as s:
        return list(s.scalars(select(model).order_by(*model.__table__.primary_key.columns)))


def due_now():
    with db.session() as s:
        s.execute(update(db.MeasurementCheckTask).values(next_check_at=db.now() - timedelta(seconds=1)))


def effective_ids():
    with db.session() as s:
        return [m.id for m in s.scalars(measure_checks.effective(s, monitor.settings(), True))]


def test_a_consistent_exact_actual_answer_is_stored_as_checked_and_reads_back_equal(monkeypatch):
    _, calls = one(monkeypatch, [measure(value_text="318")])
    [point] = rows(db.Measurement)
    assert (
        point.value_text,
        point.value,
        point.unit,
        point.period,
        point.period_type,
        point.period_precision,
        point.scope_mode,
        point.numerator,
    ) == ("318", 318.0, "个", "2026-04", "interval", "month", "object_metric_total", None)
    [check] = rows(db.MeasurementCheck)
    assert (check.gate, check.status, check.checker_version, point.check_id) == (
        "series",
        "consistent",
        ec.CHECKER_V7,
        check.id,
    )
    body = calls[0]
    assert (
        body["contract_version"] == ec.CONTRACT_V2 and body["items"][0]["fields"]["value"]["value"] == "318"
    )
    a, b = body["items"][0]["locator"]["start"], body["items"][0]["locator"]["end"]
    assert (
        body["passages"][0]["text"][a - body["passages"][0]["start"] : b - body["passages"][0]["start"]]
        == "国家药监局共批准注册医疗器械产品318个"
    )
    assert effective_ids() == [point.id]


def test_thousands_are_sent_and_stored_as_exact_decimal_text(monkeypatch):
    text = "2026年4月，国家药监局共批准注册医疗器械产品1,444个。"
    _, calls = one(
        monkeypatch, [measure(value_text="1,444", quote="国家药监局共批准注册医疗器械产品1,444个")], text=text
    )
    assert calls[0]["items"][0]["fields"]["value"]["value"] == "1444"
    [point] = rows(db.Measurement)
    assert point.value_text == "1444"


def test_a_value_a_float_cannot_hold_is_refused_not_rounded(monkeypatch):
    text = "2026年4月，国家药监局共批准注册医疗器械产品123456789012345678个。"
    one(
        monkeypatch,
        [
            measure(
                value_text="123456789012345678", quote="国家药监局共批准注册医疗器械产品123456789012345678个"
            )
        ],
        text=text,
    )
    assert rows(db.Measurement) == []
    assert [(c.gate, c.gate_reason) for c in rows(db.MeasurementCheck)] == [
        ("candidate", "stored_values_differ")
    ]


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            lambda r: r["items"][0].update(
                status="inconsistent", fields={**r["items"][0]["fields"], "value": {"result": "mismatch"}}
            ),
            "not_consistent",
        ),
        (
            lambda r: r["items"][0].update(
                status="unchecked", fields={**r["items"][0]["fields"], "scope": {"result": "unsupported"}}
            ),
            "not_consistent",
        ),
    ],
)
def test_anything_but_consistent_stays_a_candidate(monkeypatch, change, reason):
    def answer(body, response):
        change(response)
        return response

    one(monkeypatch, [measure()], post=biocheck_fake(answer))
    assert rows(db.Measurement) == []
    assert [(c.gate, c.gate_reason) for c in rows(db.MeasurementCheck)] == [("candidate", reason)]


@pytest.mark.parametrize("over", [dict(modality="planned"), dict(qualifier="approximate")])
def test_planned_or_approximate_values_never_enter_the_exact_series(monkeypatch, over):
    one(monkeypatch, [measure(**over)])
    assert rows(db.Measurement) == []
    assert [(c.status, c.gate, c.gate_reason) for c in rows(db.MeasurementCheck)] == [
        ("consistent", "candidate", "approximate_or_not_actual")
    ]


def test_an_invalid_answer_is_unchecked_and_retried(monkeypatch):
    def answer(body, response):
        response["items"][0]["fields"].pop("modality")
        return response

    one(monkeypatch, [measure()], post=biocheck_fake(answer))
    assert rows(db.Measurement) == []
    assert [c.error_code for c in rows(db.MeasurementCheck)] == ["invalid_response"]
    [task] = rows(db.MeasurementCheckTask)
    assert task.state == "waiting" and task.attempts == 1


def test_a_service_failure_is_unchecked_then_checked_on_retry(monkeypatch):
    def down(body):
        raise services.ServiceError("biocheck", "unreachable")

    fabric, _ = one(monkeypatch, [measure()], post=down)
    assert rows(db.Measurement) == [] and [c.error_code for c in rows(db.MeasurementCheck)] == [
        "service_unavailable"
    ]
    post = biocheck_fake()
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    due_now()
    monitor.tick(is_demo=True, fabric=fabric)
    assert len(rows(db.Measurement)) == 1 and [c.gate for c in rows(db.MeasurementCheck)] == [
        "candidate",
        "series",
    ]


def test_a_changed_artifact_is_never_sent(monkeypatch):
    calls = []
    post = biocheck_fake(calls=calls)
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    monkeypatch.setattr(
        llm, "chat_json", fake_triage({TEXT: ([measure()], "normal")}, [], topic_title="批准")
    )
    fabric = FakeFabric([record("R1", TEXT, TEXT, available="2026-05-08T08:00:00+08:00")], clean=True)
    fabric.tampered.add(fabric.docs[0]["version_id"])
    monitor.tick(is_demo=True, fabric=fabric)
    assert calls == [] and rows(db.Measurement) == []
    assert [c.error_code for c in rows(db.MeasurementCheck)] == ["artifact_changed"]


def test_permission_withdrawn_during_the_call_is_not_adopted(monkeypatch):
    holder = {}

    def answer(body, response):
        holder["fabric"].revoked.add(body["subject"]["version_id"])
        return response

    post = biocheck_fake(answer)
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    monkeypatch.setattr(
        llm, "chat_json", fake_triage({TEXT: ([measure()], "normal")}, [], topic_title="批准")
    )
    holder["fabric"] = FakeFabric(
        [record("R1", TEXT, TEXT, available="2026-05-08T08:00:00+08:00")], clean=True
    )
    monitor.tick(is_demo=True, fabric=holder["fabric"])
    assert rows(db.Measurement) == [] and [c.error_code for c in rows(db.MeasurementCheck)] == [
        "source_unreadable"
    ]


def test_revocation_and_recovery_move_a_point_out_of_and_back_into_the_effective_view(monkeypatch):
    fabric, _ = one(monkeypatch, [measure()])
    [point] = rows(db.Measurement)
    assert effective_ids() == [point.id]
    fabric.revoked.add(point.version_id)
    monitor.tick(is_demo=True, fabric=fabric)  # an ordinary round: nothing forced due
    assert effective_ids() == [] and rows(db.Measurement)[0].id == point.id  # history kept, not current
    with db.session() as s:
        assert monitor.known_series(s, monitor.settings(), True, datetime(2027, 1, 1, tzinfo=UTC)) == []
    fabric.revoked.clear()
    monitor.tick(is_demo=True, fabric=fabric)  # readable again: checked again this same round
    assert effective_ids() == [point.id] and len(rows(db.Measurement)) == 1
    assert [c.gate for c in rows(db.MeasurementCheck)] == ["series", "candidate", "series"]


def test_a_checker_upgrade_takes_points_out_until_the_new_checker_confirms_them(monkeypatch):
    fabric, _ = one(monkeypatch, [measure()])
    [point] = rows(db.Measurement)
    v8 = "biocheck.extraction_rules.v8"  # a hypothetical next checker
    monkeypatch.setitem(ec.KIND_RESULTS, v8, ec.KIND_RESULTS[ec.CHECKER_V7])
    monkeypatch.setitem(ec.NO_LOCATION_RESULTS, v8, ec.NO_LOCATION_RESULTS[ec.CHECKER_V7])
    monkeypatch.setitem(ec.CONTRACT_OF, v8, ec.CONTRACT_V2)
    monkeypatch.setattr(ec, "CHECKERS", frozenset(ec.KIND_RESULTS))
    monkeypatch.setattr(measure_checks, "CHECKER", v8)
    assert effective_ids() == []  # no v8 check yet: not current
    with db.session() as s:
        assert measure_checks.backfill(s, monitor.settings()) == 1
    post = biocheck_fake(lambda body, response: dict(response, checker_version=v8))
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    monitor.tick(is_demo=True, fabric=fabric)
    assert effective_ids() == [point.id] and len(rows(db.Measurement)) == 1


def test_a_series_event_whose_point_left_the_view_is_no_current_conclusion(monkeypatch):
    fabric, _ = one(monkeypatch, [measure()])
    [point] = rows(db.Measurement)
    with db.session() as s:
        topic = s.scalars(select(db.Topic)).first()
        monitor.event(
            s,
            topic,
            datetime(2026, 5, 8, tzinfo=UTC),
            "series",
            object="国家药监局",
            metric="m",
            changes=[{"kind": "step", "rows": [point.id]}],
        )
        topic_id = topic.id
    with db.session() as s:
        assert "已核" in monitor.series_basis(s, topic_id)
    fabric.revoked.add(point.version_id)
    due_now()
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        assert monitor.series_basis(s, topic_id) == ""


def test_a_point_and_an_interval_of_the_same_month_are_two_series(monkeypatch):
    a = "截至2023年12月，ICH正在活跃的协调议题共32个。"
    b = "2023年12月，ICH正在活跃的协调议题共40个。"
    for rid, text, ptype, value, day in (
        ("P", a, "point", "32", "2024-01-05"),
        ("I", b, "interval", "40", "2024-01-06"),
    ):
        one(
            monkeypatch,
            [
                measure(
                    object_text="ICH",
                    metric_text="正在活跃的协调议题",
                    value_text=value,
                    period_text="截至2023年12月" if ptype == "point" else "2023年12月",
                    period="2023-12",
                    period_type=ptype,
                    kind="stock",
                    quote=f"ICH正在活跃的协调议题共{value}个",
                )
            ],
            text=text,
            rid=rid,
            day=day,
        )
    points = rows(db.Measurement)
    assert sorted((p.period, p.period_type) for p in points) == [
        ("2023-12", "interval"),
        ("2023-12", "point"),
    ]
    with db.session() as s:
        events = list(s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series")))
    assert all(c["kind"] != "conflict" for e in events for c in e.detail["changes"])


def test_not_buildable_measures_are_candidates_with_the_reason(monkeypatch):
    one(
        monkeypatch,
        [
            measure(value_text="381", quote="国家药监局共批准注册医疗器械产品318个"),
            measure(unit_text="件"),
            measure(quote="不在原文里的话"),
        ],
    )
    by = sorted((c.status, c.reason) for c in rows(db.MeasurementCandidate))
    assert by == [
        ("not_built", "quote_not_in_text"),
        ("not_built", "unit_literal_not_at_the_value"),
        ("not_built", "value_literal_not_located"),
    ]
    assert rows(db.Measurement) == []


# ---- the real chain: Radar -> real BioCheck rules over real matrix clauses ---------------------------------

SRC = os.environ.get("BIORADAR_TEST_BIOCHECK_SRC")
PYTHON = os.environ.get("BIORADAR_TEST_BIOCHECK_PYTHON") or sys.executable
DRIVER = """
import json, os, sys
os.environ["SERVICE_TOKEN"] = "t"
from fastapi.testclient import TestClient
from app.service import create_app
from conftest import FakeLLM
with TestClient(create_app(FakeLLM()), raise_server_exceptions=False) as client:
    r = client.post("/v2/extraction-consistency", json=json.load(sys.stdin),
                    headers={"Authorization": "Bearer t"})
    print(json.dumps({"status": r.status_code, "body": r.json()}, ensure_ascii=False))
"""
real = pytest.mark.skipif(not SRC, reason="BIORADAR_TEST_BIOCHECK_SRC (a BioCheck checkout) is not set")


def real_post(body):
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.path.join(SRC, "tests"))
    p = subprocess.run(
        [PYTHON, "-c", DRIVER],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
        cwd=SRC,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout)
    if out["status"] != 200:
        raise services.ServiceError("biocheck", out["body"].get("error_code", "http_%d" % out["status"]))
    return out["body"]


def matrix_row(sid):
    rows_ = json.loads((Path(SRC) / "tests" / "fixtures" / "p3_matrix.json").read_text(encoding="utf-8"))[
        "rows"
    ]
    return next(r for r in rows_ if r["id"] == sid)


def from_row(row, **over):
    lit, fin = row["fields"], row["final"]
    period = fin.get("period") or {}
    m = dict(
        object_text=lit["object"]["literal"],
        metric_text=lit["metric"]["literal"],
        value_text=lit["value"]["literal"],
        unit_text=lit["unit"]["literal"],
        period_text=lit["period"]["literal"],
        scope_text=lit["scope"]["literal"],
        denominator_text=lit["denominator"]["literal"],
        period=period.get("value") or (f"{period['start']}/{period['end']}" if period.get("start") else ""),
        period_type=period.get("type", ""),
        kind=fin["measure_kind"],
        qualifier=fin["qualifier"],
        modality=fin["modality"],
        status="reported",
        quote=row["sentence"],
    )
    m.update(over)
    return schemas.Measure(**m)


@real
@pytest.mark.parametrize(
    "sid,stored",
    [("S01", True), ("S07", True), ("S19", True), ("S04", False), ("S13", False), ("S11", False)],
)
def test_real_chain_over_real_clauses(monkeypatch, sid, stored):
    row = matrix_row(sid)
    one(monkeypatch, [from_row(row)], text=row["clause"], post=real_post, rid=sid)
    assert bool(rows(db.Measurement)) == stored, [
        (
            c.status,
            c.gate,
            c.gate_reason,
            c.error_code,
            {
                k: v
                for k, v in (c.response or {"items": [{"fields": {}}]})["items"][0]["fields"].items()
                if v.get("result") != "match"
            },
        )
        for c in rows(db.MeasurementCheck)
    ]


@real
def test_real_chain_wrong_normalized_period_is_inconsistent(monkeypatch):
    row = matrix_row("S01")
    one(monkeypatch, [from_row(row, period="2026-03")], text=row["clause"], post=real_post)
    assert rows(db.Measurement) == []
    assert [(c.status, c.gate) for c in rows(db.MeasurementCheck)] == [("inconsistent", "candidate")]


# ---- Codex r137: consumption-time permission, effective context, any call failure, legacy points -----------


def test_an_ordinary_round_reads_permission_before_it_reads_series(monkeypatch):
    fabric, _ = one(monkeypatch, [measure()])
    [point] = rows(db.Measurement)
    fabric.revoked.add(point.version_id)
    seen = {}
    real = monitor.known_series

    def spy(session, s, is_demo, clock, limit=40):
        seen["known"] = real(session, s, is_demo, clock, limit)
        return seen["known"]

    monkeypatch.setattr(monitor, "known_series", spy)
    text = "2026年5月，国家药监局共批准注册医疗器械产品300个。"
    fabric.docs += FakeFabric(
        [record("R2", text, text, available="2026-06-08T08:00:00+08:00")], clean=True
    ).docs
    monkeypatch.setattr(llm, "chat_json", fake_triage({text: ([], "normal")}, [], topic_title="批准"))
    monitor.tick(is_demo=True, fabric=fabric)
    assert seen["known"] == []  # triage of this round was not shown the revoked point


def test_an_effective_plan_heading_reaches_the_checker(monkeypatch):
    text = "# 建设计划\n\n" + "背景资料。" * 100 + "\n\n" + TEXT
    _, calls = one(monkeypatch, [measure()], text=text)
    passage = calls[0]["passages"][0]["text"]
    assert passage.startswith("# 建设计划") and TEXT in passage


def test_a_context_beyond_the_contract_limit_stays_pending(monkeypatch):
    text = "# 建设计划\n\n" + "背景资料。" * 4100 + "\n\n" + TEXT
    _, calls = one(monkeypatch, [measure()], text=text)
    assert calls == [] and [(c.status, c.reason) for c in rows(db.MeasurementCandidate)] == [
        ("not_built", "context_over_20000_characters")
    ]


@pytest.mark.parametrize("body", ["<html>upstream error</html>", ""])
def test_a_200_that_is_not_json_is_unchecked_and_the_round_goes_on(monkeypatch, body):
    import httpx

    real = services.biocheck_v2
    monkeypatch.setattr(services.httpx, "post", lambda *a, **k: httpx.Response(200, text=body))
    fabric, _ = one(
        monkeypatch,
        [measure(), measure(value_text="318", unit_text="个", modality="planned")],
        post=lambda b: real(monitor.settings(), b),
    )
    checks = rows(db.MeasurementCheck)
    assert (
        rows(db.Measurement) == []
        and len(checks) == 2
        and {c.error_code for c in checks} == {"invalid_response"}
    )
    assert {t.state for t in rows(db.MeasurementCheckTask)} == {"waiting"}


def test_retries_are_bounded(monkeypatch):
    def down(body):
        raise RuntimeError("connection reset")

    fabric, _ = one(monkeypatch, [measure()], post=down)
    for _ in range(measure_checks.MAX_ATTEMPTS):
        due_now()
        monitor.tick(is_demo=True, fabric=fabric)
    [task] = rows(db.MeasurementCheckTask)
    assert task.state == "blocked" and task.attempts == measure_checks.MAX_ATTEMPTS


def test_a_point_stored_before_checking_is_re_extracted_checked_and_becomes_current(monkeypatch):
    fabric = FakeFabric([record("R0", TEXT, TEXT, available="2026-05-08T08:00:00+08:00")], clean=True)
    doc = fabric.docs[0]
    with db.session() as s:  # as migration 0007 leaves it: a point with no candidate and no check
        s.add(
            db.Measurement(
                workspace_id=monitor.settings().workspace_id,
                is_demo=True,
                object="国家药监局",
                metric="批准注册医疗器械产品数",
                unit="个",
                kind="flow",
                scope="",
                value=318,
                period="2026-04",
                period_start=datetime(2026, 4, 1, tzinfo=UTC),
                period_end=datetime(2026, 4, 30, tzinfo=UTC),
                status="reported",
                source=doc["source"],
                record_id="R0",
                version_id=doc["version_id"],
                quote="318个",
                available_at=datetime(2026, 5, 8, tzinfo=UTC),
            )
        )
    assert effective_ids() == []
    post = biocheck_fake()
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    monkeypatch.setattr(
        llm, "chat_json", fake_triage({TEXT: ([measure()], "normal")}, [], topic_title="批准")
    )
    monitor.tick(is_demo=True, fabric=fabric)
    placeholders = [c for c in rows(db.MeasurementCandidate) if c.reason == "stored_before_checking"]
    assert [c.status for c in placeholders] == ["reextracted"]
    points = rows(db.Measurement)
    assert len(points) == 2 and points[0].check_id is None  # the old point stays history
    assert effective_ids() == [points[1].id] and points[1].value_text == "318"
    monitor.tick(is_demo=True, fabric=fabric)
    assert (
        len([c for c in rows(db.MeasurementCandidate) if c.reason == "stored_before_checking"]) == 1
    )  # once


def test_a_version_no_longer_readable_is_not_re_extracted(monkeypatch):
    fabric = FakeFabric([], clean=True)
    with db.session() as s:
        s.add(
            db.Measurement(
                workspace_id=monitor.settings().workspace_id,
                is_demo=True,
                object="x",
                metric="m",
                value=1,
                period_start=datetime(2026, 1, 1, tzinfo=UTC),
                period_end=datetime(2026, 1, 1, tzinfo=UTC),
                source="external_archive",
                record_id="gone",
                version_id="gone",
                quote="1",
                available_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    monkeypatch.setattr(llm, "chat_json", fake_triage({}, [], topic_title=""))
    monitor.tick(is_demo=True, fabric=fabric)
    assert [(c.status, c.reason) for c in rows(db.MeasurementCandidate)] == [
        ("reextract_failed", "version_not_readable")
    ]


def test_an_analysis_records_each_point_with_the_check_that_admitted_it(monkeypatch):
    fabric, _ = one(monkeypatch, [measure()])
    [point] = rows(db.Measurement)
    with db.session() as s:
        got = measure_checks.series_points(s, [point.id])
    assert got[0]["value"] == "318" and got[0]["extraction_check"]["gate"] == "series"
    assert got[0]["extraction_check"]["checker_version"] == ec.CHECKER_V7
    fabric.revoked.add(point.version_id)
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        assert measure_checks.series_points(s, [point.id]) == []


def test_nothing_the_planner_or_an_analysis_answers_can_write_a_point_or_a_check():
    """The planner's decisions flow into analyses and reports only: no module of that path writes
    measurements,
    candidates or checks, so a confident planner cannot lift an unchecked value into a series."""
    import inspect

    from bioradar.research import analyses, pipeline

    for module in (pipeline, analyses):
        source = inspect.getsource(module)
        assert not re.search(r"db\.Measurement(Candidate|Check|CheckTask)?\b", source), module.__name__
