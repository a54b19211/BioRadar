"""Real PG and local synthetic HTTP probes for the shared query-step executor."""

import copy
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_models import AnalysisCreate, InitialQuery
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationCreate
from bioradar.research.investigations.service import InvestigationService
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph import original_record
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start

from scripts.query_policy_assertions import assert_query_policy

pytestmark = pytest.mark.ENGINE


def empty_case(runtime):
    make, actor, _, _, _, settings, _ = runtime
    settings.analysis_manual_per_hour = 20
    _, view = InvestigationService(make().engine).create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC step first question", question="What does SYNTHETIC establish?", is_demo=True
        ),
        "synthetic-step-empty",
    )
    accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=view["revision"],
            auto_retrieve=True,
            initial_queries=[InitialQuery(source_id="fixture-source", terms=["SYNTHETIC"])],
        ),
        "synthetic-step-first",
    )[1]
    return view, accepted


def expire(database, identifier):
    with Session(database) as session, session.begin():
        session.get(InvestigationAnalysisRecord, identifier).lease_until = datetime.now(UTC) - timedelta(
            seconds=1
        )


def external(runtime):
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.skip("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR supplies the actual installed pure policy")
    runtime[5].query_planner_project_dir = Path(directory)
    runtime[5].analysis_query_policy = "query_planner"


def test_baseline_first_question_records_phase_transition_and_real_receipts(runtime):
    make, actor, _, _, model, _, _ = runtime
    view, accepted = empty_case(runtime)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    payload = result.model_dump(mode="json")
    assert_query_policy(payload, expected_policy="baseline")
    trace = result.query_policy
    assert trace["phase_transitions"][0]["from"] == "initial"
    assert trace["decisions"][-1]["decision"] == "insufficient"
    assert all(item["decision"] == "continue" for item in trace["decisions"][:-1])
    assert trace["decisions"][1]["state_summary"]["observed_history"][0]["status"] == result.queries[0].status
    read = next(call for call in result.calls if call["stage"] == "read_plan")
    assert "coverage_checks" not in read["response_schema"]["properties"]
    assert "coverage_checks" not in next(p for p in model.requests if p["task"] == "read_plan")
    artifact = os.environ.get("BIORADAR_POLICY_TEST_ARTIFACT")
    if artifact:
        Path(artifact).write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2))


def test_new_source_revocation_is_durably_not_sent_without_adoption(runtime):
    make, actor, _, source, model, _, _ = runtime
    view, accepted = empty_case(runtime)
    source.enabled.clear()
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked"
    assert result.queries[0].usage["request_status"] == "not_sent"
    assert result.queries[0].cost["tool_calls"] == 0
    assert result.query_policy["pending_action"]["id"] == result.queries[0].action_id
    assert model.requests == []
    assert not any(path == "/api/history/records" for path, _ in source.calls)


def test_prepared_action_resume_checks_current_source_before_sending(runtime, database):
    make, actor, _, source, _, _, _ = runtime
    view, accepted = empty_case(runtime)
    service = make()
    checkpoint = service._checkpoint

    def stop_after_prepared(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if any(q["status"] == "prepared" for q in progress["queries"]):
            raise SQLAlchemyError("SYNTHETIC committed prepared action then worker exited")

    service._checkpoint = stop_after_prepared
    with pytest.raises(RuntimeError):
        service.execute(accepted["id"], accepted["input_hash"])
    source.enabled.clear()
    expire(database, accepted["id"])
    make().recover()
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked"
    assert result.queries[0].usage["request_status"] == "not_sent"
    assert not any(path == "/api/history/records" for path, _ in source.calls)


def test_sent_action_unknown_reserves_budget_and_never_changes_identity(runtime, database):
    make, actor, _, source, model, _, _ = runtime
    view, accepted = empty_case(runtime)
    service = make()
    checkpoint = service._checkpoint

    def stop_after_sent(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if any(q["status"] == "sent" for q in progress["queries"]):
            raise SQLAlchemyError("SYNTHETIC unknown outcome after committed query intent")

    service._checkpoint = stop_after_sent
    with pytest.raises(RuntimeError):
        service.execute(accepted["id"], accepted["input_hash"])
    expire(database, accepted["id"])
    make().recover()
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_query_outcome_unconfirmed"
    assert result.query_policy["pending_action"]["id"] == result.queries[0].action_id
    assert result.query_policy["budget"]["usage"]["tool_calls"] is None
    assert result.query_policy["budget"]["usage"]["unknown_tool_calls"] == 1
    assert result.queries[0].cost["tool_calls"] is None
    assert result.queries[0].cost["reserved_tool_calls"] > 0
    assert model.requests == []
    assert not any(path == "/api/history/records" for path, _ in source.calls)


def test_received_records_are_adopted_before_redecision_without_second_search(runtime, database):
    make, actor, _, source, _, _, _ = runtime
    view, accepted = empty_case(runtime)
    service = make()
    checkpoint = service._checkpoint

    def stop_after_received(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if any(q["status"] == "read" and not q["evidence_keys"] for q in progress["queries"]):
            raise SQLAlchemyError("SYNTHETIC returned originals committed before adoption")

    service._checkpoint = stop_after_received
    with pytest.raises(RuntimeError):
        service.execute(accepted["id"], accepted["input_hash"])
    before = sum(path == "/api/history/records" for path, _ in source.calls)
    expire(database, accepted["id"])
    make().recover()
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert result.queries[0].evidence_keys
    # The only extra search is the deliberately retained baseline followup phase.
    assert sum(path == "/api/history/records" for path, _ in source.calls) == before + 1
    assert_query_policy(result.model_dump(mode="json"), expected_policy="baseline")


def test_history_context_excludes_current_parent_and_inherits_only_same_history(runtime, database):
    make, actor, view, _, _, settings, _ = runtime
    settings.analysis_manual_per_hour = 20
    current = start(runtime)
    make().execute(current["id"], current["input_hash"])
    context = {
        "study_id": "synthetic-history-study",
        "mode": "operational_replay",
        "cutoff": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        "run_id": "synthetic-history-run",
    }
    investigation = InvestigationService(make().engine)
    accepted = make().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=investigation.get(actor, view["id"]).revision),
        "synthetic-history-analysis",
        historical_context=context,
    )[1]
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.parent_analysis_id is None and row.fixed_input["parent_result"] is None
        assert row.fixed_input["evidence_time"] == context
        assert row.trigger == "history"
    make().execute(accepted["id"], accepted["input_hash"])
    historical = make().get(actor, view["id"], accepted["id"])
    assert historical.state == "completed", historical.error_code
    followup = make().start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=investigation.get(actor, view["id"]).revision,
            parent_analysis_id=accepted["id"],
            question="SYNTHETIC historical followup",
        ),
        "synthetic-history-followup",
    )[1]
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, followup["id"])
        assert row.parent_analysis_id == accepted["id"]
        assert row.fixed_input["evidence_time"] == context
        assert row.fixed_input["parent_result"] == historical.result.model_dump(mode="json")
    assert make().request_material_update(view["id"]) is None
    with pytest.raises(DomainError) as error:
        make().start(
            actor,
            view["id"],
            AnalysisCreate(
                expected_revision=investigation.get(actor, view["id"]).revision,
                parent_analysis_id=current["id"],
            ),
            "synthetic-incompatible-history-parent",
            historical_context=context,
        )
    assert error.value.code == "analysis_parent_invalid"


def test_external_refreshes_new_evidence_and_stops_only_after_answer_coverage(runtime):
    external(runtime)
    make, actor, view, source, model, _, _ = runtime
    altered = original_record("altered", {"body": "ALTERED new discriminating evidence."})
    altered["title"] = "ALTERED distinct source"
    source.records.append(altered)

    def adapt(payload, result):
        if payload["task"] != "read_plan":
            return
        count = len(payload["evidence"])
        result["next_queries"][0]["terms"] = ["SYNTHETIC"] if count == 1 else ["ALTERED"]
        if count >= 3:
            result["coverage_checks"] = [
                {
                    "id": check["id"],
                    "status": "passed",
                    "evidence_ids": [payload["evidence"][-1]["evidence_key"]],
                    "reason": "SYNTHETIC bounded source scope actually read.",
                }
                for check in payload["coverage_checks"]
            ]

    model.transform = adapt
    accepted = start(runtime, query_policy="query_planner")
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [q.plan.terms for q in result.queries] == [["SYNTHETIC"], ["ALTERED"]]
    assert len([p for p in model.requests if p["task"] == "read_plan"]) == 3
    assert result.query_policy["decisions"][-1]["decision"] == "complete"
    assert result.query_policy["training_status"] == "heuristic_untrained"
    assert_query_policy(result.model_dump(mode="json"), expected_policy="query_planner")


def test_external_early_stop_leaves_an_unneeded_legal_query_unexecuted(runtime):
    external(runtime)
    make, actor, view, source, model, _, _ = runtime

    def sufficient(payload, result):
        if payload["task"] == "read_plan":
            result["coverage_checks"] = [
                {
                    "id": check["id"],
                    "status": "passed",
                    "evidence_ids": [payload["evidence"][0]["evidence_key"]],
                    "reason": "SYNTHETIC limited answer scope supported by the read fragment.",
                }
                for check in payload["coverage_checks"]
            ]
            result["gaps"] = []
            result["next_queries"] = []

    model.transform = sufficient
    accepted = start(
        runtime,
        query_policy="query_planner",
        auto_retrieve=True,
        initial_queries=[InitialQuery(source_id="fixture-source", terms=["UNNEEDED"])],
    )
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert result.queries == []
    assert result.query_policy["decisions"][0]["decision"] == "complete"
    assert not any(path == "/api/history/records" for path, _ in source.calls)
    assert_query_policy(result.model_dump(mode="json"), expected_policy="query_planner")


def test_graph_candidates_use_the_actual_start_scope_and_sending_gate(runtime, database):
    from bioradar.adapters.query_graph import build_graph_candidates
    from pydantic import SecretStr

    external(runtime)
    make, actor, view, source, _, settings, _ = runtime
    settings.data_fabric_url = "http://127.0.0.1:18531"
    settings.data_fabric_graph_query_token = SecretStr("SYNTHETIC-GRAPH-ONLY")
    settings.engine_source_id_allowlist["datafabric-test"] = ["clinicaltrials", "primekg_graph"]
    settings.analysis_search_source_ids = ["clinicaltrials"]
    source.enabled.update({"clinicaltrials", "primekg_graph"})
    # This exercises public-mode admission only; no graph or document HTTP is sent.
    _, public_view = InvestigationService(make().engine).create(
        actor,
        InvestigationCreate(
            title="SYNTHETIC public-mode graph scope",
            question="SYNTHETIC: inspect EGFR lookup permission",
            is_demo=False,
        ),
        "synthetic-public-graph",
    )
    accepted = make().start(
        actor,
        public_view["id"],
        AnalysisCreate(expected_revision=public_view["revision"], auto_retrieve=True),
        "synthetic-graph-scope",
    )[1]
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        fixed, progress = copy.deepcopy(row.fixed_input), copy.deepcopy(row.progress)
        fixed["is_demo"] = row.is_demo
    assert fixed["search_scope"] == ["clinicaltrials"] and fixed["graph_scope"] == ["primekg_graph"]
    reading = {"graph_names": [{"name": "EGFR", "node_type": "gene/protein"}]}
    checks = [{"id": "answer_scope", "status": "unknown"}]
    candidates = build_graph_candidates(settings, fixed, progress, reading, checks=checks)
    assert candidates and candidates[0]["arguments"]["source_id"] == "primekg_graph"
    restricted = copy.deepcopy(fixed)
    restricted["graph_scope"] = []
    assert build_graph_candidates(settings, restricted, progress, reading, checks=checks) == []
    with pytest.raises(DomainError) as error:
        make()._authorize_query_sources(restricted, candidates[0])
    assert error.value.code == "analysis_query_invalid"


def test_baseline_relaxes_only_once_across_both_query_phases(runtime):
    import httpx
    from bioradar.adapters.fabric_history import FabricHistoryReader
    from test_engine_persistence import GRANT

    make, actor, _, source, model, _, _ = runtime
    view, _ = empty_case(runtime)
    service = make()
    current = InvestigationService(service.engine).get(actor, view["id"])
    accepted = service.start(
        actor,
        view["id"],
        AnalysisCreate(
            expected_revision=current.revision,
            auto_retrieve=True,
            initial_queries=[InitialQuery(source_id="fixture-source", terms=["SYNTHETIC", "MISSING"])],
        ),
        "synthetic-one-relaxation",
    )[1]

    def source_handler(request):
        response = source.handler(request)
        if request.url.path == "/api/history/records" and len(request.url.params.get_list("terms")) > 1:
            page = response.json()
            page["items"], page["next_cursor"] = [], None
            return httpx.Response(200, json=page)
        return response

    def followup(payload, result):
        if payload["task"] == "read_plan":
            result["next_queries"][0]["terms"] = ["SYNTHETIC", "follow-up"]

    model.transform = followup
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(source_handler)) as client:
        service.reader = FabricHistoryReader(client, authorization_ref=GRANT, max_seconds=90)
        service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [(q.phase, q.plan.terms) for q in result.queries] == [
        ("initial", ["SYNTHETIC", "MISSING"]),
        ("initial", ["SYNTHETIC"]),
        ("followup", ["SYNTHETIC", "follow-up"]),
    ]
    assert sum(q.relaxed_from is not None for q in result.queries) == 1


@pytest.mark.parametrize("stop_at", ["send_deadline", "adoption"])
def test_blocked_action_retains_actual_receipt_in_policy_trace(runtime, stop_at):
    make, actor, _, source, model, _, _ = runtime
    view, accepted = empty_case(runtime)
    service = make()
    if stop_at == "send_deadline":
        original = service._checkpoint

        def expire_after_intent(identifier, token, progress, **kwargs):
            if any(q["status"] == "sent" for q in progress["queries"]):
                progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            original(identifier, token, progress, **kwargs)

        service._checkpoint = expire_after_intent
    else:

        def stop_adoption(*args, **kwargs):
            raise DomainError("analysis_tool_budget", "SYNTHETIC stop after received originals", 422)

        service._adopt_original = stop_adoption
    service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "blocked"
    receipt = result.query_policy["decisions"][-1]["receipt"]
    assert receipt["status"] == result.queries[0].status
    assert receipt["cost"] == result.queries[0].cost
    assert_query_policy(result, expected_policy="baseline")
    reads = sum(path != "/api/history/sources" for path, _ in source.calls)
    make().execute(accepted["id"], accepted["input_hash"])
    assert sum(path != "/api/history/sources" for path, _ in source.calls) == reads
    assert model.requests == []
    if stop_at == "send_deadline":
        assert receipt["status"] == "failed"
        assert result.queries[0].usage["request_status"] == "not_sent"
        assert receipt["cost"]["tool_calls"] == 0
        assert not any(path == "/api/history/records" for path, _ in source.calls)
    else:
        assert receipt["status"] == "read" and receipt["cost"]["tool_calls"] > 0


def test_model_output_cap_keeps_finishing_reserve_and_received_replay(runtime, database):
    from bioradar.adapters.reasoning_model import encode_request
    from bioradar.research.investigations.analysis import SYSTEM_PROMPT

    make, actor, view, _, model, _, _ = runtime
    service = make()
    accepted = service.start(
        actor, view["id"], AnalysisCreate(expected_revision=view["revision"]), "synthetic-reserved-output"
    )[1]
    reserve = 1200
    observed = {}

    def one_call(identifier, token, fixed, progress):
        payload = {"task": "shared_verify"}
        schema = {"type": "object"}
        upper = (
            len(
                encode_request(
                    service.settings,
                    stage="shared_verify",
                    system_prompt=SYSTEM_PROMPT,
                    payload=payload,
                    response_schema=schema,
                )
            )
            + 1024
        )
        fixed["budget"]["max_tokens"] = upper + reserve + 256
        service._checkpoint(identifier, token, progress, fixed=fixed)
        output, progress = service._call(
            identifier, token, "shared_verify", schema, fixed, progress, payload, reserve_tokens=reserve
        )
        repeated, progress = service._call(
            identifier,
            token,
            "shared_verify",
            schema,
            fixed,
            progress,
            payload,
            reserve_tokens=fixed["budget"]["max_tokens"] * 2,
        )
        assert repeated == output
        observed["upper"] = upper
        raise DomainError("analysis_model_budget", "SYNTHETIC stop after measured cap", 422)

    service._research_result = one_call
    service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        call = row.progress["calls"][0]
        assert call["model_parameters"]["max_output_tokens"] == 256
        assert call["token_upper_bound"] + reserve == row.fixed_input["budget"]["max_tokens"]
        assert call["status"] == "received" and len(row.progress["calls"]) == 1
    assert len(model.requests) == 1


def test_terminal_retrieval_survives_report_failure_and_legacy_blocked_resume_without_new_read(
    runtime, database
):
    external(runtime)
    make, actor, view, source, model, _, _ = runtime
    accepted = start(runtime, query_policy="query_planner")
    service = make()
    call = service._call

    def fail_before_synthesis(identifier, token, stage, *args, **kwargs):
        if stage == "synthesize":
            raise DomainError("analysis_output_invalid", "SYNTHETIC stop after completed retrieval", 422)
        return call(identifier, token, stage, *args, **kwargs)

    service._call = fail_before_synthesis
    service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session, session.begin():
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        state = row.progress["query_policy"]
        terminal = copy.deepcopy(state["decisions"][-1])
        assert row.state == "blocked" and row.error_code == "analysis_output_invalid"
        assert terminal["decision"] == "insufficient"
        assert state["status"] == terminal["decision"]
        assert state["reason"] == terminal["reason"]
        assert state["pending_action"] is None
        assert row.progress["mechanism_evaluation"]["status"] == "not_requested"
        decisions_before = copy.deepcopy(state["decisions"])
        queries_before = copy.deepcopy(row.progress["queries"])
        progress = copy.deepcopy(row.progress)
        # Simulate an explicitly resumed row saved by the earlier exception handler.
        progress["query_policy"].update(status="blocked", reason="analysis_output_invalid")
        row.progress = progress
        row.state = "queued"
        row.dispatch_pending = False
    before_requests = len(model.requests)
    before_source_calls = len(source.calls)
    make().execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert result.query_policy["status"] == terminal["decision"]
    assert result.query_policy["reason"] == terminal["reason"]
    assert result.query_policy["decisions"] == decisions_before
    assert [item["task"] for item in model.requests[before_requests:]] == ["synthesize", "check"]
    assert not any(path == "/api/history/records" for path, _ in source.calls[before_source_calls:])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.progress["queries"] == queries_before


@pytest.mark.parametrize("stopping_stage", ["revise", "recheck"])
def test_optional_structured_revision_budget_preserves_checked_draft(runtime, stopping_stage):
    make, actor, view, _, model, _, _ = runtime

    def needs_edit(payload, result):
        if payload["task"] == "check":
            result["status"] = "needs_review"
            result["claims"][0]["verdict"] = "overstated"
            result["claims"][0]["issue"] = "SYNTHETIC qualification needs review"
        if payload["task"] == "revise":
            result["conditional_answer"]["answer"] = "SYNTHETIC UNVERIFIED revision"

    model.transform = needs_edit
    accepted = start(runtime)
    service = make()
    original = service._call

    def stop_before_send(identifier, token, stage, *args, **kwargs):
        if stage == stopping_stage:
            raise DomainError("analysis_token_budget", "SYNTHETIC known not sent", 422)
        return original(identifier, token, stage, *args, **kwargs)

    service._call = stop_before_send
    service.execute(accepted["id"], accepted["input_hash"])
    result = service.get(actor, view["id"], accepted["id"])
    assert result.state == "needs_review", result.error_code
    assert "UNVERIFIED" not in result.result.conditional_answer.answer
    assert result.result.review.status == "needs_review"
    assert stopping_stage not in [item["task"] for item in model.requests]
    with Session(service.engine.database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.progress["structured_revision_stop_reason"] == "analysis_token_budget"
        assert (
            row.progress["initial_draft"]["conditional_answer"]
            == result.result.conditional_answer.model_dump()
        )


def test_supervised_resumption_keeps_receipts_tokens_and_enforces_its_deadline(runtime, monkeypatch):
    import bioradar.research.investigations.analysis_service as module
    from bioradar.research.investigations.analysis import analysis_budget

    make, actor, view, _, model, _, _ = runtime
    service = make()
    accepted = start(runtime)
    observed = {}

    def exercise(identifier, token, fixed, progress):
        schema, payload = {"type": "object"}, {"task": "shared_verify"}
        output, progress = service._call(identifier, token, "shared_verify", schema, fixed, progress, payload)
        now = datetime.now(UTC)
        original_start = now - timedelta(seconds=fixed["budget"]["max_seconds"] + 60)
        progress["started_at"] = original_start.isoformat()
        progress["execution_resumptions"] = [
            {
                "kind": "supervised_continuation",
                "authorization": "SYNTHETIC explicit operator test",
                "original_started_at": progress["started_at"],
                "started_at": now.isoformat(),
                "max_seconds": 5,
            }
        ]
        service._checkpoint(identifier, token, progress)
        before = analysis_budget(fixed, progress, now)
        assert before["usage"]["calls"] == 1 and before["usage"]["known_total_tokens"] == 150
        assert before["usage"]["original_elapsed_seconds"] > fixed["budget"]["max_seconds"]
        assert before["usage"]["elapsed_seconds"] == 0
        repeated, progress = service._call(
            identifier, token, "shared_verify", schema, fixed, progress, payload
        )
        assert repeated == output and len(model.requests) == 1
        _, progress = service._call(
            identifier, token, "shared_verify", schema, fixed, progress, payload, call_key="during-resumption"
        )
        after = analysis_budget(fixed, progress, now + timedelta(seconds=1))
        assert after["usage"]["calls"] == 2 and after["usage"]["known_total_tokens"] == 300

        progress["finished_at"] = (now + timedelta(seconds=1)).isoformat()
        service._checkpoint(identifier, token, progress)
        # A stale ended marker affects display only; it cannot renew the live clock.
        expired = analysis_budget(fixed, progress, now + timedelta(seconds=6))
        assert expired["usage"]["elapsed_seconds"] == 1
        assert expired["usage"]["remaining_seconds"] == -1

        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now + timedelta(seconds=6)

        monkeypatch.setattr(module, "datetime", ExpiredClock)
        repeated, progress = service._call(
            identifier, token, "shared_verify", schema, fixed, progress, payload
        )
        assert repeated == output and len(model.requests) == 2
        with pytest.raises(DomainError) as error:
            service._call(
                identifier,
                token,
                "shared_verify",
                schema,
                fixed,
                progress,
                payload,
                call_key="after-deadline",
            )
        assert error.value.code == "analysis_time_budget" and len(model.requests) == 2
        observed.update(before=before, after=after)
        raise DomainError("analysis_time_budget", "SYNTHETIC ended bounded continuation", 409)

    service._research_result = exercise
    service.execute(accepted["id"], accepted["input_hash"])
    result = service.get(actor, view["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_time_budget"
    assert len(result.calls) == 2 and len(model.requests) == 2 and observed
