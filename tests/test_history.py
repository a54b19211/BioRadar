"""Point-in-time and actual arithmetic counterexamples; external HTTP alone is replaced."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.adapters.fabric_history import FabricHistoryReader, time_upper, validate_record
from bioradar.api.app import create_app
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.service import EngineService
from bioradar.research.history.compute import horizon_end, make_predictions, score_predictions
from bioradar.research.history.models import (
    HistoricalEvent,
    HistoricalUnit,
    HistoryObservation,
    HistoryPrediction,
    HistoryStudyCreate,
)
from bioradar.research.history.service import HistoryService
from bioradar.research.mining.models import ProviderRef
from fastapi.testclient import TestClient
from test_engine_persistence import database  # noqa: F401

pytestmark = pytest.mark.ENGINE
GRANT = "development:development:sources"


def instant(year, month=1):
    return datetime(year, month, 1, tzinfo=UTC)


def ref(name):
    return ProviderRef(
        provider="test-history",
        object_type="record",
        object_id=name,
        adopted_version=name,
        version_source="provider",
        content_hash=name,
        authorization_ref=GRANT,
    )


def unit(name, *, eligible=True, lineage=None):
    return HistoricalUnit(
        unit_id=name,
        unit_kind="program",
        lineage_id=lineage or name,
        phase="PHASE2",
        label=name,
        evidence_ref=ref(name),
        eligible=eligible,
    )


def event(name, *, observed=True, public=None, start=None, end=None):
    return HistoricalEvent(
        unit_id=name,
        unit_kind="program",
        event_type="regulatory_approval",
        public_available_at=public or instant(2017),
        event_observed=observed,
        event_at=instant(2016, 6) if observed else None,
        evidence_ref=ref(name + "-outcome"),
        observation_start=start,
        observation_end=end,
    )


def observation(cutoff, *, units=(), events=(), complete=True):
    return HistoryObservation(
        cutoff=cutoff,
        source_id="history-fixture",
        is_demo=True,
        snapshot_id=None,
        snapshot_hash=None,
        units=units,
        events=events,
        coverage_complete=complete,
        scope_description="explicit synthetic manifest",
        record_count=len(units) + len(events),
    )


def request(**changes):
    values = dict(
        title="Explicit synthetic history",
        question="Public approval within 12 months?",
        source_id="history-fixture",
        is_demo=True,
        cutoff=instant(2018),
        target="regulatory_approval",
        horizon_months=12,
        training_cutoffs=(instant(2016),),
        minimum_training_units=2,
        review_budget=1,
        followup_queries=0,
    )
    return HistoryStudyCreate(**(values | changes))


def mature_training():
    return observation(instant(2016), units=(unit("old-a"), unit("old-b"))), observation(
        instant(2018),
        events=(event("old-a"), event("old-b", observed=False, start=instant(2016), end=instant(2017))),
        complete=False,
    )


def test_disjoint_risk_set_does_not_erase_terminated_training_projects():
    cohort = observation(
        instant(2018),
        units=(unit("old-a", eligible=False), unit("old-b", eligible=False), unit("new-a"), unit("new-b")),
    )
    predictions, _ = make_predictions(request(), cohort, [mature_training()])
    assert [p.probability for p in predictions if p.unit.eligible] == [0.5, 0.5]
    assert all(p.probability is None for p in predictions if not p.unit.eligible)
    assert {r.object_id for p in predictions for r in p.training_refs} == {
        "old-a",
        "old-b",
        "old-a-outcome",
        "old-b-outcome",
    }


def test_same_at_risk_lineage_is_excluded_not_counted_twice():
    cohort = observation(instant(2018), units=(unit("new", lineage="old-a"),))
    predictions, _ = make_predictions(request(), cohort, [mature_training()])
    assert predictions[0].probability is None
    assert predictions[0].training_units == 1


def test_already_disclosed_target_cannot_stay_in_test_risk_set():
    cohort = observation(instant(2018), units=(unit("old-a"), unit("new")), events=(event("old-a"),))
    predictions, _ = make_predictions(request(), cohort, [mature_training()])
    assert predictions[1].unit.eligible is False
    assert "target_already_public_at_feature_cutoff" in predictions[1].reasons
    assert predictions[0].probability == 0.5


def test_immature_early_positive_remains_censored_and_later_scores_are_real():
    predictions, _ = make_predictions(
        request(),
        observation(instant(2018), units=(unit("a"), unit("b"), unit("c"), unit("d"))),
        [mature_training()],
    )
    early = observation(instant(2018, 7), events=(event("a", public=instant(2018, 6)),))
    outcomes, metrics = score_predictions(predictions, early, 1)
    assert {o.status for o in outcomes} == {"censored"}
    assert metrics.brier_score is None and metrics.censored == 4
    later = observation(
        instant(2019),
        events=(
            event("a", public=instant(2018, 6)),
            event("b", observed=False, public=instant(2019), start=instant(2018), end=instant(2019)),
        ),
    )
    outcomes, metrics = score_predictions(predictions, later, 1)
    assert [o.status for o in outcomes] == ["event", "no_event", "unknown", "unknown"]
    assert metrics.brier_score == 0.25 and metrics.evaluation_coverage == 0.5
    assert metrics.precision_at_budget == 1 and metrics.selected_evaluable == 1
    assert metrics.recall_at_budget is None


def test_missing_future_or_conflicting_labels_never_become_zero():
    p = HistoryPrediction(
        unit=unit("a"),
        target="regulatory_approval",
        cutoff=instant(2018),
        horizon_end=instant(2019),
        probability=0.5,
        baseline="pooled_historical_rate",
    )
    with pytest.raises(ValueError):
        event("a", observed=False, public=instant(2018, 2), start=instant(2018), end=instant(2020))
    bad_negative = event("a", observed=False, public=instant(2020), start=instant(2018), end=instant(2020))
    _, metrics = score_predictions((p,), observation(instant(2019), events=(bad_negative,)), 1)
    assert metrics.evaluable_count == 0 and metrics.unknown == 1
    outcomes, _ = score_predictions(
        (p,),
        observation(
            instant(2020),
            events=(
                event("a", public=instant(2018, 6)),
                event("a", observed=False, public=instant(2019), start=instant(2018), end=instant(2019)),
            ),
        ),
        1,
    )
    assert outcomes[0].status == "unknown"


def test_source_expansion_current_view_and_incomplete_feature_scope_are_not_predictive():
    for cohort, policy in [
        (observation(instant(2018), units=(unit("new"),), complete=False), request()),
        (observation(instant(2018), units=(unit("new"),)), request(mode="current")),
    ]:
        predictions, _ = make_predictions(policy, cohort, [mature_training()])
        assert predictions[0].probability is None
    with pytest.raises(ValueError):
        make_predictions(
            request(),
            observation(instant(2018), units=(unit("new"),)),
            [
                (
                    observation(instant(2017), units=(unit("a"),)),
                    observation(instant(2019), events=(event("a"),)),
                )
            ],
        )


def source_record(name, *, kind="program", available="2016-01-01T00:00:00Z", status="ACTIVE", extra=None):
    facts = {"record_kind": kind, "title": name}
    if kind == "program":
        facts |= {
            "program_id": name,
            "lineage_id": name,
            "indication": "synthetic-disease",
            "jurisdiction": "synthetic-region",
            "mapping_available_at": available,
            "phase": ["PHASE2"],
            "status": status,
        }
    else:
        facts |= extra or {}
    raw = json.dumps(facts, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return dict(
        schema_version="datafabric.history.record.v1",
        source_id="history-fixture",
        source_record_id=name,
        version_id=digest,
        record_kind=kind,
        is_demo=True,
        title=name,
        source_url=None,
        raw_content=raw,
        raw_media_type="application/json",
        raw_sha256=digest,
        clean_text="Explicit synthetic " + name,
        parser_version="fixture.v1",
        facts=facts,
        field_locators={k: {"json_pointer": "/" + k} for k in facts},
        event_time=None,
        source_published_at=None,
        source_updated_at=None,
        first_seen_at="2026-10-10T00:00:00Z",
        received_at="2026-10-10T00:00:00Z",
        availability_proof={
            "kind": "reviewed_archive",
            "available_at": {"value": available, "precision": "instant", "meaning": "test_archive"},
            "uri": "urn:explicit-synthetic",
            "sha256": digest,
            "verified_by": "test-admin",
            "verified_at": "2026-10-10T00:00:00Z",
        },
        mapping_review={
            "reviewed_by": "test-admin",
            "reviewed_at": "2026-10-10T00:00:00Z",
            "basis": "explicit synthetic mapping",
            "available_at": {"value": available, "precision": "instant", "meaning": "test_archive"},
        },
        published_seq=1,
    )


def collection(items, cutoff, *, complete=True):
    return dict(
        items=items,
        source_id="history-fixture",
        is_demo=True,
        cutoff=cutoff,
        excluded_counts={},
        coverage=dict(
            mode="declared_manifest",
            scope_description="synthetic manifest",
            complete=complete,
            as_of=cutoff.isoformat(),
            run_id="test-run",
            max_seq=100,
            adopted_count=len(items),
            stop_reason="end_of_declared_manifest",
        ),
    )


def reader(handler=None, **kwargs):
    return FabricHistoryReader(
        httpx.Client(
            base_url="http://fabric.test",
            transport=httpx.MockTransport(handler or (lambda req: httpx.Response(500))),
        ),
        authorization_ref=GRANT,
        **kwargs,
    )


def test_actual_source_fact_binding_rejects_forged_values_and_historical_receipt():
    original = source_record("a")
    assert (
        validate_record(original, "history-fixture", True, "public_history_reconstruction", instant(2018))
        == original
    )
    for changed in [
        dict(original, facts=original["facts"] | {"status": "APPROVED"}),
        dict(original, raw_sha256="0" * 64),
        dict(original, source_id="other"),
    ]:
        with pytest.raises(DomainError):
            validate_record(changed, "history-fixture", True, "public_history_reconstruction", instant(2018))
    with pytest.raises(DomainError):
        validate_record(original, "history-fixture", True, "operational_replay", instant(2018))


def test_mapping_proof_is_required_and_identity_conflicts_cannot_be_hidden_by_third_copy():
    a = source_record("a")
    no_review = copy.deepcopy(a)
    no_review["mapping_review"] = None
    obs = reader().observation(collection([no_review], instant(2018)))
    assert not obs.units[0].eligible
    b = copy.deepcopy(a)
    b["facts"]["status"] = "APPROVED"
    assert not reader().observation(collection([a, b, a], instant(2018))).units[0].eligible


def test_page_is_not_global_record_budget_and_repeat_cursor_or_midstream_failure_is_explicit():
    records = [source_record("p" + str(n)) for n in range(125)]
    calls = []

    def paged(req):
        calls.append(req)
        start = 100 if req.url.params.get("cursor") else 0
        return httpx.Response(
            200,
            json=dict(
                schema_version="datafabric.history.collection.v1",
                items=records[start : start + int(req.url.params["limit"])],
                next_cursor="page2" if start == 0 else "page3" if int(req.url.params["limit"]) < 25 else None,
                excluded_counts={},
                coverage=collection([], instant(2018))["coverage"],
            ),
        )

    fetched = reader(paged).collect(
        source_id="history-fixture", is_demo=True, mode="public_history_reconstruction", cutoff=instant(2018)
    )
    assert len(fetched["items"]) == 125 and len(calls) == 2 and fetched["coverage"]["complete"]
    calls.clear()
    partial = reader(paged, max_records=101).collect(
        source_id="history-fixture", is_demo=True, mode="public_history_reconstruction", cutoff=instant(2018)
    )
    assert len(partial["items"]) == 101 and partial["coverage"]["complete"] is False


def test_partial_http_after_first_page_keeps_material_but_never_complete():
    calls = []

    def broken(req):
        calls.append(req)
        if len(calls) > 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json=dict(
                schema_version="datafabric.history.collection.v1",
                items=[source_record("a")],
                next_cursor="page2",
                excluded_counts={},
                coverage=collection([], instant(2018))["coverage"],
            ),
        )

    fetched = reader(broken).collect(
        source_id="history-fixture", is_demo=True, mode="public_history_reconstruction", cutoff=instant(2018)
    )
    assert len(fetched["items"]) == 1 and fetched["coverage"]["complete"] is False
    assert "history_source_unavailable" in fetched["excluded_counts"]


def test_coarse_time_is_not_midnight_and_calendar_horizon_is_deterministic():
    assert time_upper({"value": "2016-01", "precision": "month"}).day == 31
    assert time_upper({"value": "2016-01-31", "precision": "day"}).hour == 23
    assert horizon_end(datetime(2020, 2, 29, tzinfo=UTC), 12).day == 28


@pytest.fixture
def runtime(database):  # noqa: F811
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "history_migration",
        Path(__file__).resolve().parents[1] / "db/migrations/versions/0005_historical_research.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with database.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    settings = Settings(
        environment="test",
        engine_dev_credential="history-test-credential-long-enough",
        engine_source_id_allowlist={"fixture": ["history-fixture"], "datafabric-test": ["fda_crl"]},
    )
    engine = EngineService(database, settings)
    return HistoryService(engine), DevelopmentAccess(settings).actor()


def history_source():
    old = [source_record("old-a"), source_record("old-b")]
    finished = [source_record("old-a", status="APPROVED"), source_record("old-b", status="DISCONTINUED")]
    new = [
        source_record("new-a", available="2018-01-01T00:00:00Z"),
        source_record("new-b", available="2018-01-01T00:00:00Z"),
    ]
    outcomes = [
        source_record(
            "positive-old",
            kind="outcome",
            extra=dict(
                unit_id="old-a",
                unit_kind="program",
                event_type="regulatory_approval",
                event_observed=True,
                event_at="2016-06-01T00:00:00Z",
                public_available_at="2016-06-02T00:00:00Z",
            ),
        ),
        source_record(
            "negative-old",
            kind="outcome",
            extra=dict(
                unit_id="old-b",
                unit_kind="program",
                event_type="regulatory_approval",
                event_observed=False,
                observation_start="2016-01-01T00:00:00Z",
                observation_end="2017-01-01T00:00:00Z",
                public_available_at="2017-01-01T00:00:00Z",
            ),
        ),
    ]
    future_outcomes = [
        source_record(
            "positive-new",
            kind="outcome",
            extra=dict(
                unit_id="new-a",
                unit_kind="program",
                event_type="regulatory_approval",
                event_observed=True,
                event_at="2018-06-01T00:00:00Z",
                public_available_at="2018-06-02T00:00:00Z",
            ),
        )
    ]

    class Source:
        calls = 0

        def __init__(self):
            self.helper = reader()

        def sources(self):
            return [{"source_id": "history-fixture", "read_enabled": True}]

        def collect(self, *, cutoff, **kwargs):
            self.calls += 1
            items = old if cutoff == instant(2016) else finished + new + outcomes
            if cutoff >= instant(2019):
                items += future_outcomes
            return collection(items, cutoff)

        def snapshot(self, *args, **kwargs):
            return self.helper.snapshot(*args, **kwargs)

        def observation(self, *args, **kwargs):
            return self.helper.observation(*args, **kwargs)

        def ref(self, *args):
            return self.helper.ref(*args)

    return Source()


def test_real_postgres_immutable_prediction_idempotency_and_append_only_scores(runtime):
    service, actor = runtime
    source = history_source()
    service.reader = source
    saved = service.create(actor, request(), "create-1")
    assert saved.state == "partial"  # terminal rows are visible exclusions
    assert [p.probability for p in saved.predictions if p.unit.eligible] == [0.5, 0.5]
    fixed = saved.prediction_hash
    calls = source.calls
    assert service.create(actor, request(), "create-1").id == saved.id
    assert source.calls == calls
    from bioradar.research.history.models import HistoryEvaluationCreate

    early = service.evaluate(actor, saved.id, HistoryEvaluationCreate(cutoff=instant(2018, 7)), "e1")
    later = service.evaluate(actor, saved.id, HistoryEvaluationCreate(cutoff=instant(2019)), "e2")
    assert early["id"] != later["id"]
    assert early["metrics"]["brier_score"] is None
    assert later["metrics"]["brier_score"] == 0.25
    assert service.get(actor, saved.id).prediction_hash == fixed
    assert len(service.get(actor, saved.id).evaluations) == 2
    calls = source.calls
    assert (
        service.evaluate(actor, saved.id, HistoryEvaluationCreate(cutoff=instant(2019)), "e2")["id"]
        == later["id"]
    )
    assert source.calls == calls
    assert service.get(actor, saved.id).investigation_id
    assert service.export(actor, saved.id).startswith("# Explicit synthetic history")


def test_real_http_schema_auth_no_secret_echo_and_current_upstream_permission(runtime, monkeypatch):
    service, actor = runtime
    from contextlib import contextmanager

    source = history_source()

    @contextmanager
    def connected(*args):
        yield source

    monkeypatch.setattr("bioradar.research.history.routes.history_reader", connected)
    monkeypatch.setattr(
        "bioradar.adapters.fabric_history.current_history_sources", lambda settings: source.sources()
    )
    app = create_app(service.engine.settings)
    with TestClient(app, base_url="http://localhost") as client:
        app.state.database = service.engine.database
        assert client.get("/api/engine/history/studies").status_code == 401
        headers = {
            "Authorization": "Bearer history-test-credential-long-enough",
            "Idempotency-Key": str(uuid4()),
        }
        response = client.post(
            "/api/engine/history/studies", json=request().model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 201, response.text
        saved = response.json()

        assert client.get("/api/engine/history/studies/" + saved["id"], headers=headers).status_code == 200
        assert (
            client.get("/api/engine/history/studies/" + saved["id"] + "/export", headers=headers).status_code
            == 200
        )
        version = saved["predictions"][0]["unit"]["evidence_ref"]["adopted_version"]
        original = client.get(
            "/api/engine/history/studies/" + saved["id"] + "/evidence/" + version, headers=headers
        )
        assert original.status_code == 200 and original.json()["raw_content"]
        assert (
            client.get(
                "/api/engine/history/studies/" + saved["id"] + "/evidence/" + "0" * 64, headers=headers
            ).status_code
            == 404
        )
        snapshot_id = saved["sources"][0]["snapshot_id"]
        investigation_id = saved["investigation_id"]
        assert client.get("/api/engine/snapshots/" + snapshot_id, headers=headers).status_code == 200
        assert (
            client.get("/api/engine/investigations/" + investigation_id, headers=headers).status_code == 200
        )
        monkeypatch.setattr(source, "sources", lambda: [])
        for path in (
            "/api/engine/history/studies/" + saved["id"],
            "/api/engine/history/studies/" + saved["id"] + "/export",
            "/api/engine/history/studies/" + saved["id"] + "/evidence/" + version,
            "/api/engine/snapshots/" + snapshot_id,
            "/api/engine/investigations/" + investigation_id,
        ):
            denied = client.get(path, headers=headers)
            assert denied.status_code == 403 and "raw_content" not in denied.text

        def stopped(*args):
            raise DomainError("source_unavailable", "permission check unavailable", 503)

        monkeypatch.setattr("bioradar.research.history.routes.history_reader", stopped)
        assert client.get("/api/engine/history/studies/" + saved["id"], headers=headers).status_code == 503
        # The list contains only user-authored metadata, not restricted source fields.
        listing = client.get("/api/engine/history/studies", headers=headers)
        assert listing.status_code == 200 and "raw_content" not in listing.text
        response = client.post(
            "/api/engine/history/studies", json={"question": "secret-not-returned"}, headers=headers
        )
        assert response.status_code == 422 and "secret-not-returned" not in response.text


def test_unknown_risk_status_and_future_mapping_are_not_active_programs():
    original = source_record("a")
    original["facts"]["status"] = None
    assert not reader().observation(collection([original], instant(2018))).units[0].eligible
    changed = source_record("b")
    changed["mapping_review"]["available_at"]["value"] = "2020-01-01T00:00:00Z"
    assert not reader().observation(collection([changed], instant(2018))).units[0].eligible


def test_late_training_label_and_incomplete_training_features_do_not_enter_rate():
    features, labels = mature_training()
    labels = labels.model_copy(
        update={
            "events": (
                event("old-a", public=instant(2019)),
                event("old-b", observed=False, start=instant(2016), end=instant(2017)),
            )
        }
    )
    predictions, _ = make_predictions(
        request(), observation(instant(2018), units=(unit("new"),)), [(features, labels)]
    )
    assert predictions[0].probability is None and predictions[0].training_units == 1
    predictions, gaps = make_predictions(
        request(),
        observation(instant(2018), units=(unit("new"),)),
        [(features.model_copy(update={"coverage_complete": False}), labels)],
    )
    assert predictions[0].probability is None and "incomplete_training_scope_excluded" in gaps


def test_fact_summary_preserves_actual_types_dates_links_and_quoted_field_locator():
    from bioradar.research.history.facts import summarize_records

    record = source_record(
        "explicit-synthetic-letter",
        kind="document",
        extra={
            "letter_type": "APPROVAL",
            "letter_date": "01/01/2016",
            "application_numbers": ["NDA-SYNTHETIC"],
            "text": "Synthetic original letter content.",
        },
    )
    record["source_id"] = "fda_crl"
    summary = summarize_records([record, record])
    assert summary["displayed_versions"] == 1
    row = summary["rows"][0]
    assert row["fields"]["letter_type"]["value"] == "APPROVAL"
    assert row["fields"]["application_numbers"]["value"] == ["NDA-SYNTHETIC"]
    assert row["source_field_excerpt"]["text"] == "Synthetic original letter content."
    assert row["source_field_excerpt"]["locator"] == {"json_pointer": "/text"}
    assert row["version_id"] == record["version_id"]
    assert "CRL不等于永久失败" in row["boundary"]


def test_reviewed_trial_lineage_is_used_and_coarse_dates_remain_conservative():
    from bioradar.adapters.fabric_history import fact_time

    original = source_record("trial-source")
    original["record_kind"] = "trial"
    original["facts"]["trial_id"] = "T1"
    original["facts"]["lineage_id"] = "known-L"
    original["facts"]["mapping_available_at"] = "2015-12-31"
    one = reader().observation(collection([original], instant(2018))).units[0]
    assert one.eligible and one.lineage_id == "known-L"
    assert fact_time("2015-12-31").hour == 23
    assert fact_time("2017-01-01", lower=True).hour == 0
    with pytest.raises(ValueError):
        time_upper("2016-01-01")  # server receipt still requires an aware instant


def test_many_predictions_keep_full_training_counts_without_squared_reference_output():
    train_units = tuple(unit("train-" + str(i)) for i in range(500))
    test_units = tuple(unit("test-" + str(i)) for i in range(500))
    labels = tuple(
        event(u.unit_id, observed=False, start=instant(2016), end=instant(2017)) for u in train_units
    )
    predictions, _ = make_predictions(
        request(),
        observation(instant(2018), units=test_units),
        [(observation(instant(2016), units=train_units), observation(instant(2018), events=labels))],
    )
    assert all(p.training_units == 500 and p.probability == 0 for p in predictions)
    assert all(p.training_refs_truncated and len(p.training_refs) == 8 for p in predictions)
    assert sum(len(p.training_refs) for p in predictions) == 4000


def test_rolling_landmarks_share_one_label_read_without_sharing_future_features(runtime):
    service, actor = runtime
    source = history_source()
    service.reader = source
    saved = service.create(actor, request(training_cutoffs=(instant(2015), instant(2016))), "rolling")
    assert source.calls == 4  # cohort + one qualified label view + two distinct feature cutoffs
    assert len(saved.training) == 2
    assert all(t["label_cutoff"].startswith("2018") for t in saved.training)


def test_missing_or_non_boolean_current_read_grant_never_authorizes_cached_source():
    for flag in ({}, {"read_enabled": None}, {"read_enabled": "true"}, {"read_enabled": False}):
        engine = EngineService(None, Settings())
        engine.history_source_reader = lambda f=flag: [{"source_id": "history-fixture", **f}]
        with pytest.raises(DomainError) as failure:
            engine.authorize_history_source("history-fixture")
        assert failure.value.status_code == 403
    engine = EngineService(None, Settings())
    engine.history_source_reader = lambda: [{"source_id": "history-fixture", "read_enabled": True}]
    engine.authorize_history_source("history-fixture")
