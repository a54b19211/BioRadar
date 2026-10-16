"""Manual investigation behavior against the actual PostgreSQL migration and HTTP API."""

import copy
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.api.app import create_app
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.models import EngineRun
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.models import InvestigationCreate, InvestigationEntryCreate
from bioradar.research.investigations.service import (
    InvestigationEntryRecord,
    InvestigationRecord,
    InvestigationService,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import CREDENTIAL, ROOT, accept, controlled_result, tokens
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import service as service  # noqa: F401
from test_engine_persistence import settings as settings  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401

pytestmark = pytest.mark.ENGINE


def candidate_origin(service, settings, snapshot, database):
    actor, adopted, run = accept(service, settings, snapshot)
    token = tokens(database, run["id"])
    service.start_run(run["id"], *token)
    result = controlled_result(snapshot)
    assert service.finish_run(run["id"], *token, result)
    return (
        actor,
        adopted,
        {
            "run_id": run["id"],
            "result_hash": result.result_hash,
            "candidate_key": result.candidates[0].candidate_key,
        },
    )


def create_case(service, settings, *, origin=None, key="case", is_demo=False):
    actor = DevelopmentAccess(settings).actor()
    request = InvestigationCreate(
        title="Synthetic investigation",
        question="Does the synthetic statement have contrary material?",
        questions=["Check the marked synthetic source"],
        origin=origin,
        is_demo=is_demo,
    )
    runtime = InvestigationService(service)
    _, view = runtime.create(actor, request, key)
    return runtime, actor, request, view


def append(runtime, actor, view, kind, key, **payload):
    body = InvestigationEntryCreate.model_validate(
        {
            "kind": kind,
            "expected_revision": view["revision"],
            **payload,
        }
    )
    return runtime.append(actor, view["id"], body, key)[1]


def material(adopted, snapshot, *, number=0, stance="support"):
    record = snapshot.records[number]
    return {
        "snapshot_id": adopted["id"],
        "record_ref": record.record_ref.model_dump(mode="json"),
        "quote": record.normalized_text[:30],
        "stance": stance,
        "text": "Synthetic researcher classification",
    }


def test_saved_investigation_binds_candidate_and_preserves_judgment_revisions(
    service,
    settings,
    snapshot,
    database,
):
    actor, adopted, origin = candidate_origin(service, settings, snapshot, database)
    with Session(database) as session:
        old_run = copy.deepcopy(session.get(EngineRun, origin["run_id"]).result)
    runtime, _, request, view = create_case(service, settings, origin=origin)
    assert view["origin"]["snapshot_id"] == adopted["id"]
    assert view["is_demo"] and view["entries"] == [] and view["revision"] == 1
    assert runtime.create(actor, request, "case")[1] == view
    assert view["retrieval_status"] == "not_connected" and view["professional_status"] == "not_reviewed"
    view = append(runtime, actor, view, "material", "support", **material(adopted, snapshot))
    first_material = view["entries"][-1]
    assert first_material["payload"]["support"]["quote_status"] == "normalized_only"
    assert first_material["created_by"] == actor.actor_id
    view = append(
        runtime,
        actor,
        view,
        "judgment",
        "judgment-1",
        text="Provisional synthetic judgment",
        conditions="Wait for professional assessment",
        assessment="provisional",
        basis_entry_ids=[first_material["id"]],
    )
    first_judgment = view["entries"][-1]
    assert first_judgment["payload"]["based_on_revision"] == 2
    assert first_judgment["payload"]["parent_judgment_entry_id"] is None
    assert not view["needs_review"]
    view = append(
        runtime, actor, view, "material", "opposing", **material(adopted, snapshot, number=1, stance="oppose")
    )
    assert view["needs_review"]
    second_material = view["entries"][-1]
    view = append(runtime, actor, view, "note", "note", text="New material requires a fresh judgment")
    assert view["needs_review"]
    view = append(
        runtime,
        actor,
        view,
        "judgment",
        "judgment-2",
        text="The synthetic conclusion is now uncertain",
        conditions="Need independent evidence",
        assessment="unable_to_determine",
        basis_entry_ids=[first_material["id"], second_material["id"]],
    )
    assert not view["needs_review"] and view["judgment_count"] == 2
    assert view["entries"][1] == first_judgment
    assert view["entries"][-1]["payload"]["parent_judgment_entry_id"] == first_judgment["id"]
    reopened = InvestigationService(EngineService(database, settings)).get(actor, view["id"])
    assert reopened.model_dump(mode="json") == view
    with Session(database) as session:
        assert session.get(EngineRun, origin["run_id"]).result == old_run
        assert session.scalar(select(func.count()).select_from(EngineRun)) == 1


def test_candidate_origin_wrong_hash_key_or_unfinished_run_cannot_create(
    service, settings, snapshot, database
):
    actor, _, origin = candidate_origin(service, settings, snapshot, database)
    runtime = InvestigationService(service)
    for field, value, code in (
        ("result_hash", "f" * 64, "result_conflict"),
        ("candidate_key", "absent", "not_found"),
    ):
        body = InvestigationCreate(
            title="Synthetic", question="Synthetic question", origin={**origin, field: value}
        )
        with pytest.raises(DomainError) as failure:
            runtime.create(actor, body, field)
        assert failure.value.code == code
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationRecord)) == 0


def test_direct_question_without_evidence_only_records_inability_and_fixture_cannot_be_washed(
    service,
    settings,
    snapshot,
):
    runtime, actor, _, view = create_case(service, settings)
    assert not view["is_demo"] and view["origin"] is None
    with pytest.raises(DomainError) as failure:
        append(
            runtime,
            actor,
            view,
            "judgment",
            "unbased",
            text="Unsupported judgment",
            assessment="provisional",
            basis_entry_ids=[],
        )
    assert failure.value.code == "judgment_basis_required"
    view = append(
        runtime,
        actor,
        view,
        "judgment",
        "unknown",
        text="There is no adopted evidence",
        assessment="unable_to_determine",
        basis_entry_ids=[],
    )
    _, adopted = service.adopt_snapshot(actor, snapshot, "direct-source")
    view = append(runtime, actor, view, "material", "demo-source", **material(adopted, snapshot))
    assert view["is_demo"] and view["needs_review"]


def test_omitting_known_opposing_material_from_new_judgment_keeps_review_pending(service, settings, snapshot):
    runtime, actor, _, view = create_case(service, settings)
    _, adopted = service.adopt_snapshot(actor, snapshot, "source")
    view = append(runtime, actor, view, "material", "support", **material(adopted, snapshot))
    support_id = view["entries"][-1]["id"]
    view = append(
        runtime,
        actor,
        view,
        "judgment",
        "first",
        text="Synthetic initial judgment",
        assessment="provisional",
        basis_entry_ids=[support_id],
    )
    view = append(
        runtime, actor, view, "material", "oppose", **material(adopted, snapshot, number=1, stance="oppose")
    )
    opposing_id = view["entries"][-1]["id"]
    view = append(
        runtime,
        actor,
        view,
        "judgment",
        "ignored",
        text="New words without adopting the opposing material",
        assessment="provisional",
        basis_entry_ids=[support_id],
    )
    assert view["needs_review"]
    view = append(
        runtime,
        actor,
        view,
        "judgment",
        "included",
        text="Explicitly considers both materials",
        assessment="unable_to_determine",
        basis_entry_ids=[support_id, opposing_id],
    )
    assert not view["needs_review"] and view["professional_status"] == "not_reviewed"


def non_demo_scope_fixture(snapshot):
    """Synthetic permission-boundary input, only in disposable test schemas."""
    records = []
    for record in snapshot.records:
        ref = record.record_ref.model_copy(update={"provider": "data_fabric:datafabric-test"})
        records.append(
            record.model_copy(
                update={
                    "source_instance": "datafabric-test",
                    "source_id": "clinicaltrials",
                    "record_ref": ref,
                    "evidence_refs": (ref,),
                    "ingestion_mode": "sample_export",
                }
            )
        )
    return snapshot.model_copy(
        update={
            "source_instance": "datafabric-test",
            "is_demo": False,
            "records": tuple(records),
            "coverage": tuple(
                coverage.model_copy(
                    update={"source_instance": "datafabric-test", "source_id": "clinicaltrials"}
                )
                for coverage in snapshot.coverage
            ),
        }
    )


def test_fixture_and_non_demo_source_cannot_mix_even_when_first_materials_are_concurrent(
    service,
    settings,
    snapshot,
):
    runtime, actor, _, view = create_case(service, settings)
    actual_scope = non_demo_scope_fixture(snapshot)
    _, demo_source = service.adopt_snapshot(actor, snapshot, "demo-source")
    _, non_demo_source = service.adopt_snapshot(actor, actual_scope, "non-demo-source")
    materials = [material(demo_source, snapshot), material(non_demo_source, actual_scope)]

    def add(index):
        try:
            return append(runtime, actor, view, "material", f"first-{index}", **materials[index])
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, (0, 1)))
    saved = [result for result in results if isinstance(result, dict)]
    assert len(saved) == 1 and "revision_conflict" in results
    latest = runtime.get(actor, view["id"]).model_dump(mode="json")
    other = 1 if isinstance(results[0], dict) else 0
    with pytest.raises(DomainError) as mixed:
        append(runtime, actor, latest, "material", "mixed-retry", **materials[other])
    assert mixed.value.code == "fixture_scope_mismatch"
    assert runtime.get(actor, view["id"]).material_count == 1
    # A teaching label cannot conceal already bound source provenance.
    _, _, _, labeled = create_case(service, settings, key="marked", is_demo=True)
    labeled = append(
        runtime, actor, labeled, "material", "real-first", **material(non_demo_source, actual_scope)
    )
    assert labeled["is_demo"]
    with pytest.raises(DomainError) as mixed:
        append(runtime, actor, labeled, "material", "fixture-second", **material(demo_source, snapshot))
    assert mixed.value.code == "fixture_scope_mismatch"


def test_material_full_reference_and_literal_quote_are_checked_once_on_adoption(service, settings, snapshot):
    runtime, actor, _, view = create_case(service, settings)
    _, adopted = service.adopt_snapshot(actor, snapshot, "source")
    valid = material(adopted, snapshot)
    for field in ("adopted_version", "authorization_ref", "content_hash", "object_id"):
        invalid = copy.deepcopy(valid)
        invalid["record_ref"][field] = "wrong-reference"
        with pytest.raises(DomainError) as failure:
            append(runtime, actor, view, "material", field, **invalid)
        assert failure.value.code == "material_source_mismatch"
    with pytest.raises(DomainError) as failure:
        append(
            runtime, actor, view, "material", "bad-quote", **{**valid, "quote": "Invented source statement"}
        )
    assert failure.value.code == "material_quote_mismatch"
    assert runtime.get(actor, view["id"]).revision == 1
    view = append(
        runtime, actor, view, "material", "no-quote", **{**valid, "quote": None, "stance": "uncertain"}
    )
    support = view["entries"][-1]["payload"]["support"]
    assert support["quote_status"] == "unavailable" and support["normalized_start"] is None
    assert support["locator"] == snapshot.records[0].locator.model_dump(mode="json")


def test_judgment_basis_cannot_use_other_investigation_note_or_duplicate_material_ids(
    service, settings, snapshot
):
    runtime, actor, _, view = create_case(service, settings)
    _, adopted = service.adopt_snapshot(actor, snapshot, "source")
    view = append(runtime, actor, view, "material", "material", **material(adopted, snapshot))
    own_id = view["entries"][-1]["id"]
    view = append(runtime, actor, view, "note", "note", text="A note is not source material")
    note_id = view["entries"][-1]["id"]
    _, _, _, other = create_case(service, settings, key="other")
    other = append(runtime, actor, other, "material", "other-material", **material(adopted, snapshot))
    for basis in ([note_id], [other["entries"][-1]["id"]], [own_id, own_id], [str(uuid4())]):
        with pytest.raises(DomainError) as failure:
            append(
                runtime,
                actor,
                view,
                "judgment",
                str(uuid4()),
                text="Invalid basis",
                assessment="provisional",
                basis_entry_ids=basis,
            )
        assert failure.value.code == "judgment_basis_invalid"
    assert runtime.get(actor, view["id"]).revision == view["revision"]


def test_progress_closing_and_reopening_are_manual_and_history_is_retained(service, settings):
    runtime, actor, _, view = create_case(service, settings)
    qid = view["questions"][0]["question_id"]
    with pytest.raises(DomainError) as invalid:
        append(
            runtime,
            actor,
            view,
            "status",
            "wrong-question",
            status="paused",
            text="Pause",
            question_updates=[{"question_id": str(uuid4()), "status": "blocked"}],
        )
    assert invalid.value.code == "question_update_invalid"
    view = append(
        runtime,
        actor,
        view,
        "status",
        "pause",
        status="paused",
        text="Waiting for materials",
        question_updates=[{"question_id": qid, "status": "blocked"}],
    )
    assert view["status"] == "paused" and view["questions"][0]["status"] == "blocked"
    view = append(
        runtime,
        actor,
        view,
        "status",
        "close",
        status="closed",
        text="Researcher stopped",
        question_updates=[],
    )
    with pytest.raises(DomainError) as closed:
        append(runtime, actor, view, "note", "closed-note", text="Cannot append while closed")
    assert closed.value.code == "investigation_closed"
    view = append(
        runtime,
        actor,
        view,
        "status",
        "reopen",
        status="open",
        text="Resume work",
        question_updates=[{"question_id": qid, "status": "checking"}],
    )
    assert view["questions"][0]["status"] == "checking"
    assert [entry["payload"]["status"] for entry in view["entries"]] == ["paused", "closed", "open"]
    assert view["professional_status"] == "not_reviewed"


def test_concurrent_revisions_and_idempotent_retries_never_duplicate_or_overwrite(
    service, settings, database
):
    runtime, actor, _, view = create_case(service, settings)
    body = InvestigationEntryCreate.model_validate(
        {"expected_revision": 1, "kind": "note", "text": "Saved once"}
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.append(actor, view["id"], body, "same-key"), range(2)))
    assert results[0] == results[1]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationEntryRecord)) == 1
    with pytest.raises(DomainError) as stale:
        runtime.append(actor, view["id"], body, "new-key-stale")
    assert stale.value.code == "revision_conflict"
    conflicting = InvestigationEntryCreate.model_validate(
        {"expected_revision": 1, "kind": "note", "text": "Different"}
    )
    with pytest.raises(DomainError) as idem:
        runtime.append(actor, view["id"], conflicting, "same-key")
    assert idem.value.code == "idempotency_conflict"
    latest = runtime.get(actor, view["id"]).model_dump(mode="json")
    next_body = InvestigationEntryCreate.model_validate(
        {"expected_revision": latest["revision"], "kind": "note", "text": "Next"}
    )

    def save(key):
        try:
            return runtime.append(actor, view["id"], next_body, key)[0]
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ("concurrent-a", "concurrent-b")))
    assert sorted(map(str, outcomes)) == ["201", "revision_conflict"]
    assert runtime.get(actor, view["id"]).revision == 3


def test_current_source_revocation_blocks_details_list_and_both_write_replays(
    service, settings, snapshot, database
):
    runtime, actor, create_body, view = create_case(service, settings)
    _, adopted = service.adopt_snapshot(actor, snapshot, "source")
    entry_body = InvestigationEntryCreate.model_validate(
        {"expected_revision": 1, "kind": "material", **material(adopted, snapshot)}
    )
    _, view = runtime.append(actor, view["id"], entry_body, "entry-key")
    restricted = settings.model_copy(update={"engine_source_id_allowlist": {"fixture": []}})
    reader = InvestigationService(EngineService(database, restricted))
    current_actor = DevelopmentAccess(restricted).actor()
    for action in (
        lambda: reader.get(current_actor, view["id"]),
        lambda: reader.create(current_actor, create_body, "case"),
        lambda: reader.append(current_actor, view["id"], entry_body, "entry-key"),
    ):
        with pytest.raises(DomainError) as blocked:
            action()
        assert blocked.value.status_code == 404
    assert reader.list(current_actor).items == []
    assert runtime.get(actor, view["id"]).material_count == 1


def test_investigation_pagination_returns_metadata_and_downgrade_refuses_history(service, settings, database):
    runtime, actor, _, _ = create_case(service, settings)
    create_case(service, settings, key="second")
    first = runtime.list(actor, limit=1)
    second = runtime.list(actor, first.next_cursor, limit=1)
    assert len(first.items) == len(second.items) == 1 and first.items[0].id != second.items[0].id
    assert "entries" not in first.items[0].model_dump() and "origin" not in first.items[0].model_dump()
    spec = importlib.util.spec_from_file_location(
        "investigation_migration", ROOT / "db/migrations/versions/0004_investigations.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with database.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        with pytest.raises(RuntimeError, match="Cannot discard"):
            migration.downgrade()
    assert runtime.list(actor).items


def test_http_investigation_security_validation_and_frozen_snapshot_comparison(
    service, settings, snapshot, database
):
    app = create_app(settings)
    app.state.database = database
    headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "http-create"}
    body = {"title": "Synthetic HTTP record", "question": "Can the record be reopened?", "questions": []}
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        app.state.database = database
        assert client.get("/api/engine/investigations").status_code == 401
        response = client.post("/api/engine/investigations", headers=headers, json=body)
        assert response.status_code == 201
        view = response.json()
        note = {"expected_revision": 1, "kind": "note", "text": "Saved over HTTP"}
        response = client.post(
            f"/api/engine/investigations/{view['id']}/entries",
            json=note,
            headers={**headers, "Idempotency-Key": "http-note"},
        )
        assert response.status_code == 201 and response.json()["revision"] == 2
        assert (
            client.get(f"/api/engine/investigations/{view['id']}", headers=headers).json() == response.json()
        )
        invalid = client.post(
            "/api/engine/investigations",
            headers={**headers, "Idempotency-Key": "forged"},
            json={**body, "created_by": "fake-expert"},
        )
        assert invalid.status_code == 422 and "fake-expert" not in invalid.text
        too_large = client.post("/api/engine/investigations", headers=headers, content=b" " * 64001)
        assert too_large.status_code == 422 and too_large.json()["error"]["code"] == "engine_budget_exceeded"
        actor = DevelopmentAccess(settings).actor()
        _, before = service.adopt_snapshot(actor, snapshot, "before")
        after_snapshot = snapshot.model_copy(update={"records": snapshot.records[:2]})
        _, after = service.adopt_snapshot(actor, after_snapshot, "after")
        comparison = client.get(
            "/api/engine/snapshot-comparisons",
            headers=headers,
            params={"before_id": before["id"], "after_id": after["id"]},
        )
        assert comparison.status_code == 200
        assert comparison.json()["counts"] == {"added": 0, "content_updated": 0, "same": 2, "not_seen": 1}
        assert "normalized_text" not in comparison.text and "read_failed" not in comparison.text
        assert (
            client.get(
                "/api/engine/snapshot-comparisons",
                headers=headers,
                params={"before_id": before["id"], "after_id": str(uuid4())},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/engine/session",
                json={"credential": CREDENTIAL},
                headers={"Origin": "http://localhost:15176"},
            ).status_code
            == 200
        )
        denied = client.post(
            f"/api/engine/investigations/{view['id']}/entries",
            json=note,
            headers={"Idempotency-Key": "missing-origin"},
        )
        assert denied.status_code == 403


def test_record_and_entry_budget_fail_without_partial_saves(service, settings, database, monkeypatch):
    import bioradar.research.investigations.service as module

    runtime, actor, _, view = create_case(service, settings)
    monkeypatch.setattr(module, "MAX_VIEW_BYTES", 10)
    with pytest.raises(DomainError) as failed:
        append(runtime, actor, view, "note", "over-size", text="Cannot fit")
    assert failed.value.code == "investigation_budget_exceeded"
    monkeypatch.setattr(module, "MAX_VIEW_BYTES", 2_000_000)
    monkeypatch.setattr(module, "MAX_ENTRIES", 0)
    with pytest.raises(DomainError) as failed:
        append(runtime, actor, view, "note", "over-count", text="Cannot add")
    assert failed.value.code == "investigation_budget_exceeded"
    assert runtime.get(actor, view["id"]).revision == 1
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationEntryRecord)) == 0
