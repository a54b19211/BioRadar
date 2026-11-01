"""SYNTHETIC monitor decisions on dedicated PostgreSQL; no model or upstream calls."""

import pytest
from bioradar.research.mining.models import (
    Candidate,
    EngineRecord,
    EvidenceSupport,
    MethodResult,
    MiningResult,
    canonical_hash,
)
from bioradar.research.monitor.models import MonitorLead
from bioradar.topics.models import TopicScopeRecord
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_monitor import record
from test_monitor import runtime as runtime  # noqa: F401

pytestmark = pytest.mark.ENGINE


def prepared(runtime):
    factory, source, settings = runtime
    settings.monitor_investigations_per_batch = 1
    settings.monitor_hourly_investigations = 0
    source.records = [
        record(901, "锦川医院报告7例不明原因肺炎。SYNTHETIC", seq=1),
        record(902, "锦川设备公司净订单更正为20万元。SYNTHETIC", seq=2),
    ]
    monitor = factory()
    assert monitor.claim()
    batch_id = monitor.accept_page("fixture-source")
    _, _, _, payload, _, _ = monitor._load_batch(batch_id)
    monitor._adopt_and_queue(batch_id, payload)
    _, _, _, payload, _, run_id = monitor._load_batch(batch_id)
    records = [EngineRecord.model_validate(row) for row in payload["snapshot"]["records"]]
    candidates = []
    for index, row in enumerate(records):
        candidates.append(
            Candidate(
                candidate_key=canonical_hash(["SYNTHETIC-section16", index]),
                method_id="SYNTHETIC-section16",
                method_version="SYNTHETIC-v1",
                group_key=canonical_hash(["SYNTHETIC-group", index]),
                kind="association" if index == 0 else "observed_change",
                title=row.normalized_text,
                reasons=("SYNTHETIC fixture",),
                supports=(
                    EvidenceSupport(
                        record_ref=row.record_ref,
                        document_hash=row.document_hash,
                        locator=row.locator,
                        normalized_text_hash=row.normalized_text_hash,
                        evidence_refs=row.evidence_refs,
                    ),
                ),
                metrics={
                    "kdd_direction": "public_health" if index == 0 else "business",
                    "attention": {
                        "automatic_investigation_allowed": True,
                        "checkable_claims": [{"text": row.normalized_text}],
                    },
                },
                importance="medium" if index == 0 else "low",
                urgency="routine" if index == 0 else "immediate",
                next_questions=("SYNTHETIC: which independent source resolves this?",),
            )
        )
    method = MethodResult(
        method_id="SYNTHETIC-section16",
        method_version="SYNTHETIC-v1",
        readiness="exploratory_only",
        execution_status="success",
        candidates=tuple(candidates),
    )
    result = MiningResult(
        snapshot_hash="SYNTHETIC",
        policy_hash="SYNTHETIC",
        result_hash="SYNTHETIC",
        candidates=tuple(reversed(candidates)),
        method_results=(method,),
    )
    return monitor, batch_id, payload, run_id, candidates, result


def test_monitor_selects_importance_before_urgency_and_records_actual_cap(runtime):
    monitor, batch_id, payload, run_id, candidates, result = prepared(runtime)
    monitor._select_leads(batch_id, run_id, payload, result)
    _, _, _, saved, _, _ = monitor._load_batch(batch_id)
    with Session(monitor.engine.database) as session:
        selected = [
            session.get(MonitorLead, lead_id).payload["candidate"]["candidate_key"]
            for lead_id in saved["selected_leads"]
        ]
    assert selected == [candidate.candidate_key for candidate in candidates]
    assert saved["selection_summary"]["runtime_configuration"]["investigations_per_batch"] == 1
    assert (
        monitor.batches(monitor.actor)
        .items[0]
        .selection_summary.runtime_configuration["investigations_per_batch"]
        == 1
    )
    monitor.release()


def test_medium_topic_is_created_even_outside_selected_investigation_capacity(runtime, monkeypatch):
    monitor, batch_id, payload, run_id, candidates, result = prepared(runtime)
    result = result.model_copy(update={"candidates": (candidates[1],)})
    monitor._select_leads(batch_id, run_id, payload, result)
    monkeypatch.setattr(monitor, "_propagate_context_updates", lambda *args: True)
    monkeypatch.setattr(monitor, "investigate", lambda *args: None)
    monitor.advance(batch_id)
    with Session(monitor.engine.database) as session:
        scopes = list(session.scalars(select(TopicScopeRecord)))
        assert len(scopes) == 1 and scopes[0].definition["domain"] == "public_health"
        lead = next(
            row
            for row in session.scalars(select(MonitorLead))
            if row.payload["candidate"]["candidate_key"] == candidates[0].candidate_key
        )
        assert lead.payload["auto_topic"]["status"] == "created"
        assert lead.state == "deferred" and lead.payload["investigation_eligible"] is False
    monitor.release()
