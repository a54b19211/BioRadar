"""SYNTHETIC automatic updates through real PostgreSQL; no paid/external model."""

import copy

import pytest
from bioradar.research.investigations import query_policy
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.material_impact import MATERIAL_IMPACT_VERSION
from bioradar.research.investigations.models import InvestigationAutomation, InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.models import canonical_hash
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start
from test_material_impact import changed

pytestmark = pytest.mark.ENGINE


def parent(runtime, monkeypatch, *, enabled=True):
    if enabled:
        freeze = query_policy.freeze_policy
        monkeypatch.setattr(
            query_policy,
            "freeze_policy",
            lambda settings, requested=None: {
                **freeze(settings, requested),
                "material_impact_version": MATERIAL_IMPACT_VERSION,
            },
        )
    make, actor, view, _, model, *_ = runtime
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    saved = make().get(actor, view["id"], accepted["id"])
    assert saved.state == "completed", saved.error_code
    return accepted["id"], copy.deepcopy(saved.result.model_dump(mode="json")), len(model.requests)


def append(runtime, index, key, *, automatic=True, equivalent=False):
    make, actor, view, *_ = runtime
    engine = make().engine
    source = engine.get_snapshot(actor, runtime[-1]["id"]).snapshot
    old = source.records[index]
    updated = changed(
        old,
        text=None if equivalent else old.normalized_text + " SYNTHETIC additional limitation.",
        raw_changed=not equivalent,
    )
    updated_source = source.model_copy(update={"records": (updated,)})
    _, adopted = engine.adopt_snapshot(actor, updated_source, "SYNTHETIC-update:" + key)
    service = InvestigationService(engine)
    current = service.get(actor, view["id"])
    _, output = service.append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "expected_revision": current.revision,
                "kind": "material",
                "snapshot_id": adopted["id"],
                "record_ref": updated.record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC source version; relevance is not inferred from domain.",
            }
        ),
        "SYNTHETIC-entry:" + key,
        automation=InvestigationAutomation(
            batch_id="00000000-0000-0000-0000-000000000001",
            lead_key=canonical_hash(key),
        )
        if automatic
        else None,
    )
    return output["revision"]


def count(database):
    with Session(database) as session:
        return session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord))


def test_pending_is_durable_visible_and_later_actual_dependency_still_queues(runtime, database, monkeypatch):
    make, actor, view, _, model, *_ = runtime
    identifier, original_result, posts = parent(runtime, monkeypatch)
    revision = append(runtime, 1, "background")
    make().recover()
    observed = make().get(actor, view["id"], identifier)
    receipt = observed.progress["material_update_impact"]
    assert receipt["status"] == "impact_pending" and receipt["checked_revision"] == revision
    assert observed.result.model_dump(mode="json") == original_result
    assert count(database) == 1 and len(model.requests) == posts
    with monkeypatch.context() as check:
        check.setattr(
            type(make()), "request_material_update", lambda *_: pytest.fail("same revision rechecked")
        )
        make().recover()
    append(runtime, 0, "actual-dependency")
    make().recover()
    with Session(database) as session:
        child = session.scalar(
            select(InvestigationAnalysisRecord).where(
                InvestigationAnalysisRecord.parent_analysis_id == identifier
            )
        )
        assert child is not None and child.state == "queued" and child.trigger == "material_update"
    assert len(model.requests) == posts
    assert make().get(actor, view["id"], identifier).result.model_dump(mode="json") == original_result


@pytest.mark.parametrize(
    "equivalent,manual,enabled,expected_count",
    [
        (True, False, True, 1),
        (False, True, True, 2),
        (False, False, False, 2),
    ],
)
def test_equivalent_manual_priority_and_legacy_none_keep_existing_behavior(
    runtime, database, monkeypatch, equivalent, manual, enabled, expected_count
):
    make, actor, view, _, model, *_ = runtime
    identifier, original_result, posts = parent(runtime, monkeypatch, enabled=enabled)
    original_view = make().get(actor, view["id"], identifier).model_dump(mode="json")
    append(runtime, 0, "priority", automatic=not manual, equivalent=equivalent)
    make().recover()
    saved = make().get(actor, view["id"], identifier)
    assert count(database) == expected_count and len(model.requests) == posts
    assert saved.result.model_dump(mode="json") == original_result
    if equivalent:
        assert saved.progress["material_update_impact"]["status"] == "equivalent_inputs"
    else:
        assert "material_update_impact" not in saved.progress
        assert saved.model_dump(mode="json") == original_view
