"""Real PG plus synthetic HTTP checks for bounded professional quality feedback."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.reasoning_model import complete_json
from bioradar.research.investigations.analysis_models import AnalysisCreate
from bioradar.research.investigations.service import InvestigationService
from sqlalchemy.exc import SQLAlchemyError
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_graph import original_record
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_query_policy_runtime import expire, external
from test_shared_research_adapters import installed_biocheck as installed_biocheck  # noqa: F401

from scripts.query_policy_assertions import assert_query_policy

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def quality_runtime(runtime, installed_biocheck):
    make, _, _, _, model, settings, _ = runtime
    core = installed_biocheck.biocheck_project_dir / "app/core.py"
    core.write_text(
        core.read_text().replace(
            "def revise(packet, review, call):", "def revise(packet, review, call, **kwargs):"
        )
        + """
def audit_support(packet, review, call, targets=None):
    return call('support_audit', INSTRUCTION, dict(packet, review=review, targets=targets))
def quality_feedback(review, audit=None, source_errors=()):
    draft = review['draft']
    result = {'evidence_gaps': [], 'repair_units': [], 'review_required': []}
    if source_errors:
        result['review_required'] = list(source_errors)
    elif 'bad' in draft:
        result['repair_units'] = ['U1']
    elif 'n=2' in draft:
        result['evidence_gaps'] = [{'kind':'missing_evidence','unit_id':'U1','claim_index':0,
            'span':'SYNTHETIC limited claim','reason':'Need ALTERED discriminating source.',
            'action':'retrieve'}]
    return result
"""
    )
    settings.biocheck_project_dir = installed_biocheck.biocheck_project_dir
    state = SimpleNamespace(mode="gap", stages=[], payloads=[])

    def handler(request):
        wire = json.loads(request.content)
        stage = wire["response_format"]["json_schema"]["name"]
        if not stage.startswith("biocheck_"):
            return model.handler(request)
        payload = json.loads(wire["messages"][1]["content"])
        state.stages.append(stage)
        state.payloads.append(payload)
        first = payload["evidence"][0]
        if stage == "biocheck_plan":
            output = {"points": ["SYNTHETIC bounded report"]}
        elif stage == "biocheck_write":
            word = "bad" if state.mode in {"repair", "noop"} else "report"
            output = {"draft": f"SYNTHETIC {word} n={len(payload['evidence'])} [{first['id']}]."}
        elif stage == "biocheck_revise":
            before = payload["review"]["draft"]
            after = before if state.mode == "noop" else before.replace("bad", "good")
            output = {
                "draft": after,
                "changed_units": [{"unit_id": "U1", "before": before, "after": after}]
                if before != after
                else [],
            }
        elif stage == "biocheck_support_audit":
            output = {
                "status": "PASS",
                "original_status": payload["review"]["status"],
                "checks": [{"unit_id": "U1", "claim_index": 0, "status": "PASS"}],
                "verification_errors": [],
            }
        else:
            draft = payload["draft"]
            supported = "bad" not in draft
            output = {
                "status": "PASS" if supported else "REVIEW",
                "draft": draft,
                "problem_units": [] if supported else ["U1"],
                "verification_errors": [],
                "coverage": {"verdict": "complete"},
                "units": [
                    {
                        "unit_id": "U1",
                        "text": draft,
                        "start": 0,
                        "end": len(draft),
                        "issues": [],
                        "claims": [
                            {
                                "span": draft,
                                "kind": "fact",
                                "verdict": "supported" if supported else "contradicted",
                                "evidence": [{"evidence_id": first["id"], "version": first["version"]}],
                            }
                        ],
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": "synthetic-quality-completion",
                "model": "synthetic-model",
                "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler))

    def factory():
        service = make()
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        return service

    try:
        yield factory, state
    finally:
        client.close()


def test_quality_gap_reopens_retrieval_once_and_new_report_has_new_receipts(runtime, quality_runtime):
    external(runtime)
    factory, meter = quality_runtime
    _, actor, view, source, model, _, _ = runtime
    altered = original_record("altered", {"body": "ALTERED distinct discriminating evidence."})
    altered["title"] = "ALTERED source"
    source.records.append(altered)

    def plan_gap(payload, result):
        if payload["task"] == "read_plan" and payload.get("quality_feedback"):
            result["next_queries"][0]["terms"] = ["ALTERED"]

    model.transform = plan_gap
    current = InvestigationService(factory().engine).get(actor, view["id"])
    accepted = factory().start(
        actor, view["id"], AnalysisCreate(expected_revision=current.revision), "synthetic-quality-gap"
    )[1]
    factory().execute(accepted["id"], accepted["input_hash"])
    result = factory().get(actor, view["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert [q.plan.terms for q in result.queries] == [["SYNTHETIC"], ["ALTERED"]]
    reports = result.result.report["rounds"]
    assert len(reports) == 2 and all(item["kind"] == "generation" for item in reports)
    assert reports[0]["report"]["text_sha256"] != reports[1]["report"]["text_sha256"]
    assert reports[0]["quality_feedback"]["evidence_gaps"]
    assert reports[1]["quality_feedback"]["evidence_gaps"] == []
    checks = [c for c in result.calls if c["stage"] == "biocheck_check"]
    audits = [c for c in result.calls if c["stage"] == "biocheck_support_audit"]
    assert len(checks) == len(audits) == 2
    assert len({c["call_key"] for c in checks + audits}) == 4
    assert "report-round:1:" in checks[1]["call_key"]
    assert meter.stages.count("biocheck_revise") == 0
    assert_query_policy(result.model_dump(mode="json"), expected_policy="query_planner")


@pytest.mark.parametrize("mode", ["repair", "noop"])
def test_only_changed_allowed_units_get_new_whole_check_and_audit(runtime, quality_runtime, mode):
    factory, meter = quality_runtime
    meter.mode = mode
    _, actor, view, _, _, _, _ = runtime
    current = InvestigationService(factory().engine).get(actor, view["id"])
    accepted = factory().start(
        actor, view["id"], AnalysisCreate(expected_revision=current.revision), "synthetic-quality-repair"
    )[1]
    factory().execute(accepted["id"], accepted["input_hash"])
    result = factory().get(actor, view["id"], accepted["id"])
    assert meter.stages.count("biocheck_revise") == 1, result.error_code
    reports = result.result.report["rounds"]
    assert len(reports) == 2 and reports[1]["kind"] == "repair"
    if mode == "noop":
        assert reports[1]["status"] == "not_changed"
        assert meter.stages.count("biocheck_check") == 1
        assert "bad" in result.result.report["final_text"]
        assert result.state == "needs_review"
    else:
        assert result.state == "completed", result.error_code
        assert "good" in result.result.report["final_text"]
        assert meter.stages.count("biocheck_check") == 2
        assert meter.stages.count("biocheck_support_audit") == 1
        assert result.result.report["support_audit"]["text_sha256"] == result.result.report["text_sha256"]
        assert any(c["call_key"] == "report-round:1:biocheck_check" for c in result.calls)


def configure_quality_gap(runtime):
    external(runtime)
    _, _, _, source, model, _, _ = runtime
    altered = original_record("altered", {"body": "ALTERED distinct discriminating evidence."})
    altered["title"] = "ALTERED source"
    source.records.append(altered)

    def plan_gap(payload, result):
        if payload["task"] == "read_plan" and payload.get("quality_feedback"):
            result["next_queries"][0]["terms"] = ["ALTERED"]

    model.transform = plan_gap


def accept_quality(factory, runtime, key):
    actor, view = runtime[1], runtime[2]
    current = InvestigationService(factory().engine).get(actor, view["id"])
    return factory().start(
        actor,
        view["id"],
        AnalysisCreate(expected_revision=current.revision),
        key,
    )[1]


def test_later_generation_deadline_preserves_the_prior_checked_report(runtime, quality_runtime):
    configure_quality_gap(runtime)
    factory, meter = quality_runtime
    accepted = accept_quality(factory, runtime, "synthetic-quality-late-generation")
    service, fired = factory(), [False]
    checkpoint = service._checkpoint

    def expire_new_generation(identifier, token, progress, **kwargs):
        if progress.get("active_report_round") == 1 and not fired[0]:
            fired[0] = True
            progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        checkpoint(identifier, token, progress, **kwargs)

    service._checkpoint = expire_new_generation
    service.execute(accepted["id"], accepted["input_hash"])
    result = factory().get(runtime[1], runtime[2]["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert "n=2" in result.result.report["final_text"]
    assert result.result.report["rounds"][1]["status"] == "stopped_budget"
    assert result.result.report["rounds"][1]["error_code"] == "analysis_time_budget"
    assert meter.stages.count("biocheck_write") == 1
    assert len(result.evidence) == 3


def test_unknown_quality_query_keeps_the_checked_body_and_original_action(runtime, quality_runtime, database):
    configure_quality_gap(runtime)
    factory, meter = quality_runtime
    accepted = accept_quality(factory, runtime, "synthetic-quality-unknown-query")
    service = factory()
    checkpoint = service._checkpoint

    def stop_after_sent(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if progress["query_policy"].get("quality_round") and any(
            q["status"] == "sent" for q in progress["queries"]
        ):
            raise SQLAlchemyError("SYNTHETIC quality query intent committed then worker exited")

    service._checkpoint = stop_after_sent
    with pytest.raises(RuntimeError):
        service.execute(accepted["id"], accepted["input_hash"])
    expire(database, accepted["id"])
    factory().recover()
    factory().execute(accepted["id"], accepted["input_hash"])
    result = factory().get(runtime[1], runtime[2]["id"], accepted["id"])
    assert result.state == "blocked" and result.error_code == "analysis_query_outcome_unconfirmed"
    assert "n=2" in result.result.report["final_text"]
    assert meter.stages.count("biocheck_write") == 1
    assert result.query_policy["pending_action"]["id"] == result.queries[-1].action_id
    assert result.query_policy["budget"]["usage"]["unknown_tool_calls"] == 1
    assert_query_policy(result.model_dump(mode="json"), expected_policy="query_planner")


def test_received_repair_crossing_deadline_keeps_old_checked_text_without_reposting(runtime, quality_runtime):
    factory, meter = quality_runtime
    meter.mode = "repair"
    accepted = accept_quality(factory, runtime, "synthetic-quality-expired-repair")
    service = factory()
    checkpoint = service._checkpoint

    def expire_after_revision(identifier, token, progress, **kwargs):
        if any(c["stage"] == "biocheck_revise" and c["status"] == "received" for c in progress["calls"]):
            progress["started_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        checkpoint(identifier, token, progress, **kwargs)

    service._checkpoint = expire_after_revision
    service.execute(accepted["id"], accepted["input_hash"])
    result = factory().get(runtime[1], runtime[2]["id"], accepted["id"])
    assert result.state == "needs_review", result.error_code
    assert "bad" in result.result.report["final_text"]
    assert result.result.report["rounds"][1]["status"] == "stopped_budget"
    assert result.result.report["rounds"][1]["revision"]["status"] == "changed"
    assert meter.stages.count("biocheck_revise") == 1
    assert meter.stages.count("biocheck_check") == 1
    posts = list(meter.stages)
    factory().execute(accepted["id"], accepted["input_hash"])
    assert meter.stages == posts


def test_baseline_feedback_does_not_expand_the_original_query_sequence(runtime, quality_runtime):
    factory, meter = quality_runtime
    accepted = accept_quality(factory, runtime, "synthetic-baseline-quality-no-expanded-query")
    factory().execute(accepted["id"], accepted["input_hash"])
    result = factory().get(runtime[1], runtime[2]["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert len(result.queries) == 1
    assert len(result.result.report["rounds"]) == 1
    assert result.result.report["quality_feedback"]["evidence_gaps"]
    assert result.result.report["rounds"][0]["feedback_stop_reason"] == "baseline_fixed_sequence"
    assert meter.stages.count("biocheck_write") == 1


def test_later_generation_unsent_input_limit_preserves_checked_report(runtime, quality_runtime):
    from bioradar.adapters.reasoning_model import ModelCallError

    configure_quality_gap(runtime)
    factory, meter = quality_runtime
    accepted = accept_quality(factory, runtime, "synthetic-quality-input-limit")
    service = factory()

    def input_limit(*args, **kwargs):
        raise ModelCallError(
            "model_input_budget",
            "SYNTHETIC oversized optional report input was not sent",
            SimpleNamespace(status="not_sent"),
            422,
        )

    service._quality_synthesis = input_limit
    service.execute(accepted["id"], accepted["input_hash"])
    result = factory().get(runtime[1], runtime[2]["id"], accepted["id"])
    assert result.state == "completed", result.error_code
    assert "n=2" in result.result.report["final_text"]
    assert result.result.report["rounds"][1]["status"] == "stopped_budget"
    assert result.result.report["rounds"][1]["error_code"] == "model_input_budget"
    assert meter.stages.count("biocheck_write") == 1
