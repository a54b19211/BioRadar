"""SYNTHETIC frozen monitor receipts -> real isolated PG topic state; no model requests."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from bioradar.research.mining.models import canonical_hash
from bioradar.research.monitor.models import MonitorState
from bioradar.research.monitor.service import MonitorService
from bioradar.topics.models import TopicEvaluationRecord, TopicObservationRecord, TopicScopeRecord
from bioradar.topics.service import TopicService
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_topics import create, dated, definition
from test_topics import runtime as runtime  # noqa: F401

pytestmark = pytest.mark.ENGINE
DAY = datetime(2021, 4, 2, tzinfo=UTC)
MODE = "public_history_reconstruction"


def original(number=1, *, available=DAY, changed=False):
    text = "SYNTHETIC 北江医院收治7例不明原因肺炎。"
    if changed:
        text += "SYNTHETIC 当前记录撤回，待核原件。"
    row = dated(number, available.date().isoformat(), text)
    row["availability_proof"] = {
        "available_at": {"value": available.isoformat(), "precision": "instant"},
        "verified_by": "SYNTHETIC archive review",
        "verified_at": "2026-10-01T00:00:00Z",
        "uri": "https://example.invalid/SYNTHETIC-proof",
        "sha256": "b" * 64,
    }
    return row


def freeze_day(
    service, items, cutoff, *, complete=True, ambiguous=(), missing=False, previous=(), retained=()
):
    source = "fixture-source"
    wrapper = {
        "receipt_id": "SYNTHETIC-" + canonical_hash([cutoff.isoformat(), [r["version_id"] for r in items]]),
        "collection": {
            "source_id": source,
            "is_demo": True,
            "cutoff": cutoff.isoformat(),
            "items": copy.deepcopy(items),
            "coverage": {"complete": complete, "scope_description": "SYNTHETIC"},
            "excluded_counts": {},
        },
        "selected_refs": [service.reader.ref(row).model_dump(mode="json") for row in [*items, *retained]],
        "ambiguous_refs": [service.reader.ref(row).model_dump(mode="json") for row in ambiguous],
    }
    with Session(service.engine.database) as session, session.begin():
        state = session.get(MonitorState, service.actor.workspace_id, with_for_update=True)
        if state is None:
            state = MonitorState(workspace_id=service.actor.workspace_id, source_progress={})
            session.add(state)
        progress = copy.deepcopy(state.source_progress)
        progress["_history_replay"] = {
            "mode": MODE,
            "last_committed_cutoff": None,
            "pending_day": {
                "mode": MODE,
                "cutoff": cutoff.isoformat(),
                "scope_hash": "c" * 64,
                "status": "topics",
                "collections": {} if missing else {source: wrapper},
                "batch_ids": [],
                "run_ids": [],
                "topic_receipts": {},
                "previous_records": {source: copy.deepcopy(list(previous))},
            },
        }
        state.source_progress = progress


def setup_topic(runtime):
    make, source, _ = runtime
    identifier = create(make, definition(domain="public_health", bucket="day", lookback_buckets=12))
    service = make()
    source.records = [original()]
    return make, source, identifier, service


def test_missing_authority_collection_waits_without_advancing_topic(runtime):
    _, _, identifier, service = setup_topic(runtime)
    freeze_day(service, [], DAY, missing=True)
    result = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert result["status"] == "awaiting_collection"
    assert service.get(service.actor, identifier).observation_revision == 0


def test_empty_day_is_a_new_evaluation_with_zero_followup_dispatch_and_stable_retry(runtime, monkeypatch):
    make, source, identifier, service = setup_topic(runtime)
    calls = []

    def saved(self, evaluation_id, monitor):
        calls.append(evaluation_id)
        with Session(self.engine.database) as session:
            state = copy.deepcopy(session.get(TopicEvaluationRecord, evaluation_id).followup)
        state["status"] = "saved"
        self._save_followup(evaluation_id, state)

    monkeypatch.setattr(TopicService, "followup", saved)
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    service._active_monitor = monitor
    freeze_day(service, source.records, DAY)
    first = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert first["status"] == "completed"
    calls.clear()
    second_day = DAY + timedelta(days=1)
    freeze_day(service, source.records, second_day)
    second = service.advance_history_day(identifier, mode=MODE, cutoff=second_day)
    assert second["status"] == "completed" and second["evaluation_id"] != first["evaluation_id"]
    assert calls == []
    duplicate = service.advance_history_day(identifier, mode=MODE, cutoff=second_day)
    assert duplicate == second
    report = service.evaluation(service.actor, identifier, second["evaluation_id"])
    assert report.evidence_time == {"mode": MODE, "cutoff": second_day.isoformat()}
    assert report.coverage.scope == "verified_historical_availability"
    assert report.coverage.source_watermarks == {}
    assert report.created_at.year == datetime.now(UTC).year and report.as_of == second_day
    with Session(service.engine.database) as session:
        assert session.scalar(select(func.count()).select_from(TopicEvaluationRecord)) == 2
        authority = session.get(MonitorState, service.actor.workspace_id).source_progress["_history_replay"]
        assert authority["last_committed_cutoff"] is None  # Topic never advances monitor authority.
    monitor.release()
    assert make().tick()["state"] == "history_step_required"  # No later current-stream read of this topic.


@pytest.mark.parametrize("partial,ambiguous", [(True, False), (False, True)])
def test_partial_or_ambiguous_new_version_keeps_previous_selected_fact(
    runtime, monkeypatch, partial, ambiguous
):
    _, source, identifier, service = setup_topic(runtime)

    def saved(self, evaluation_id, monitor):
        with Session(self.engine.database) as session:
            state = copy.deepcopy(session.get(TopicEvaluationRecord, evaluation_id).followup)
        state["status"] = "saved"
        self._save_followup(evaluation_id, state)

    monkeypatch.setattr(TopicService, "followup", saved)
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    service._active_monitor = monitor
    freeze_day(service, source.records, DAY)
    first = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert first.get("evaluation_id")
    newer = original(available=DAY if ambiguous else DAY + timedelta(days=1), changed=True)
    freeze_day(
        service,
        [newer],
        DAY + timedelta(days=1),
        complete=not partial,
        ambiguous=[newer] if ambiguous else [],
    )
    result = service.advance_history_day(identifier, mode=MODE, cutoff=DAY + timedelta(days=1))
    assert result["gaps"] and result["evaluation_id"] != first["evaluation_id"]
    with Session(service.engine.database) as session:
        scope = session.scalar(select(TopicScopeRecord).where(TopicScopeRecord.topic_id == identifier))
        selected = list(
            session.scalars(
                select(TopicObservationRecord).where(
                    TopicObservationRecord.scope_id == scope.id,
                    TopicObservationRecord.selected_from.is_not(None),
                    TopicObservationRecord.selected_to.is_(None),
                )
            )
        )
        assert [row.version_id for row in selected] == [source.records[0]["version_id"]]
    monitor.release()


def test_requested_day_cannot_override_monitor_frozen_cutoff(runtime):
    _, source, identifier, service = setup_topic(runtime)
    freeze_day(service, source.records, DAY)
    result = service.advance_history_day(identifier, mode=MODE, cutoff=DAY + timedelta(days=1))
    assert result["status"] == "blocked"
    assert service.get(service.actor, identifier).observation_revision == 0


def test_waiting_day_reuses_one_evaluation_and_does_not_advance_authority(runtime):
    _, source, identifier, service = setup_topic(runtime)
    freeze_day(service, source.records, DAY)
    first = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert first["status"] == "waiting" and first["evaluation_id"]
    repeated = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert repeated == first
    with Session(service.engine.database) as session:
        assert session.scalar(select(func.count()).select_from(TopicEvaluationRecord)) == 1
    freeze_day(service, source.records, DAY + timedelta(days=1))
    blocked = service.advance_history_day(identifier, mode=MODE, cutoff=DAY + timedelta(days=1))
    assert blocked["status"] == "blocked" and "history_previous_topic_day_unfinished" in blocked["gaps"]


def test_native_followup_reads_fixed_historical_scope_and_never_creates_model_analysis(runtime):
    _, source, identifier, service = setup_topic(runtime)
    service.engine.settings.topic_hourly_investigations = 2
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    service._active_monitor = monitor
    freeze_day(service, source.records, DAY)
    result = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert result["status"] == "completed" and "analysis_id" not in result
    read_calls = [
        params
        for path, params in source.calls
        if path.startswith("/api/history/versions/") or path == "/api/history/records"
    ]
    assert read_calls
    for pairs in read_calls:
        params = dict(pairs)
        assert params["mode"] == MODE and datetime.fromisoformat(params["cutoff"]) == DAY
    assert not any(path in {"/api/history/changes", "/api/history/fetches"} for path, _ in source.calls)
    report = service.evaluation(service.actor, identifier, result["evaluation_id"])
    assert report.investigation_id and report.automation_status == "saved"
    monitor.release()


def test_new_calendar_milestone_keeps_rule_needed_but_empty_day_dispatch_is_zero(runtime, monkeypatch):
    from test_graph import original_record

    make, source, _ = runtime
    identifier = create(make, definition(domain="public_health", bucket="day", lookback_buckets=12))
    service = make()
    row = original_record(
        "SYNTHETIC-calendar-milestone",
        {
            "trial_id": "NCT00000001",
            "status": "RECRUITING",
            "phase": ["PHASE1"],
            "summary": "SYNTHETIC 北江医院收治7例不明原因肺炎。",
            "primary_completion_date": "2021-04-03",
            "primary_completion_date_type": "ESTIMATED",
        },
        kind="trial",
        demo=True,
    )
    row["published_seq"] = 1
    row["source_published_at"] = {"value": "2021-04-02", "precision": "day", "meaning": "SYNTHETIC"}
    row["availability_proof"] = original()["availability_proof"]
    source.records = [row]
    calls = []

    def saved(self, evaluation_id, monitor):
        calls.append(evaluation_id)
        with Session(self.engine.database) as session:
            state = copy.deepcopy(session.get(TopicEvaluationRecord, evaluation_id).followup)
        state["status"] = "saved"
        self._save_followup(evaluation_id, state)

    monkeypatch.setattr(TopicService, "followup", saved)
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    service._active_monitor = monitor
    for days in (0, 1, 2):
        cutoff = DAY + timedelta(days=days)
        freeze_day(service, source.records, cutoff)
        result = service.advance_history_day(identifier, mode=MODE, cutoff=cutoff)
        assert result["status"] == "completed"
        if days == 0:
            assert calls
            calls.clear()
    monitor.release()
    report = service.evaluation(service.actor, identifier, result["evaluation_id"])
    assert any(signal.kind == "milestone_check" for signal in report.signals)
    assert report.tracking["model_update"]["needed"] is True  # Existing decision rule stays intact.
    assert report.tracking["model_dispatch"] == {
        "status": "not_dispatched",
        "reason": "deferred_no_new_evidence",
        "policy": service.engine.settings.history_replay_dispatch_policy,
    }
    assert report.automation_status == "deferred" and result["status"] == "completed"
    assert result["evaluation_status"] == "completed" and result["assessment_status"] == "deferred"
    from bioradar.research.investigations.analysis_service import AnalysisService

    public_policy = AnalysisService(service.engine, service.reader).options(service.actor, is_demo=True)[
        "runtime"
    ]["history_replay_dispatch_policy"]
    assert public_policy == result["dispatch_policy"] == report.tracking["model_dispatch"]["policy"]
    assert result["model_dispatch"] == report.tracking["model_dispatch"]
    assert report.tracking["risk"]["status"] == "unresolved"
    with Session(service.engine.database) as session:
        pending = session.get(TopicEvaluationRecord, report.id)
        scope_row = session.get(TopicScopeRecord, pending.scope_id)
        assert any(
            signal.key not in scope_row.progress.get("_notified_signals", [])
            for signal in report.signals
            if signal.kind == "milestone_check"
        )
    assert calls == []


def test_history_analysis_intent_binds_declared_sources_and_server_time_before_admission(
    runtime, monkeypatch
):
    from bioradar.research.investigations import analysis_service

    _, source, identifier, service = setup_topic(runtime)
    freeze_day(service, source.records, DAY)
    waiting = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert waiting["status"] == "waiting"
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    monkeypatch.setattr(analysis_service, "semantic_enabled", lambda settings: True)
    service.engine.settings.topic_hourly_investigations = 2
    captured = []

    def stop_before_admission(self, actor, inquiry, body, key, **kwargs):
        assert body.source_ids == ["fixture-source"]
        context = kwargs["historical_context"]
        assert context["mode"] == MODE and datetime.fromisoformat(context["cutoff"]) == DAY
        assert context["history_scope_hash"] == "c" * 64 and context["history_series_id"]
        assert body.historical_scope is None  # One server-owned time input, not two conflicting inputs.
        captured.append(key)
        raise RuntimeError("SYNTHETIC stop before analysis admission; no model request")

    monkeypatch.setattr(analysis_service.AnalysisService, "start", stop_before_admission)
    with pytest.raises(RuntimeError, match="SYNTHETIC stop before analysis admission"):
        service.followup(waiting["evaluation_id"], monitor)
    assert len(captured) == 1
    monitor.release()


def test_partial_then_empty_day_resolve_exact_frozen_previous_original_and_reject_unknown_ref(runtime):
    _, source, identifier, service = setup_topic(runtime)
    service.engine.settings.topic_hourly_investigations = 2
    monitor = MonitorService(service.engine, service.reader)
    assert monitor.claim()
    service._active_monitor = monitor
    old = source.records[0]
    freeze_day(service, [old], DAY)
    first = service.advance_history_day(identifier, mode=MODE, cutoff=DAY)
    assert first["status"] == "completed"
    for offset, complete in ((1, False), (2, True)):
        cutoff = DAY + timedelta(days=offset)
        freeze_day(service, [], cutoff, complete=complete, previous=[old], retained=[old])
        result = service.advance_history_day(identifier, mode=MODE, cutoff=cutoff)
        assert result["status"] == "completed" and result["assessment_status"] == "deferred"
        assert result["model_dispatch"]["status"] == "not_dispatched"
        report = service.evaluation(service.actor, identifier, result["evaluation_id"])
        assert report.coverage.active_records == 1
    unknown = original(2, available=DAY + timedelta(days=3))
    freeze_day(service, [], DAY + timedelta(days=3), previous=[old], retained=[unknown])
    missing = service.advance_history_day(identifier, mode=MODE, cutoff=DAY + timedelta(days=3))
    assert missing["status"] == "awaiting_collection" and missing["gaps"] == [
        "history_selected_original_missing"
    ]
    monitor.release()


def test_current_tick_cannot_poll_an_unbound_topic_in_history_workspace(runtime, monkeypatch):
    _, source, identifier, service = setup_topic(runtime)
    freeze_day(service, source.records, DAY)
    assert service.get(service.actor, identifier).evidence_time is None
    source.calls.clear()

    def no_current_fetch(*args, **kwargs):
        pytest.fail("SYNTHETIC current fetch must not run in an authoritative history workspace")

    monkeypatch.setattr(service, "_fetch_source", no_current_fetch)
    assert service.tick()["state"] == "history_step_required"
    assert source.calls == []
    assert service.get(service.actor, identifier).observation_revision == 0
