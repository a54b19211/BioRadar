"""The radar's own measurement tables from before the cut-over are frozen history: a reading plan is still
shown as recorded, and nothing writes them."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select, update

from bioradar import app, db

AT = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
V1, V2 = "a" * 64, "b" * 64
# checker versions the frozen rows carry: the last one the radar's own chain wrote, and a later one
V16, V17 = "biocheck.extraction_rules.v16", "biocheck.extraction_rules.v17"
FROZEN = (
    db.Measurement,
    db.MeasurementCandidate,
    db.MeasurementCheck,
    db.RecordRead,
    db.RecordReadWindow,
    db.RecordReadAttempt,
    db.RecordCorrection,
)


def history_rows():
    """Two recorded points of one topic (the second corrected by a recorded correction), their candidates and
    checks, and one reading plan - written the way the chain left them, below the ORM."""
    with db.engine().begin() as c:
        c.execute(
            insert(db.Topic.__table__).values(
                id="t1",
                workspace_id="default",
                title="炭疽",
                direction="public_health",
                is_demo=True,
                state="rising",
                alert="normal",
                heat=0,
                awakenings=0,
                closed=False,
                first_seen_at=AT,
                last_activity_at=AT,
                summary="",
            )
        )
        for n, (version, value) in enumerate(((V1, 10), (V2, 12)), 1):
            c.execute(
                insert(db.MeasurementCandidate.__table__).values(
                    id=n,
                    workspace_id="default",
                    is_demo=True,
                    source="who_don",
                    record_id=f"R{n}",
                    version_id=version,
                    measure={"quote": f"新增确诊病例{value}例"},
                    status="checked",
                    observed_at=AT,
                )
            )
            c.execute(
                insert(db.MeasurementCheck.__table__).values(
                    id=n,
                    candidate_id=n,
                    checker_version=V16,
                    contract_version="extraction_consistency.v3",
                    status="consistent",
                    gate="series",
                    created_at=AT,
                )
            )
            c.execute(
                insert(db.Measurement.__table__).values(
                    id=n,
                    workspace_id="default",
                    is_demo=True,
                    object="炭疽",
                    metric="新增确诊病例",
                    unit="例",
                    kind="flow",
                    scope="湖北省",
                    domain_basis="topic",
                    domain_id="t1",
                    family="case_count",
                    dims=[],
                    dims_key="sha256:x",
                    dims_reasons=[],
                    value=float(value),
                    value_text=str(value),
                    period=f"2026-0{n}",
                    period_type="interval",
                    period_precision="month",
                    scope_mode="stated",
                    period_start=datetime(2026, n, 1, tzinfo=UTC),
                    period_end=datetime(2026, n, 28, tzinfo=UTC),
                    status="reported",
                    source="who_don",
                    record_id=f"R{n}",
                    version_id=version,
                    quote=f"新增确诊病例{value}例",
                    available_at=AT,
                    received_at=AT,
                    candidate_id=n,
                    check_id=n,
                    artifact_key="",
                )
            )
        c.execute(
            insert(db.RecordCorrection.__table__).values(
                workspace_id="default",
                is_demo=True,
                target_source="who_don",
                target_record_id="R2",
                target_version_id="",
                relation="corrects",
                by_source="who_don",
                by_record_id="R3",
                by_version_id="c" * 64,
                visible_at=AT,
                created_at=AT,
            )
        )
        c.execute(
            insert(db.RecordRead.__table__).values(
                id=1,
                workspace_id="default",
                is_demo=True,
                source="who_don",
                record_id="R1",
                version_id=V1,
                title="",
                part={},
                total_chars=3000,
                window_chars=1500,
                overlap_chars=300,
                windows_total=2,
                state="reading",
                read_until=1500,
                direction="public_health",
                observed_at=AT,
                relations=[],
                artifact_key="",
                created_at=AT,
                updated_at=AT,
            )
        )
        for k, state in enumerate(("sent", "budget_spent")):
            c.execute(
                insert(db.RecordReadWindow.__table__).values(
                    read_id=1, window_index=k, start_char=k * 1200, end_char=k * 1200 + 1500, state=state
                )
            )
        c.execute(
            insert(db.TopicEvent.__table__).values(
                topic_id="t1", at=AT, kind="series", detail={"changes": [{"kind": "step", "rows": [1, 2]}]}
            )
        )


def counts():
    with db.session() as s:
        return [s.scalar(select(func.count()).select_from(m)) for m in FROZEN]


def test_a_recorded_reading_plan_is_still_shown():
    history_rows()
    out = app.measure_coverage(is_demo=True)
    [item] = out["items"]
    assert out["plans_by_state"] == {"reading": 1}
    assert (item["model_read_chars"], item["unread_chars"], item["unread_by_reason"]) == (
        1500,
        1500,
        {"budget_spent": 1200},
    )
    assert item["checker_versions"] == [V16]  # the version its recorded check was stored with


def test_a_session_refuses_every_write_to_the_frozen_tables():
    history_rows()
    before = counts()
    with pytest.raises(db.FrozenHistory), db.session() as s:  # a changed row, at the flush
        s.get(db.Measurement, 1).value_text = "11"
    with pytest.raises(db.FrozenHistory), db.session() as s:  # a deleted one
        s.delete(s.get(db.RecordCorrection, 1))
    with pytest.raises(db.FrozenHistory), db.session() as s:  # a new one
        s.add(db.RecordRead(workspace_id="default", is_demo=True, source="s", record_id="r", version_id=V1))
    with pytest.raises(db.FrozenHistory), db.session() as s:  # a bulk statement
        s.execute(update(db.MeasurementCheck).values(status="inconsistent"))
    assert counts() == before
    with db.session() as s:
        assert s.get(db.Measurement, 1).value_text == "10"
