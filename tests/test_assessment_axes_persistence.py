"""SYNTHETIC assessment persistence on actual PostgreSQL, existing random schemas."""

import copy

import pytest
from bioradar.research.investigations.models import InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.monitor.models import MonitorLead
from bioradar.topics.models import TopicEvaluationRecord, TopicObservationRecord
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_monitor import finish_pending, seeded
from test_monitor import runtime as monitor_runtime  # noqa: F401
from test_topics import NOW, create, dated, definition
from test_topics import runtime as topic_runtime  # noqa: F401

pytestmark = pytest.mark.ENGINE


def test_legacy_lead_read_projects_F6_without_rewriting_saved_candidate(monitor_runtime):  # noqa: F811
    make, source, _ = monitor_runtime
    seeded(source)
    make().tick()
    finish_pending(make)
    make().tick()
    with Session(make().engine.database) as session:
        rows = list(session.scalars(select(MonitorLead)))
        assert rows
        before = {row.id: copy.deepcopy(row.payload) for row in rows}
    shown = make().leads(make().actor).items
    assert shown and all(lead.assessment_axes is not None for lead in shown)
    assert all(item.grade == "F" for lead in shown for item in lead.assessment_axes.source_reliability)
    assert all(item.grade == "6" for lead in shown for item in lead.assessment_axes.information_credibility)
    assert all(lead.assessment_axes.heat is None for lead in shown)
    selected = next(lead for lead in shown if lead.investigation_id)
    monitor = make()
    writer = InvestigationService(monitor.engine)
    view = writer.get(monitor.actor, selected.investigation_id)
    material = next(item for item in view.entries if item.kind == "material")
    original_ref = material.payload.support.record_ref.model_dump(mode="json")
    origin = view.origin.model_dump(mode="json")
    target = {"type": "candidate", **{key: origin[key] for key in ("run_id", "result_hash", "candidate_key")}}
    saved_entry = None
    for grade in ("B", "D"):
        request = InvestigationEntryCreate.model_validate(
            {
                "expected_revision": view.revision,
                "kind": "judgment",
                "text": "SYNTHETIC explicit publisher reliability revision",
                "assessment": "provisional",
                "basis_entry_ids": [material.id],
                "review": {
                    "decision": "needs_verification",
                    "scope": "SYNTHETIC publisher scope only",
                    "target": target,
                },
                "assessment_axes": {
                    "source_reliability": [
                        {
                            "grade": grade,
                            "record_ref": original_ref,
                            "publisher_scope": "SYNTHETIC publisher only",
                            "assessment_basis_refs": [original_ref],
                            "assessment_basis": "SYNTHETIC human reliability history basis",
                        }
                    ]
                },
            }
        )
        writer.append(monitor.actor, view.id, request, "SYNTHETIC-axes-review-" + grade)
        view = writer.get(monitor.actor, view.id)
        last = view.entries[-1]
        current = next(lead for lead in make().leads(monitor.actor).items if lead.id == selected.id)
        rating = next(
            item
            for item in current.assessment_axes.source_reliability
            if item.record_ref.model_dump(mode="json") == original_ref
        )
        assert rating.grade == grade and rating.assessed_by == last.created_by
        assert rating.assessed_at == last.created_at and rating.native_review_entry_id == last.id
        if saved_entry is not None:
            assert rating.previous_assessment_ref == saved_entry.id
            assert (
                next(item for item in view.entries if item.id == saved_entry.id)
                .payload.assessment_axes.source_reliability[0]
                .grade
                == "B"
            )
        saved_entry = last
    with Session(make().engine.database) as session:
        assert {row.id: row.payload for row in session.scalars(select(MonitorLead))} == before


def test_topic_source_and_evaluation_axes_roundtrip_preserve_original_heat(topic_runtime):  # noqa: F811
    make, source, _ = topic_runtime
    source.records = [dated(1, "2026-09-01", "SYNTHETIC 北江医院报告7例肺炎聚集，尚待核实。")]
    identifier = create(make, definition(domain="public_health"))
    make().tick(now=NOW)
    service = make()
    report = service.evaluations(service.actor, identifier).items[0]
    assert report.assessment_axes.heat == report.tracking["heat"]
    assert report.assessment_axes.evidence_level == report.tracking["evidence"]["level"]
    assert report.evidence[0].assessment_axes.source_reliability[0].grade == "F"
    assert report.evidence[0].assessment_axes.heat is None
    with Session(service.engine.database) as session:
        observation = session.scalar(select(TopicObservationRecord))
        assert observation.contribution["assessment_axes"]["information_credibility"][0]["grade"] == "6"
        saved = session.get(TopicEvaluationRecord, report.id)
        before = copy.deepcopy(saved.result)
        assert saved.result["assessment_axes"]["heat"] == report.tracking["heat"]
    assert make().get(service.actor, identifier).assessment_axes == report.assessment_axes
    again = make().evaluation(service.actor, identifier, report.id)
    assert again.assessment_axes == report.assessment_axes
    with Session(service.engine.database) as session:
        assert session.get(TopicEvaluationRecord, report.id).result == before
