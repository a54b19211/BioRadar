"""Explicit synthetic source changes, real PostgreSQL state and real mining functions."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import change_page, project_record
from bioradar.api.app import create_app
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.models import EngineIdempotency, EngineRun
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.runner import run_mining
from bioradar.research.monitor.models import MonitorBatch, MonitorControl, MonitorLead, MonitorState
from bioradar.research.monitor.service import MonitorService, fair_window
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import CREDENTIAL, GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_graph import original_record

pytestmark = pytest.mark.ENGINE


def record(number, summary, *, status="RECRUITING", seq=1, day="2026-10-10"):
    value = original_record(
        "NCT" + str(number).zfill(8),
        {
            "trial_id": "NCT" + str(number).zfill(8),
            "status": status,
            "phase": ["PHASE1"],
            "summary": summary,
            "body": summary,
            "interventions": [{"type": "DRUG", "name": "SYNTHETIC-" + str(number)}],
        },
        kind="trial",
        demo=True,
    )
    value["published_seq"] = seq
    value["source_updated_at"] = {"value": day, "precision": "day", "meaning": "registry_update_posted"}
    return value


class Source:
    def __init__(self):
        self.records = []
        self.calls = []
        self.fail_sources = set()
        self.enabled = {"fixture-source"}

    def handler(self, request):
        path, params = request.url.path, request.url.params
        self.calls.append((path, list(params.multi_items())))
        if path == "/api/history/sources":
            return httpx.Response(
                200, json={"items": [{"source_id": s, "read_enabled": True} for s in self.enabled]}
            )
        source = params.get("source_id")
        if source in self.fail_sources:
            return httpx.Response(503, json={"private": "SECRET-MUST-NOT-APPEAR"})
        values = [r for r in self.records if r["source_id"] == source]
        maximum = max((r["published_seq"] for r in values), default=0)
        if path == "/api/history/changes":
            after = int(params["after"])
            cursor = params.get("cursor")
            last, high = map(int, cursor.split(":")) if cursor else (after, maximum)
            limit = int(params["limit"])
            selected = [r for r in values if last < r["published_seq"] <= high][:limit]
            more = (
                any(
                    r["published_seq"] > selected[-1]["published_seq"] and r["published_seq"] <= high
                    for r in values
                )
                if selected
                else False
            )
            heads = {}
            for r in selected:
                candidates = [
                    v
                    for v in values
                    if v["source_record_id"] == r["source_record_id"] and v["published_seq"] <= high
                ]
                heads[r["source_record_id"]] = max(candidates, key=lambda v: v["published_seq"])["version_id"]
            end = selected[-1]["published_seq"] if more else high
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.history.changes.v1",
                    "items": selected,
                    "max_seq": high,
                    "next_after": end,
                    "next_cursor": str(end) + ":" + str(high) if more else None,
                    "current_version_ids": heads,
                    "scope": "committed_adoptions_not_source_change_feed",
                },
            )
        if path.startswith("/api/history/versions/"):
            value = next((r for r in values if r["version_id"] == path.rsplit("/", 1)[1]), None)
            return httpx.Response(200, json=value) if value else httpx.Response(404)
        if path == "/api/history/records":
            term = params.get("terms", "").casefold()
            heads = {}
            for r in values:
                heads[r["source_record_id"]] = r
            found = [
                r
                for r in heads.values()
                if term in (r["title"] + " " + r["clean_text"] + " " + r["source_record_id"]).casefold()
            ]
            return httpx.Response(
                200,
                json={
                    "schema_version": "datafabric.history.collection.v1",
                    "items": found[:3],
                    "next_cursor": "more" if len(found) > 3 else None,
                    "coverage": {"complete": False, "scope_description": "SYNTHETIC adopted records"},
                    "excluded_counts": {},
                },
            )
        return httpx.Response(404)


@pytest.fixture
def runtime(database):
    for name in ("0006_monitor.py", "0007_topics.py"):
        path = Path(__file__).resolve().parents[1] / "db/migrations/versions" / name
        spec = importlib.util.spec_from_file_location("monitor_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with database.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
    source = Source()
    settings = Settings(
        environment="test",
        engine_dev_credential=CREDENTIAL,
        database_url=None,
        redis_url=None,
        monitor_enabled=True,
        monitor_is_demo=True,
        monitor_batch_records=3,
        monitor_context_records=100,
        monitor_investigations_per_batch=2,
    )
    client = httpx.Client(base_url="http://127.0.0.1:18511", transport=httpx.MockTransport(source.handler))

    def make():
        reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90)
        engine = EngineService(database, settings)
        engine.history_source_reader = reader.sources
        return MonitorService(engine, reader)

    yield make, source, settings
    client.close()


def finish_pending(runtime):
    monitor = runtime()
    with Session(monitor.engine.database) as session:
        rows = list(session.scalars(select(EngineRun).where(EngineRun.state == "queued")))
        fixed = [(r.id, r.attempt, r.accept_token, r.input_hash) for r in rows]
    for args in fixed:
        value = monitor.engine.start_run(*args)
        if value is not None:
            monitor.engine.finish_run(*args, run_mining(*value))


def seeded(source):
    source.records = [
        record(1, "SYNTHETIC adaptive immune vesicle treatment with transient activation.", seq=1),
        record(2, "SYNTHETIC adaptive immune vesicle delivery with persistent regulation.", seq=2),
        record(3, "SYNTHETIC unrelated boron imaging method for a different observation.", seq=3),
    ]


def test_unattended_two_batches_persist_queries_and_restart_without_duplicate(runtime):
    make, source, _ = runtime
    seeded(source)
    make().tick()  # No API/CLI run or investigation request.
    finish_pending(make)  # Real existing start/run_mining/finish functions.
    make().tick()
    with Session(make().engine.database) as session:
        assert session.scalar(select(func.count()).select_from(MonitorLead)) > 0
        leads = list(session.scalars(select(MonitorLead)))
        assert any(lead.investigation_id for lead in leads)
        assert session.scalar(select(MonitorBatch.state)) == "completed"
    assert any(path == "/api/history/records" for path, _ in source.calls)
    view = InvestigationService(make().engine).get(
        make().actor, next(lead.investigation_id for lead in leads if lead.investigation_id)
    )
    selected = next(lead for lead in leads if lead.investigation_id == view.id)
    candidate = selected.payload["candidate"]
    assert view.question == (
        f"待调查线索（程序候选，尚未核实）：{candidate['title'][:200]}\n"
        f"调查问题：{candidate['next_questions'][0]}"
    )
    assert not any(e.kind == "judgment" for e in view.entries)
    assert all(e.payload.automation is not None for e in view.entries if e.kind in {"note", "material"})
    make().tick()  # New coordinator instance simulates a clean restart.
    assert InvestigationService(make().engine).get(make().actor, view.id).revision == view.revision
    with Session(make().engine.database) as session:
        assert session.scalar(select(func.count()).select_from(MonitorBatch)) == 1
    revised = record(1, source.records[0]["facts"]["summary"], status="TERMINATED", seq=4, day="2026-10-17")
    source.records.append(revised)
    make().tick()
    finish_pending(make)
    make().tick()
    with Session(make().engine.database) as session:
        batches = list(session.scalars(select(MonitorBatch).order_by(MonitorBatch.created_at)))
        assert len(batches) == 2 and all(b.state == "completed" for b in batches)
        assert any(
            c["method_id"] == "state_transitions"
            for b in batches
            for c in session.get(EngineRun, b.run_id).result["candidates"]
        )
        p = session.get(MonitorState, make().actor.workspace_id).source_progress
        assert next(iter(p.values()))["acked_after"] == 4


def test_pause_cas_and_heartbeat_revision_are_independent(runtime):
    make, source, _ = runtime
    monitor = make()
    actor = monitor.actor
    monitor.control(actor, MonitorControl(expected_revision=1, paused=True), "pause")
    assert make().tick()["state"] == "paused_or_busy" and not source.calls
    monitor.control(actor, MonitorControl(expected_revision=1, paused=True), "pause")
    with pytest.raises(DomainError, match="刷新"):
        monitor.control(actor, MonitorControl(expected_revision=1, paused=False), "stale")
    monitor.control(actor, MonitorControl(expected_revision=2, paused=False), "resume")
    make().tick()
    assert make().status(actor).control_revision == 3


def test_global_hourly_budget_and_same_lead_cooldown_survive_restart(runtime):
    make, _, settings = runtime
    settings.monitor_hourly_investigations = 1
    first = make()
    assert first.claim()
    assert first.reserve_investigation("a" * 64, "first", "routine")
    first.release()
    second = make()
    assert second.claim()
    assert not second.reserve_investigation("b" * 64, "second", "routine")
    assert not second.reserve_investigation("a" * 64, "new-version", "routine")
    # Recovery of the same durable attempt does not reserve a second slot.
    assert second.reserve_investigation("a" * 64, "first", "routine")
    second.release()
    with Session(first.engine.database) as session, session.begin():
        rows = list(
            session.scalars(
                select(EngineIdempotency).where(EngineIdempotency.operation == "monitor.dig.start")
            )
        )
        rows[0].created_at = datetime.now(UTC) - timedelta(hours=2)
    third = make()
    assert third.claim()
    assert third.reserve_investigation("b" * 64, "second", "routine")
    third.release()


def test_budget_deferred_leads_do_not_hold_source_cursor(runtime):
    make, source, settings = runtime
    settings.monitor_hourly_investigations = 0
    seeded(source)
    make().tick()
    finish_pending(make)
    make().tick()
    with Session(make().engine.database) as session:
        leads = list(session.scalars(select(MonitorLead)))
        assert leads and all(lead.state == "deferred" for lead in leads)
        assert not any(lead.investigation_id for lead in leads)
        assert session.scalar(select(MonitorBatch.state)) == "completed"
    settings.monitor_hourly_investigations = 1
    make().tick()
    with Session(make().engine.database) as session:
        assert any(lead.investigation_id for lead in session.scalars(select(MonitorLead)))


def test_failed_source_rotates_and_frozen_cursor_does_not_advance(runtime):
    make, source, settings = runtime
    settings.engine_source_id_allowlist = {"fixture": ["fixture-source", "other-source"]}
    source.enabled.add("other-source")
    source.fail_sources.add("fixture-source")
    monitor = make()
    assert monitor.tick()["state"] == "source_unavailable"
    other = record(1, "SYNTHETIC other source", seq=1)
    other["source_id"] = "other-source"
    source.records.append(other)
    assert make().tick()["state"] == "polled"
    with Session(monitor.engine.database) as session:
        p = session.get(MonitorState, monitor.actor.workspace_id).source_progress
        assert p[monitor.stream_key("fixture-source")].get("acked_after", 0) == 0
        assert p[monitor.stream_key("other-source")].get("active_batch_id")
    assert "SECRET" not in str(make().status(monitor.actor))


def test_fixed_pages_keep_initial_after_and_watermark(runtime):
    make, source, settings = runtime
    settings.monitor_batch_records = 1
    seeded(source)
    for _ in range(3):
        make().tick()
        finish_pending(make)
        make().tick()
    requests = [dict(p) for path, p in source.calls if path == "/api/history/changes"]
    assert requests[0]["after"] == "0" and all(r["limit"] == "1" for r in requests[:3])
    assert requests[1]["after"] == "0" and requests[1]["cursor"] == "1:3"
    with Session(make().engine.database) as session:
        assert len(list(session.scalars(select(MonitorBatch)))) == 3


def test_native_trial_fields_are_typed_but_not_a_drug_program(runtime):
    make, _, _ = runtime
    item = record(1, "SYNTHETIC natural language immune response, not JSON keys.", seq=1)
    item["clean_text"] = item["raw_content"]
    output = project_record(make().reader, item, as_of=datetime.now(UTC), bootstrap=True)
    assert output.normalized_text == item["facts"]["summary"]
    assert output.state_assertions and output.canonical_entity_refs[0].entity_type == "registered_trial"
    assert output.project_ref is None and output.company_refs == ()
    assert "historical_backfill" in output.record_flags
    assert output.publication_time_verified is False


def test_automation_is_server_only_and_cannot_sign_judgments(runtime):
    make, _, settings = runtime
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = make().engine.database
        response = client.post(
            "/api/engine/investigations/x/entries",
            headers={"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "forged"},
            json={
                "kind": "note",
                "expected_revision": 1,
                "text": "forged",
                "automation": {"source": "monitor", "batch_id": "x", "lead_key": "a" * 64},
            },
        )
        assert response.status_code == 422
    assert "monitor" not in json.dumps(response.json()).lower()


def test_read_failure_is_not_empty_and_bad_head_cannot_be_acknowledged(runtime):
    make, source, _ = runtime
    seeded(source)
    original = source.handler

    def bad(request):
        response = original(request)
        if request.url.path.endswith("/changes"):
            value = response.json()
            value["current_version_ids"] = {}
            return httpx.Response(200, json=value)
        return response

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(bad)) as client:
        reader = FabricHistoryReader(client, authorization_ref=GRANT)
        with pytest.raises(DomainError) as error:
            change_page(reader, source_id="fixture-source", is_demo=True, after=0)
    assert error.value.code == "history_source_invalid"


def test_fair_window_never_promotes_source_expansion_to_full_coverage(runtime):
    make, _, _ = runtime
    records = [
        project_record(make().reader, record(i, "SYNTHETIC content " + str(i)), as_of=datetime.now(UTC))
        for i in range(1, 5)
    ]
    selected, omitted = fair_window(
        {
            "big": {"context": [r.model_dump(mode="json") for r in records[:3]]},
            "small": {"context": [records[3].model_dump(mode="json")]},
        },
        2,
    )
    assert len(selected) == 2 and omitted == 2
    assert records[3] in selected


def test_cross_source_lead_and_batch_cannot_leak_revoked_support(runtime):
    make, source, settings = runtime
    settings.monitor_hourly_investigations = 0
    settings.engine_source_id_allowlist = {"fixture": ["fixture-source", "other-source"]}
    source.enabled.add("other-source")
    seeded(source)
    extra = record(7, "SYNTHETIC adaptive immune vesicle combination and persistent regulation.", seq=1)
    extra["source_id"] = "other-source"
    source.records.append(extra)
    for _ in range(4):
        make().tick()
        finish_pending(make)
    monitor = make()
    assert any(b.source_id == "other-source" for b in monitor.batches(monitor.actor).items)
    assert monitor.leads(monitor.actor).items
    source.enabled.remove("fixture-source")
    revoked = make()
    batches, leads = revoked.batches(revoked.actor), revoked.leads(revoked.actor)
    assert batches.items == [] and batches.unavailable_count >= 2
    assert leads.items == [] and leads.unavailable_count > 0


def test_adopt_committed_without_reply_resumes_frozen_query_not_a_new_search(runtime):
    make, source, settings = runtime
    settings.monitor_hourly_investigations = 1
    seeded(source)
    make().tick()
    finish_pending(make)
    monitor = make()
    original = monitor.engine.adopt_snapshot
    interrupted = []

    def committed_unknown(actor, snapshot, key):
        response = original(actor, snapshot, key)
        if key.startswith("monitor-followup:") and not interrupted:
            interrupted.append(key)
            raise RuntimeError("SYNTHETIC local commit reply lost")
        return response

    monitor.engine.adopt_snapshot = committed_unknown
    with pytest.raises(RuntimeError, match="reply lost"):
        monitor.tick()
    with Session(monitor.engine.database) as session:
        lead = session.scalar(select(MonitorLead).where(MonitorLead.state == "investigating"))
        identifier, before = lead.id, json.loads(json.dumps(lead.payload))
    term = before["plan"][0]["term"]
    searches_before = sum(
        path == "/api/history/records" and dict(params).get("terms") == term for path, params in source.calls
    )
    frozen_version = before["query_intents"][0]["snapshot"]["records"][0]["record_ref"]["adopted_version"]
    source.records.append(record(1, "SYNTHETIC changed after the uncertain write.", seq=4))
    make().tick()
    with Session(monitor.engine.database) as session:
        after = session.get(MonitorLead, identifier).payload
    assert after["query_intents"][0] == before["query_intents"][0]
    assert after["queries"][0]["records"][0]["record_ref"]["adopted_version"] == frozen_version
    assert (
        sum(
            path == "/api/history/records" and dict(params).get("terms") == term
            for path, params in source.calls
        )
        == searches_before
    )
    assert after["queries"][0]["snapshot_id"]


def test_closed_first_deferred_lead_does_not_starve_next_or_spend_quota(runtime):
    from bioradar.research.investigations.models import InvestigationCreate, InvestigationEntryCreate

    make, source, settings = runtime
    settings.monitor_hourly_investigations = 0
    seeded(source)
    make().tick()
    finish_pending(make)
    make().tick()
    monitor = make()
    runtime_service = InvestigationService(monitor.engine)
    _, closed = runtime_service.create(
        monitor.actor,
        InvestigationCreate(title="SYNTHETIC closed", question="SYNTHETIC", is_demo=True),
        "closed-investigation",
    )
    runtime_service.append(
        monitor.actor,
        closed["id"],
        InvestigationEntryCreate.model_validate(
            {"kind": "status", "expected_revision": 1, "status": "closed", "text": "用户关闭"}
        ),
        "close",
    )
    with Session(monitor.engine.database) as session, session.begin():
        leads = list(session.scalars(select(MonitorLead).order_by(MonitorLead.id)))
        assert len(leads) >= 2
        first, second = leads[:2]
        first.investigation_id = closed["id"]
        first.updated_at = datetime.now(UTC) - timedelta(hours=2)
        second.updated_at = datetime.now(UTC) - timedelta(hours=1)
        next_id = second.id
    settings.monitor_hourly_investigations = 1
    make().tick()
    with Session(monitor.engine.database) as session:
        assert session.get(MonitorLead, next_id).investigation_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(EngineIdempotency)
                .where(EngineIdempotency.operation == "monitor.dig.start")
            )
            == 1
        )
    assert runtime_service.get(monitor.actor, closed["id"]).revision == 2


def test_operator_retry_keeps_same_partial_failure_input_and_persistent_pause(runtime, monkeypatch):
    from bioradar.research.mining import runner

    make, source, _ = runtime
    seeded(source)
    make().tick()
    monitor = make()
    with Session(monitor.engine.database) as session:
        batch = session.scalar(select(MonitorBatch))
        identifier, snapshot_id, run_id = batch.id, batch.snapshot_id, batch.run_id
        run = session.get(EngineRun, run_id)
        args, initial_hash = (run.id, run.attempt, run.accept_token, run.input_hash), run.input_hash
    inputs = monitor.engine.start_run(*args)
    actual = runner.IMPLEMENTATIONS["text_topics"]

    def fail_one_method(prepared):
        raise RuntimeError("SYNTHETIC method interruption")

    monkeypatch.setitem(runner.IMPLEMENTATIONS, "text_topics", fail_one_method)
    assert monitor.engine.finish_run(*args, runner.run_mining(*inputs))
    monkeypatch.setitem(runner.IMPLEMENTATIONS, "text_topics", actual)
    make().tick()
    with Session(monitor.engine.database) as session:
        assert session.get(MonitorBatch, identifier).state == "blocked"
    monitor.control(monitor.actor, MonitorControl(expected_revision=1, paused=True), "retry-paused")
    repaired = make().retry_batch(monitor.actor, identifier)
    assert repaired["same_fixed_input"]
    replay = make().retry_batch(monitor.actor, identifier)
    assert replay["already_requested"]
    with Session(monitor.engine.database) as session:
        row = session.get(EngineRun, run_id)
        assert row.attempt == 2 and row.snapshot_id == snapshot_id and row.input_hash == initial_hash
        assert row.attempt_diagnostics[0]["method_results"]
        assert session.get(MonitorState, monitor.actor.workspace_id).paused
    finish_pending(make)
    assert make().tick()["state"] == "paused_or_busy"


def test_source_version_without_new_candidate_still_flags_old_judgment_for_review(runtime):
    from bioradar.research.investigations.models import InvestigationCreate, InvestigationEntryCreate
    from bioradar.research.investigations.service import InvestigationEntryRecord
    from bioradar.research.mining.models import ProviderRef

    make, source, settings = runtime
    settings.monitor_hourly_investigations = 0
    source.records = [record(1, "SYNTHETIC isolated material for a human judgment.", seq=1)]
    make().tick()
    finish_pending(make)
    make().tick()
    monitor = make()
    runtime_service = InvestigationService(monitor.engine)
    with Session(monitor.engine.database) as session:
        old = session.scalar(select(MonitorBatch))
        old_snapshot, old_ref = old.snapshot_id, old.payload["snapshot"]["records"][0]["record_ref"]
    _, view = runtime_service.create(
        monitor.actor,
        InvestigationCreate(title="SYNTHETIC human review", question="SYNTHETIC", is_demo=True),
        "human-review",
    )
    _, view = runtime_service.append(
        monitor.actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "kind": "material",
                "expected_revision": view["revision"],
                "snapshot_id": old_snapshot,
                "record_ref": old_ref,
                "stance": "context",
                "text": "原有背景",
            }
        ),
        "human-material",
    )
    material_id = view["entries"][0]["id"]
    _, view = runtime_service.append(
        monitor.actor,
        view["id"],
        InvestigationEntryCreate.model_validate(
            {
                "kind": "judgment",
                "expected_revision": view["revision"],
                "text": "明确合成的有条件判断",
                "assessment": "provisional",
                "basis_entry_ids": [material_id],
            }
        ),
        "human-judgment",
    )
    assert not view["needs_review"]
    withdrawn = original_record("NCT00000001", {"body": "", "publication_state": "withdrawn"}, demo=True)
    withdrawn["published_seq"] = 2
    source.records.append(withdrawn)
    make().tick()
    finish_pending(make)
    make().tick()
    after = runtime_service.get(monitor.actor, view["id"])
    assert after.needs_review and after.judgment_count == 1
    assert after.latest_judgment_entry_id == view["latest_judgment_entry_id"]
    new_materials = [e for e in after.entries if e.kind == "material" and e.id != material_id]
    assert len(new_materials) == 1 and new_materials[0].payload.automation is not None
    assert new_materials[0].payload.support.record_ref.adopted_version == withdrawn["version_id"]
    with Session(monitor.engine.database) as session:
        latest = session.scalar(select(MonitorBatch).order_by(MonitorBatch.created_at.desc()))
        assert latest.state == "completed"
        assert latest.payload["selected_leads"] == []
        snapshot = latest.payload["snapshot"]
        native = ProviderRef.model_validate(old_ref).identity_key
        assert all(
            ProviderRef.model_validate(r["record_ref"]).identity_key != native
            or "deleted" in r["record_flags"]
            for r in snapshot["records"]
        )
        count = session.scalar(select(func.count()).select_from(InvestigationEntryRecord))
    make().tick()
    with Session(monitor.engine.database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationEntryRecord)) == count


def test_empty_current_graph_removes_old_relations_and_unreferenced_graph_documents(runtime):
    from bioradar.research.mining.models import MiningPolicy, NormalizedSnapshot
    from test_graph import bundle
    from test_graph import source as graph_source

    make, source, settings = runtime
    settings.engine_source_id_allowlist = {"fixture": ["fixture-source", "reviewed_graph"]}
    source.enabled.add("reviewed_graph")
    graph, doc = bundle()
    graph_reader, current, _ = graph_source(graph, doc)
    source.records = [doc, current]
    original_handler = source.handler

    def handle(request):
        if "/api/history/graph/" in request.url.path:
            return graph_reader.client.send(request)
        return original_handler(request)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)) as client:
        reader = FabricHistoryReader(client, authorization_ref=GRANT)
        engine = EngineService(make().engine.database, settings)
        engine.history_source_reader = reader.sources
        monitor = MonitorService(engine, reader)
        assert monitor.claim()
        first_id = monitor.accept_page("reviewed_graph")
        _, _, _, first, _, _ = monitor._load_batch(first_id)
        assert any(r["relations"] for r in first["snapshot"]["records"])
        monitor._complete(first_id, first)
        monitor.release()
        empty = graph.model_copy(update={"nodes": (), "edges": (), "document_refs": ()})
        replacement = original_record(
            current["source_record_id"], empty.model_dump(mode="json"), source="reviewed_graph", kind="graph"
        )
        replacement["published_seq"] = 2
        source.records.append(replacement)
        next_monitor = MonitorService(EngineService(engine.database, settings), reader)
        next_monitor.engine.history_source_reader = reader.sources
        assert next_monitor.claim()
        next_id = next_monitor.accept_page("reviewed_graph")
        _, _, _, after, _, _ = next_monitor._load_batch(next_id)
        snapshot = NormalizedSnapshot.model_validate(after["snapshot"])
        assert all(not r.relations for r in snapshot.records)
        assert all(r.record_ref.adopted_version != current["version_id"] for r in snapshot.records)
        assert all(r.record_ref.adopted_version != doc["version_id"] for r in snapshot.records)
        result = run_mining(snapshot, MiningPolicy(as_of=datetime.now(UTC), method_ids=("research_network",)))
        assert not result.candidates
        next_monitor.release()
    graph_reader.client.close()


def test_long_document_budget_omits_only_feature_window_and_keeps_consuming(runtime):
    make, source, settings = runtime
    settings.engine_max_snapshot_bytes = 32_000
    source.records = [record(1, "SYNTHETIC long prose " * 10_000, seq=1)]
    make().tick()
    with Session(make().engine.database) as session:
        batch = session.scalar(select(MonitorBatch))
        assert batch.state == "completed" and batch.payload["snapshot"] is None
        assert batch.payload["manifest"][0]["version_id"] == source.records[0]["version_id"]
        summary = batch.payload["selection_summary"]
        assert summary["new_records"] == summary["unknown"] == 1
        assert summary["evaluated"] == summary["selected"] == summary["no_candidate"] == 0
        assert summary["record_outcomes"][0]["reason"] == "not_in_frozen_computation_window"
        assert (
            "whole_records_omitted_from_bounded_feature_window_versions_retained_at_source"
            in (batch.payload["limitations"])
        )
        progress = session.get(MonitorState, make().actor.workspace_id).source_progress
        assert next(iter(progress.values()))["acked_after"] == 1
    source.records.append(record(2, "SYNTHETIC short unrelated observation.", seq=2))
    make().tick()
    with Session(make().engine.database) as session:
        next_batch = session.scalar(select(MonitorBatch).order_by(MonitorBatch.created_at.desc()))
        assert next_batch.state == "queued"
        assert next_batch.payload["snapshot"]["records"]


def test_recovered_investigation_revision_conflict_blocks_its_actual_batch_and_is_repairable(
    runtime, monkeypatch
):
    from bioradar.research.investigations.models import InvestigationEntryCreate

    make, source, _ = runtime
    seeded(source)
    make().tick()
    finish_pending(make)
    original = InvestigationService.append

    def interrupted(service, *args, **kwargs):
        if kwargs.get("automation"):
            raise RuntimeError("SYNTHETIC stop after durable append intent")
        return original(service, *args, **kwargs)

    monkeypatch.setattr(InvestigationService, "append", interrupted)
    with pytest.raises(RuntimeError, match="durable append intent"):
        make().tick()
    monkeypatch.setattr(InvestigationService, "append", original)
    monitor = make()
    with Session(monitor.engine.database) as session:
        batch = session.scalar(select(MonitorBatch))
        batch_id = batch.id
        lead = session.scalar(select(MonitorLead).where(MonitorLead.investigation_id.is_not(None)))
        investigation_id = lead.investigation_id
    # A real human append changes the revision while the old automation intent remains frozen.
    runtime_service = InvestigationService(monitor.engine)
    current = runtime_service.get(monitor.actor, investigation_id)
    runtime_service.append(
        monitor.actor,
        investigation_id,
        InvestigationEntryCreate.model_validate(
            {"kind": "note", "expected_revision": current.revision, "text": "人工在中断期间补充说明"}
        ),
        "human-during-interruption",
    )
    assert make().tick()["state"] == "blocked"
    with Session(monitor.engine.database) as session:
        blocked = session.get(MonitorBatch, batch_id)
        assert blocked.state == "blocked" and blocked.error_code == "revision_conflict"
        progress = session.get(MonitorState, monitor.actor.workspace_id).source_progress
        assert next(iter(progress.values()))["state"] == "blocked"
        assert next(iter(progress.values())).get("acked_after", 0) == 0
    assert make().retry_batch(monitor.actor, batch_id)["same_fixed_input"]
    make().tick()
    with Session(monitor.engine.database) as session:
        assert session.get(MonitorBatch, batch_id).state == "completed"


def test_crash_after_lead_commit_rejoins_same_batch_selection_instead_of_orphaning_leads(runtime):
    make, source, settings = runtime
    settings.monitor_hourly_investigations = 0
    seeded(source)
    make().tick()
    finish_pending(make)
    monitor = make()
    original = monitor._save_batch

    def interrupted(identifier, **kwargs):
        if kwargs.get("state") == "investigating":
            raise RuntimeError("SYNTHETIC selection receipt lost before commit")
        return original(identifier, **kwargs)

    monitor._save_batch = interrupted
    with pytest.raises(RuntimeError, match="selection receipt lost"):
        monitor.tick()
    with Session(monitor.engine.database) as session:
        lead_ids = set(session.scalars(select(MonitorLead.id)))
        assert lead_ids and session.scalar(select(MonitorBatch.state)) == "queued"
    make().tick()
    with Session(monitor.engine.database) as session:
        batch = session.scalar(select(MonitorBatch))
        assert batch.state == "completed"
        assert set(batch.payload["selected_leads"]) == lead_ids
        assert all(lead.state == "deferred" for lead in session.scalars(select(MonitorLead)))


def test_system_status_configuration_never_claims_a_monitor_heartbeat(runtime):
    _, _, settings = runtime
    settings.data_fabric_url = "http://127.0.0.1:18511"
    settings.data_fabric_history_read_token = "SYNTHETIC-local-read-token"
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        value = client.get("/api/system/status").json()
    assert value["discovery_mode"] == "bounded_incremental_monitor"
    assert value["autonomous_investigation"] is True
    assert value["monitor_configuration"] == "configured_not_observed"
    assert value["monitor_status_url"] == "/api/engine/monitor/status"
    assert "SYNTHETIC-local-read-token" not in json.dumps(value)


def test_deferred_commit_intent_recovers_after_batch_ack_and_conflict_can_release_without_rewinding(
    runtime, monkeypatch
):
    from bioradar.research.investigations.models import InvestigationEntryCreate

    make, source, settings = runtime
    settings.monitor_hourly_investigations = 0
    seeded(source)
    make().tick()
    finish_pending(make)
    make().tick()
    monitor = make()
    with Session(monitor.engine.database) as session:
        batch_id = session.scalar(select(MonitorBatch.id))
    settings.monitor_hourly_investigations = 1
    original = InvestigationService.append

    def interrupted(service, *args, **kwargs):
        if kwargs.get("automation"):
            raise RuntimeError("SYNTHETIC deferred intent interruption")
        return original(service, *args, **kwargs)

    monkeypatch.setattr(InvestigationService, "append", interrupted)
    with pytest.raises(RuntimeError, match="deferred intent"):
        make().tick()
    monkeypatch.setattr(InvestigationService, "append", original)
    with Session(monitor.engine.database) as session:
        lead = session.scalar(select(MonitorLead).where(MonitorLead.state == "investigating"))
        lead_id, investigation_id = lead.id, lead.investigation_id
        assert session.get(MonitorBatch, batch_id).state == "completed"
    runtime_service = InvestigationService(monitor.engine)
    current = runtime_service.get(monitor.actor, investigation_id)
    runtime_service.append(
        monitor.actor,
        investigation_id,
        InvestigationEntryCreate.model_validate(
            {"kind": "note", "expected_revision": current.revision, "text": "人工修改造成真实revision冲突"}
        ),
        "human-deferred-conflict",
    )
    source.records.append(record(4, "SYNTHETIC new source work continues independently.", seq=4))
    make().tick()  # Conflict blocks only this deferred lead; new source work is still accepted.
    with Session(monitor.engine.database) as session:
        assert session.get(MonitorLead, lead_id).state == "blocked"
        assert session.get(MonitorBatch, batch_id).state == "completed"
        assert session.scalar(select(func.count()).select_from(MonitorBatch)) == 2
        before = session.get(MonitorState, monitor.actor.workspace_id).source_progress
        acked = next(iter(before.values()))["acked_after"]
    released = make().retry_batch(monitor.actor, batch_id)
    assert released["state"] == "completed" and released["same_fixed_input"]
    assert make().retry_batch(monitor.actor, batch_id)["already_requested"]
    make().tick()
    with Session(monitor.engine.database) as session:
        assert session.get(MonitorLead, lead_id).state == "investigated"
        assert session.get(MonitorBatch, batch_id).state == "completed"
        progress = session.get(MonitorState, monitor.actor.workspace_id).source_progress
        assert next(iter(progress.values()))["acked_after"] == acked
        assert (
            session.scalar(
                select(func.count())
                .select_from(EngineIdempotency)
                .where(EngineIdempotency.operation == "monitor.dig.start")
            )
            == 1
        )


def test_revoked_support_in_unfinished_batch_cannot_starve_other_source(runtime, monkeypatch):
    make, source, settings = runtime
    settings.engine_source_id_allowlist = {"fixture": ["fixture-source", "other-source", "third-source"]}
    source.enabled.update({"other-source", "third-source"})
    settings.monitor_hourly_investigations = 0
    seeded(source)
    other = record(8, "SYNTHETIC adaptive immune vesicle additional material.", seq=1)
    other["source_id"] = "other-source"
    source.records.append(other)
    # Adopt the first source, then stop the other source's cross-source batch
    # immediately after its selection intent becomes durable.
    make().tick()
    finish_pending(make)
    make().tick()  # other source accepted
    finish_pending(make)
    make().tick()  # empty third source
    make().tick()  # first source completed
    monitor = make()
    original = monitor._save_batch

    def interrupted(identifier, **kwargs):
        result = original(identifier, **kwargs)
        if kwargs.get("state") == "investigating":
            raise RuntimeError("SYNTHETIC stop after cross-source selection")
        return result

    monitor._save_batch = interrupted
    with pytest.raises(RuntimeError, match="cross-source selection"):
        monitor.tick()
    with Session(monitor.engine.database) as session:
        blocked_batch = session.scalar(select(MonitorBatch).where(MonitorBatch.source_id == "other-source"))
        original_ack = next(
            value.get("acked_after", 0)
            for value in session.get(MonitorState, monitor.actor.workspace_id).source_progress.values()
            if value["source_id"] == "other-source"
        )
        assert blocked_batch.state == "investigating"
        blocked_id = blocked_batch.id
    source.enabled.remove("fixture-source")
    third = record(9, "SYNTHETIC independent third-source observation.", seq=1)
    third["source_id"] = "third-source"
    source.records.append(third)
    calls_before = len(source.calls)
    for _ in range(3):
        make().tick()
        finish_pending(make)
    new_reads = [
        dict(params)["source_id"]
        for path, params in source.calls[calls_before:]
        if path == "/api/history/changes"
    ]
    assert "third-source" in new_reads
    with Session(monitor.engine.database) as session:
        progress = session.get(MonitorState, monitor.actor.workspace_id).source_progress
        other_progress = next(v for v in progress.values() if v["source_id"] == "other-source")
        assert other_progress.get("acked_after", 0) == original_ack
        assert session.get(MonitorBatch, blocked_id).state == "investigating"
        assert (
            session.scalar(select(MonitorBatch).where(MonitorBatch.source_id == "third-source")) is not None
        )


def _only_state_transition_policy(monkeypatch):
    from bioradar.research.mining.models import MiningPolicy
    from bioradar.research.monitor import service as monitor_service

    class StateOnlyPolicy(MiningPolicy):
        method_ids: tuple[str, ...] = ("state_transitions",)

    monkeypatch.setattr(monitor_service, "MiningPolicy", StateOnlyPolicy)


def test_selection_summary_new_version_is_not_automatically_worth_investigation(runtime, monkeypatch):
    make, source, _ = runtime
    _only_state_transition_policy(monkeypatch)
    source.records = [record(1, "SYNTHETIC initial registration without an observed change.", seq=1)]
    make().tick()
    finish_pending(make)
    make().tick()
    monitor = make()
    summary = monitor.batches(monitor.actor).items[0].selection_summary
    assert summary.new_records == summary.evaluated == 1
    assert summary.selected == summary.unknown == 0 and summary.no_candidate == 1
    assert summary.methods[0]["execution_status"] == "success"
    assert summary.methods[0]["processed_manifest_versions"] == 1
    assert summary.record_outcomes[0]["outcome"] == "no_candidate"
    assert not monitor.leads(monitor.actor).items


@pytest.mark.parametrize("diagnostic", ["failed", "unsupported", "partial", "excluded"])
def test_selection_summary_incomplete_or_excluded_method_scope_stays_unknown(
    runtime, monkeypatch, diagnostic
):
    from bioradar.research.mining import runner
    from bioradar.research.mining.models import ExcludedRecord, MethodResult

    make, source, settings = runtime
    _only_state_transition_policy(monkeypatch)
    settings.engine_retry_limit = 1
    source.records = [record(1, "SYNTHETIC scope diagnostic.", seq=1)]

    def method(prepared):
        return MethodResult(
            method_id="state_transitions",
            method_version=runner.METHODS["state_transitions"].version,
            readiness="unsupported" if diagnostic == "unsupported" else "exploratory_only",
            execution_status="success" if diagnostic == "excluded" else diagnostic,
            reasons=("SYNTHETIC explicit method diagnostic",),
            truncated_count=1 if diagnostic == "partial" else 0,
            excluded_records=(
                (
                    ExcludedRecord(
                        record_ref=prepared.records[0].record.record_ref, reason="SYNTHETIC excluded input"
                    ),
                )
                if diagnostic == "excluded"
                else ()
            ),
        )

    monkeypatch.setitem(runner.IMPLEMENTATIONS, "state_transitions", method)
    make().tick()
    finish_pending(make)
    make().tick()
    monitor = make()
    batch = monitor.batches(monitor.actor).items[0]
    summary = batch.selection_summary
    assert summary.new_records == summary.evaluated == summary.unknown == 1
    assert summary.selected == summary.no_candidate == 0
    assert summary.methods[0]["execution_status"] == ("success" if diagnostic == "excluded" else diagnostic)
    if diagnostic == "failed":
        assert batch.state == "blocked"
    if diagnostic == "excluded":
        assert summary.methods[0]["excluded_manifest_versions"] == 1
    with Session(monitor.engine.database) as session:
        stored = session.get(MonitorBatch, batch.id).payload["selection_summary"]
        assert stored == summary.model_dump(mode="json")


def test_selected_lead_keeps_actual_importance_urgency_and_legacy_unknown_scope(runtime):
    from bioradar.research.mining.models import Candidate

    make, source, _ = runtime
    seeded(source)
    make().tick()
    finish_pending(make)
    make().tick()
    monitor = make()
    batch = monitor.batches(monitor.actor).items[0]
    summary = batch.selection_summary
    leads = monitor.leads(monitor.actor).items
    assert leads and summary.selected > 0
    assert summary.selected + summary.no_candidate + summary.unknown == summary.new_records == 3
    assert summary.evaluated == 3
    assert any(method["execution_status"] == "success" for method in summary.methods)
    with Session(monitor.engine.database) as session:
        stored_batch = session.get(MonitorBatch, batch.id)
        manifest = {monitor.reader.ref(item).version_key for item in stored_batch.payload["manifest"]}
        selected = set()
        for lead in leads:
            row = session.get(MonitorLead, lead.id)
            candidate = Candidate.model_validate(row.payload["candidate"])
            selected.update(s.record_ref.version_key for s in candidate.supports)
            assert lead.new_to_radar and lead.importance == candidate.importance
            assert lead.urgency == candidate.urgency
            assert candidate.importance in lead.importance_reason
            assert candidate.urgency in lead.urgency_reason
            assert "选中仅表示继续调查" in lead.importance_reason
        assert summary.selected == len(manifest & selected)
        old_payload = dict(stored_batch.payload)
        old_payload.pop("selection_summary")
        stored_batch.payload = old_payload
        old_lead = session.get(MonitorLead, leads[0].id)
        old_payload = dict(old_lead.payload)
        old_payload.pop("importance_reason")
        old_payload.pop("urgency_reason")
        old_lead.payload = old_payload
        session.commit()
    assert monitor.batches(monitor.actor).items[0].selection_summary is None
    old = next(lead for lead in monitor.leads(monitor.actor).items if lead.id == leads[0].id)
    assert old.importance_reason is old.urgency_reason is None
