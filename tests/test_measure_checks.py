"""T-29 P3: a measure becomes a series point only through its own check (contract v2 / checker v5).

The gate, the Fabric readback (binding and permission, before the call and again before adoption), the
append-only checks, lossless storage and the effective view (checker upgrade, revocation, recovery). The v5
stand-in answers all-match unless a test alters it; test_real_chain_* use the real BioCheck rules over the
real matrix clauses when BIORADAR_TEST_BIOCHECK_SRC names a BioCheck checkout (skipped, never passed,
otherwise)."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import extraction_contract as ec
import pytest
from conftest import FakeFabric, biocheck_v5, record
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
    """One record triaged with `measures`; the check worker answers with `post` (default all-match v5)."""
    calls = []
    post = post or biocheck_v5(calls=calls)
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
        ec.CHECKER_V5,
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

    one(monkeypatch, [measure()], post=biocheck_v5(answer))
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

    one(monkeypatch, [measure()], post=biocheck_v5(answer))
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
    post = biocheck_v5()
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    due_now()
    monitor.tick(is_demo=True, fabric=fabric)
    assert len(rows(db.Measurement)) == 1 and [c.gate for c in rows(db.MeasurementCheck)] == [
        "candidate",
        "series",
    ]


def test_a_changed_artifact_is_never_sent(monkeypatch):
    calls = []
    post = biocheck_v5(calls=calls)
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

    post = biocheck_v5(answer)
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
    due_now()
    monitor.tick(is_demo=True, fabric=fabric)  # revalidation finds the binding unreadable
    assert effective_ids() == [] and rows(db.Measurement)[0].id == point.id  # history kept, not current
    with db.session() as s:
        assert monitor.known_series(s, monitor.settings(), True, datetime(2027, 1, 1, tzinfo=UTC)) == []
    fabric.revoked.clear()
    due_now()
    monitor.tick(is_demo=True, fabric=fabric)
    assert effective_ids() == [point.id] and len(rows(db.Measurement)) == 1
    assert [c.gate for c in rows(db.MeasurementCheck)] == ["series", "candidate", "series"]


def test_a_checker_upgrade_takes_points_out_until_the_new_checker_confirms_them(monkeypatch):
    fabric, _ = one(monkeypatch, [measure()])
    [point] = rows(db.Measurement)
    v6 = "biocheck.extraction_rules.v6"
    monkeypatch.setitem(ec.KIND_RESULTS, v6, ec.KIND_RESULTS[ec.CHECKER_V5])
    monkeypatch.setitem(ec.NO_LOCATION_RESULTS, v6, ec.NO_LOCATION_RESULTS[ec.CHECKER_V5])
    monkeypatch.setitem(ec.CONTRACT_OF, v6, ec.CONTRACT_V2)
    monkeypatch.setattr(ec, "CHECKERS", frozenset(ec.KIND_RESULTS))
    monkeypatch.setattr(measure_checks, "CHECKER", v6)
    assert effective_ids() == []  # no v6 check yet: not current
    with db.session() as s:
        assert measure_checks.backfill(s, monitor.settings()) == 1
    post = biocheck_v5(lambda body, response: dict(response, checker_version=v6))
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


# ---- the real chain: Radar -> real BioCheck v5 rules over real matrix clauses ------------------------------

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
