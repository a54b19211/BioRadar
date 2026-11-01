"""Final assessment references are repaired once through durable actual PG model receipts."""

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


def test_final_current_claim_repair_received_restart_preserves_prose_and_never_repeats(
    runtime, database, monkeypatch
):
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.fail("Native query-planner is required")
    make, actor, view, _, model, settings, _ = runtime
    settings.query_planner_project_dir = Path(directory)
    settings.analysis_query_policy = "query_planner"
    posts = []

    def warning(identifier):
        return {
            "decision": "watch",
            "supporting_claim_ids": [identifier],
            "opposing_claim_ids": [],
            "critical_unknowns": ["SYNTHETIC truth unresolved"],
            "next_actions": ["SYNTHETIC verify one specific observation"],
            "reason": "SYNTHETIC overall watch with a targeted check",
        }

    def transform(payload, value):
        if payload["task"] == "read_plan":
            value["next_queries"], value["gaps"] = [], []
            value["warning_assessment"] = warning(value["claims"][0]["id"])
        elif payload["task"] == "synthesize":
            old = value["claims"][0]["id"]
            value["claims"][0]["id"] = "SYNTHETIC_final_claim"
            value["warning_assessment"] = warning(old)
            value["competing_hypotheses"] = []

    model.transform = transform

    def transport(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        posts.append(payload["task"])
        if payload["task"] != "repair_final_references":
            return model.handler(request)
        original = payload["invalid_output"]
        repaired = copy.deepcopy(original)
        assert payload["allowed_claim_ids"] == ["SYNTHETIC_final_claim"]
        repaired["warning_assessment"]["supporting_claim_ids"] = ["SYNTHETIC_final_claim"]
        return httpx.Response(
            200,
            json={
                "id": "SYNTHETIC-assessment-repair",
                "model": "synthetic-model",
                "choices": [{"message": {"content": json.dumps(repaired)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    accepted = start(runtime, auto_retrieve=True, query_policy="query_planner")
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(transport)) as client:
        service = make()
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        checkpoint = service._checkpoint

        def exit_after_received(identifier, token, progress, **kwargs):
            checkpoint(identifier, token, progress, **kwargs)
            if any(
                call.get("call_key", "").endswith(":assessment-repair") and call["status"] == "received"
                for call in progress["calls"]
            ):
                raise SQLAlchemyError("SYNTHETIC exit after final reference response committed")

        service._checkpoint = exit_after_received
        with pytest.raises(RuntimeError):
            service.execute(accepted["id"], accepted["input_hash"])
        assert posts.count("repair_final_references") == 1
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            repair = copy.deepcopy(row.progress["calls"][-1])
            assert repair["status"] == "received" and repair["call_key"] == "synthesize:assessment-repair"
        expire(database, accepted["id"])
        service = make()
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        service.recover()
        service.execute(accepted["id"], accepted["input_hash"])
    result = make().get(actor, view["id"], accepted["id"])
    assert result.state in {"completed", "needs_review"}, result.error_code
    assert result.result.warning_assessment.supporting_claim_ids == ["SYNTHETIC_final_claim"]
    assert result.result.assessment_binding["status"] == "bound"
    assert posts.count("repair_final_references") == 1
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        first = next(c for c in row.progress["calls"] if c.get("call_key") == "synthesize")
        repaired = next(
            c for c in row.progress["calls"] if c.get("call_key") == "synthesize:assessment-repair"
        )
        assert repaired == repair
        assert first["output"]["warning_assessment"]["supporting_claim_ids"] != ["SYNTHETIC_final_claim"]
        assert first["output"]["conditional_answer"] == repaired["output"]["conditional_answer"]
        assert first["output"]["claims"] == repaired["output"]["claims"]
        assert row.result["warning_assessment"]["supporting_claim_ids"] == ["SYNTHETIC_final_claim"]
    before = len(posts)
    make().execute(accepted["id"], accepted["input_hash"])
    assert len(posts) == before
