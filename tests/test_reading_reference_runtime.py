"""PG recovery of one corrected reading and retained prior coverage; synthetic HTTP only."""

import copy
import json
import os
from pathlib import Path

import httpx
import pytest
from bioradar.adapters.reasoning_model import complete_json
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start
from test_query_policy_runtime import expire

pytestmark = pytest.mark.ENGINE


@pytest.mark.parametrize("case", ["received_recovery", "invalid_optional"])
def test_reference_repair_uses_pg_receipts_without_repeating_or_claiming_full_coverage(
    runtime, database, case
):
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.skip("Needs installed query-planner for actual research execution")
    make, actor, view, source, model, settings, _ = runtime
    source.include_collection_sequence = True
    settings.query_planner_project_dir = Path(directory)
    settings.analysis_query_policy = "query_planner"
    reads = 0
    posts = []
    prior = {}

    def transform(payload, value):
        nonlocal reads
        if payload["task"] != "read_plan":
            return
        reads += 1
        # Calculations may cause a legitimate reread before a new source arrives.
        # Inject this fault only after actual additional material was adopted.
        new_material = case == "invalid_optional" and len(payload["evidence"]) > 1
        if case == "received_recovery" and reads == 1 or new_material:
            value["claims"][0]["evidence_links"][0]["passage_id"] = "SYNTHETIC-missing-passage"
        if case == "received_recovery" or new_material:
            value["next_queries"] = []
            value["gaps"] = []

    model.transform = transform

    def transport(request):
        wire = json.loads(request.content)
        payload = json.loads(wire["messages"][1]["content"])
        posts.append(payload["task"])
        if payload["task"] != "repair_structured_reading_fields":
            response = model.handler(request)
            if payload["task"] == "read_plan":
                body = response.json()
                body["usage"] = {"prompt_tokens": 30000, "completion_tokens": 50, "total_tokens": 30050}
                return httpx.Response(200, json=body)
            return response
        assert "invalid_output" not in payload and "original_request" not in payload
        assert payload["invalid_fields"][0]["invalid_part"] == "SYNTHETIC-missing-passage"
        value = {
            "changes": [
                {
                    "path": payload["field_paths"][0],
                    "operation": "replace",
                    "value": next(iter(payload["allowed_reference_ids"].values()))[0]
                    if case == "received_recovery"
                    else "SYNTHETIC-still-missing",
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "id": "synthetic-reading-repair",
                "model": "synthetic-model",
                "choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    accepted = start(runtime, auto_retrieve=True, query_policy="query_planner")
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(transport)) as client:
        service = make()
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        checkpoint = service._checkpoint

        def observe(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            state = progress.get("query_policy", {})
            if (
                case == "invalid_optional"
                and state.get("reading")
                and not state.get("refresh_not_semantically_reviewed")
            ):
                prior.update(
                    reading=copy.deepcopy(state["reading"]), fingerprint=state["reading_evidence_hash"]
                )
            if case == "received_recovery" and any(
                c["stage"] == "read_plan_repair" and c["status"] == "received" for c in progress["calls"]
            ):
                raise SQLAlchemyError("SYNTHETIC worker exit after durable corrected model response")

        service._checkpoint = observe
        if case == "received_recovery":
            with pytest.raises(RuntimeError):
                service.execute(accepted["id"], accepted["input_hash"])
            assert posts.count("repair_structured_reading_fields") == 1
            with Session(database) as session:
                saved = session.get(InvestigationAnalysisRecord, accepted["id"]).progress
                assert saved["calls"][-1]["stage"] == "read_plan_repair"
                assert saved["calls"][-1]["status"] == "received"
                assert next(iter(saved["reading_repairs"].values()))["status"] == "prepared"
            expire(database, accepted["id"])
            service = make()
            service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
            service.recover()
        service.execute(accepted["id"], accepted["input_hash"])
        result = make().get(actor, view["id"], accepted["id"])
        assert result.state in {"completed", "needs_review"}, result.model_dump(mode="json")
        with Session(database) as session:
            saved = copy.deepcopy(session.get(InvestigationAnalysisRecord, accepted["id"]).progress)
        assert posts.count("repair_structured_reading_fields") == 1
        repair = next(iter(saved["reading_repairs"].values()))
        invalid_call = next(c for c in saved["calls"] if c.get("call_key") == repair["original_call_key"])
        assert invalid_call["status"] == "received"
        assert (
            invalid_call["output"]["claims"][0]["evidence_links"][0]["passage_id"]
            == "SYNTHETIC-missing-passage"
        )
        if case == "invalid_optional":
            assert result.state == "needs_review"
            assert saved["query_policy"]["reading"] == prior["reading"]
            assert saved["query_policy"]["reading_evidence_hash"] == prior["fingerprint"]
            assert saved["query_policy"]["refresh_not_semantically_reviewed"] is True
            assert len(saved["evidence"]) == 2
            assert repair["status"] == "invalid"
        else:
            assert repair["status"] == "validated"
            # Later calculation results can legitimately trigger a new reading.
            read_calls = [call for call in saved["calls"] if call["stage"] == "read_plan"]
            assert len({call["call_key"] for call in read_calls}) == reads
            if len(read_calls) > 1:
                assert not read_calls[0]["payload"].get("analysis_results")
                assert all(call["payload"].get("analysis_results") for call in read_calls[1:])
        before = len(posts)
        service.execute(accepted["id"], accepted["input_hash"])
        assert len(posts) == before


def test_unexpected_execution_failure_retains_safe_diagnostic_without_exception_message(runtime, database):
    make, actor, view, _, model, _, _ = runtime
    accepted = start(runtime)
    service = make()
    sensitive = "SYNTHETIC-private-key-do-not-log"

    def fail_reading(*args, **kwargs):
        raise KeyError(sensitive)

    service._policy_research = fail_reading
    with pytest.raises(RuntimeError) as exc:
        service.execute(accepted["id"], accepted["input_hash"])
    assert sensitive not in str(exc.value)
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        diagnostic = row.progress["execution_diagnostic"]
        assert row.state == "failed" and row.error_code == "analysis_execution_failed"
        assert row.progress["calls"] == []
    assert diagnostic["exception_type"] == "KeyError"
    assert 1 <= len(diagnostic["frames"]) <= 16
    assert any(frame["function"] == "fail_reading" for frame in diagnostic["frames"])
    assert all(set(frame) == {"file", "function", "line"} for frame in diagnostic["frames"])
    assert all("/" not in frame["file"] and isinstance(frame["line"], int) for frame in diagnostic["frames"])
    assert sensitive not in json.dumps(diagnostic)
    result = make().get(actor, view["id"], accepted["id"]).model_dump(mode="json")
    assert result["error_code"] == "analysis_execution_failed"
    assert "execution_diagnostic" not in result and sensitive not in json.dumps(result)
    assert model.requests == []
