"""Explicit synthetic source versions, pure reducers and real isolated PostgreSQL topics."""

import copy
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import project_record
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.models import EngineSnapshot
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.service import InvestigationRecord, InvestigationService
from bioradar.research.mining.topic_series import evaluate_series, project_contribution
from bioradar.topics.models import (
    TopicControl,
    TopicCreate,
    TopicDefinition,
    TopicEvaluationRecord,
    TopicObservationRecord,
    TopicScopeRecord,
    TopicUpdate,
)
from bioradar.topics.service import TopicService
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import CREDENTIAL, GRANT
from test_engine_persistence import database as database  # noqa: F401
from test_graph import original_record
from test_monitor import Source, record

pytestmark = pytest.mark.ENGINE
NOW = datetime(2026, 10, 18, tzinfo=UTC)


def definition(**changes):
    return TopicDefinition.model_validate(
        {
            "title": "明确教学观察",
            "question": "哪些登记或原始指标变化值得核对？",
            "domain": "pipeline",
            "source_instance": "fixture",
            "source_ids": ["fixture-source"],
            "profiles": ["text_exposure", "registered_state", "typed_metric", "source_event"],
            "bucket": "month",
            "lookback_buckets": 12,
            **changes,
        }
    )


def dated(number, day, text, *, seq=None):
    value = record(number, text, seq=seq or number)
    value["source_published_at"] = {"value": day, "precision": "day", "meaning": "synthetic_publication"}
    return value


def metric(number, start, end, amount, *, upper=None, population="SYNTHETIC population"):
    body = {
        "schema_version": "datafabric.observation.v1",
        "title": "SYNTHETIC metric",
        "kind": "metric",
        "subject": {"namespace": "fixture-source", "id": "synthetic-unit"},
        "metric_id": "synthetic_weekly_metric",
        "metric_version": "synthetic-v1",
        "value": {"lower": str(amount), "upper": str(amount if upper is None else upper)},
        "unit": "count",
        "population": population,
        "region": "SYNTHETIC region",
        "period_start": start,
        "period_end": end,
        "measurement_basis": "reported",
        "status": "active",
        "basis": {
            "kind": "source_bound",
            "ref": {
                "source_id": "fixture-source",
                "source_record_id": "synthetic-original",
                "version_id": "a" * 64,
                "raw_sha256": "b" * 64,
            },
            "field_pointers": {},
        },
    }
    value = original_record("synthetic-metric-" + str(number), body)
    value["parser_version"] = "datafabric.observation.projection.v0.1"
    value["published_seq"] = number
    return value


def event(number=1):
    body = {
        "schema_version": "datafabric.observation.v1",
        "title": "SYNTHETIC notice",
        "kind": "event",
        "subject": {"namespace": "fixture-source", "id": "synthetic-notice"},
        "event_type": "synthetic_notice",
        "event_time": {"value": "2026-10-15", "precision": "day", "meaning": "synthetic_notice_publication"},
        "status": "active",
        "basis": {
            "kind": "source_bound",
            "ref": {
                "source_id": "fixture-source",
                "source_record_id": "synthetic-original",
                "version_id": "a" * 64,
                "raw_sha256": "b" * 64,
            },
            "field_pointers": {},
        },
    }
    value = original_record("synthetic-event", body)
    value["parser_version"] = "datafabric.observation.projection.v0.1"
    value["published_seq"] = number
    return value


def contribution(value, scope=None):
    reader = FabricHistoryReader(httpx.Client(base_url="http://127.0.0.1"), authorization_ref=GRANT)
    return project_contribution(
        project_record(reader, value, as_of=NOW),
        value,
        (scope or definition()).model_dump(mode="json"),
        as_of=NOW,
    )


def compute(values, scope=None):
    return evaluate_series(
        values,
        (scope or definition()).model_dump(mode="json"),
        as_of=NOW,
        seed_complete=True,
        source_watermarks={"fixture-source": 1},
    )


def test_pure_long_history_dedup_missing_months_and_backfill():
    values = [
        contribution(dated(i, day, "SYNTHETIC adaptive vesicle response " + str(i)))
        for i, day in enumerate(["2026-01-01", "2026-07-01", "2026-09-01"], 1)
    ]
    result = compute(values)
    assert not any(s["kind"] == "text_pattern" for s in result["signals"])
    assert "missing_exposure_bucket_not_zero" in result["data_gaps"]
    assert sum(b["exposures"] for b in result["buckets"]) == 3
    duplicate = copy.deepcopy(values[-1])
    duplicate["source_record_id"] = "same-text-repost"
    duplicate["version_id"] = "c" * 64
    later = compute([*values, duplicate])
    assert sum(b["exposures"] for b in later["buckets"]) == 3
    assert later["coverage"]["original_source_coverage_known"] is False


def test_typed_exact_parser_intervals_unknown_scope_and_periods():
    overlapping = [
        metric(1, "2026-01-01", "2026-04-01", 1),
        metric(2, "2026-01-01", "2026-07-01", 2),
        metric(3, "2026-07-01", "2026-10-01", 3),
    ]
    result = compute([contribution(v) for v in overlapping])
    assert not any(s["kind"] == "metric_change" for s in result["signals"])
    assert result["metric_series"][0]["descriptive_slope"] is None
    wide = [
        metric(i + 1, start, end, 0, upper=100 + i)
        for i, (start, end) in enumerate(
            [("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01"), ("2026-03-01", "2026-04-01")]
        )
    ]
    assert not any(s["kind"] == "metric_change" for s in compute([contribution(v) for v in wide])["signals"])
    unknown = metric(1, "2026-01-01", "2026-02-01", 1, population=None)
    assert compute([contribution(unknown)])["metric_series"][0]["population"] is None
    forged = event()
    forged["parser_version"] = "unverified-generic-json"
    assert not any(s["kind"] == "source_event" for s in compute([contribution(forged)])["signals"])
    assert any(s["kind"] == "source_event" for s in compute([contribution(event())])["signals"])


@pytest.fixture
def runtime(database):
    for name in ("0006_monitor.py", "0007_topics.py"):
        path = Path(__file__).resolve().parents[1] / "db/migrations/versions" / name
        spec = importlib.util.spec_from_file_location("topic_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with database.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    source = Source()
    settings = Settings(
        environment="test",
        engine_dev_credential=CREDENTIAL,
        database_url=None,
        redis_url=None,
        monitor_enabled=True,
        monitor_is_demo=True,
        monitor_batch_records=2,
        monitor_hourly_investigations=0,
    )
    client = httpx.Client(base_url="http://127.0.0.1:18511", transport=httpx.MockTransport(source.handler))

    def make():
        reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90, max_bytes=32_000_000)
        return TopicService(EngineService(database, settings), reader)

    yield make, source, settings
    client.close()


def create(runtime, scope=None):
    service = runtime()
    _, body = service.create(
        service.actor, TopicCreate(is_demo=True, definition=scope or definition()), "synthetic-topic"
    )
    return body["id"]


def latest(runtime, identifier):
    service = runtime()
    return service.evaluations(service.actor, identifier).items[0]


def test_seed_history_replacement_withdrawal_and_scope_revision(runtime):
    make, source, _ = runtime
    source.records = [
        dated(1, "2026-07-01", "SYNTHETIC adaptive vesicle response"),
        dated(2, "2026-08-01", "SYNTHETIC optical transport response"),
        dated(3, "2026-09-01", "SYNTHETIC adaptive vesicle regulation"),
    ]
    identifier = create(make)
    make().tick(now=NOW)
    first = latest(make, identifier)
    assert first.coverage.seed_complete is False
    make().tick(now=NOW)
    complete = latest(make, identifier)
    assert complete.coverage.seed_complete is True and complete.coverage.active_records == 3
    assert make().get(make().actor, identifier).observation_count == 3
    changed = dated(1, "2026-09-02", "SYNTHETIC revised optical regulation", seq=4)
    source.records.append(changed)
    make().tick(now=NOW)
    revised = latest(make, identifier)
    assert revised.observation_revision > complete.observation_revision
    assert sum(b.exposures for b in revised.buckets if b.start == "2026-07-01") == 0
    assert make().evaluation(make().actor, identifier, complete.id).model_dump() == complete.model_dump()
    removed = dated(1, "2026-09-02", "SYNTHETIC withdrawn optical regulation", seq=5)
    removed["facts"]["publication_state"] = "withdrawn"
    removed = original_record(removed["source_record_id"], removed["facts"], kind="trial")
    removed["published_seq"] = 5
    source.records.append(removed)
    make().tick(now=NOW)
    assert latest(make, identifier).coverage.withdrawn_records == 1
    view = make().get(make().actor, identifier)
    _, updated = make().update(
        make().actor,
        identifier,
        TopicUpdate(
            is_demo=True,
            definition=definition(terms_all=["optical"]),
            expected_revision=view.control_revision,
        ),
        "scope-v2",
    )
    assert updated["scope_revision"] == 2 and updated["observation_count"] == 0
    make().tick(now=NOW)
    assert make().get(make().actor, identifier).sources[0].after == 0
    old = make().evaluation(make().actor, identifier, complete.id)
    assert old.definition.terms_all == []


def test_seed_event_is_not_lost_on_unmatched_final_page(runtime):
    make, source, _ = runtime
    value = event()
    value["title"] = "SYNTHETIC selected"
    source.records = [
        value,
        dated(2, "2026-06-01", "SYNTHETIC unrelated text"),
        dated(3, "2026-07-01", "SYNTHETIC unrelated text"),
    ]
    identifier = create(make, definition(terms_all=["selected"]))
    make().tick(now=NOW)
    first = latest(make, identifier)
    assert first.automation_status == "deferred" and first.coverage.seed_complete is False
    make().tick(now=NOW)
    second = latest(make, identifier)
    assert second.observation_revision == first.observation_revision
    assert second.id != first.id and second.coverage.seed_complete is True
    assert second.automation_status == "not_needed"
    make().tick(now=NOW)
    assert latest(make, identifier).id == second.id


def test_new_event_survives_text_budget_and_all_counts(runtime, monkeypatch):
    import bioradar.topics.service as module

    monkeypatch.setattr(module, "EVALUATION_LIMIT", 2)
    make, source, settings = runtime
    settings.monitor_batch_records = 10
    source.records = [dated(i, "2026-09-01", "SYNTHETIC adaptive vesicle " + str(i)) for i in range(1, 6)]
    source.records.append(event(6))
    identifier = create(make)
    make().tick(now=NOW)
    report = latest(make, identifier)
    assert report.coverage.active_records == 6 and report.coverage.omitted_records >= 3
    assert report.automation_status == "deferred"
    assert any(s.kind == "source_event" for s in report.signals)
    assert not any(s.kind == "text_pattern" for s in report.signals)


def test_pause_catchup_due_and_old_evidence_current_permission(runtime):
    make, source, _ = runtime
    source.records = [dated(1, "2026-09-01", "SYNTHETIC adaptive vesicle response")]
    identifier = create(make)
    make().tick(now=NOW)
    first = latest(make, identifier)
    service = make()
    view = service.get(service.actor, identifier)
    service.control(
        service.actor, identifier, TopicControl(expected_revision=view.control_revision, paused=True), "pause"
    )
    source.records.append(dated(2, "2026-09-02", "SYNTHETIC optical response", seq=2))
    assert make().tick(now=NOW)["state"] == "no_topics"
    view = make().get(make().actor, identifier)
    make().control(
        make().actor,
        identifier,
        TopicControl(expected_revision=view.control_revision, paused=False),
        "resume",
    )
    make().tick(now=NOW)
    assert latest(make, identifier).coverage.active_records == 2
    key = first.evidence[0].key
    assert (
        make().evidence(make().actor, identifier, first.id, key)["version_id"] == first.evidence[0].version_id
    )
    before = latest(make, identifier).id
    make().tick(now=NOW + timedelta(hours=1))
    assert latest(make, identifier).id == before
    make().tick(now=NOW + timedelta(hours=25))
    assert latest(make, identifier).id != before
    source.enabled.clear()
    with pytest.raises(DomainError) as exc:
        make().evaluation(make().actor, identifier, first.id)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("uncertain_phase", ["evidence", "query"])
def test_followup_unknown_snapshot_commit_uses_fixed_receipt(runtime, monkeypatch, uncertain_phase):
    from bioradar.research.mining.models import canonical_hash

    make, source, settings = runtime
    settings.monitor_hourly_investigations = 6
    source.records = [dated(1, "2026-09-01", "SYNTHETIC focus adaptive vesicle response")]
    identifier = create(make, definition(terms_all=["focus"]))
    make().tick(now=NOW)
    settings.monitor_batch_records = 1
    revised = record(1, "SYNTHETIC focus adaptive vesicle response", status="TERMINATED", seq=2)
    additional = record(2, "SYNTHETIC focus additional verification material", seq=3)
    source.records.extend([revised, additional])
    original = EngineService.adopt_snapshot
    saved_response = TopicService._saved
    target = make().reader.ref(revised if uncertain_phase == "evidence" else additional)
    failed, attempts, hidden = [], [], []

    def uncertain(self, actor, snapshot, key):
        if snapshot.records[0].record_ref == target:
            attempts.append(canonical_hash(snapshot))
        response = original(self, actor, snapshot, key)
        if snapshot.records[0].record_ref == target and not failed:
            failed.append(key)
            raise RuntimeError("SYNTHETIC commit response lost")
        return response

    def temporarily_invisible(self, operation, key):
        if operation == "snapshot.create" and key in failed and not hidden:
            hidden.append(key)
            return None
        return saved_response(self, operation, key)

    monkeypatch.setattr(EngineService, "adopt_snapshot", uncertain)
    monkeypatch.setattr(TopicService, "_saved", temporarily_invisible)
    with pytest.raises(RuntimeError):
        make().tick(now=NOW)
    with Session(make().engine.database) as session:
        pending = session.scalar(
            select(TopicEvaluationRecord.id).where(
                TopicEvaluationRecord.followup["status"].astext == "deferred"
            )
        )
    make().tick(now=NOW)
    report = make().evaluation(make().actor, identifier, pending)
    assert report.automation_status == "saved" and report.investigation_id
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    with Session(make().engine.database) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationRecord)) == 1
        assert session.scalar(select(func.count()).select_from(EngineSnapshot)) == 2
    view = InvestigationService(make().engine).get(make().actor, report.investigation_id)
    assert all(e.kind != "judgment" for e in view.entries)
    assert any(e.payload.automation and e.payload.automation.source == "topic" for e in view.entries)


def superseding(original, *, title="SYNTHETIC corrected", withdrawn=False):
    body = copy.deepcopy(original["facts"])
    body["title"] = title
    body["period_start"], body["period_end"] = "2026-08-01", "2026-08-08"
    body["status"] = "withdrawn" if withdrawn else "active"
    body["supersedes"] = {
        k: original[k] for k in ("source_id", "source_record_id", "version_id", "raw_sha256")
    }
    value = original_record("synthetic-correction", body)
    value["title"] = title
    value["parser_version"] = "datafabric.observation.projection.v0.1"
    value["published_seq"] = 2
    return value


def test_cross_native_supersedes_and_revision_before_original(runtime):
    make, source, settings = runtime
    settings.monitor_batch_records = 10
    initial = metric(1, "2026-07-01", "2026-07-08", 10)
    initial["title"] = "SYNTHETIC selected"
    source.records = [initial]
    identifier = create(make, definition(terms_all=["selected"]))
    make().tick(now=NOW)
    old = latest(make, identifier)
    correction = superseding(initial)
    source.records.append(correction)
    make().tick(now=NOW)
    fixed = latest(make, identifier)
    assert fixed.coverage.active_records == 0
    assert any(s.kind == "evidence_revision" for s in fixed.signals)
    assert make().evaluation(make().actor, identifier, old.id).coverage.active_records == 1
    # A separate new scope sees the correction before the older item in adoption order.
    view = make().get(make().actor, identifier)
    make().update(
        make().actor,
        identifier,
        TopicUpdate(is_demo=True, definition=definition(), expected_revision=view.control_revision),
        "reseed-all",
    )
    first = copy.deepcopy(correction)
    first["published_seq"] = 1
    second = copy.deepcopy(initial)
    second["published_seq"] = 2
    source.records = [first, second]
    make().tick(now=NOW)
    assert latest(make, identifier).coverage.active_records == 1
    assert len(latest(make, identifier).metric_series[0].points) == 1


def test_typed_withdrawal_is_not_trial_registration_withdrawn(runtime):
    make, source, settings = runtime
    settings.monitor_batch_records = 10
    initial = metric(1, "2026-07-01", "2026-07-08", 10)
    source.records = [initial]
    identifier = create(make)
    make().tick(now=NOW)
    correction = superseding(initial, withdrawn=True)
    trial = record(9, "SYNTHETIC registered withdrawal", status="WITHDRAWN", seq=3)
    source.records += [correction, trial]
    make().tick(now=NOW)
    report = latest(make, identifier)
    assert report.coverage.active_records == 1
    assert report.coverage.withdrawn_records == 1
    assert report.registered_states[0].status == "WITHDRAWN"


def test_original_templates_accept_typed_metrics_and_who_notice(runtime):
    make, source, settings = runtime
    settings.engine_source_id_allowlist["datafabric-test"] = [
        "external_archive",
        "who_don",
        "legacy_documents",
    ]
    service = make()
    templates = {t.id: t for t in service.templates(service.actor).items}
    assert templates["business"].definition.source_ids == ["external_archive"]
    assert templates["public_health"].definition.source_ids == ["who_don", "external_archive"]
    assert "legacy_documents" in templates["research"].definition.source_ids
    assert "legacy_documents" in templates["pipeline"].definition.source_ids
    for domain in ("business", "public_health"):
        scope = templates[domain].definition.model_dump(mode="json")
        value = metric(1, "2026-07-01", "2026-07-08", 10)
        value["source_id"] = "external_archive"
        projected = project_record(service.reader, value, as_of=NOW)
        assert project_contribution(projected, value, scope, as_of=NOW)["matched"] is True
    notice = original_record(
        "SYNTHETIC-WHO",
        {
            "notice_id": "SYNTHETIC-WHO",
            "publication_date_and_time": "2026-10-15T00:00:00Z",
            "summary": "<p>SYNTHETIC notice summary</p>",
        },
        source="who_don",
    )
    notice["parser_version"] = "datafabric.who.don.projection.v0.1"
    notice["clean_text"] = "SYNTHETIC notice summary"
    notice["source_published_at"] = {
        "value": "2026-10-15",
        "precision": "day",
        "meaning": "notice_publication",
    }
    notice["event_time"] = notice["source_published_at"]
    projected = project_record(service.reader, notice, as_of=NOW)
    assert projected.normalized_text == notice["clean_text"]
    scope = templates["public_health"].definition.model_dump(mode="json")
    value = project_contribution(projected, notice, scope, as_of=NOW)
    result = evaluate_series([value], scope, as_of=NOW, seed_complete=True, source_watermarks={"who_don": 1})
    assert any(
        s["kind"] == "source_event" and s["details"]["event_type"] == "outbreak_notice_published"
        for s in result["signals"]
    )
    assert not result["metric_series"]
    settings.engine_source_id_allowlist["datafabric-test"] = ["legacy_documents"]
    restricted = make()
    narrowed = {t.id: t for t in restricted.templates(restricted.actor).items}
    assert narrowed["business"].definition.source_ids == ["external_archive"]
    assert narrowed["public_health"].definition.source_ids == ["who_don"]
    with pytest.raises(DomainError) as failure:
        restricted.create(
            restricted.actor,
            TopicCreate(is_demo=False, definition=narrowed["business"].definition),
            "synthetic-unavailable-template-source",
        )
    assert failure.value.status_code == 403


def test_numeric_resource_limits_and_longer_than_monitor_window():
    huge = metric(1, "2026-01-01", "2026-02-01", "1e9999999")
    result = compute([contribution(huge)])
    assert "numeric_calculation_limit" in result["data_gaps"]
    assert result["metric_series"][0]["descriptive_slope"] is None
    base = contribution(dated(1, "2026-09-01", "SYNTHETIC adaptive vesicle"))
    values = []
    for i in range(2101):
        item = copy.deepcopy(base)
        item.update(
            source_record_id="SYNTHETIC-" + str(i),
            version_id=format(i, "064x"),
            unit=str(i),
            narrative_hash=str(i),
        )
        values.append(item)
    result = compute(values)
    assert result["coverage"]["active_records"] == 2101
    assert sum(b["exposures"] for b in result["buckets"]) == 2101


def test_attention_event_survives_old_milestone_preview(runtime):
    values, originals = [], []
    for i in range(201):
        body = {
            "trial_id": "NCT" + str(i).zfill(8),
            "status": "RECRUITING",
            "phase": ["PHASE1"],
            "primary_completion_date": "2026-01-01",
            "primary_completion_date_type": "ESTIMATED",
            "body": "SYNTHETIC old registered milestone " + str(i),
        }
        original = original_record(body["trial_id"], body, kind="trial")
        original["published_seq"] = i + 1
        originals.append(original)
        item = contribution(original)
        item["include_attention"] = False
        values.append(item)
    values.append(contribution(event()))
    result = compute(values)
    assert result["signals"][0]["kind"] == "source_event"
    assert result["signals"][0]["evidence_keys"][0] == result["evidence"][0]["key"]
    assert "signal_preview_limited" in result["data_gaps"]
    make, source, settings = runtime
    settings.monitor_batch_records = 50
    settings.monitor_hourly_investigations = 6
    source.records = [*originals, event(202)]
    identifier = create(make)
    for _ in range(5):
        make().tick(now=NOW)
    report = latest(make, identifier)
    assert report.automation_status == "saved"
    investigation = InvestigationService(make().engine).get(make().actor, report.investigation_id)
    materials = [e for e in investigation.entries if e.kind == "material"]
    assert materials and materials[0].payload.support.record_ref == make().reader.ref(source.records[-1])


def test_differing_daily_periods_and_denominator_do_not_create_trends():
    values = [
        metric(i + 1, start, end, count)
        for i, (start, end, count) in enumerate(
            [
                ("2026-01-01", "2026-01-02", 10),
                ("2026-01-02", "2026-01-04", 20),
                ("2026-01-04", "2026-01-08", 40),
            ]
        )
    ]
    result = compute([contribution(v) for v in values])
    assert result["metric_series"][0]["descriptive_slope"] is None
    assert not any(s["kind"] == "metric_change" for s in result["signals"])
    monthly = [
        metric(i + 1, start, end, 10 + i)
        for i, (start, end) in enumerate(
            [("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01"), ("2026-03-01", "2026-04-01")]
        )
    ]
    projected = []
    for i, original in enumerate(monthly):
        body = copy.deepcopy(original["facts"])
        body.update(
            denominator={"lower": str(100 * (i + 1)), "upper": str(100 * (i + 1))}, denominator_unit="tests"
        )
        current = original_record(original["source_record_id"], body)
        current["parser_version"] = original["parser_version"]
        projected.append(contribution(current))
    result = compute(projected)
    assert "metric_denominator_changed_requires_review" in result["data_gaps"]
    assert not any(s["kind"] == "metric_change" for s in result["signals"])


def test_citation_metadata_is_not_evidence_revision_and_revoked_lists_hide(runtime):
    make, source, _ = runtime
    body = {"body": "SYNTHETIC mechanistic observation", "doi": "SYNTHETIC-DOI", "citedByCount": 1}
    first = original_record("synthetic-paper", body)
    source.records = [first]
    identifier = create(make)
    make().tick(now=NOW)
    body["citedByCount"] = 999
    changed = original_record("synthetic-paper", body)
    changed["published_seq"] = 2
    source.records.append(changed)
    make().tick(now=NOW)
    report = latest(make, identifier)
    assert report.automation_status == "not_needed"
    assert not any(s.kind == "evidence_revision" for s in report.signals)
    body["correction_links"] = [{"type": "SYNTHETIC-CORRECTION", "id": "synthetic-notice"}]
    correction = original_record("synthetic-paper", body)
    correction["published_seq"] = 3
    source.records.append(correction)
    make().tick(now=NOW)
    assert any(s.kind == "evidence_revision" for s in latest(make, identifier).signals)
    source.enabled.clear()
    service = make()
    result = service.list(service.actor)
    assert result.items == [] and result.unavailable_count == 1
    with pytest.raises(DomainError) as failure:
        make().get(make().actor, identifier)
    assert failure.value.status_code == 403


def test_human_append_conflict_blocks_followup_not_source(runtime, monkeypatch):
    from bioradar.research.investigations.models import InvestigationEntryCreate

    make, source, settings = runtime
    settings.monitor_hourly_investigations = 6
    source.records = [record(1, "SYNTHETIC initial state")]
    identifier = create(make)
    make().tick(now=NOW)
    source.records.append(record(1, "SYNTHETIC state changed", status="TERMINATED", seq=2))
    original = InvestigationService.append
    injected = []

    def concurrent(self, actor, target, body, key, **kwargs):
        if kwargs.get("automation") is not None and not injected:
            injected.append(target)
            original(
                self,
                actor,
                target,
                InvestigationEntryCreate.model_validate(
                    {
                        "kind": "note",
                        "expected_revision": body.root.expected_revision,
                        "text": "SYNTHETIC researcher's intervening note",
                    }
                ),
                "synthetic-human",
            )
        return original(self, actor, target, body, key, **kwargs)

    monkeypatch.setattr(InvestigationService, "append", concurrent)
    make().tick(now=NOW)
    report = latest(make, identifier)
    assert report.automation_status == "blocked" and report.investigation_id
    assert make().get(make().actor, identifier).sources[0].error_code is None
    make().tick(now=NOW)
    assert latest(make, identifier).id == report.id


def test_global_pause_during_original_read_stops_adoption(runtime, monkeypatch):
    from bioradar.research.monitor.models import MonitorControl, MonitorState
    from bioradar.research.monitor.service import MonitorService

    make, source, settings = runtime
    settings.monitor_hourly_investigations = 6
    source.records = [record(1, "SYNTHETIC initial state")]
    identifier = create(make)
    make().tick(now=NOW)
    source.records.append(record(1, "SYNTHETIC state changed", status="TERMINATED", seq=2))
    read = FabricHistoryReader.read_version
    paused = []

    def pause_after_read(self, *args, **kwargs):
        result = read(self, *args, **kwargs)
        if not paused:
            paused.append(True)
            with Session(make().engine.database) as session, session.begin():
                row = session.get(MonitorState, "development", with_for_update=True)
                row.paused, row.control_revision = True, row.control_revision + 1
        return result

    monkeypatch.setattr(FabricHistoryReader, "read_version", pause_after_read)
    make().tick(now=NOW)
    with Session(make().engine.database) as session:
        assert session.scalar(select(func.count()).select_from(EngineSnapshot)) == 0
        row = session.get(MonitorState, "development")
        revision = row.control_revision
    assert latest(make, identifier).automation_status == "deferred"
    monitor = MonitorService(make().engine, make().reader)
    monitor.control(
        monitor.actor, MonitorControl(expected_revision=revision, paused=False), "synthetic-resume"
    )
    make().tick(now=NOW)
    assert latest(make, identifier).automation_status == "saved"


def test_persistent_text_baseline_then_new_material_starts_one_inquiry(runtime):
    make, source, settings = runtime
    settings.monitor_batch_records = 50
    settings.monitor_hourly_investigations = 6
    source.records = []
    number = 0
    for month, relevant in [("07", 0), ("08", 2), ("09", 4)]:
        for j in range(4):
            number += 1
            phrase = "adaptive vesicle response" if j < relevant else "optical transporter measurement"
            source.records.append(
                dated(number, f"2026-{month}-01", f"SYNTHETIC {phrase} material-{number}", seq=number)
            )
    for original in source.records:
        original["clean_text"] += " tracking"
        # Freeze the synthetic original again rather than editing its versioned text.
        original.update(
            dated(
                int(original["published_seq"]),
                original["source_published_at"]["value"],
                original["clean_text"],
                seq=original["published_seq"],
            )
        )
    identifier = create(make, definition(terms_all=["tracking"]))
    make().tick(now=NOW)
    baseline = latest(make, identifier)
    assert any(s.kind == "text_pattern" for s in baseline.signals)
    assert baseline.automation_status == "not_needed"
    make().tick(now=NOW)
    assert latest(make, identifier).id == baseline.id
    source.records.append(
        dated(13, "2026-09-15", "SYNTHETIC adaptive vesicle response tracking distinct material-13", seq=13)
    )
    source.records.append(dated(14, "2026-09-15", "SYNTHETIC outside scope", seq=14))
    settings.monitor_batch_records = 1
    make().tick(now=NOW)
    assert latest(make, identifier).automation_status == "not_needed"
    assert latest(make, identifier).coverage.seed_complete is False
    make().tick(now=NOW)
    changed = latest(make, identifier)
    assert changed.automation_status == "saved" and changed.investigation_id
    assert any(s.kind == "text_pattern" for s in changed.signals)
    runtime_service = InvestigationService(make().engine)
    view = runtime_service.get(make().actor, changed.investigation_id)
    assert not any(entry.kind == "judgment" for entry in view.entries)
    queries = [params for path, params in source.calls if path == "/api/history/records"]
    assert queries
    count = len(view.entries)
    make().tick(now=NOW)
    assert runtime_service.get(make().actor, view.id).revision == view.revision
    assert len(runtime_service.get(make().actor, view.id).entries) == count
    assert make().evaluation(make().actor, identifier, baseline.id).model_dump() == baseline.model_dump()


def test_topic_note_only_retains_whole_frozen_evaluation_permissions(runtime, monkeypatch):
    from bioradar.research.investigations.models import (
        InvestigationAutomation,
        InvestigationCreate,
        InvestigationEntryCreate,
    )

    make, source, settings = runtime
    settings.monitor_batch_records = 50
    dependencies = [f"synthetic-basis-{i}" for i in range(5)]
    settings.engine_source_id_allowlist["fixture"] = [
        *settings.engine_source_id_allowlist["fixture"],
        *dependencies,
    ]
    source.enabled.update(dependencies)
    originals = []
    for i, dep in enumerate(dependencies):
        value = metric(i + 1, "2026-09-01", "2026-10-01", i + 1)
        body = json.loads(value["raw_content"])
        body["basis"]["ref"]["source_id"] = dep
        value = original_record(value["source_record_id"], body)
        value["parser_version"] = "datafabric.observation.projection.v0.1"
        value["published_seq"] = i + 1
        originals.append(value)
    source.records = originals
    identifier = create(make)
    make().tick(now=NOW)
    evaluation = latest(make, identifier)
    service = make()
    investigations = InvestigationService(service.engine)
    create_body = InvestigationCreate(
        title="SYNTHETIC note before evidence", question="SYNTHETIC", is_demo=True
    )
    _, target = investigations.create(service.actor, create_body, "synthetic-note-only")
    note = InvestigationEntryCreate.model_validate(
        {"kind": "note", "text": evaluation.draft, "expected_revision": target["revision"]}
    )
    automation = InvestigationAutomation(source="topic", topic_id=identifier, evaluation_id=evaluation.id)
    investigations.append(service.actor, target["id"], note, "synthetic-note", automation=automation)
    assert not any(e.kind == "material" for e in investigations.get(service.actor, target["id"]).entries)
    # The fifth basis never became a material preview; the note still depends on it.
    source.enabled.remove(dependencies[-1])
    for operation in (
        lambda s: InvestigationService(s.engine).get(s.actor, target["id"]),
        lambda s: InvestigationService(s.engine).create(s.actor, create_body, "synthetic-note-only"),
        lambda s: InvestigationService(s.engine).append(
            s.actor, target["id"], note, "synthetic-note", automation=automation
        ),
    ):
        with pytest.raises(DomainError) as failure:
            operation(make())
        assert failure.value.status_code == 403
    listing = InvestigationService(make().engine).list(make().actor)
    assert listing.items == [] and listing.unavailable_count == 1


def test_followup_transient_failure_after_page_keeps_source_rotation(runtime, monkeypatch):
    make, source, settings = runtime
    settings.engine_source_id_allowlist["fixture"].append("other-source")
    source.enabled.add("other-source")
    source.records = [event()]
    other = dated(2, "2026-09-01", "SYNTHETIC other registered observation")
    other["source_id"] = "other-source"
    other["published_seq"] = 1
    source.records.append(other)
    create(make, definition(source_ids=["fixture-source", "other-source"]))

    def temporarily_unavailable(*args, **kwargs):
        raise DomainError("history_read_budget", "SYNTHETIC temporary read unavailable", 503)

    monkeypatch.setattr(TopicService, "followup", temporarily_unavailable)
    make().tick(now=NOW)
    make().tick(now=NOW)
    polled = [dict(params)["source_id"] for path, params in source.calls if path == "/api/history/changes"]
    assert polled == ["fixture-source", "other-source"]


def test_control_idempotent_reply_checks_its_old_scope_sources(runtime):
    make, source, settings = runtime
    source.records = [dated(1, "2026-09-01", "SYNTHETIC original scope")]
    identifier = create(make)
    make().tick(now=NOW)
    service = make()
    view = service.get(service.actor, identifier)
    body = TopicControl(expected_revision=view.control_revision, paused=True)
    service.control(service.actor, identifier, body, "synthetic-old-control")
    view = make().get(make().actor, identifier)
    settings.engine_source_id_allowlist["fixture"].append("other-source")
    source.enabled.add("other-source")
    make().update(
        make().actor,
        identifier,
        TopicUpdate(
            is_demo=True,
            definition=definition(source_ids=["other-source"]),
            expected_revision=view.control_revision,
        ),
        "synthetic-new-scope",
    )
    source.enabled.remove("fixture-source")
    assert make().get(make().actor, identifier).scope_revision == 2
    with pytest.raises(DomainError) as failure:
        make().control(make().actor, identifier, body, "synthetic-old-control")
    assert failure.value.status_code == 403


def test_registration_counts_deduplicate_nct_and_preserve_source_conflicts(runtime):
    make, source, settings = runtime
    settings.monitor_batch_records = 50
    source.records = [record(1, "SYNTHETIC registry report")]
    duplicate = original_record(
        "legacy:clinicaltrials:synthetic-original",
        {
            "trial_id": "NCT00000001",
            "status": "RECRUITING",
            "phase": ["PHASE1"],
            "body": "SYNTHETIC reprinted",
        },
        kind="trial",
    )
    duplicate["published_seq"] = 2
    source.records.append(duplicate)
    identifier = create(make)
    make().tick(now=NOW)
    assert latest(make, identifier).coverage.registered_status_counts == {"RECRUITING": 1}
    conflict = original_record(
        duplicate["source_record_id"],
        {
            "trial_id": "NCT00000001",
            "status": "TERMINATED",
            "phase": ["PHASE1"],
            "body": "SYNTHETIC inconsistent source",
        },
        kind="trial",
    )
    conflict["published_seq"] = 3
    source.records.append(conflict)
    make().tick(now=NOW)
    assert latest(make, identifier).coverage.registered_status_counts == {
        "conflicting_registration_status": 1
    }


def test_persistent_ledger_exceeds_compute_window_without_copying_prose(runtime, capsys):
    from bioradar.adapters.fabric_monitor import change_page
    from sqlalchemy import text

    make, source, settings = runtime
    settings.monitor_context_records = 2
    source.records = [
        dated(i, f"2026-{7 + i % 3:02d}-01", f"SYNTHETIC adaptive vesicle registration material-{i}", seq=i)
        for i in range(1, 2102)
    ]
    identifier = create(make)
    service = make()
    with Session(service.engine.database) as session:
        before = session.scalar(text("SELECT pg_total_relation_size('topic_observations'::regclass)"))
        scope_id = session.scalar(select(TopicScopeRecord.id).where(TopicScopeRecord.topic_id == identifier))
    expected = {}
    while True:
        page = change_page(
            service.reader,
            source_id="fixture-source",
            is_demo=True,
            after=expected.get("after", 0),
            cursor=expected.get("cursor"),
            minimum_after=expected.get("last_page_after", 0),
            limit=100,
        )
        assert service.apply_page(scope_id, "fixture-source", expected, page, now=NOW)
        with Session(service.engine.database) as session:
            expected = copy.deepcopy(session.get(TopicScopeRecord, scope_id).progress["fixture-source"])
        if page["next_cursor"] is None:
            break
    evaluation_id = service.evaluate(scope_id, now=NOW)
    report = make().evaluation(make().actor, identifier, evaluation_id)
    assert report.coverage.active_records == 2101
    assert sum(b.exposures for b in report.buckets) == 2101
    with Session(service.engine.database) as session:
        after = session.scalar(text("SELECT pg_total_relation_size('topic_observations'::regclass)"))
        count = session.scalar(select(func.count()).select_from(TopicObservationRecord))
        example = session.scalar(select(TopicObservationRecord.contribution).limit(1))
        assert "raw_content" not in example and "normalized_text" not in example
    print(
        json.dumps(
            {
                "fixture": "2101_short_synthetic_registrations",
                "contributions": count,
                "relation_bytes_increment": after - before,
                "note": "includes_indexes_and_TOAST_not_a_real_corpus_capacity_estimate",
            }
        )
    )
    assert after > before


def test_closed_topic_inquiry_needs_human_review_not_permanent_deferred(runtime):
    from bioradar.research.investigations.models import InvestigationEntryCreate

    make, source, settings = runtime
    settings.monitor_hourly_investigations = 6
    source.records = [record(1, "SYNTHETIC initial state")]
    identifier = create(make)
    make().tick(now=NOW)
    source.records.append(record(1, "SYNTHETIC stopped registration", status="TERMINATED", seq=2))
    make().tick(now=NOW)
    previous = latest(make, identifier)
    assert previous.automation_status == "saved"
    investigation = InvestigationService(make().engine).get(make().actor, previous.investigation_id)
    InvestigationService(make().engine).append(
        make().actor,
        investigation.id,
        InvestigationEntryCreate.model_validate(
            {
                "kind": "status",
                "expected_revision": investigation.revision,
                "status": "closed",
                "text": "SYNTHETIC researcher closes inquiry",
            }
        ),
        "synthetic-close-topic-inquiry",
    )
    source.records.append(record(1, "SYNTHETIC conflicting completion", status="COMPLETED", seq=3))
    make().tick(now=NOW)
    current = latest(make, identifier)
    assert current.automation_status == "needs_review"
    assert current.investigation_id == investigation.id
    assert InvestigationService(make().engine).get(make().actor, investigation.id).status == "closed"
