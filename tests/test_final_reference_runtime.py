"""One shared source-reference repair slot and durable unfinished reading delivery."""

import copy
import json
import os
from datetime import UTC, datetime, timedelta
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


@pytest.mark.parametrize("case", ["repaired", "invalid_repair", "invalid_response", "time_budget", "unknown"])
def test_synthesis_reference_repair_and_prior_reading_survive_durable_recovery(runtime, database, case):
    directory = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not directory:
        pytest.fail("Native query-planner is required")
    make, actor, view, _, model, settings, _ = runtime
    settings.query_planner_project_dir = Path(directory)
    settings.analysis_query_policy = "query_planner"
    posts = []

    def transform(payload, value):
        if payload["task"] == "read_plan":
            value["next_queries"], value["gaps"] = [], []
        elif payload["task"] == "synthesize":
            value["claims"][0]["evidence_links"][0]["passage_id"] = "SYNTHETIC-missing-passage"

    model.transform = transform

    def transport(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        posts.append(payload["task"])
        if payload["task"] != "repair_final_references":
            return model.handler(request)
        if case == "unknown":
            raise httpx.ReadTimeout("SYNTHETIC unknown response", request=request)
        fixed = copy.deepcopy(payload["invalid_output"])
        link = fixed["claims"][0]["evidence_links"][0]
        link["passage_id"] = payload["allowed_reference_ids"][link["evidence_key"]][0]
        if case == "invalid_repair":
            # Even a valid ID does not authorize changing the claim to pass the check.
            fixed["claims"][0]["text"] = "SYNTHETIC changed claim must be rejected"
        return httpx.Response(
            200,
            json={
                "id": "SYNTHETIC-final-reference-repair",
                "model": "synthetic-model",
                "choices": [{
                    "message": {
                        "content": (
                            "SYNTHETIC malformed JSON" if case == "invalid_response" else json.dumps(fixed)
                        )
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    accepted = start(runtime, auto_retrieve=True, query_policy="query_planner")
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(transport)) as client:
        service = make()
        service.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
        checkpoint = service._checkpoint

        def exit_after_durable_result(identifier, token, progress, **kwargs):
            if case == "time_budget" and any(
                c["stage"] == "synthesize" and c["status"] == "received" for c in progress["calls"]
            ):
                # Controlled clock exhaustion after receiving the original synthesis.
                progress["started_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
            checkpoint(identifier, token, progress, **kwargs)
            if case == "repaired" and any(
                c.get("call_key", "").endswith(":assessment-repair") and c["status"] == "received"
                for c in progress["calls"]
            ):
                raise SQLAlchemyError("SYNTHETIC exit after repaired receipt")
            if case in {"invalid_repair", "invalid_response", "time_budget"} and progress.get(
                "final_delivery_result"
            ):
                raise SQLAlchemyError("SYNTHETIC exit after retained reading saved")

        service._checkpoint = exit_after_durable_result
        if case == "unknown":
            service.execute(accepted["id"], accepted["input_hash"])
        else:
            with pytest.raises(RuntimeError):
                service.execute(accepted["id"], accepted["input_hash"])
        with Session(database) as session:
            saved = copy.deepcopy(session.get(InvestigationAnalysisRecord, accepted["id"]).progress)
        before = len(posts)
        if case != "unknown":
            expire(database, accepted["id"])
            resumed = make()
            resumed.model_call = lambda config, **kwargs: complete_json(config, client=client, **kwargs)
            resumed.recover()
            resumed.execute(accepted["id"], accepted["input_hash"])
        result = make().get(actor, view["id"], accepted["id"])
        with Session(database) as session:
            row = session.get(InvestigationAnalysisRecord, accepted["id"])
            current = copy.deepcopy(row.progress)
            stored = copy.deepcopy(row.result)
        assert posts.count("repair_final_references") == (0 if case == "time_budget" else 1)
        if case == "unknown":
            assert result.state == "blocked" and result.error_code == "model_transport_unconfirmed"
            assert stored is None and "final_delivery_result" not in current
            assert current["calls"][-1]["status"] == "unconfirmed"
        elif case == "repaired":
            assert result.state in {"completed", "needs_review"}, result.error_code
            original = next(c for c in current["calls"] if c.get("call_key") == "synthesize")
            repair = next(c for c in current["calls"] if c.get("call_key") == "synthesize:assessment-repair")
            assert repair in saved["calls"]
            assert original["output"]["conditional_answer"] == stored["conditional_answer"]
            assert original["output"]["claims"][0]["text"] == stored["claims"][0]["text"]
            assert stored["claims"][0]["evidence_links"][0]["passage_id"] != "SYNTHETIC-missing-passage"
            assert (
                original["output"]["claims"][0]["evidence_links"][0]["passage_id"]
                == "SYNTHETIC-missing-passage"
            )
        else:
            assert len(posts) == before
            assert result.state == "needs_review" and stored["report"] is None
            assert stored["review"]["status"] == "needs_review"
            assert stored["conditional_answer"] == saved["query_policy"]["reading"]["conditional_answer"]
            assert current["final_delivery_result"] == saved["final_delivery_result"] == stored
            assert current["calls"] == saved["calls"]
            assert result.error_code == (
                "analysis_time_budget" if case == "time_budget"
                else "model_output_invalid" if case == "invalid_response"
                else "analysis_reference_repair_invalid"
            )
            assert "最终综合未完成" in stored["review"]["overall_limitations"][0]
            assert stored.get("warning_assessment") is None
            assert stored["query_policy"]["synthesis_reading_basis"]["status"] == "unfinished_reading_draft"
            from bioradar.delivery.research_report import render_report_markdown

            markdown = render_report_markdown(result.model_dump(mode="json"))
            assert "# 阅读草稿（综合未完成）" in markdown
            assert stored["conditional_answer"]["answer"] in markdown
            assert stored["review"]["overall_limitations"][0] in markdown
        before = len(posts)
        make().execute(accepted["id"], accepted["input_hash"])
        assert len(posts) == before
