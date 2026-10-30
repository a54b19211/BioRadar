"""Actual PostgreSQL and HTTP-adapter tests; model responses here are explicitly synthetic."""

import copy
import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.reasoning_model import complete_json
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.analysis import freeze_passages, model_evidence, validate_claims
from bioradar.research.investigations.analysis_models import (
    AnalysisCreate,
    AnalysisEvidence,
    ModelClaim,
)
from bioradar.research.investigations.analysis_service import AnalysisService, InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationCreate, InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.monitor.models import MonitorState
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import CREDENTIAL, GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph import original_record
from test_monitor import Source

pytestmark = pytest.mark.ENGINE


class Model:
    def __init__(self):
        self.requests = []
        self.wires = []
        self.change = None
        self.invalid_passage = False
        self.timeout = False
        self.transform = None

    def handler(self, request):
        wire = json.loads(request.content)
        self.wires.append(wire)
        payload = json.loads(wire["messages"][1]["content"])
        self.requests.append(payload)
        if self.change:
            change, self.change = self.change, None
            change()
        if self.timeout:
            raise httpx.ReadTimeout("SYNTHETIC SECRET MUST NOT LEAK", request=request)
        if payload["task"] in {"question_plan", "shared_verify"}:
            result = (
                {
                    "queries": [
                        {
                            "source_id": payload["search_scope"][0],
                            "terms": ["SYNTHETIC"],
                            "reason": "SYNTHETIC question lookup",
                        }
                    ],
                    "limitations": [],
                }
                if payload["task"] == "question_plan"
                else {"status": "PASS"}
            )
            if self.transform:
                self.transform(payload, result)
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-completion",
                    "model": "synthetic-model",
                    "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                },
            )
        items = payload["evidence"]
        first = items[0]
        link = {
            "evidence_key": first["evidence_key"],
            "passage_id": "nonexistent" if self.invalid_passage else first["passages"][0]["id"],
            "role": "supports",
            "reason": "SYNTHETIC bounded restatement, not a medical conclusion.",
        }
        claim = {
            "id": "c1",
            "text": "SYNTHETIC source reports this bounded statement.",
            "kind": "source_statement",
            "scope": "SYNTHETIC source only",
            "conditions": [],
            "status": "supported",
            "evidence_links": [link],
        }
        answer = {
            "answer": "SYNTHETIC bounded interpretation using " + str(len(items)) + " selected fragments.",
            "conditions": ["No professional review."],
            "unknowns": ["Underlying clinical effect is unknown."],
        }
        if payload["task"] == "read_plan":
            result = {
                "claims": [claim],
                "explanations": [],
                "gaps": [
                    {
                        "id": "g1",
                        "question": "Is additional synthetic material available?",
                        "why_it_matters": "Could qualify the bounded statement.",
                    }
                ],
                "next_queries": [
                    {
                        "gap_id": "g1",
                        "target_claim_id": "c1",
                        "source_id": payload["search_scope"][0],
                        "terms": ["SYNTHETIC"],
                        "anchoring_evidence_keys": [first["evidence_key"]],
                        "reason": "Follow a marked synthetic source term.",
                        "expected_discrimination": "Read additional material rather than infer absence.",
                    }
                ]
                if payload["search_scope"]
                else [],
                "conditional_answer": answer,
            }
        elif payload["task"] in {"synthesize", "revise"}:
            result = {
                "claims": [claim],
                "explanations": [],
                "gaps": [],
                "conditional_answer": answer,
                "update": {
                    "stance": "initial",
                    "changed_claims": [],
                    "reasons": ["SYNTHETIC evidence was actually read; uncertainties retained."],
                    "decisive_evidence_keys": [first["evidence_key"]],
                },
            }
            schema = wire.get("response_format", {}).get("json_schema", {}).get("schema", {})
            if {"warning_assessment", "competing_hypotheses"}.issubset(schema.get("properties", {})):
                result.update(warning_assessment=None, competing_hypotheses=[])
        elif payload["task"] == "compare":
            result = {
                "stance": "maintained",
                "changed_claims": [],
                "reasons": ["SYNTHETIC current evidence preserves a bounded interpretation."],
                "decisive_evidence_keys": [first["evidence_key"]],
            }
        else:
            result = {
                "status": "passed",
                "claims": [
                    {
                        "claim_id": c["id"],
                        "verdict": "grounded",
                        "issue": "",
                        "evidence_keys": [first["evidence_key"]],
                    }
                    for c in payload["draft_to_check"]["claims"]
                ],
                "overall_limitations": ["SYNTHETIC transport probe, not a professional assessment."],
                "findings": [],
            }
        if self.transform:
            self.transform(payload, result)
        return httpx.Response(
            200,
            json={
                "id": "synthetic-completion",
                "model": "synthetic-model",
                "choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )


@pytest.fixture
def runtime(database, snapshot):
    for name in ("0006_monitor.py", "0007_topics.py", "0008_investigation_analyses.py"):
        path = Path(__file__).resolve().parents[1] / "db/migrations/versions" / name
        spec = importlib.util.spec_from_file_location("analysis_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with database.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
    settings = Settings(
        environment="test",
        engine_dev_credential=CREDENTIAL,
        semantic_llm_enabled=True,
        semantic_llm_base_url="http://127.0.0.1:19999/v1",
        semantic_llm_api_key="SYNTHETIC-ONLY-TEST-KEY",
        semantic_llm_model="synthetic-model",
        semantic_llm_model_revision="synthetic-v1",
        analysis_search_source_ids=["fixture-source"],
        analysis_auto_enabled=True,
        monitor_enabled=True,
        monitor_is_demo=True,
        analysis_manual_per_hour=2,
        analysis_min_call_seconds=1,
    )
    source, model = Source(), Model()
    source.records = [
        original_record("synthetic-followup", {"body": "SYNTHETIC follow-up states a limitation."})
    ]
    source_client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(source.handler))
    model_client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(model.handler))

    def make():
        reader = FabricHistoryReader(source_client, authorization_ref=GRANT, max_seconds=90)
        engine = EngineService(database, settings)
        return AnalysisService(
            engine,
            reader,
            model_call=lambda config, **kwargs: complete_json(config, client=model_client, **kwargs),
        )

    service = make()
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.engine.adopt_snapshot(actor, snapshot, "synthetic-source")
    investigation = InvestigationService(service.engine)
    _, view = investigation.create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC analysis",
            question="What does this marked synthetic source establish?",
            is_demo=True,
        ),
        "synthetic-case",
    )
    _, view = investigation.append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "kind": "material",
                "expected_revision": view["revision"],
                "snapshot_id": adopted["id"],
                "record_ref": snapshot.records[0].record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC source, no human conclusion.",
            }
        ),
        "synthetic-material",
    )
    yield make, actor, view, source, model, settings, adopted
    source_client.close()
    model_client.close()


def start(runtime, **changes):
    make, actor, view, *_ = runtime
    current = InvestigationService(make().engine).get(actor, view["id"])
    return make().start(
        actor, view["id"], AnalysisCreate(expected_revision=current.revision, **changes), "analysis-start"
    )[1]


def test_three_real_http_adapter_stages_query_and_no_human_judgment(runtime, database):
    make, actor, view, source, model, _, _ = runtime

    def first_pass(payload, result):
        if payload["task"] == "read_plan":
            result["claims"][0]["text"] = "SYNTHETIC FIRST PASS unverified interpretation."
            result["conditional_answer"]["answer"] = "SYNTHETIC FIRST PASS unverified answer."
            result["next_queries"][0]["reason"] = "SYNTHETIC FIRST PASS query rationale."
            result["next_queries"][0]["expected_discrimination"] = "SYNTHETIC FIRST PASS expectation."

    model.transform = first_pass
    accepted = start(runtime)
    assert accepted["state"] == "queued"
    assert model.requests == []  # API/monitor admission cannot perform model inference.
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [p["task"] for p in model.requests] == ["read_plan", "synthesize", "check"]
    assert result.queries[0].new_versions == 1
    assert any(path == "/api/history/records" for path, _ in source.calls)
    assert len(model.requests[1]["evidence"]) == 2
    assert "comparison_target" not in model.requests[0]
    for payload in model.requests[1:]:
        assert "reading" not in payload and "previous_analysis" not in payload
        assert "FIRST PASS" not in json.dumps(payload)
        query = payload["queries"][0]
        assert query["source_id"] == "fixture-source" and query["terms"] == ["SYNTHETIC"]
        assert query["status"] == "read" and query["returned"] == 1 and query["evidence_keys"]
        assert "plan" not in query and "reason" not in query and "expected_discrimination" not in query
    assert "FIRST PASS" in result.queries[0].plan.reason
    assert all(c["receipt"]["usage"]["total_tokens"] == 150 for c in result.calls)
    for item in result.evidence:
        assert item.selection_hash
        assert "".join(p.text for p in item.passages) == item.text
        shown = [
            e for payload in model.requests for e in payload["evidence"] if e["evidence_key"] == item.key
        ]
        assert all(
            e["passages"] == [{"id": p.id, "text": p.text} for p in item.passages] and "text" not in e
            for e in shown
        )
    for claim in result.result.claims:
        for link in claim.evidence_links:
            source = next(e for e in result.evidence if e.key == link.evidence_key)
            passage = next(p for p in source.passages if p.id == link.passage_id)
            assert (link.quote, link.start, link.end) == (passage.text, passage.start, passage.end)
    report = InvestigationService(make().engine).get(actor, view["id"])
    assert report.judgment_count == 0 and any(
        e.payload.machine_analysis_id for e in report.entries if e.kind == "note"
    )
    before = len(model.requests)
    make().execute(accepted["id"], accepted["input_hash"])
    current = InvestigationService(make().engine).get(actor, view["id"])
    code, duplicate = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=current.revision, parent_analysis_id=accepted["id"]),
        "same-basis-new-click",
    )
    assert code == 200 and duplicate["id"] == accepted["id"] and len(model.requests) == before
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        first_output = row.progress["calls"][0]["output"]
        assert "FIRST PASS" in first_output["claims"][0]["text"]
        assert "FIRST PASS" in first_output["conditional_answer"]["answer"]


def test_unknown_passage_is_rejected_before_any_query(runtime):
    make, actor, view, source, model, *_ = runtime
    model.invalid_passage = True
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_passage_invalid"
    assert len(model.requests) == 1 and not any(p == "/api/history/records" for p, _ in source.calls)


def test_unknown_model_post_does_not_repeat_and_explicit_retry_keeps_old_row(runtime, database):
    make, actor, view, _, model, *_ = runtime
    model.timeout = True
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    old = make().get(actor, view["id"], accepted["id"])
    assert old.state == "blocked" and old.calls[0]["status"] == "unconfirmed"
    make().recover()
    make().execute(accepted["id"], accepted["input_hash"])
    assert len(model.requests) == 1
    current = InvestigationService(make().engine).get(actor, view["id"])
    _, retry = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=current.revision, retry_of_analysis_id=accepted["id"]),
        "explicit-retry",
    )
    assert retry["id"] != accepted["id"]
    assert make().get(actor, view["id"], accepted["id"]).calls == old.calls
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 2


def test_manual_external_material_automatically_queues_child_and_own_material_does_not(runtime, database):
    make, actor, view, _, model, _, adopted = runtime
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    old = make().get(actor, view["id"], accepted["id"]).model_dump(mode="json")
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
    runtime_service = InvestigationService(make().engine)
    current = runtime_service.get(actor, view["id"])
    snap = make().engine.get_snapshot(actor, adopted["id"]).snapshot
    runtime_service.append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "expected_revision": current.revision,
                "kind": "material",
                "snapshot_id": adopted["id"],
                "record_ref": snap.records[1].record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC externally supplied new material.",
            }
        ),
        "external-material",
    )
    with Session(database) as session:
        child = session.scalar(
            select(InvestigationAnalysisRecord).where(
                InvestigationAnalysisRecord.parent_analysis_id == accepted["id"]
            )
        )
        assert child is not None and child.trigger == "material_update" and child.state == "queued"
        child_id, child_hash = child.id, child.input_hash
    make().execute(child_id, child_hash)
    updated = make().get(actor, view["id"], child_id)
    assert updated.state == "completed" and updated.result.update.stance == "maintained"
    assert make().get(actor, view["id"], accepted["id"]).model_dump(mode="json") == old
    assert len(model.requests) == 8
    assert InvestigationService(make().engine).get(actor, view["id"]).judgment_count == 0


def occupy_material_update_pool(runtime):
    make, actor, *_ = runtime
    _, inquiry = InvestigationService(make().engine).create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC pool holder", question="SYNTHETIC awaiting material", is_demo=True
        ),
        "material-pool-holder",
    )
    return make().start(
        actor,
        inquiry["id"],
        AnalysisCreate(expected_revision=1, auto_retrieve=True),
        "material-pool-reservation",
        defer_on_budget=True,
    )[1]["id"]


def test_manual_budget_and_delayed_material_update_are_durable(runtime, database):
    make, actor, view, _, model, settings, adopted = runtime
    settings.analysis_manual_per_hour = 1
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    holder_id = occupy_material_update_pool(runtime)
    current = InvestigationService(make().engine).get(actor, view["id"])
    snap = make().engine.get_snapshot(actor, adopted["id"]).snapshot
    InvestigationService(make().engine).append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "expected_revision": current.revision,
                "kind": "material",
                "snapshot_id": adopted["id"],
                "record_ref": snap.records[1].record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC second material awaits the existing manual budget.",
            }
        ),
        "budget-material",
    )
    with Session(database) as session, session.begin():
        rows = list(
            session.scalars(
                select(InvestigationAnalysisRecord)
                .where(InvestigationAnalysisRecord.investigation_id == view["id"])
                .order_by(InvestigationAnalysisRecord.created_at)
            )
        )
        assert len(rows) == 2 and rows[1].progress["manual_budget_pending"] is True
        holder = session.get(InvestigationAnalysisRecord, holder_id)
        state = copy.deepcopy(holder.progress)
        state["budget_reserved_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        holder.progress = state
        child = rows[1].id
    make().recover()
    with Session(database) as session:
        assert session.get(InvestigationAnalysisRecord, child).progress["manual_budget_pending"] is False
    assert len(model.requests) == 3


def test_human_revision_during_model_call_preserves_receipt_and_stops_business_append(runtime):
    make, actor, view, _, model, *_ = runtime
    accepted = start(runtime)

    def human_note():
        current = InvestigationService(make().engine).get(actor, view["id"])
        InvestigationService(make().engine).append(
            actor,
            view["id"],
            InvestigationEntryCreate.model_validate(
                {
                    "expected_revision": current.revision,
                    "kind": "note",
                    "text": "SYNTHETIC researcher changed the record.",
                }
            ),
            "human-note",
        )

    model.change = human_note
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "revision_conflict"
    assert result.calls[0]["status"] == "received"
    assert len(model.requests) == 1 and result.note_entry_id is None


def test_complete_input_and_machine_note_respect_source_revocation(runtime):
    make, actor, view, source, _, *_ = runtime
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    source.enabled.clear()
    with pytest.raises(DomainError) as error:
        make().get(actor, view["id"], accepted["id"])
    assert error.value.status_code == 403
    with pytest.raises(DomainError):
        InvestigationService(make().engine).get(actor, view["id"])


def test_trial_reason_is_read_from_raw_json_not_invented_from_summary(runtime):
    make, actor, view, source, model, *_ = runtime
    native = original_record(
        "synthetic-clinical",
        {
            "trial_id": "NCT99999999",
            "status": "TERMINATED",
            "why_stopped": "SYNTHETIC strategic stop; not related to safety or efficacy.",
            "body": "SYNTHETIC brief summary without a termination reason.",
        },
        kind="trial",
    )
    source.records = [native]
    engine = make().engine
    snapshot = make().reader.snapshot(
        {"items": [native], "source_id": "fixture-source", "is_demo": True, "coverage": {}}
    )
    _, adopted = engine.adopt_snapshot(actor, snapshot, "clinical-snapshot")
    current = InvestigationService(engine).get(actor, view["id"])
    InvestigationService(engine).append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "expected_revision": current.revision,
                "kind": "material",
                "snapshot_id": adopted["id"],
                "record_ref": snapshot.records[0].record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC clinical source.",
            }
        ),
        "clinical-material",
    )
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    evidence = model.requests[0]["evidence"]
    selected = next(e for e in evidence if "why_stopped" in e["facts"])
    assert native["facts"]["why_stopped"] in "".join(p["text"] for p in selected["passages"])
    assert all("text" not in e for e in evidence)
    result = make().get(actor, view["id"], accepted["id"])
    bound = next(
        e
        for e in result.evidence
        if e.history_binding and e.history_binding["source_record_id"] == native["source_record_id"]
    )
    assert bound.text_basis == "source_raw_content"
    read = make().evidence(actor, view["id"], accepted["id"], bound.key)
    assert read.normalized_text[bound.start : bound.end] == bound.text


def test_genuine_uncertainty_stays_visible_without_forced_revision(runtime):
    make, actor, view, *_ = runtime
    model_call = make().model_call

    def checker(config, **kwargs):
        response = model_call(config, **kwargs)
        if kwargs["stage"] == "check":
            response.parsed["status"] = "needs_review"
            response.parsed["claims"][0]["verdict"] = "uncertain"
            response.parsed["claims"][0]["issue"] = "SYNTHETIC stronger than the quoted statement."
        return response

    service = make()
    service.model_call = checker
    accepted = start(runtime)
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review" and result.result.review.status == "needs_review"
    assert result.result.professional_status == "not_reviewed"
    assert len(result.calls) == 3 and result.initial_draft is None


@pytest.mark.parametrize("boundary", ["model_response", "snapshot", "append"])
def test_unknown_commits_resume_fixed_receipts_without_second_model_post(
    runtime, database, boundary, monkeypatch
):
    from sqlalchemy.exc import SQLAlchemyError

    make, actor, view, _, model, *_ = runtime
    accepted = start(runtime)
    service = make()
    fired = False
    if boundary == "model_response":
        checkpoint = service._checkpoint

        def ambiguous(identifier, token, progress, **kwargs):
            nonlocal fired
            checkpoint(identifier, token, progress, **kwargs)
            if not fired and any(c["status"] == "received" for c in progress["calls"]):
                fired = True
                raise SQLAlchemyError("SYNTHETIC unknown result after a committed checkpoint")

        service._checkpoint = ambiguous
    elif boundary == "snapshot":
        adopt = service.engine.adopt_snapshot

        def ambiguous(*args, **kwargs):
            nonlocal fired
            result = adopt(*args, **kwargs)
            if not fired:
                fired = True
                raise SQLAlchemyError("SYNTHETIC unknown result after committed adoption")
            return result

        service.engine.adopt_snapshot = ambiguous
    else:
        append = InvestigationService.append

        def ambiguous(instance, *args, **kwargs):
            nonlocal fired
            result = append(instance, *args, **kwargs)
            if not fired and kwargs.get("machine_analysis_id") == accepted["id"]:
                fired = True
                raise SQLAlchemyError("SYNTHETIC unknown response after actual committed append")
            return result

        monkeypatch.setattr(InvestigationService, "append", ambiguous)
    with pytest.raises(RuntimeError):
        service.execute(accepted["id"], accepted["input_hash"])
    assert fired
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.state == "running"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    resumed = make()
    resumed.recover()
    resumed.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [r["task"] for r in model.requests] == ["read_plan", "synthesize", "check"]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1


def test_pause_after_model_response_checkpoints_then_resumes_without_new_call(runtime, database):
    make, actor, view, _, model, *_ = runtime
    accepted = start(runtime)

    def pause():
        with Session(database) as session, session.begin():
            session.add(
                MonitorState(
                    workspace_id=actor.workspace_id,
                    paused=True,
                    control_revision=1,
                    source_progress={},
                    next_source=0,
                )
            )

    model.change = pause
    make().execute(accepted["id"], accepted["input_hash"])
    waiting = make().get(actor, view["id"], accepted["id"])
    assert waiting.state == "queued" and waiting.calls[0]["status"] == "received"
    assert len(model.requests) == 1
    with Session(database) as session, session.begin():
        session.get(MonitorState, actor.workspace_id).paused = False
    make().execute(accepted["id"], accepted["input_hash"])
    assert make().get(actor, view["id"], accepted["id"]).state == "completed"
    assert len(model.requests) == 3


def test_same_auto_reservation_freezes_one_analysis_even_if_materials_change(runtime, database, snapshot):
    from bioradar.research.engine.schemas import RunCreate
    from bioradar.research.mining.models import MiningPolicy
    from bioradar.research.monitor.service import MonitorService

    make, actor, view, _, model, _, adopted = runtime
    service = make()
    _, run = service.engine.create_run(
        actor,
        RunCreate(
            snapshot_id=adopted["id"],
            policy=MiningPolicy(as_of=datetime.now(UTC), method_ids=("text_topics",)),
        ),
        "synthetic-monitor-run",
    )
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    key, batch_id = "a" * 64, "synthetic-monitor-batch"
    assert monitor.reserve_investigation(key, batch_id, "routine")
    from bioradar.research.mining.models import canonical_hash

    origin = {
        "source": "monitor",
        "lead_key": key,
        "batch_id": batch_id,
        "run_id": run["id"],
        "reservation_key": "monitor-dig:" + canonical_hash([key, batch_id]),
    }
    _, first = service.start(
        actor, view["id"], AnalysisCreate(expected_revision=view["revision"]), "auto-one", origin=origin
    )
    current = InvestigationService(service.engine).get(actor, view["id"])
    InvestigationService(service.engine).append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "expected_revision": current.revision,
                "kind": "note",
                "text": "SYNTHETIC intervening human note.",
            }
        ),
        "intervening",
    )
    newer = InvestigationService(service.engine).get(actor, view["id"])
    _, same = make().start(
        actor, view["id"], AnalysisCreate(expected_revision=newer.revision), "auto-two", origin=origin
    )
    assert first["id"] == same["id"] and model.requests == []
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
    monitor.release()


def test_external_material_during_first_unfinished_analysis_is_not_swallowed(runtime, database):
    make, actor, view, _, model, _, adopted = runtime
    first = start(runtime)
    current = InvestigationService(make().engine).get(actor, view["id"])
    snapshot = make().engine.get_snapshot(actor, adopted["id"]).snapshot
    InvestigationService(make().engine).append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "expected_revision": current.revision,
                "kind": "material",
                "snapshot_id": adopted["id"],
                "record_ref": snapshot.records[1].record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC new evidence before the first draft finished.",
            }
        ),
        "first-attempt-new-evidence",
    )
    make().execute(first["id"], first["input_hash"])
    assert make().get(actor, view["id"], first["id"]).error_code == "revision_conflict"
    with Session(database) as session:
        second = session.scalar(
            select(InvestigationAnalysisRecord).where(InvestigationAnalysisRecord.id != first["id"])
        )
        assert second.trigger == "material_update" and second.parent_analysis_id is None
        second_id, second_hash = second.id, second.input_hash
    make().execute(second_id, second_hash)
    assert make().get(actor, view["id"], second_id).state == "completed"
    assert len(model.requests) == 3


def test_waiting_manual_update_does_not_take_new_slot_while_paused_or_closed(runtime, database):
    from bioradar.research.investigations.service import InvestigationRecord

    make, actor, view, _, _, settings, adopted = runtime
    settings.analysis_manual_per_hour = 1
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    holder_id = occupy_material_update_pool(runtime)
    current = InvestigationService(make().engine).get(actor, view["id"])
    snapshot = make().engine.get_snapshot(actor, adopted["id"]).snapshot
    InvestigationService(make().engine).append(
        actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "expected_revision": current.revision,
                "kind": "material",
                "snapshot_id": adopted["id"],
                "record_ref": snapshot.records[1].record_ref.model_dump(mode="json"),
                "stance": "context",
                "text": "SYNTHETIC pending manual update.",
            }
        ),
        "pending-update",
    )
    with Session(database) as session, session.begin():
        old = session.get(InvestigationAnalysisRecord, holder_id)
        data = copy.deepcopy(old.progress)
        data["budget_reserved_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        old.progress = data
        session.add(
            MonitorState(
                workspace_id=actor.workspace_id,
                paused=True,
                control_revision=1,
                source_progress={},
                next_source=0,
            )
        )
    make().recover()
    with Session(database) as session:
        pending = session.scalar(
            select(InvestigationAnalysisRecord).where(
                InvestigationAnalysisRecord.investigation_id == view["id"],
                InvestigationAnalysisRecord.id != accepted["id"],
            )
        )
        assert pending.progress["manual_budget_pending"] is True
        pending_id = pending.id
    with Session(database) as session, session.begin():
        session.get(MonitorState, actor.workspace_id).paused = False
        session.get(InvestigationRecord, view["id"]).status = "closed"
    make().recover()
    with Session(database) as session:
        pending = session.get(InvestigationAnalysisRecord, pending_id)
        assert pending.state == "blocked" and pending.progress["budget_reserved_at"] is None


def test_worker_reader_budget_includes_model_wait_and_old_unstarted_candidate(runtime, database, monkeypatch):
    import time
    from types import SimpleNamespace

    from bioradar.adapters import fabric_history, reasoning_model
    from bioradar.api import config
    from bioradar.data import database as database_module
    from bioradar.research.history import service as history_module
    from bioradar.research.investigations.analysis_service import execute_analysis, execution_seconds
    from pydantic import SecretStr

    make, actor, view, source, model, settings, _ = runtime
    accepted = start(runtime)
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        fixed = copy.deepcopy(row.fixed_input)
        del fixed["model_parameters"]["job_timeout_seconds"]
        row.fixed_input = fixed
        assert execution_seconds(fixed) == 780
    settings.data_fabric_url = "http://127.0.0.1"
    settings.data_fabric_history_read_token = SecretStr("SYNTHETIC-TOKEN")
    original_client = httpx.Client
    original_monotonic = time.monotonic
    elapsed = [0]
    monkeypatch.setattr(
        fabric_history, "time", SimpleNamespace(monotonic=lambda: original_monotonic() + elapsed[0])
    )
    monkeypatch.setattr(
        history_module.httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(source.handler)),
    )
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(database_module, "create_database", lambda unused: database)
    monkeypatch.setattr(reasoning_model, "complete_json", make().model_call)
    model.change = lambda: elapsed.__setitem__(
        0, 6
    )  # No sleep; source budget advances over legacy 5 seconds.
    execute_analysis(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert len(model.requests) == 3
    with Session(database) as session:
        assert (
            "job_timeout_seconds"
            not in session.get(InvestigationAnalysisRecord, accepted["id"]).fixed_input["model_parameters"]
        )


def test_disabling_semantics_after_received_response_stops_followup_without_discarding_it(runtime):
    make, actor, view, source, model, settings, _ = runtime
    accepted = start(runtime)
    model.change = lambda: setattr(settings, "semantic_llm_enabled", False)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "queued" and result.error_code == "analysis_paused"
    assert result.calls[0]["status"] == "received"
    assert not any(path == "/api/history/records" for path, _ in source.calls)
    assert len(model.requests) == 1


def test_material_recovery_passes_closed_prefix_and_ignores_other_workspace(runtime, database, monkeypatch):
    from uuid import uuid4

    from bioradar.research.investigations.service import InvestigationEntryRecord, InvestigationRecord

    make, actor, view, _, model, settings, adopted = runtime
    settings.analysis_manual_per_hour = 20  # Synthetic admission budget; production default is unchanged.
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session, session.begin():
        original = session.get(InvestigationAnalysisRecord, accepted["id"])
        first_material = session.scalar(
            select(InvestigationEntryRecord)
            .where(
                InvestigationEntryRecord.investigation_id == view["id"],
                InvestigationEntryRecord.kind == "material",
            )
            .order_by(InvestigationEntryRecord.revision)
            .limit(1)
        )
        foreign_ids, closed_ids = [], []
        for number in range(8):
            identifier = str(uuid4())
            foreign = number < 4
            (foreign_ids if foreign else closed_ids).append(identifier)
            session.add(
                InvestigationRecord(
                    id=identifier,
                    workspace_id="unrelated-workspace" if foreign else actor.workspace_id,
                    title="SYNTHETIC unreachable recovery prefix",
                    question=view["question"],
                    questions=[],
                    origin=None,
                    status="closed",
                    revision=100 + number,
                    is_demo=True,
                    created_by=actor.actor_id,
                    updated_at=datetime.now(UTC),
                )
            )
            session.flush()
            session.add(
                InvestigationEntryRecord(
                    id=str(uuid4()),
                    investigation_id=identifier,
                    revision=100 + number,
                    kind="material",
                    payload=copy.deepcopy(first_material.payload),
                    created_by=actor.actor_id,
                )
            )
            values = {
                column.name: copy.deepcopy(getattr(original, column.name))
                for column in InvestigationAnalysisRecord.__table__.columns
            }
            values.update(
                id=str(uuid4()),
                workspace_id="unrelated-workspace" if foreign else actor.workspace_id,
                investigation_id=identifier,
                parent_analysis_id=None,
                based_on_revision=2,
                note_entry_id=None,
            )
            values["progress"].pop("material_update_checked_at", None)
            session.add(InvestigationAnalysisRecord(**values))
    current = InvestigationService(make().engine).get(actor, view["id"])
    source = make().engine.get_snapshot(actor, adopted["id"]).snapshot.records[1]
    with monkeypatch.context() as fault:

        def unavailable(*args, **kwargs):
            raise DomainError("history_permission_unavailable", "SYNTHETIC append-hook outage", 503)

        fault.setattr(AnalysisService, "request_material_update", unavailable)
        InvestigationService(make().engine).append(
            actor,
            view["id"],
            InvestigationEntryCreate.model_validate(
                {
                    "expected_revision": current.revision,
                    "kind": "material",
                    "snapshot_id": adopted["id"],
                    "record_ref": source.record_ref.model_dump(mode="json"),
                    "stance": "context",
                    "text": "SYNTHETIC material committed before the follow-up hook became unavailable.",
                }
            ),
            "sweep-recovery-material",
        )
    make().recover(limit=4)
    with Session(database) as session:
        rows = list(
            session.scalars(
                select(InvestigationAnalysisRecord).where(
                    InvestigationAnalysisRecord.investigation_id.in_(foreign_ids + closed_ids)
                )
            )
        )
        assert all(
            r.progress.get("material_update_checked_at") for r in rows if r.investigation_id in closed_ids
        )
        assert all(
            not r.progress.get("material_update_checked_at")
            for r in rows
            if r.investigation_id in foreign_ids
        )
    make().recover(limit=4)
    with Session(database) as session:
        child = session.scalar(
            select(InvestigationAnalysisRecord).where(
                InvestigationAnalysisRecord.investigation_id == view["id"],
                InvestigationAnalysisRecord.id != accepted["id"],
            )
        )
        assert child is not None and child.trigger == "material_update"
    assert len(model.requests) == 3


def test_passage_selection_keeps_unicode_source_coordinates_and_rejects_wrong_bindings(snapshot):
    text = "甲" * 275 + "。\n" + "条件：不代表疗效失败。" + "乙" * 780
    source = "前缀原文" + text + "尾部尚未选择"
    start = len("前缀原文")
    record = snapshot.records[0]
    value = AnalysisEvidence(
        key="e-first",
        material_entry_id=None,
        snapshot_id="synthetic-snapshot",
        record_ref=record.record_ref,
        source_id=record.source_id,
        title="SYNTHETIC unicode source",
        text=text,
        start=start,
        end=start + len(text),
        text_sha256=hashlib.sha256(source.encode()).hexdigest(),
    ).model_dump(mode="json")
    frozen = freeze_passages(value)
    assert frozen == freeze_passages(value)
    assert "".join(p["text"] for p in frozen["passages"]) == text
    assert sum(len(p["text"]) for p in frozen["passages"]) == len(text)
    for passage in frozen["passages"]:
        assert 1 <= len(passage["text"]) <= 500
        assert source[passage["start"] : passage["end"]] == passage["text"]
    wire = model_evidence([frozen])[0]
    assert "text" not in wire
    assert all(set(p) == {"id", "text"} for p in wire["passages"])

    def claim(evidence_key, passage_id):
        return ModelClaim(
            id="c1",
            text="SYNTHETIC bounded claim",
            kind="source_statement",
            scope="SYNTHETIC source only",
            conditions=[],
            status="supported",
            evidence_links=[
                {
                    "evidence_key": evidence_key,
                    "passage_id": passage_id,
                    "role": "limits",
                    "reason": "SYNTHETIC condition must remain visible.",
                }
            ],
        )

    first = frozen["passages"][0]
    verified = validate_claims([claim(frozen["key"], first["id"])], [frozen])[0]
    assert verified.evidence_links[0].quote == source[first["start"] : first["end"]]
    second = freeze_passages({**value, "key": "e-second"})
    with pytest.raises(DomainError) as error:
        validate_claims([claim(frozen["key"], second["passages"][0]["id"])], [frozen, second])
    assert error.value.code == "analysis_passage_invalid"
    for field in ("text", "start", "text_sha256", "passage_text"):
        changed = copy.deepcopy(frozen)
        if field == "text":
            changed["text"] = "改" + changed["text"][1:]
        elif field == "start":
            changed["start"] += 1
        elif field == "text_sha256":
            changed["text_sha256"] = "0" * 64
        else:
            changed["passages"][0]["text"] = "改" + changed["passages"][0]["text"][1:]
        with pytest.raises(DomainError) as error:
            validate_claims([claim(changed["key"], first["id"])], [changed])
        assert error.value.code == "analysis_selection_mismatch"


def test_legacy_quote_only_analysis_remains_readable_in_export_shape(runtime, database):
    make, actor, view, *_ = runtime
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        fixed, progress, result = (
            copy.deepcopy(row.fixed_input),
            copy.deepcopy(row.progress),
            copy.deepcopy(row.result),
        )
        fixed["version"] = result["version"] = "bioradar-investigation-analysis-v0.2"
        fixed["prompt_version"] = "bioradar-investigation-reading-v0.2"
        fixed["model_parameters"].pop("output_format")
        for item in progress["evidence"]:
            item.pop("passages", None)
            item.pop("selection_hash", None)
        for claim in result["claims"]:
            for link in claim["evidence_links"]:
                link.pop("passage_id", None)
        row.fixed_input, row.progress, row.result = fixed, progress, result
    old = make().get(actor, view["id"], accepted["id"])
    assert old.result.version == "bioradar-investigation-analysis-v0.2"
    assert old.result.claims[0].evidence_links[0].quote
    assert old.result.claims[0].evidence_links[0].passage_id is None
    assert old.evidence[0].passages == [] and old.evidence[0].selection_hash is None
    exported = old.model_dump(mode="json")
    assert exported["result"]["claims"][0]["evidence_links"][0]["quote"]
    historical = copy.deepcopy(exported["result"])
    historical["review"]["findings"] = [
        {
            "path": "/conditional_answer/unknowns/0",
            "issue": "SYNTHETIC historical pointer retained as recorded.",
            "evidence_keys": [],
        }
    ]
    historical["review"]["checked_sections"] = ["conditional_answer"]
    from bioradar.research.investigations.analysis_models import AnalysisResult

    reread = AnalysisResult.model_validate(historical)
    assert reread.review.findings[0].path == "/conditional_answer/unknowns/0"
    assert reread.review.checked_sections == ["conditional_answer"]


def test_nonhistory_materials_stop_at_character_budget_after_field_overhead(runtime, snapshot):
    from bioradar.research.investigations.analysis import MAX_CHARACTERS
    from bioradar.research.mining.models import NormalizedSnapshot

    make, actor, view, _, model, *_ = runtime
    value = snapshot.model_dump(mode="json")
    records = []
    for index in range(10):
        record = copy.deepcopy(value["records"][0])
        content = f"SYNTHETIC-{index} " + "a" * 6000
        record["normalized_text"] = content
        record["normalized_text_hash"] = hashlib.sha256(content.encode()).hexdigest()
        record["embedding"] = None
        record["record_ref"]["object_id"] = f"synthetic-budget-{index}"
        record["record_ref"]["adopted_version"] = record["normalized_text_hash"]
        record["record_ref"]["content_hash"] = record["normalized_text_hash"]
        records.append(record)
    value.update(snapshot_id="synthetic-budget", records=records, coverage=[])
    selected = NormalizedSnapshot.model_validate(value)
    engine = make().engine
    _, adopted = engine.adopt_snapshot(actor, selected, "synthetic-long-source")
    inquiry = InvestigationService(engine)
    current = inquiry.get(actor, view["id"])
    for index, record in enumerate(selected.records):
        _, result = inquiry.append(
            actor,
            view["id"],
            InvestigationEntryCreate.model_validate(
                {
                    "expected_revision": current.revision,
                    "kind": "material",
                    "snapshot_id": adopted["id"],
                    "record_ref": record.record_ref.model_dump(mode="json"),
                    "stance": "context",
                    "text": "SYNTHETIC long source for input budget.",
                }
            ),
            f"synthetic-long-material-{index}",
        )
        current = inquiry.get(actor, result["id"])
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert model.requests
    assert (
        sum(
            sum(len(p["text"]) for p in item["passages"]) + len(json.dumps(item["facts"], ensure_ascii=False))
            for item in model.requests[0]["evidence"]
        )
        <= MAX_CHARACTERS
    )
    assert len(result.evidence) < 10
    assert any("字符预算" in limit for limit in result.limitations)


@pytest.mark.parametrize("scenario", ["fixed", "commit_unknown", "remaining_finding", "false_finding"])
def test_full_draft_finding_revises_once_and_reconciles_received_revision(runtime, database, scenario):
    from sqlalchemy.exc import SQLAlchemyError

    make, actor, view, source, model, *_ = runtime

    def response(payload, result):
        if payload["task"] == "synthesize" and scenario != "false_finding":
            result["conditional_answer"]["unknowns"] = ["SYNTHETIC no material has been obtained."]
        elif payload["task"] == "check":
            result["findings"] = [
                {
                    "section": "claims" if scenario == "false_finding" else "conditional_answer",
                    "claim_id": "c1" if scenario == "false_finding" else None,
                    "issue": "SYNTHETIC review may itself be wrong; verify against current evidence.",
                    "evidence_keys": [payload["evidence"][0]["evidence_key"]],
                }
            ]
        elif payload["task"] == "revise":
            assert payload["review_findings"]["findings"]
            assert "draft_to_revise" in payload
            if scenario != "false_finding":
                result["conditional_answer"]["unknowns"] = ["SYNTHETIC long-term outcome remains unknown."]
        elif payload["task"] == "recheck":
            assert "review_findings" not in payload
            assert "SYNTHETIC no material" not in json.dumps(payload["draft_to_check"])
            if scenario == "remaining_finding":
                result["findings"] = [
                    {
                        "section": "conditional_answer",
                        "claim_id": None,
                        "issue": "SYNTHETIC a documented limitation still needs human review.",
                        "evidence_keys": [payload["evidence"][0]["evidence_key"]],
                    }
                ]

    model.transform = response
    accepted = start(runtime)
    service = make()
    if scenario == "commit_unknown":
        checkpoint = service._checkpoint
        fired = False

        def ambiguous(identifier, token, progress, **kwargs):
            nonlocal fired
            checkpoint(identifier, token, progress, **kwargs)
            if not fired and any(
                c["stage"] == "revise" and c["status"] == "received" for c in progress["calls"]
            ):
                fired = True
                raise SQLAlchemyError("SYNTHETIC revision response commit outcome unknown")

        service._checkpoint = ambiguous
    if scenario == "commit_unknown":
        with pytest.raises(RuntimeError, match="unconfirmed"):
            service.execute(accepted["id"], accepted["input_hash"])
    else:
        service.execute(accepted["id"], accepted["input_hash"])
    if scenario == "commit_unknown":
        pending = make().get(actor, view["id"], accepted["id"])
        assert pending.initial_draft.review.findings and len(model.requests) == 4
        with Session(database) as session, session.begin():
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        make().recover()
        make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    expected = "needs_review" if scenario == "remaining_finding" else "completed"
    assert result.state == expected, result.error_code
    assert [p["task"] for p in model.requests] == [
        "read_plan",
        "synthesize",
        "check",
        "revise",
        "recheck",
    ]
    assert result.initial_draft.review.status == "needs_review"
    for payload in model.requests[1:]:
        assert "reading" not in payload and "previous_analysis" not in payload
        assert all("plan" not in query for query in payload["queries"])
    if scenario == "false_finding":
        assert result.result.claims == result.initial_draft.claims
    else:
        assert "no material" in result.initial_draft.conditional_answer.unknowns[0]
        assert result.result.conditional_answer.unknowns == ["SYNTHETIC long-term outcome remains unknown."]
    assert bool(result.result.review.findings) == (scenario == "remaining_finding")
    assert sum(path == "/api/history/records" for path, _ in source.calls) == 1
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 1
    assert InvestigationService(make().engine).get(actor, view["id"]).judgment_count == 0


@pytest.mark.parametrize(
    "section,claim_id,valid",
    [("conditional_answer", None, True), ("claims", "unknown-claim", False), ("unknown", None, False)],
)
def test_finding_section_and_optional_claim_bind_to_current_draft(
    runtime, database, section, claim_id, valid
):
    make, actor, view, _, model, *_ = runtime

    def response(payload, result):
        if payload["task"] == "check":
            result["findings"] = [
                {
                    "section": section,
                    "claim_id": claim_id,
                    "issue": "SYNTHETIC issue in the current draft.",
                    "evidence_keys": [payload["evidence"][0]["evidence_key"]],
                }
            ]

    model.transform = response
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    if valid:
        assert result.state == "completed", result.error_code
        assert len(model.requests) == 5
        assert result.initial_draft.review.findings[0].section == "conditional_answer"
        assert result.initial_draft.review.findings[0].path is None
        assert result.initial_draft.review.checked_sections == []
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            original = next(c["output"] for c in row.progress["calls"] if c["stage"] == "check")
            assert original["findings"][0]["section"] == section
            assert "checked_sections" not in original
    else:
        assert result.state == "blocked" and result.error_code == "analysis_review_invalid"
        assert len(model.requests) == 3 and result.initial_draft is None


@pytest.mark.parametrize("stance", ["maintained", "revised"])
def test_same_basis_old_version_runs_new_analysis_with_only_unverified_parent_claims(
    runtime, database, stance
):
    from bioradar.research.mining.models import canonical_hash

    make, actor, view, _, model, *_ = runtime
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        fixed, result = copy.deepcopy(row.fixed_input), copy.deepcopy(row.result)
        fixed["version"] = result["version"] = "bioradar-investigation-analysis-v0.3"
        fixed["prompt_version"] = "bioradar-investigation-reading-v0.3"
        result["conditional_answer"]["unknowns"] = ["SYNTHETIC STALE OLD UNKNOWN"]
        result["review"]["overall_limitations"] = ["SYNTHETIC STALE OLD REVIEW"]
        row.fixed_input, row.result = fixed, result
        previous_hash = canonical_hash(result)
    current = InvestigationService(make().engine).get(actor, view["id"])
    code, new = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=current.revision, parent_analysis_id=accepted["id"]),
        "explicit-new-compute-version",
    )
    assert code == 202 and new["id"] != accepted["id"]

    def response(payload, result):
        if payload["task"] == "compare":
            result.update(
                stance=stance,
                changed_claims=[],
                reasons=["SYNTHETIC current evidence preserves the same bounded interpretation."],
            )

    model.transform = response
    make().execute(new["id"], new["input_hash"])
    updated = make().get(actor, view["id"], new["id"])
    if stance == "maintained":
        assert updated.state == "completed", updated.error_code
        assert updated.result.update.changed_claims == [] and updated.result.update.reasons
        assert [p["task"] for p in model.requests[3:]] == [
            "read_plan",
            "synthesize",
            "check",
            "compare",
            "final_check",
        ]
    else:
        assert updated.state == "needs_review" and updated.error_code == "analysis_update_invalid"
        assert updated.result.claims and updated.result.update.stance == "unable_to_compare"
        assert [p["task"] for p in model.requests[3:]] == ["read_plan", "synthesize", "check", "compare"]
    for payload in model.requests[3:]:
        assert "previous_analysis" not in payload and "reading" not in payload
        if payload["task"] not in {"compare", "final_check"}:
            assert "comparison_target" not in payload
            continue
        parent = payload["comparison_target"]
        assert set(parent) == {"qualification", "claims"}
        assert all(
            set(claim) == {"id", "text", "kind", "scope", "conditions", "status"}
            for claim in parent["claims"]
        )
        assert "STALE OLD" not in json.dumps(payload)
    with Session(database) as session:
        assert (
            canonical_hash(session.get(InvestigationAnalysisRecord, accepted["id"]).result) == previous_hash
        )


def test_six_small_claims_keep_individual_passage_bindings(runtime):
    make, actor, view, _, model, *_ = runtime

    def six_claims(payload, result):
        if payload["task"] in {"read_plan", "synthesize"}:
            template = result["claims"][0]
            result["claims"] = [
                dict(copy.deepcopy(template), id=f"c{index}", text=f"SYNTHETIC source fact {index}.")
                for index in range(1, 7)
            ]

    model.transform = six_claims
    accepted = start(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert len(result.result.claims) == 6
    assert len(result.result.review.claims) == 6
    assert len(model.requests) == 3
    for claim in result.result.claims:
        assert claim.evidence_links[0].passage_id and claim.evidence_links[0].quote


@pytest.mark.parametrize(
    "outcome",
    [
        "passed",
        "remaining_issue",
        "timeout",
        "tries_rewrite",
        "commit_unknown",
        "pause",
        "human_revision",
        "sent_crash",
    ],
)
def test_parent_only_enters_comparison_and_failed_comparison_preserves_current_draft(
    runtime, database, outcome
):
    from bioradar.research.mining.models import canonical_hash
    from sqlalchemy.exc import SQLAlchemyError

    make, actor, view, _, model, *_ = runtime
    parent = start(runtime)
    make().execute(parent["id"], parent["input_hash"])
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, parent["id"])
        fixed, old_result = copy.deepcopy(row.fixed_input), copy.deepcopy(row.result)
        fixed["version"] = old_result["version"] = "bioradar-investigation-analysis-v0.4.3"
        fixed["prompt_version"] = "bioradar-investigation-reading-v0.4.3"
        old_result["claims"][0]["text"] = "SYNTHETIC OLD UNVERIFIED HYPOTHESIS"
        old_result["review"]["status"] = "needs_review"
        row.fixed_input, row.result, row.state = fixed, old_result, "needs_review"
        parent_hash = canonical_hash(old_result)
    current = InvestigationService(make().engine).get(actor, view["id"])
    _, created = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=current.revision, parent_analysis_id=parent["id"]),
        "explicit-isolated-parent-comparison",
    )
    current_hashes = []

    def response(payload, result):
        if payload["task"] not in {"compare", "final_check"}:
            assert "comparison_target" not in payload
            assert "OLD UNVERIFIED" not in json.dumps(payload)
        if payload["task"] in {"check", "recheck"}:
            assert "update" not in payload["draft_to_check"]
        if payload["task"] == "check" or (payload["task"] == "recheck" and outcome == "remaining_issue"):
            result["findings"] = [
                {
                    "section": "conditional_answer",
                    "claim_id": None,
                    "issue": "SYNTHETIC issue requires checking this current draft.",
                    "evidence_keys": [payload["evidence"][0]["evidence_key"]],
                }
            ]
        if payload["task"] == "compare":
            assert "OLD UNVERIFIED" in json.dumps(payload["comparison_target"])
            assert "update" not in payload["current_analysis"]
            current_hashes.append(canonical_hash(payload["current_analysis"]))
            # The current draft must be durable before the separately fallible call.
            with Session(database) as session:
                row = session.get(InvestigationAnalysisRecord, created["id"])
                assert row.result["update"]["stance"] == "unable_to_compare"
                observed = {
                    key: row.result[key] for key in ("claims", "explanations", "gaps", "conditional_answer")
                }
                # The public draft has server-verified quote/span fields as well.
                assert observed["conditional_answer"] == payload["current_analysis"]["conditional_answer"]
            if outcome == "timeout":
                raise httpx.ReadTimeout("SYNTHETIC ambiguous comparison request")
            if outcome == "tries_rewrite":
                result["claims"] = [{"text": "SYNTHETIC forbidden rewrite"}]
            if outcome == "pause":
                with Session(database) as session, session.begin():
                    session.add(
                        MonitorState(
                            workspace_id=actor.workspace_id,
                            paused=True,
                            control_revision=1,
                            source_progress={},
                            next_source=0,
                        )
                    )
            if outcome == "human_revision":
                inquiry = InvestigationService(make().engine)
                latest = inquiry.get(actor, view["id"])
                inquiry.append(
                    actor,
                    view["id"],
                    InvestigationEntryCreate.model_validate(
                        {
                            "expected_revision": latest.revision,
                            "kind": "note",
                            "text": "SYNTHETIC researcher changed the inquiry during comparison.",
                        }
                    ),
                    "human-during-comparison",
                )

    model.transform = response
    service = make()
    fired = False
    if outcome in {"commit_unknown", "sent_crash"}:
        checkpoint = service._checkpoint

        def ambiguous(identifier, token, progress, **kwargs):
            nonlocal fired
            checkpoint(identifier, token, progress, **kwargs)
            target = "sent" if outcome == "sent_crash" else "received"
            if not fired and any(
                c["stage"] == "compare" and c["status"] == target for c in progress["calls"]
            ):
                fired = True
                raise SQLAlchemyError("SYNTHETIC comparison checkpoint outcome unknown")

        service._checkpoint = ambiguous
        with pytest.raises(RuntimeError, match="unconfirmed"):
            service.execute(created["id"], created["input_hash"])
        with Session(database) as session, session.begin():
            row = session.get(InvestigationAnalysisRecord, created["id"])
            assert row.result["update"]["stance"] == "unable_to_compare"
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        make().recover()
        make().execute(created["id"], created["input_hash"])
    else:
        service.execute(created["id"], created["input_hash"])
    if outcome == "pause":
        waiting = make().get(actor, view["id"], created["id"])
        assert waiting.state == "queued" and waiting.error_code == "analysis_paused"
        assert waiting.calls[-1]["status"] == "received" and waiting.note_entry_id is None
        assert waiting.result.update.stance == "unable_to_compare"
        with Session(database) as session, session.begin():
            session.get(MonitorState, actor.workspace_id).paused = False
        make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    tasks = [p["task"] for p in model.requests[3:]]
    expected_tasks = ["read_plan", "synthesize", "check", "revise", "recheck"]
    if outcome not in {"remaining_issue", "sent_crash"}:
        expected_tasks.append("compare")
    if outcome in {"passed", "commit_unknown", "pause"}:
        expected_tasks.append("final_check")
    assert tasks == expected_tasks
    assert result.result.claims and result.initial_draft is not None
    if outcome in {"passed", "commit_unknown", "pause"}:
        assert "update" in result.result.review.checked_sections
    else:
        assert result.result.review.checked_sections == []
    if outcome in {"passed", "commit_unknown", "pause"}:
        assert result.state == "completed" and result.result.update.stance == "maintained"
        assert result.error_code is None and len(current_hashes) == 1
    elif outcome == "human_revision":
        assert result.state == "blocked" and result.error_code == "revision_conflict"
        assert result.calls[-1]["status"] == "received" and result.note_entry_id is None
        assert result.result.update.stance == "unable_to_compare"
    else:
        assert result.state == "needs_review" and result.result.update.stance == "unable_to_compare"
        if outcome == "sent_crash":
            assert result.error_code == "model_call_outcome_unconfirmed"
            assert result.calls[-1]["status"] == "sent" and result.note_entry_id is None
        elif outcome == "timeout":
            assert result.error_code == "model_transport_unconfirmed"
            assert result.calls[-1]["status"] == "unconfirmed"
        elif outcome == "tries_rewrite":
            assert result.error_code == "analysis_update_invalid"
        else:
            assert result.error_code is None and result.result.review.findings
    assert all(
        "OLD UNVERIFIED" not in json.dumps(p)
        for p in model.requests[3:]
        if p["task"] not in {"compare", "final_check"}
    )
    before = len(model.requests)
    make().execute(created["id"], created["input_hash"])
    assert len(model.requests) == before
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, created["id"])
        assert row.fixed_input["model_parameters"]["max_calls"] == 16
        assert row.fixed_input["model_parameters"]["job_timeout_seconds"] == 1860
        assert canonical_hash(session.get(InvestigationAnalysisRecord, parent["id"]).result) == parent_hash
        assert session.scalar(select(func.count()).select_from(InvestigationAnalysisRecord)) == 2
    assert InvestigationService(make().engine).get(actor, view["id"]).judgment_count == 0


def test_empty_model_reading_is_classified_and_preserved_without_fabricated_content(runtime, database):
    make, actor, view, source, model, *_ = runtime

    def empty_reading(payload, result):
        if payload["task"] == "read_plan":
            result.clear()

    model.transform = empty_reading
    created = start(runtime)
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "blocked" and result.error_code == "analysis_reading_invalid"
    assert result.result is None and result.note_entry_id is None
    assert len(model.requests) == 1 and result.calls[0]["status"] == "received"
    assert result.calls[0]["receipt"]["finish_reason"] == "stop"
    assert not any(path == "/api/history/records" for path, _ in source.calls)
    with Session(database) as session:
        call = session.get(InvestigationAnalysisRecord, created["id"]).progress["calls"][0]
        assert call["output"] == {}
        assert set(call["response_schema"]["required"]) == {
            "claims",
            "explanations",
            "gaps",
            "next_queries",
            "conditional_answer",
        }
        wire = model.wires[0]["response_format"]
        assert wire["type"] == "json_schema" and wire["json_schema"]["strict"] is True
        assert wire["json_schema"]["schema"] == call["response_schema"]
    make().execute(created["id"], created["input_hash"])
    assert len(model.requests) == 1
    assert InvestigationService(make().engine).get(actor, view["id"]).judgment_count == 0


def test_legacy_output_format_cannot_resume_a_cached_response_under_structured_decoding(runtime, database):
    make, actor, view, source, model, *_ = runtime
    created = start(runtime)

    def pause():
        with Session(database) as session, session.begin():
            session.add(
                MonitorState(
                    workspace_id=actor.workspace_id,
                    paused=True,
                    control_revision=1,
                    source_progress={},
                    next_source=0,
                )
            )

    model.change = pause
    make().execute(created["id"], created["input_hash"])
    waiting = make().get(actor, view["id"], created["id"])
    assert waiting.state == "queued" and waiting.calls[0]["status"] == "received"
    receipt = copy.deepcopy(waiting.calls[0]["receipt"])
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, created["id"])
        fixed = copy.deepcopy(row.fixed_input)
        assert fixed["model_parameters"].pop("output_format") == "json_schema"
        row.fixed_input = fixed
        session.get(MonitorState, actor.workspace_id).paused = False
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "blocked" and result.error_code == "analysis_model_changed"
    assert result.calls[0]["receipt"] == receipt and result.note_entry_id is None
    assert len(model.requests) == 1
    assert not any(path == "/api/history/records" for path, _ in source.calls)
    with Session(database) as session:
        assert session.get(InvestigationAnalysisRecord, created["id"]).fixed_input == fixed


def test_incomplete_model_review_is_classified_without_fabricating_coverage(runtime):
    make, actor, view, _, model, *_ = runtime

    def incomplete(payload, result):
        if payload["task"] == "check":
            result.pop("claims")

    model.transform = incomplete
    created = start(runtime)
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "blocked" and result.error_code == "analysis_review_invalid"
    assert result.calls[-1]["status"] == "received"
    assert result.result is None and len(model.requests) == 3


def test_cached_old_version_response_cannot_continue_under_new_analysis_code(runtime, database):
    make, actor, view, source, model, *_ = runtime
    created = start(runtime)

    def pause():
        with Session(database) as session, session.begin():
            session.add(
                MonitorState(
                    workspace_id=actor.workspace_id,
                    paused=True,
                    control_revision=1,
                    source_progress={},
                    next_source=0,
                )
            )

    model.change = pause
    make().execute(created["id"], created["input_hash"])
    waiting = make().get(actor, view["id"], created["id"])
    assert waiting.state == "queued" and waiting.calls[0]["status"] == "received"
    original_receipt = copy.deepcopy(waiting.calls[0]["receipt"])
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, created["id"])
        fixed = copy.deepcopy(row.fixed_input)
        fixed["version"] = "bioradar-investigation-analysis-v0.4.3"
        fixed["prompt_version"] = "bioradar-investigation-reading-v0.4.3"
        row.fixed_input = fixed
        session.get(MonitorState, actor.workspace_id).paused = False
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "blocked" and result.error_code == "analysis_model_changed"
    assert result.version == "bioradar-investigation-analysis-v0.4.3"
    assert result.calls[0]["receipt"] == original_receipt
    assert len(model.requests) == 1 and result.note_entry_id is None
    assert not any(path == "/api/history/records" for path, _ in source.calls)


@pytest.mark.parametrize("keys", [None, ["not-an-input-evidence-key"]])
def test_finding_without_attached_evidence_can_be_checked_but_unknown_keys_are_rejected(
    runtime, database, keys
):
    make, actor, view, _, model, *_ = runtime

    def response(payload, result):
        if payload["task"] == "check":
            finding = {
                "section": "conditional_answer",
                "claim_id": None,
                "issue": "SYNTHETIC a draft inconsistency, not an additional source fact.",
            }
            if keys is not None:
                finding["evidence_keys"] = keys
            result["findings"] = [finding]

    model.transform = response
    created = start(runtime)
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    if keys is None:
        assert result.state == "completed", result.error_code
        assert [p["task"] for p in model.requests] == [
            "read_plan",
            "synthesize",
            "check",
            "revise",
            "recheck",
        ]
        assert result.initial_draft.review.findings[0].evidence_keys == []
        assert result.initial_draft.review.claims[0].evidence_keys
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, created["id"])
            call = next(c for c in row.progress["calls"] if c["stage"] == "check")
            assert "evidence_keys" not in call["output"]["findings"][0]
            assert "evidence_keys" not in call["response_schema"]["$defs"]["ModelFinding"]["required"]
    else:
        assert result.state == "blocked" and result.error_code == "analysis_reference_invalid"
        assert len(model.requests) == 3 and result.initial_draft is None
    assert InvestigationService(make().engine).get(actor, view["id"]).judgment_count == 0


@pytest.mark.parametrize(
    "setting,value",
    [
        ("semantic_llm_enable_thinking", True),
        ("semantic_llm_temperature", 0.6),
        ("semantic_llm_top_p", 0.95),
        ("semantic_llm_top_k", 20),
        ("semantic_llm_timeout_seconds", 150),
        ("semantic_llm_max_output_tokens", 12288),
    ],
)
def test_cached_response_cannot_continue_after_frozen_model_parameters_change(
    runtime, database, setting, value
):
    make, actor, view, source, model, settings, _ = runtime
    created = start(runtime)

    def pause():
        with Session(database) as session, session.begin():
            session.add(
                MonitorState(
                    workspace_id=actor.workspace_id,
                    paused=True,
                    control_revision=1,
                    source_progress={},
                    next_source=0,
                )
            )

    model.change = pause
    make().execute(created["id"], created["input_hash"])
    waiting = make().get(actor, view["id"], created["id"])
    assert waiting.state == "queued" and waiting.calls[0]["status"] == "received"
    receipt = copy.deepcopy(waiting.calls[0]["receipt"])
    with Session(database) as session, session.begin():
        original_fixed = copy.deepcopy(session.get(InvestigationAnalysisRecord, created["id"]).fixed_input)
        session.get(MonitorState, actor.workspace_id).paused = False
    setattr(settings, setting, value)
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "blocked" and result.error_code == "analysis_model_changed"
    assert result.calls[0]["receipt"] == receipt and result.note_entry_id is None
    assert len(model.requests) == 1
    assert not any(path == "/api/history/records" for path, _ in source.calls)
    with Session(database) as session:
        assert session.get(InvestigationAnalysisRecord, created["id"]).fixed_input == original_fixed


def test_model_parameter_change_creates_new_basis_and_same_parameters_reuse_it(runtime, database):
    make, actor, view, _, model, settings, _ = runtime
    parent = start(runtime)
    make().execute(parent["id"], parent["input_hash"])
    old = make().get(actor, view["id"], parent["id"]).model_dump(mode="json")
    settings.semantic_llm_enable_thinking = True
    settings.semantic_llm_temperature = 0.6
    settings.semantic_llm_top_p = 0.95
    settings.semantic_llm_top_k = 20
    settings.semantic_llm_max_output_tokens = 12288
    settings.semantic_llm_timeout_seconds = 150
    inquiry = InvestigationService(make().engine)
    latest = inquiry.get(actor, view["id"])
    code, created = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=latest.revision, parent_analysis_id=parent["id"]),
        "explicit-new-model-parameters",
    )
    assert code == 202 and created["id"] != parent["id"]
    assert created["input_hash"] != parent["input_hash"]
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "completed", result.error_code
    assert [p["task"] for p in model.requests[3:]] == [
        "read_plan",
        "synthesize",
        "check",
        "compare",
        "final_check",
    ]
    for wire in model.wires[3:]:
        assert wire["chat_template_kwargs"]["enable_thinking"] is True
        assert wire["temperature"] == 0.6 and wire["top_p"] == 0.95 and wire["top_k"] == 20
        assert wire["max_tokens"] == 12288
    with Session(database) as session:
        old_row = session.get(InvestigationAnalysisRecord, parent["id"])
        new_row = session.get(InvestigationAnalysisRecord, created["id"])
        assert old_row.fixed_input["basis_signature"] != new_row.fixed_input["basis_signature"]
        assert new_row.fixed_input["model_parameters"]["job_timeout_seconds"] == 1860
        assert "model" not in new_row.fixed_input["model_parameters"]
        assert "model_revision" not in new_row.fixed_input["model_parameters"]
    latest = inquiry.get(actor, view["id"])
    code, duplicate = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=latest.revision, parent_analysis_id=created["id"]),
        "same-model-parameters-new-click",
    )
    assert code == 200 and duplicate["id"] == created["id"] and len(model.requests) == 8
    assert make().get(actor, view["id"], parent["id"]).model_dump(mode="json") == old


def test_missing_historical_sampling_uses_old_defaults_not_current_live_values(runtime, database):
    make, actor, view, _, model, settings, _ = runtime
    created = start(runtime)
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, created["id"])
        fixed = copy.deepcopy(row.fixed_input)
        for key in ("temperature", "top_p", "top_k", "static_options"):
            fixed["model_parameters"].pop(key)
        row.fixed_input = fixed
    settings.semantic_llm_enable_thinking = True
    settings.semantic_llm_temperature = 0.6
    settings.semantic_llm_top_p = 0.95
    settings.semantic_llm_top_k = 20
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "blocked" and result.error_code == "analysis_model_changed"
    assert model.requests == []
    with Session(database) as session:
        unchanged = session.get(InvestigationAnalysisRecord, created["id"]).fixed_input
        assert unchanged == fixed


def test_selected_citation_table_uses_revised_current_passages_not_initial_links(runtime):
    make, actor, view, _, model, *_ = runtime

    def response(payload, result):
        if payload["task"] == "check":
            assert payload["selected_citations"]
            first = payload["selected_citations"][0]
            assert first["evidence_key"] == payload["evidence"][0]["evidence_key"]
            assert first["quote"] == payload["evidence"][0]["passages"][0]["text"]
            result["findings"] = [
                {
                    "section": "claims",
                    "claim_id": "c1",
                    "issue": "SYNTHETIC verify a different current source; the criticism may be wrong.",
                }
            ]
        elif payload["task"] == "revise":
            assert payload["selected_citations"][0]["evidence_key"] == payload["evidence"][0]["evidence_key"]
            selected = payload["evidence"][1]
            result["claims"][0]["evidence_links"] = [
                {
                    "evidence_key": selected["evidence_key"],
                    "passage_id": selected["passages"][0]["id"],
                    "role": "supports",
                    "reason": "SYNTHETIC revised bounded source selection.",
                }
            ]
        elif payload["task"] == "recheck":
            selected = payload["evidence"][1]
            table = payload["selected_citations"]
            assert len(table) == 1
            assert table[0] == {
                "claim_id": "c1",
                "evidence_key": selected["evidence_key"],
                "passage_id": selected["passages"][0]["id"],
                "role": "supports",
                "reason": "SYNTHETIC revised bounded source selection.",
                "quote": selected["passages"][0]["text"],
            }
            assert len(payload["evidence"]) == 2
            result["claims"][0]["evidence_keys"] = [selected["evidence_key"]]

    model.transform = response
    created = start(runtime)
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "completed" and len(model.requests) == 5
    assert all("selected_citations" not in p for p in model.requests[:2])
    revised = next(p for p in model.requests if p["task"] == "recheck")["selected_citations"][0]
    assert result.result.claims[0].evidence_links[0].quote == revised["quote"]
    assert result.initial_draft.claims[0].evidence_links[0].evidence_key != revised["evidence_key"]
    for wire in model.wires:
        assert wire["max_tokens"] == 4096 and wire["temperature"] == 0


def test_explicit_provider_switch_creates_a_new_frozen_basis_and_keeps_old_results(runtime, database):
    make, actor, view, _, model, settings, _ = runtime
    parent = start(runtime)
    make().execute(parent["id"], parent["input_hash"])
    old = make().get(actor, view["id"], parent["id"]).model_dump(mode="json")
    with Session(database) as session:
        old_fixed = copy.deepcopy(session.get(InvestigationAnalysisRecord, parent["id"]).fixed_input)
    assert "provider" not in old_fixed["model_parameters"]
    assert "reasoning_effort" not in old_fixed["model_parameters"]
    inquiry = InvestigationService(make().engine)
    code, reused = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=inquiry.get(actor, view["id"]).revision,
            parent_analysis_id=parent["id"],
        ),
        "same-original-provider-basis",
    )
    assert code == 200 and reused["id"] == parent["id"] and len(model.requests) == 3
    settings.semantic_llm_provider = "deepseek"
    settings.semantic_llm_enable_thinking = True
    settings.semantic_llm_reasoning_effort = "max"
    inquiry = InvestigationService(make().engine)
    latest = inquiry.get(actor, view["id"])
    code, created = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=latest.revision, parent_analysis_id=parent["id"]),
        "explicit-provider-change",
    )
    assert code == 202 and created["id"] != parent["id"]
    assert created["input_hash"] != parent["input_hash"]
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "completed", result.error_code
    assert [p["task"] for p in model.requests[3:]] == [
        "read_plan",
        "synthesize",
        "check",
        "compare",
        "final_check",
    ]
    for wire in model.wires[3:]:
        assert wire["response_format"] == {"type": "json_object"}
        assert wire["thinking"] == {"type": "enabled"} and wire["reasoning_effort"] == "max"
        assert "temperature" not in wire and "top_k" not in wire
        assert "chat_template_kwargs" not in wire
    with Session(database) as session:
        old_row = session.get(InvestigationAnalysisRecord, parent["id"])
        fixed = session.get(InvestigationAnalysisRecord, created["id"]).fixed_input
        assert old_row.fixed_input == old_fixed
        assert fixed["basis_signature"] != old_fixed["basis_signature"]
        assert fixed["version"] == old_fixed["version"]
        assert fixed["prompt_version"] == old_fixed["prompt_version"]
        assert fixed["model_parameters"]["provider"] == "deepseek"
        assert fixed["model_parameters"]["output_format"] == "json_object"
        assert fixed["model_parameters"]["reasoning_effort"] == "max"
    latest = inquiry.get(actor, view["id"])
    code, duplicate = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=latest.revision, parent_analysis_id=created["id"]),
        "same-explicit-provider-basis",
    )
    assert code == 200 and duplicate["id"] == created["id"] and len(model.requests) == 8
    assert make().get(actor, view["id"], parent["id"]).model_dump(mode="json") == old
    assert inquiry.get(actor, view["id"]).judgment_count == 0


def test_explicit_provider_switch_cannot_resume_cached_response_from_the_old_provider(runtime, database):
    make, actor, view, source, model, settings, _ = runtime
    created = start(runtime)

    def pause():
        with Session(database) as session, session.begin():
            session.add(
                MonitorState(
                    workspace_id=actor.workspace_id,
                    paused=True,
                    control_revision=1,
                    source_progress={},
                    next_source=0,
                )
            )

    model.change = pause
    make().execute(created["id"], created["input_hash"])
    waiting = make().get(actor, view["id"], created["id"])
    assert waiting.state == "queued" and waiting.calls[0]["status"] == "received"
    receipt = copy.deepcopy(waiting.calls[0]["receipt"])
    with Session(database) as session, session.begin():
        fixed = copy.deepcopy(session.get(InvestigationAnalysisRecord, created["id"]).fixed_input)
        session.get(MonitorState, actor.workspace_id).paused = False
    settings.semantic_llm_provider = "deepseek"
    settings.semantic_llm_enable_thinking = True
    settings.semantic_llm_reasoning_effort = "high"
    make().execute(created["id"], created["input_hash"])
    result = make().get(actor, view["id"], created["id"])
    assert result.state == "blocked" and result.error_code == "analysis_model_changed"
    assert result.calls[0]["receipt"] == receipt and result.note_entry_id is None
    assert len(model.requests) == 1
    assert not any(path == "/api/history/records" for path, _ in source.calls)
    with Session(database) as session:
        assert session.get(InvestigationAnalysisRecord, created["id"]).fixed_input == fixed
