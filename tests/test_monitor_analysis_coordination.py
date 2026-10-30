"""One real PostgreSQL coordination replay; all source/model HTTP is synthetic."""

import copy
import hashlib
from uuid import uuid4

import pytest
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.monitor.models import MonitorBatch, MonitorState
from bioradar.research.monitor.service import MonitorService
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start

pytestmark = pytest.mark.ENGINE


def test_monitor_context_waits_for_active_analysis_and_reuses_intent_after_report(runtime, database):
    make, actor, investigation, _, model, _, adopted = runtime
    service = make()
    accepted = start(runtime)
    with Session(database) as session:
        original_fixed = copy.deepcopy(session.get(InvestigationAnalysisRecord, accepted["id"]).fixed_input)
    old = service.engine.get_snapshot(actor, adopted["id"]).snapshot
    text = "SYNTHETIC fixed source revision for automatic context coordination."
    digest = hashlib.sha256(text.encode()).hexdigest()
    ref = old.records[0].record_ref.model_copy(update={"adopted_version": digest, "content_hash": digest})
    changed = old.records[0].model_copy(
        update={
            "record_ref": ref,
            "evidence_refs": (ref,),
            "document_hash": digest,
            "normalized_text": text,
            "normalized_text_hash": digest,
        }
    )
    _, adopted_new = service.engine.adopt_snapshot(
        actor,
        old.model_copy(update={"snapshot_id": "synthetic-updated-context", "records": (changed,)}),
        "new-source",
    )
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    batch_id = str(uuid4())
    payload = {
        "context_update_refs": [ref.model_dump(mode="json")],
        "investigation_ids": [],
        "after": 0,
        "next_after": 1,
        "next_cursor": None,
        "max_seq": 1,
    }
    try:
        with Session(database) as session, session.begin():
            state = session.get(MonitorState, actor.workspace_id)
            state.source_progress = {
                monitor.stream_key("fixture-source"): {
                    "active_batch_id": batch_id,
                    "acked_after": 0,
                }
            }
            session.add(
                MonitorBatch(
                    id=batch_id,
                    workspace_id=actor.workspace_id,
                    batch_key="a" * 64,
                    source_id="fixture-source",
                    is_demo=True,
                    state="investigating",
                    payload=copy.deepcopy(payload),
                    snapshot_id=adopted_new["id"],
                    updated_at=state.heartbeat_at,
                )
            )
        before = InvestigationService(service.engine).get(actor, investigation["id"])
        assert monitor._propagate_context_updates(batch_id, payload, adopted_new["id"]) is False
        assert InvestigationService(service.engine).get(actor, before.id).revision == before.revision
        with Session(database) as session:
            assert session.get(InvestigationAnalysisRecord, accepted["id"]).fixed_input == original_fixed
            batch = session.get(MonitorBatch, batch_id)
            assert batch.state == "investigating" and batch.error_code == "analysis_in_progress"
            assert not batch.payload.get("context_updates_complete")
            intent_key = batch.payload["context_update_intents"][0]["key"]
            assert (
                session.get(MonitorState, actor.workspace_id).source_progress[
                    monitor.stream_key("fixture-source")
                ]["acked_after"]
                == 0
            )

        def while_running():
            with Session(database) as session:
                assert session.get(InvestigationAnalysisRecord, accepted["id"]).state == "running"
            assert monitor._propagate_context_updates(batch_id, payload, adopted_new["id"]) is False
            assert InvestigationService(service.engine).get(actor, before.id).revision == before.revision

        model.change = while_running
        service.execute(accepted["id"], accepted["input_hash"])
        with Session(database) as session:
            completed = session.get(InvestigationAnalysisRecord, accepted["id"])
            assert completed.state == "completed", completed.error_code
            fixed, report = copy.deepcopy(completed.fixed_input), copy.deepcopy(completed.result)
            assert report["report"]["final_text"] and report["report"]["text_sha256"]
            retry_payload = copy.deepcopy(session.get(MonitorBatch, batch_id).payload)
        finished_view = InvestigationService(service.engine).get(actor, before.id)
        assert finished_view.revision > before.revision  # The completed report saved its own note.
        assert monitor._propagate_context_updates(batch_id, retry_payload, adopted_new["id"]) is True
        assert retry_payload["context_update_intents"][0]["key"] == intent_key
        assert (
            retry_payload["context_update_intents"][0]["revision_rebases"][-1]["to"] == finished_view.revision
        )
        monitor._complete(batch_id, retry_payload)
        assert monitor._propagate_context_updates(batch_id, retry_payload, adopted_new["id"]) is True
        after = InvestigationService(service.engine).get(actor, before.id)
        matching = [e for e in after.entries if e.kind == "material" and e.payload.support.record_ref == ref]
        assert len(matching) == 1 and after.revision == finished_view.revision + 1
        with Session(database) as session:
            persisted = session.get(InvestigationAnalysisRecord, accepted["id"])
            assert persisted.fixed_input == fixed and persisted.result == report
            assert session.get(MonitorBatch, batch_id).error_code is None
            assert (
                session.get(MonitorState, actor.workspace_id).source_progress[
                    monitor.stream_key("fixture-source")
                ]["acked_after"]
                == 1
            )
    finally:
        monitor.release()
