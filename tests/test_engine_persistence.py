"""PostgreSQL-only runtime checks, isolated from real intelligence acceptance.

Set BIORADAR_ENGINE_TEST_DATABASE_URL to run. Each test uses a temporary schema
and the actual 0002 migration, not SQLite or an in-memory business-state substitute.
"""

import copy
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.api.app import create_app
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.models import EngineRun, EngineSnapshot
from bioradar.research.engine.schemas import FeedbackCreate, RunAction, RunCreate
from bioradar.research.engine.service import RESULT_BUDGET_MESSAGE, EngineService
from bioradar.research.mining.models import (
    Candidate,
    EngineRecord,
    EvidenceSupport,
    Locator,
    MethodResult,
    MiningPolicy,
    MiningResult,
    NormalizedSnapshot,
    ProviderRef,
    ResourceLimits,
    SnapshotCoverage,
    canonical_hash,
)
from bioradar.research.mining.prepare import snapshot_hash
from bioradar.research.mining.registry import ENGINE_COMPUTE_VERSION, method_versions
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.ENGINE
ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL = "test-development-user-credential-only"
GRANT = "development:development:sources"


@pytest.fixture
def database():
    url = os.environ.get("BIORADAR_ENGINE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("BIORADAR_ENGINE_TEST_DATABASE_URL is required for PostgreSQL runtime checks")
    if not url.startswith("postgresql+psycopg://"):
        pytest.fail("Runtime verification must use PostgreSQL with psycopg")
    schema = "engine_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-c search_path={schema} -c statement_timeout=10000"}
    )
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        for filename in ("0002_engine.py", "0003_decision.py", "0004_investigations.py"):
            spec = importlib.util.spec_from_file_location(
                "engine_migration", ROOT / "db/migrations/versions" / filename
            )
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.upgrade()
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def settings():
    return Settings(environment="test", engine_dev_credential=CREDENTIAL, database_url=None, redis_url=None)


@pytest.fixture
def service(database, settings):
    return EngineService(database, settings)


@pytest.fixture
def snapshot():
    instant = datetime(2026, 10, 14, tzinfo=UTC)
    records = []
    for number in range(3):
        content = f"Targeted immune therapy study explores cellular response in cohort {number}."
        digest = canonical_hash(content)
        ref = ProviderRef(
            provider="fixture",
            object_type="document",
            object_id=f"f-{number}",
            adopted_version=digest,
            version_source="export_hash",
            content_hash=digest,
            authorization_ref=GRANT,
        )
        records.append(
            EngineRecord(
                record_ref=ref,
                evidence_refs=(ref,),
                document_hash=digest,
                source_instance="fixture",
                source_id="fixture-source",
                observed_at=instant,
                ingestion_mode="fixture",
                published_at=instant,
                published_time_raw=instant.isoformat(),
                time_precision="instant",
                publication_time_verified=True,
                normalized_text=content,
                text_status="inline_authorized",
                normalization_version="fixture-text-v1",
                locator=Locator(type="paragraph", paragraph_id="body"),
            )
        )
    return NormalizedSnapshot(
        snapshot_id="fixture-observation",
        is_demo=True,
        source_instance="fixture",
        coverage_mode="bounded_sample",
        observed_at=instant,
        records=tuple(records),
        coverage=(
            SnapshotCoverage(
                source_instance="fixture",
                source_id="fixture-source",
                mode="bounded_sample",
                scope_description="three synthetic runtime test records",
                record_count=3,
            ),
        ),
    )


def fixed_policy():
    return MiningPolicy(as_of=datetime(2026, 10, 15, tzinfo=UTC), method_ids=("lexical_emergence",))


def accept(service, settings, snapshot, *, snapshot_key="snapshot", run_key="run"):
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, snapshot, snapshot_key)
    _, run = service.create_run(actor, RunCreate(snapshot_id=adopted["id"], policy=fixed_policy()), run_key)
    return actor, adopted, run


def tokens(database, run_id):
    with Session(database) as session:
        row = session.get(EngineRun, run_id)
        return row.attempt, row.accept_token, row.input_hash


def controlled_result(snapshot, *, status="success"):
    """Controlled fixture output isolates runtime fencing from scientific methods."""
    policy = fixed_policy()
    identifier = policy.method_ids[0]
    candidate = Candidate(
        candidate_key="runtime-fixture-candidate",
        method_id=identifier,
        method_version=method_versions(policy.method_ids)[identifier],
        group_key="runtime-fixture",
        kind="hypothesis",
        title="synthetic runtime candidate",
        reasons=("runtime fixture only",),
        supports=(
            EvidenceSupport(
                record_ref=snapshot.records[0].record_ref,
                document_hash=snapshot.records[0].document_hash,
                locator=snapshot.records[0].locator,
            ),
        ),
    )
    candidates = (candidate,) if status in {"success", "partial"} else ()
    result = MiningResult(
        snapshot_hash=snapshot_hash(snapshot),
        policy_hash=canonical_hash(policy),
        method_results=(
            MethodResult(
                method_id=identifier,
                method_version=candidate.method_version,
                readiness="exploratory_only",
                execution_status=status,
                candidates=candidates,
                reasons=("fixture method diagnosis",),
            ),
        ),
        candidates=candidates,
        result_hash="",
    )
    digest = canonical_hash(result.model_dump(mode="json", exclude={"result_hash"}))
    return result.model_copy(update={"result_hash": digest})


def test_snapshot_and_run_idempotency_are_persistent_and_conflicting_bodies_fail(
    service, settings, snapshot, database
):
    actor, adopted, accepted = accept(service, settings, snapshot)
    assert service.adopt_snapshot(actor, snapshot, "snapshot")[1] == adopted
    body = RunCreate(snapshot_id=adopted["id"], policy=fixed_policy())
    assert service.create_run(actor, body, "run")[1] == accepted
    changed = snapshot.model_copy(update={"snapshot_id": "different-label"})
    with pytest.raises(DomainError) as conflict:
        service.adopt_snapshot(actor, changed, "snapshot")
    assert conflict.value.status_code == 409
    # A different HTTP key with equivalent scientific content reuses adoption/run.
    assert service.adopt_snapshot(actor, changed, "other-snapshot")[1]["id"] == adopted["id"]
    assert service.create_run(actor, body, "other-run")[1]["id"] == accepted["id"]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(EngineSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(EngineRun)) == 1


def test_concurrent_acceptance_has_one_scientific_run(service, settings, snapshot, database):
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, snapshot, "snapshot")
    body = RunCreate(snapshot_id=adopted["id"], policy=fixed_policy())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.create_run, actor, body, f"request-{i}") for i in range(2)]
        results = [future.result() for future in futures]
    assert results[0][1]["id"] == results[1][1]["id"]
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(EngineRun)) == 1


def test_unavailable_queue_keeps_durable_queued_input(service, settings, snapshot):
    actor, _, run = accept(service, settings, snapshot)
    assert service.dispatch(run["id"]) is False
    view = service.get_run(actor, run["id"])
    assert view.state == "queued" and view.result is None and view.dispatch_pending
    assert view.candidate_count is None
    assert service.recover()["dependency_unavailable"] == 1


def test_explicit_retry_fences_late_worker_and_only_accepts_once(service, settings, snapshot, database):
    actor, _, run = accept(service, settings, snapshot)
    old = tokens(database, run["id"])
    assert service.start_run(run["id"], *old) is not None
    assert service.start_run(run["id"], *old) is None
    assert service.fail_run(run["id"], *old)
    failed = service.get_run(actor, run["id"])
    _, retry = service.retry_run(
        actor, run["id"], RunAction(action="retry_failed", expected_revision=failed.revision), "retry"
    )
    assert retry["attempt"] == 2 and retry["state"] == "queued"
    new = tokens(database, run["id"])
    assert new[1] != old[1] and new[2] == old[2]
    assert service.start_run(run["id"], *new) is not None
    result = controlled_result(snapshot)
    assert service.finish_run(run["id"], *old, result) is False
    assert service.finish_run(run["id"], *new, result) is True
    assert service.finish_run(run["id"], *new, result) is False
    accepted = service.get_run(actor, run["id"])
    assert accepted.state == "completed" and accepted.result.result_hash == result.result_hash
    assert accepted.attempt_diagnostics[0].state == "failed"


def test_all_failed_method_result_can_retry_same_fixed_input(service, settings, snapshot, database):
    actor, _, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    assert service.start_run(run["id"], *current)
    assert service.finish_run(run["id"], *current, controlled_result(snapshot, status="failed"))
    failed = service.get_run(actor, run["id"])
    assert failed.state == "failed" and failed.error.code == "all_methods_failed"
    _, retried = service.retry_run(
        actor, run["id"], RunAction(action="retry_failed", expected_revision=failed.revision), "retry"
    )
    assert retried["state"] == "queued" and retried["result"] is None
    diagnosis = service.get_run(actor, run["id"]).attempt_diagnostics[0]
    assert diagnosis.method_results[0].execution_status == "failed"


def test_result_with_wrong_hash_or_input_is_not_adopted(service, settings, snapshot, database):
    actor, _, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    assert service.start_run(run["id"], *current)
    result = controlled_result(snapshot)
    for changed in [
        result.model_copy(update={"result_hash": "0" * 64}),
        result.model_copy(update={"snapshot_hash": "0" * 64}),
    ]:
        with pytest.raises(DomainError):
            service.finish_run(run["id"], *current, changed)
    assert service.get_run(actor, run["id"]).state == "running"
    assert service.get_run(actor, run["id"]).result is None


def test_feedback_is_append_only_bound_to_exact_result_and_lists_hide_text(
    service, settings, snapshot, database
):
    actor, adopted, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    result = controlled_result(snapshot)
    service.finish_run(run["id"], *current, result)
    body = FeedbackCreate(
        candidate_key=result.candidates[0].candidate_key,
        result_hash=result.result_hash,
        decision="needs_check",
        reason="synthetic runtime review",
    )
    first = service.add_feedback(actor, run["id"], body, "feedback")[1]
    assert service.add_feedback(actor, run["id"], body, "feedback")[1] == first
    assert len(service.get_run(actor, run["id"]).feedback) == 1
    assert service.get_run(actor, run["id"]).result.result_hash == result.result_hash
    with pytest.raises(DomainError):
        service.add_feedback(actor, run["id"], body.model_copy(update={"result_hash": "0" * 64}), "bad")
    assert service.list_runs(actor).items[0].result is None
    assert service.list_runs(actor).items[0].candidate_count == 1
    summaries = service.list_snapshots(actor).model_dump_json()
    assert "normalized_text" not in summaries and "Targeted immune" not in summaries
    assert service.get_snapshot(actor, adopted["id"]).snapshot.records[0].normalized_text


def test_source_and_fixture_scope_cannot_be_claimed_by_json(service, settings, snapshot):
    actor = DevelopmentAccess(settings).actor()
    with pytest.raises(DomainError):
        service.adopt_snapshot(
            actor, snapshot.model_copy(update={"source_instance": "not-granted"}), "not-granted"
        )
    with pytest.raises(DomainError):
        service.adopt_snapshot(actor, snapshot.model_copy(update={"is_demo": False}), "false-real")
    wrong_ref = snapshot.records[0].record_ref.model_copy(update={"authorization_ref": "another-grant"})
    wrong_record = snapshot.records[0].model_copy(update={"record_ref": wrong_ref})
    with pytest.raises(DomainError):
        service.adopt_snapshot(actor, snapshot.model_copy(update={"records": (wrong_record,)}), "wrong-grant")


def test_scope_revocation_denies_existing_snapshot_result_and_counts(service, settings, snapshot, database):
    _, adopted, run = accept(service, settings, snapshot)
    revoked_settings = settings.model_copy(update={"engine_source_allowlist": ["datafabric-test"]})
    revoked = EngineService(database, revoked_settings)
    actor = DevelopmentAccess(revoked_settings).actor()
    with pytest.raises(DomainError) as hidden:
        revoked.get_snapshot(actor, adopted["id"])
    assert hidden.value.status_code == 404
    with pytest.raises(DomainError):
        revoked.get_run(actor, run["id"])
    assert revoked.list_runs(actor).items == []
    assert revoked.list_snapshots(actor).items == []


def test_authenticated_http_acceptance_uses_persisted_state_and_server_actor(database, settings, snapshot):
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "http-snapshot"}
        assert client.get("/api/engine/snapshots").status_code == 401
        adopted = client.post("/api/engine/snapshots", json=snapshot.model_dump(mode="json"), headers=headers)
        assert adopted.status_code == 201
        body = RunCreate(snapshot_id=adopted.json()["id"], policy=fixed_policy()).model_dump(mode="json")
        accepted = client.post(
            "/api/engine/runs", json=body, headers={**headers, "Idempotency-Key": "http-run"}
        )
        assert accepted.status_code == 202 and accepted.json()["state"] == "queued"
        assert accepted.json()["result"] is None
        assert "accept_token" not in accepted.text and CREDENTIAL not in accepted.text
        stored = client.get("/api/engine/runs/" + accepted.json()["id"], headers=headers)
        assert stored.status_code == 200 and stored.json()["dispatch_pending"]
        bad = client.post("/api/engine/runs", json={**body, "workspace_id": "other"}, headers=headers)
        assert bad.status_code == 422


def test_compute_version_change_requires_new_identity_and_refuses_old_worker(
    service, settings, snapshot, database, monkeypatch
):
    actor, adopted, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    monkeypatch.setattr("bioradar.research.engine.service.ENGINE_COMPUTE_VERSION", "future-compute")
    assert service.start_run(run["id"], *current) is None
    assert service.get_run(actor, run["id"]).error.code == "method_version_changed"
    _, new_run = service.create_run(
        actor, RunCreate(snapshot_id=adopted["id"], policy=fixed_policy()), "new-version"
    )
    assert new_run["id"] != run["id"] and new_run["input_hash"] != run["input_hash"]


def test_source_instance_permission_does_not_expand_source_id_scope(service, settings, snapshot, database):
    actor, adopted, run = accept(service, settings, snapshot)
    bad_record = snapshot.records[0].model_copy(update={"source_id": "unapproved-private-source"})
    with pytest.raises(DomainError) as forbidden:
        service.adopt_snapshot(
            actor, snapshot.model_copy(update={"records": (bad_record,)}), "unapproved-source"
        )
    assert forbidden.value.status_code == 403
    narrowed = settings.model_copy(update={"engine_source_id_allowlist": {"fixture": ["different-source"]}})
    restricted = EngineService(database, narrowed)
    restricted_actor = DevelopmentAccess(narrowed).actor()
    with pytest.raises(DomainError) as hidden:
        restricted.get_snapshot(restricted_actor, adopted["id"])
    assert hidden.value.status_code == 404
    with pytest.raises(DomainError):
        restricted.get_run(restricted_actor, run["id"])
    assert restricted.list_snapshots(restricted_actor).items == []
    assert restricted.list_runs(restricted_actor).items == []


@pytest.mark.parametrize("tamper", ["version", "grant", "document_hash", "quote", "locator", "selection"])
def test_result_source_and_quote_binding_is_validated_before_acceptance(
    service,
    settings,
    snapshot,
    database,
    tamper,
):
    actor, _, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    assert service.start_run(run["id"], *current)
    result = controlled_result(snapshot)
    candidate = result.candidates[0]
    support = candidate.supports[0]
    if tamper in {"version", "grant"}:
        field = "adopted_version" if tamper == "version" else "authorization_ref"
        support = support.model_copy(
            update={"record_ref": support.record_ref.model_copy(update={field: "other"})}
        )
    elif tamper == "document_hash":
        support = support.model_copy(update={"document_hash": "0" * 64})
    elif tamper == "quote":
        support = support.model_copy(
            update={
                "quote": "false quote",
                "quote_status": "normalized_only",
                "normalized_start": 0,
                "normalized_end": 11,
                "normalized_text_hash": snapshot.records[0].normalized_text_hash,
            }
        )
    elif tamper == "locator":
        support = support.model_copy(update={"locator": Locator(type="paragraph", paragraph_id="other")})
    changed = candidate.model_copy(update={"supports": (support,)})
    method = result.method_results[0].model_copy(update={"candidates": (changed,)})
    changed_result = result.model_copy(update={"method_results": (method,), "candidates": (changed,)})
    if tamper == "selection":
        changed_result = changed_result.model_copy(
            update={"candidates": (changed.model_copy(update={"title": "other"}),)}
        )
    changed_result = changed_result.model_copy(
        update={
            "result_hash": canonical_hash(changed_result.model_dump(mode="json", exclude={"result_hash"})),
        }
    )
    with pytest.raises(DomainError):
        service.finish_run(run["id"], *current, changed_result)
    assert service.get_run(actor, run["id"]).result is None


def test_lost_enqueue_ack_is_reconciled_without_double_adoption(
    service, settings, snapshot, database, monkeypatch
):
    actor, _, run = accept(service, settings, snapshot)
    configured = settings.model_copy(update={"redis_url": Settings(redis_url="redis://fixture").redis_url})
    engine = EngineService(database, configured)
    enqueued = []

    class FakeRedis:
        def close(self):
            pass

    class ExistingJob:
        def get_status(self, refresh=True):
            return "queued"

    def fetch(*args, **kwargs):
        if enqueued:
            return ExistingJob()
        from rq.exceptions import NoSuchJobError

        raise NoSuchJobError

    class LostAckQueue:
        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            enqueued.append(kwargs["job_id"])
            raise TimeoutError("fixture: enqueue occurred but reply was lost")

    monkeypatch.setattr("bioradar.research.engine.service.Redis.from_url", lambda *a, **k: FakeRedis())
    monkeypatch.setattr("bioradar.research.engine.service.Job.fetch", fetch)
    monkeypatch.setattr("bioradar.research.engine.service.Queue", LostAckQueue)
    assert engine.dispatch(run["id"]) is False
    assert engine.get_run(actor, run["id"]).dispatch_pending
    assert engine.dispatch(run["id"]) is True
    assert len(enqueued) == 1
    view = engine.get_run(actor, run["id"])
    assert view.state == "queued" and view.result is None and not view.dispatch_pending


def test_actual_worker_calculates_frozen_pg_snapshot_and_replay_is_idempotent(
    service,
    settings,
    snapshot,
    database,
    monkeypatch,
):
    from bioradar.research.engine import worker

    actor, _, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "create_database", lambda _: database)
    assert worker.execute_run(run["id"], *current) == run["id"]
    accepted = service.get_run(actor, run["id"])
    assert accepted.state in {"completed", "partial"}
    assert accepted.result is not None and accepted.candidate_count > 0
    assert accepted.result.method_results[0].execution_status in {"success", "partial"}
    result_hash = accepted.result.result_hash
    assert worker.execute_run(run["id"], *current) == run["id"]
    assert service.get_run(actor, run["id"]).result.result_hash == result_hash


def test_historical_policy_is_returned_exactly_without_new_default_fields(
    service,
    settings,
    snapshot,
    database,
):
    actor, _, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    service.finish_run(run["id"], *current, controlled_result(snapshot))
    with Session(database) as session, session.begin():
        row = session.get(EngineRun, run["id"])
        historical = copy.deepcopy(row.policy)
        historical["limits"].pop("max_paths")
        row.policy = historical
        payload = copy.deepcopy(row.result)
        payload["policy_hash"] = canonical_hash(historical)
        payload["result_hash"] = canonical_hash({k: v for k, v in payload.items() if k != "result_hash"})
        row.result, row.result_hash = payload, payload["result_hash"]
    view = service.get_run(actor, run["id"])
    assert view.state == "completed" and view.result is not None
    assert view.policy is None and view.policy_view_status == "historical_requires_new_run"
    assert "max_paths" not in view.original_policy["limits"]
    assert view.result.policy_hash == canonical_hash(view.original_policy)
    with Session(database) as session:
        assert "max_paths" not in session.get(EngineRun, run["id"]).policy["limits"]


def test_old_queued_policy_is_refused_before_computing_with_new_defaults(
    service,
    settings,
    snapshot,
    database,
):
    actor, _, run = accept(service, settings, snapshot)
    with Session(database) as session, session.begin():
        row = session.get(EngineRun, run["id"])
        historical = copy.deepcopy(row.policy)
        historical["limits"].pop("max_paths")
        row.policy = historical
    current = tokens(database, run["id"])
    assert service.start_run(run["id"], *current) is None
    view = service.get_run(actor, run["id"])
    assert view.state == "failed" and view.error.code == "policy_schema_changed"
    with pytest.raises(DomainError) as changed:
        service.retry_run(
            actor,
            run["id"],
            RunAction(action="retry_failed", expected_revision=view.revision),
            "legacy-retry",
        )
    assert changed.value.code == "policy_schema_changed"


def test_precise_nullable_label_provenance_extension_preserves_old_snapshot_hash(
    service,
    settings,
    snapshot,
    database,
):
    actor, adopted, _ = accept(service, settings, snapshot)
    old_payload = snapshot.model_dump(mode="json")
    for record in old_payload["records"]:
        record["labels"].pop("provenance", None)
    original_content = {k: v for k, v in old_payload.items() if k != "snapshot_id"}
    original_content["coverage"] = sorted(original_content["coverage"], key=canonical_hash)
    original_content["metadata_gaps"] = sorted(original_content["metadata_gaps"])
    old_hash = canonical_hash(original_content)
    assert snapshot_hash(snapshot) == old_hash
    with Session(database) as session, session.begin():
        row = session.get(EngineSnapshot, adopted["id"])
        row.payload, row.snapshot_hash = old_payload, old_hash
    assert service.get_snapshot(actor, adopted["id"]).snapshot_hash == old_hash
    from bioradar.research.mining.models import SectionProvenance

    enriched_record = snapshot.records[0].model_copy(
        update={
            "labels": snapshot.records[0].labels.model_copy(
                update={
                    "provenance": SectionProvenance(
                        schema_version="fixture-label-v1",
                        provider_ref=snapshot.records[0].record_ref,
                        available_at=snapshot.observed_at,
                    )
                },
            )
        }
    )
    enriched = snapshot.model_copy(update={"records": (enriched_record, *snapshot.records[1:])})
    assert snapshot_hash(enriched) != old_hash


@pytest.mark.parametrize("field", ["max_candidates_per_method", "review_capacity", "max_temporal_bins"])
def test_candidate_and_temporal_budgets_cannot_exceed_server_scope(
    service,
    settings,
    snapshot,
    database,
    field,
):
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, snapshot, "budget-snapshot")
    policy = fixed_policy()
    policy = policy.model_copy(
        update={
            "limits": policy.limits.model_copy(update={field: getattr(ResourceLimits(), field) + 1}),
        }
    )
    with pytest.raises(DomainError) as budget:
        service.create_run(actor, RunCreate(snapshot_id=adopted["id"], policy=policy), "budget-run")
    assert budget.value.code == "engine_budget_exceeded" and budget.value.status_code == 422
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(EngineRun)) == 0


def test_worker_persists_safe_result_budget_failure_without_truncation_or_same_input_retry(
    service,
    settings,
    snapshot,
    database,
    monkeypatch,
):
    from bioradar.research.engine import worker

    actor, _, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    bounded = settings.model_copy(update={"engine_max_result_bytes": 1024})
    result = controlled_result(snapshot)
    assert len(result.model_dump_json().encode("utf-8")) > bounded.engine_max_result_bytes
    monkeypatch.setattr(worker, "Settings", lambda: bounded)
    monkeypatch.setattr(worker, "create_database", lambda _: database)
    monkeypatch.setattr("bioradar.research.mining.runner.run_mining", lambda *_: result)
    with pytest.raises(RuntimeError, match="see persisted run state"):
        worker.execute_run(run["id"], *current)
    view = service.get_run(actor, run["id"])
    assert view.state == "failed" and view.result is None
    assert view.error.code == "result_budget_exceeded" and view.error.message == RESULT_BUDGET_MESSAGE
    assert view.input_hash == run["input_hash"] and view.attempt == 1
    with pytest.raises(DomainError) as retry:
        service.retry_run(
            actor,
            run["id"],
            RunAction(action="retry_failed", expected_revision=view.revision),
            "invalid-budget-retry",
        )
    assert retry.value.code == "result_budget_exceeded" and retry.value.message == RESULT_BUDGET_MESSAGE


def test_legacy_v1_result_is_read_exactly_and_current_version_uses_new_run_identity(
    service,
    settings,
    snapshot,
    database,
):
    actor, adopted, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    service.finish_run(run["id"], *current, controlled_result(snapshot))
    # Simulate a persisted v1 row in this isolated schema; real historical rows are untouched.
    with Session(database) as session, session.begin():
        row = session.get(EngineRun, run["id"])
        versions = {**row.method_versions, "__engine_compute__": "bioradar-mining-compute-v1"}
        row.method_versions = versions
        row.input_hash = canonical_hash(
            {"snapshot_hash": snapshot_hash(snapshot), "policy": row.policy, "method_versions": versions}
        )
        payload = copy.deepcopy(row.result)
        payload["compute_version"] = "bioradar-mining-compute-v1"
        payload["result_hash"] = canonical_hash({k: v for k, v in payload.items() if k != "result_hash"})
        row.result, row.result_hash = payload, payload["result_hash"]
        historical_hash = row.input_hash
    historical = service.get_run(actor, run["id"])
    assert historical.state == "completed"
    assert historical.result.model_dump(mode="json") == payload
    assert historical.result.compute_version == "bioradar-mining-compute-v1"
    _, current_run = service.create_run(
        actor, RunCreate(snapshot_id=adopted["id"], policy=fixed_policy()), "current-compute"
    )
    assert ENGINE_COMPUTE_VERSION == "bioradar-mining-compute-v0.2"
    assert current_run["id"] != run["id"] and current_run["input_hash"] != historical_hash
    with Session(database) as session:
        assert session.get(EngineRun, run["id"]).result == payload
        assert session.get(EngineRun, current_run["id"]).method_versions["__engine_compute__"] == (
            ENGINE_COMPUTE_VERSION
        )
    assert len(service.list_runs(actor).items) == 2


def test_queued_legacy_v1_compute_is_refused_before_current_code_runs(
    service,
    settings,
    snapshot,
    database,
):
    actor, _, run = accept(service, settings, snapshot)
    with Session(database) as session, session.begin():
        row = session.get(EngineRun, run["id"])
        versions = {**row.method_versions, "__engine_compute__": "bioradar-mining-compute-v1"}
        row.method_versions = versions
        row.input_hash = canonical_hash(
            {"snapshot_hash": snapshot_hash(snapshot), "policy": row.policy, "method_versions": versions}
        )
    assert service.start_run(run["id"], *tokens(database, run["id"])) is None
    view = service.get_run(actor, run["id"])
    assert view.state == "failed" and view.error.code == "method_version_changed"
    assert view.result is None and view.original_policy == fixed_policy().model_dump(mode="json")


def test_recover_reports_still_pending_dispatch_when_redis_fails_after_fetch(
    service,
    settings,
    snapshot,
    database,
    monkeypatch,
):
    from rq.exceptions import NoSuchJobError

    actor, _, run = accept(service, settings, snapshot)
    configured = settings.model_copy(update={"redis_url": Settings(redis_url="redis://fixture").redis_url})
    engine = EngineService(database, configured)

    class FakeRedis:
        def close(self):
            pass

    class FailingQueue:
        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            raise TimeoutError("fixture: Redis failed after job lookup")

    def fetch(*args, **kwargs):
        raise NoSuchJobError

    monkeypatch.setattr("bioradar.research.engine.service.Redis.from_url", lambda *a, **k: FakeRedis())
    monkeypatch.setattr("bioradar.research.engine.service.Job.fetch", fetch)
    monkeypatch.setattr("bioradar.research.engine.service.Queue", FailingQueue)
    assert engine.recover() == {"examined": 1, "dispatched": 0, "failed": 0, "dependency_unavailable": 1}
    view = engine.get_run(actor, run["id"])
    assert view.state == "queued" and view.dispatch_pending and view.result is None
