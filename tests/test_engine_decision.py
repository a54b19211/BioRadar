"""Decision runtime checks use real PostgreSQL and marked synthetic decision inputs."""

import copy
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bioradar.api.app import create_app
from bioradar.api.errors import DomainError
from bioradar.research.decision.models import (
    DecisionBrief,
    DecisionInput,
    DecisionOption,
    DecisionPolicy,
    DecisionScenario,
    HardConstraint,
    InputBasis,
    MetricSpec,
    NumericInterval,
    OutcomeCell,
)
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.models import EngineRun
from bioradar.research.engine.schemas import DecisionRunCreate, RunAction
from bioradar.research.engine.service import EngineService
from bioradar.research.mining.models import canonical_hash
from bioradar.research.mining.prepare import snapshot_hash
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_engine_persistence import CREDENTIAL, ROOT, accept, controlled_result, tokens
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import service as service  # noqa: F401
from test_engine_persistence import settings as settings  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def decision():
    now = datetime.now(UTC) - timedelta(seconds=1)
    start, end = (now + timedelta(days=1)).date(), (now + timedelta(days=365)).date()
    basis = InputBasis(
        kind="declared_assumption",
        claimed_author="synthetic engineering fixture author",
        authored_at=now - timedelta(days=1),
        rationale="Synthetic values for runtime checks; no real policy or business benefit claimed.",
    )
    metrics = tuple(
        MetricSpec(
            metric_id=identifier,
            label=identifier,
            direction=direction,
            unit=unit,
            definition="explicitly synthetic " + identifier,
            population="synthetic test cohort",
            geography="synthetic test region",
            jurisdiction="synthetic test jurisdiction",
            period_start=start,
            period_end=end,
            comparability_basis="Same synthetic unit and cohort, solely for engineering checks.",
        )
        for identifier, direction, unit in (
            ("cost", "minimize", "test-unit"),
            ("capacity", "maximize", "test-slot"),
        )
    )
    return DecisionInput(
        input_id="runtime-decision-fixture",
        is_demo=True,
        brief=DecisionBrief(
            title="Synthetic decision runtime case",
            audience="university",
            decision_question="Can the marked synthetic alternatives be compared and replayed?",
            mandate="Engineering fixture only; no actual decision authority.",
            as_of=now,
            decision_deadline=now + timedelta(days=30),
            horizon_start=start,
            horizon_end=end,
            baseline_option_id="baseline",
            scope_description="Two synthetic alternatives; absent facts remain absent.",
        ),
        metrics=metrics,
        options=tuple(
            DecisionOption(option_id=identifier, label=identifier, description="synthetic option")
            for identifier in ("baseline", "alternative")
        ),
        scenarios=(
            DecisionScenario(
                scenario_id="declared", label="declared", description="synthetic scenario", basis=basis
            ),
        ),
        outcomes=tuple(
            OutcomeCell(
                option_id=option,
                scenario_id="declared",
                metric_id=metric,
                value=NumericInterval(lower=value, upper=value),
                basis=basis,
            )
            for option, metric, value in (
                ("baseline", "cost", "8"),
                ("baseline", "capacity", "8"),
                ("alternative", "cost", "9"),
                ("alternative", "capacity", "12"),
            )
        ),
        constraints=(
            HardConstraint(
                constraint_id="budget",
                label="synthetic budget",
                metric_id="cost",
                operator="le",
                threshold=NumericInterval(lower="10", upper="10"),
                basis=basis,
            ),
        ),
    )


def decision_policy():
    return DecisionPolicy(pareto_metric_ids=("cost", "capacity"), regret_metric_id="capacity")


def accepted(service, settings, decision, *, key="decision", snapshot_ids=()):
    actor = DevelopmentAccess(settings).actor()
    code, run = service.decisions().create(
        actor,
        DecisionRunCreate(snapshot_ids=list(snapshot_ids), input=decision, policy=decision_policy()),
        key,
    )
    assert code == 202
    return actor, run


def result_for(decision, snapshots=()):
    from bioradar.research.decision.runner import evaluate_decision

    return evaluate_decision(decision, decision_policy(), snapshots)


def with_source(decision, snapshot):
    from bioradar.research.decision.models import DecisionEvidenceLink

    basis = decision.scenarios[0].basis.model_copy(
        update={
            "evidence": (
                DecisionEvidenceLink(
                    snapshot_hash=snapshot_hash(snapshot),
                    record_ref=snapshot.records[0].record_ref,
                    quote="Targeted immune therapy",
                ),
            )
        }
    )
    return decision.model_copy(
        update={
            "evidence_snapshot_hashes": (snapshot_hash(snapshot),),
            "scenarios": (decision.scenarios[0].model_copy(update={"basis": basis}),),
        }
    )


def test_decision_shares_run_authority_has_server_receipt_and_persistent_idempotency(
    service, settings, decision, database
):
    actor, run = accepted(service, settings, decision)
    view = service.decisions().get(actor, run["id"])
    assert view.submitted_by == "development-user" and view.accepted_at.tzinfo is not None
    assert view.original_input == decision.model_dump(mode="json")
    body = DecisionRunCreate(input=decision, policy=decision_policy())
    assert view.submitted_request_hash == canonical_hash(body)
    assert accepted(service, settings, decision)[1] == run
    relabeled = decision.model_copy(update={"input_id": "another-label"})
    assert accepted(service, settings, relabeled, key="new-http-key")[1]["id"] == run["id"]
    changed = decision.model_copy(update={"brief": decision.brief.model_copy(update={"audience": "central"})})
    with pytest.raises(DomainError) as conflict:
        accepted(service, settings, changed)
    assert conflict.value.code == "idempotency_conflict"
    with Session(database) as session:
        row = session.get(EngineRun, run["id"])
        assert row.run_kind == "decision" and row.snapshot_id is None
        assert session.scalar(select(func.count()).select_from(EngineRun)) == 1


def test_old_mining_routes_and_decision_lists_are_isolated_and_lists_hide_inputs(
    service, settings, snapshot, decision
):
    actor, _, mining = accept(service, settings, snapshot)
    _, run = accepted(service, settings, decision)
    assert [item.id for item in service.list_runs(actor).items] == [mining["id"]]
    decisions = service.decisions().list(actor).items
    assert [item.id for item in decisions] == [run["id"]]
    assert (
        decisions[0].decision_input is None
        and decisions[0].original_input is None
        and decisions[0].result is None
    )
    assert "Synthetic values" not in decisions[0].model_dump_json()
    with pytest.raises(DomainError) as wrong_kind:
        service.get_run(actor, run["id"])
    assert wrong_kind.value.status_code == 404
    with pytest.raises(DomainError):
        service.decisions().get(actor, mining["id"])


def test_four_audiences_keep_one_fact_basis_and_changed_assumptions_get_new_run(service, settings, decision):
    runs = []
    for audience in ("central", "provincial", "university", "enterprise"):
        current = decision.model_copy(
            update={"brief": decision.brief.model_copy(update={"audience": audience})}
        )
        _, run = accepted(service, settings, current, key=audience)
        runs.append(run)
    assert len({run["evidence_basis_hash"] for run in runs}) == 1
    assert len({run["input_hash"] for run in runs}) == 4
    changed_cell = decision.outcomes[-1].model_copy(update={"value": NumericInterval(lower="13", upper="13")})
    changed = decision.model_copy(update={"outcomes": (*decision.outcomes[:-1], changed_cell)})
    _, new = accepted(service, settings, changed, key="new-assumption")
    assert new["input_hash"] not in {run["input_hash"] for run in runs}
    assert new["evidence_basis_hash"] == runs[0]["evidence_basis_hash"]


def test_future_evidence_cutoff_and_claimed_authorship_do_not_become_current_facts(
    service, settings, decision
):
    future = datetime.now(UTC) + timedelta(days=365)
    changed = decision.model_copy(
        update={
            "brief": decision.brief.model_copy(
                update={"as_of": future, "decision_deadline": future + timedelta(days=1)}
            )
        }
    )
    with pytest.raises(DomainError) as cutoff:
        accepted(service, settings, changed)
    assert cutoff.value.code == "decision_future_cutoff"
    scenario = decision.scenarios[0].model_copy(
        update={"basis": decision.scenarios[0].basis.model_copy(update={"authored_at": future})}
    )
    with pytest.raises(DomainError) as declaration:
        accepted(service, settings, decision.model_copy(update={"scenarios": (scenario,)}))
    assert declaration.value.code == "decision_future_declaration"


@pytest.mark.parametrize("tamper", ["quote", "ref", "path"])
def test_frozen_source_evidence_tampering_is_rejected_before_pg_acceptance(
    service, settings, snapshot, decision, database, tamper
):
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, snapshot, "source")
    sourced = with_source(decision, snapshot)
    link = sourced.scenarios[0].basis.evidence[0]
    if tamper == "quote":
        link = link.model_copy(update={"quote": "invented source quotation"})
    elif tamper == "ref":
        link = link.model_copy(
            update={"record_ref": link.record_ref.model_copy(update={"adopted_version": "other"})}
        )
    else:
        link = link.model_copy(update={"field_path": "/raw_password"})
    basis = sourced.scenarios[0].basis.model_copy(update={"evidence": (link,)})
    sourced = sourced.model_copy(
        update={"scenarios": (sourced.scenarios[0].model_copy(update={"basis": basis}),)}
    )
    with pytest.raises(DomainError) as invalid:
        accepted(service, settings, sourced, snapshot_ids=(adopted["id"],))
    assert invalid.value.code == "decision_evidence_invalid"
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(EngineRun)) == 0


def test_source_fixture_cannot_be_washed_into_real_decision_input(service, settings, snapshot, decision):
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, snapshot, "source")
    sourced = with_source(decision, snapshot).model_copy(update={"is_demo": False})
    with pytest.raises(DomainError) as invalid:
        accepted(service, settings, sourced, snapshot_ids=(adopted["id"],))
    assert invalid.value.code == "fixture_scope_mismatch"


def test_decision_source_revocation_hides_old_result_and_metadata(
    service, settings, snapshot, decision, database
):
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, snapshot, "source")
    sourced = with_source(decision, snapshot)
    _, run = accepted(service, settings, sourced, snapshot_ids=(adopted["id"],))
    configured = settings.model_copy(update={"engine_source_allowlist": ["datafabric-test"]})
    revoked = EngineService(database, configured)
    principal = DevelopmentAccess(configured).actor()
    with pytest.raises(DomainError) as missing:
        revoked.decisions().get(principal, run["id"])
    assert missing.value.status_code == 404
    assert revoked.decisions().list(principal).items == []
    with pytest.raises(DomainError):
        revoked.decisions().create(
            principal,
            DecisionRunCreate(snapshot_ids=[adopted["id"]], input=sourced, policy=decision_policy()),
            "decision",
        )


def test_revoking_one_of_multiple_source_sites_hides_entire_decision(settings, snapshot, decision, database):
    configured = settings.model_copy(
        update={"engine_source_id_allowlist": {"fixture": ["fixture-source", "second-source"]}}
    )
    engine = EngineService(database, configured)
    actor = DevelopmentAccess(configured).actor()
    _, first = engine.adopt_snapshot(actor, snapshot, "first")
    records = []
    for record in snapshot.records:
        reference = record.record_ref.model_copy(
            update={"object_id": "second-" + record.record_ref.object_id}
        )
        records.append(
            record.model_copy(
                update={"record_ref": reference, "evidence_refs": (reference,), "source_id": "second-source"}
            )
        )
    second_snapshot = snapshot.model_copy(
        update={
            "records": tuple(records),
            "coverage": (snapshot.coverage[0].model_copy(update={"source_id": "second-source"}),),
        }
    )
    _, second = engine.adopt_snapshot(actor, second_snapshot, "second")
    sourced = with_source(decision, snapshot).model_copy(
        update={"evidence_snapshot_hashes": (snapshot_hash(snapshot), snapshot_hash(second_snapshot))}
    )
    _, run = accepted(engine, configured, sourced, snapshot_ids=(first["id"], second["id"]))
    assert len(engine.decisions().get(actor, run["id"]).source_snapshots) == 2
    narrow = configured.model_copy(update={"engine_source_id_allowlist": {"fixture": ["fixture-source"]}})
    revoked = EngineService(database, narrow)
    principal = DevelopmentAccess(narrow).actor()
    assert revoked.get_snapshot(principal, first["id"])
    with pytest.raises(DomainError) as unavailable:
        revoked.decisions().get(principal, run["id"])
    assert unavailable.value.status_code == 404
    assert revoked.decisions().list(principal).items == []


def test_declared_demo_can_use_real_classified_test_source_but_cannot_mix_fixture(
    service, settings, snapshot, decision
):
    # Synthetic records intentionally exercise both source classifications; the
    # decision stays demo and is never evidence for an actual policy result.
    records = tuple(
        record.model_copy(
            update={
                "source_instance": "datafabric-test",
                "source_id": "clinicaltrials",
                "ingestion_mode": "sample_export",
            }
        )
        for record in snapshot.records
    )
    real_classified = snapshot.model_copy(
        update={
            "is_demo": False,
            "source_instance": "datafabric-test",
            "records": records,
            "coverage": (
                snapshot.coverage[0].model_copy(
                    update={"source_instance": "datafabric-test", "source_id": "clinicaltrials"}
                ),
            ),
        }
    )
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, real_classified, "real-classified")
    sourced = with_source(decision, real_classified)
    _, run = accepted(service, settings, sourced, snapshot_ids=(adopted["id"],))
    assert run["is_demo"] and not run["source_snapshots"][0]["is_demo"]
    _, fixture = service.adopt_snapshot(actor, snapshot, "fixture")
    mixed = sourced.model_copy(
        update={"evidence_snapshot_hashes": (snapshot_hash(real_classified), snapshot_hash(snapshot))}
    )
    with pytest.raises(DomainError) as rejected:
        accepted(service, settings, mixed, key="mixed", snapshot_ids=(adopted["id"], fixture["id"]))
    assert rejected.value.code == "fixture_scope_mismatch"


def test_decision_missing_outcomes_persist_unknown_instead_of_empty_success(
    service, settings, decision, database
):
    missing = decision.model_copy(update={"outcomes": ()})
    actor, run = accepted(service, settings, missing)
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    service.finish_run(run["id"], *current, result_for(missing))
    view = service.decisions().get(actor, run["id"])
    assert view.state == "partial" and view.result.execution_status == "partial"
    assert all(option.status == "unknown" for option in view.result.feasibility)
    assert view.result.conditional_comparison_set == ()
    assert view.result.regret.status == "unsupported" and view.result.investigation_questions


def test_decision_worker_result_budget_failure_is_safe_and_requires_new_scope(
    service, settings, decision, database, monkeypatch
):
    from bioradar.research.engine import worker

    actor, run = accepted(service, settings, decision)
    bounded = settings.model_copy(update={"engine_max_result_bytes": 1024})
    monkeypatch.setattr(worker, "Settings", lambda: bounded)
    monkeypatch.setattr(worker, "create_database", lambda _: database)
    with pytest.raises(RuntimeError):
        worker.execute_run(run["id"], *tokens(database, run["id"]))
    view = service.decisions().get(actor, run["id"])
    assert view.state == "failed" and view.result is None and view.error.code == "result_budget_exceeded"
    assert "缩小采用范围" in view.error.message
    with pytest.raises(DomainError) as rejected:
        service.decisions().retry(
            actor,
            run["id"],
            RunAction(action="retry_failed", expected_revision=view.revision),
            "budget-retry",
        )
    assert rejected.value.code == "result_budget_exceeded"


def test_decision_actual_worker_uses_same_pg_fencing_and_returns_replayable_result(
    service, settings, decision, database, monkeypatch
):
    from bioradar.research.engine import worker

    actor, run = accepted(service, settings, decision)
    current = tokens(database, run["id"])
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "create_database", lambda _: database)
    assert worker.execute_run(run["id"], *current) == run["id"]
    view = service.decisions().get(actor, run["id"])
    assert view.state in {"completed", "partial"} and view.result.is_demo
    assert view.result.regret.least_upper_bound_options == ("alternative",)
    result_hash = view.result.result_hash
    assert worker.execute_run(run["id"], *current) == run["id"]
    assert service.decisions().get(actor, run["id"]).result.result_hash == result_hash


@pytest.mark.parametrize(
    "tamper",
    ["input", "option", "gate_value", "gate_threshold", "missing_gate", "duplicate_gate", "missing_baseline"],
)
def test_decision_result_is_bound_to_frozen_input_and_complete_comparison_conditions(
    service, settings, decision, database, tamper
):
    actor, run = accepted(service, settings, decision)
    current = tokens(database, run["id"])
    assert service.start_run(run["id"], *current)
    result = result_for(decision)
    if tamper == "input":
        result = result.model_copy(update={"input_hash": "0" * 64})
    elif tamper == "option":
        result = result.model_copy(update={"conditional_comparison_set": ("unbound-option",)})
    elif tamper == "missing_baseline":
        result = result.model_copy(update={"baseline_differences": result.baseline_differences[:-1]})
    else:
        feasibility = result.feasibility[0]
        gate = feasibility.checks[0]
        if tamper in {"gate_value", "gate_threshold"}:
            field = "value" if tamper == "gate_value" else "threshold"
            gate = gate.model_copy(update={field: NumericInterval(lower="999", upper="999")})
            checks = (gate, *feasibility.checks[1:])
        elif tamper == "missing_gate":
            checks = feasibility.checks[1:]
        else:
            checks = (*feasibility.checks, gate)
        changed = feasibility.model_copy(update={"checks": checks})
        result = result.model_copy(update={"feasibility": (changed, *result.feasibility[1:])})
    result = result.model_copy(
        update={"result_hash": canonical_hash(result.model_dump(mode="json", exclude={"result_hash"}))}
    )
    with pytest.raises(DomainError):
        service.finish_run(run["id"], *current, result)
    assert service.decisions().get(actor, run["id"]).result is None


def test_decision_retry_preserves_attempt_history_and_fences_old_worker(
    service, settings, decision, database
):
    actor, run = accepted(service, settings, decision)
    old = tokens(database, run["id"])
    service.start_run(run["id"], *old)
    assert service.fail_run(run["id"], *old)
    failed = service.decisions().get(actor, run["id"])
    _, retry = service.decisions().retry(
        actor, run["id"], RunAction(action="retry_failed", expected_revision=failed.revision), "retry"
    )
    assert retry["attempt"] == 2 and retry["input_hash"] == run["input_hash"]
    assert service.finish_run(run["id"], *old, result_for(decision)) is False
    current = tokens(database, run["id"])
    assert service.start_run(run["id"], *current)
    assert service.finish_run(run["id"], *current, result_for(decision))
    view = service.decisions().get(actor, run["id"])
    assert (
        view.attempt_diagnostics[0].attempt == 1
        and view.attempt_diagnostics[0].error_code == "computation_failed"
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "duplicate_member",
        "exclude_pass",
        "failed_member",
        "self_pareto",
        "missing_pareto",
        "regret_set",
        "regret_exclusion",
        "regret_unit",
        "missing_regret_case",
    ],
)
def test_comparison_sets_and_case_completeness_cannot_be_forged_with_valid_ids(
    service, settings, decision, database, tamper
):
    actor, run = accepted(service, settings, decision)
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    result = result_for(decision)
    if tamper == "duplicate_member":
        result = result.model_copy(
            update={"conditional_comparison_set": (*result.conditional_comparison_set, "baseline")}
        )
    elif tamper == "exclude_pass":
        result = result.model_copy(update={"excluded_from_comparison": {"baseline": ("forged",)}})
    elif tamper == "failed_member":
        item = result.feasibility[0].model_copy(update={"status": "fail"})
        result = result.model_copy(update={"feasibility": (item, *result.feasibility[1:])})
    elif tamper == "self_pareto":
        comparison = result.pareto_comparisons[0]
        comparison = comparison.model_copy(update={"option_b": comparison.option_a})
        result = result.model_copy(
            update={"pareto_comparisons": (comparison, *result.pareto_comparisons[1:])}
        )
    elif tamper == "missing_pareto":
        result = result.model_copy(update={"pareto_comparisons": result.pareto_comparisons[1:]})
    else:
        regret = result.regret
        if tamper == "regret_set":
            regret = regret.model_copy(update={"conditional_comparison_set": ("baseline",)})
        elif tamper == "regret_exclusion":
            regret = regret.model_copy(update={"excluded_options": ("baseline",)})
        elif tamper == "regret_unit":
            regret = regret.model_copy(update={"unit": "another-unit"})
        else:
            regret = regret.model_copy(update={"cases": regret.cases[:-1]})
        result = result.model_copy(update={"regret": regret})
    result = result.model_copy(
        update={"result_hash": canonical_hash(result.model_dump(mode="json", exclude={"result_hash"}))}
    )
    with pytest.raises(DomainError) as rejected:
        service.finish_run(run["id"], *current, result)
    assert rejected.value.code == "decision_result_binding_invalid"
    assert service.decisions().get(actor, run["id"]).result is None


def test_user_policy_budget_unsupported_is_preserved_and_adjusting_policy_creates_new_run(
    service, settings, decision, database
):
    from bioradar.research.decision.runner import evaluate_decision

    actor = DevelopmentAccess(settings).actor()
    policy = decision_policy().model_copy(update={"max_options": 1})
    _, run = service.decisions().create(
        actor, DecisionRunCreate(input=decision, policy=policy), "small-policy"
    )
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    result = evaluate_decision(decision, policy)
    assert result.execution_status == "unsupported"
    assert service.finish_run(run["id"], *current, result)
    view = service.decisions().get(actor, run["id"])
    assert (
        view.result.execution_status == "unsupported"
        and "policy_budget_exceeded:options" in view.result.data_gaps
    )
    assert view.result.feasibility == () and view.result.regret.status == "unsupported"
    _, new = accepted(service, settings, decision, key="larger-policy")
    assert new["id"] != run["id"] and new["input_hash"] != run["input_hash"]


def test_decision_compute_version_change_refuses_queued_and_creates_new_identity(
    service, settings, decision, database, monkeypatch
):
    actor, run = accepted(service, settings, decision)
    monkeypatch.setattr(
        "bioradar.research.engine.decision_service.DECISION_COMPUTE_VERSION", "future-decision"
    )
    assert service.start_run(run["id"], *tokens(database, run["id"])) is None
    old = service.decisions().get(actor, run["id"])
    assert old.error.code == "decision_version_changed"
    _, new = accepted(service, settings, decision, key="upgraded")
    assert new["id"] != run["id"] and new["input_hash"] != run["input_hash"]


def test_historical_decision_input_is_read_raw_without_new_defaults(service, settings, decision, database):
    actor, run = accepted(service, settings, decision)
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    service.finish_run(run["id"], *current, result_for(decision))
    with Session(database) as session, session.begin():
        row = session.get(EngineRun, run["id"])
        envelope = copy.deepcopy(row.decision_input)
        envelope["input"]["brief"].pop("why_now")
        row.decision_input = envelope
    view = service.decisions().get(actor, run["id"])
    assert view.decision_input is None and view.input_view_status == "historical_requires_new_run"
    assert "why_now" not in view.original_input["brief"] and view.result is not None


def test_decision_http_is_authenticated_and_client_cannot_supply_receipt(database, settings, decision):
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        body = DecisionRunCreate(input=decision, policy=decision_policy()).model_dump(mode="json")
        assert client.get("/api/engine/decision-runs").status_code == 401
        headers = {"Authorization": "Bearer " + CREDENTIAL, "Idempotency-Key": "http-decision"}
        response = client.post("/api/engine/decision-runs", json=body, headers=headers)
        assert response.status_code == 202 and response.json()["dispatch_pending"]
        assert response.json()["submitted_by"] == "development-user"
        assert "accept_token" not in response.text and CREDENTIAL not in response.text
        invalid = client.post(
            "/api/engine/decision-runs",
            json={**body, "submitted_by": "government-official"},
            headers={**headers, "Idempotency-Key": "bad-receipt"},
        )
        assert invalid.status_code == 422
        assert client.get("/api/engine/runs/" + response.json()["id"], headers=headers).status_code == 404


def test_http_decision_budget_is_enforced_before_json_validation_including_chunked_body(database, settings):
    bounded = settings.model_copy(update={"engine_max_decision_bytes": 1024})
    app = create_app(bounded)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.database = database
        headers = {
            "Authorization": "Bearer " + CREDENTIAL,
            "Idempotency-Key": "oversized-json",
            "Content-Type": "application/json",
        }
        chunks = iter((b'{"padding":"' + b"x" * 900, b"x" * 900 + b'"}'))
        response = client.post("/api/engine/decision-runs", content=chunks, headers=headers)
        assert response.status_code == 422 and response.json()["error"]["code"] == "engine_budget_exceeded"
        assert "padding" not in response.text and CREDENTIAL not in response.text
        response = client.post(
            "/api/engine/decision-runs", content=b"{}", headers={**headers, "Content-Length": "2000"}
        )
        assert response.status_code == 422 and response.json()["error"]["code"] == "engine_budget_exceeded"
    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(EngineRun)) == 0


def test_decision_migration_preserves_actual_old_mining_payload_and_enforces_shapes(
    service, settings, snapshot, database
):
    actor, _, run = accept(service, settings, snapshot)
    current = tokens(database, run["id"])
    service.start_run(run["id"], *current)
    service.finish_run(run["id"], *current, controlled_result(snapshot))
    spec = importlib.util.spec_from_file_location(
        "decision_migration", ROOT / "db/migrations/versions/0003_decision.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with database.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        before = dict(
            connection.execute(text("SELECT input_hash, policy, result, result_hash FROM analysis_runs"))
            .mappings()
            .one()
        )
        migration.downgrade()
        migration.upgrade()
        after = dict(
            connection.execute(text("SELECT input_hash, policy, result, result_hash FROM analysis_runs"))
            .mappings()
            .one()
        )
        assert before == after
    assert service.get_run(actor, run["id"]).result == controlled_result(snapshot)
    with Session(database) as session, pytest.raises(IntegrityError), session.begin():
        row = session.get(EngineRun, run["id"])
        row.run_kind = "decision"
        session.flush()
