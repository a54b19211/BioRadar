"""Confirmed paragraph replies survive a real PostgreSQL checkpoint/process exit."""

from datetime import UTC, datetime

import pytest
from bioradar.adapters.biocheck import build_packet, check_biocheck
from bioradar.adapters.reasoning_model import ModelCallReceipt, ModelCompletion, ModelUsage
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import snapshot as snapshot  # noqa: F401
from test_investigation_analysis import runtime as runtime  # noqa: F401
from test_investigation_analysis import start
from test_kdd_finishing_budget import local_biocheck as local_biocheck  # noqa: F401
from test_query_policy_runtime import expire


@pytest.mark.ENGINE
def test_paragraph_received_checkpoint_before_report_restores_from_database(
    runtime, database, local_biocheck
):
    make, _, _, _, _, settings, _ = runtime
    settings.semantic_llm_max_output_tokens = 32768
    accepted = start(runtime)
    sent = []

    def model(config, **kwargs):
        payload = kwargs["payload"]
        sent.append(kwargs["stage"])
        if kwargs["stage"] == "biocheck_verify_coverage":
            output = {
                "checked_unit_ids": payload["checked_unit_ids"],
                "coverage": {"verdict": "complete", "reason": "SYNTHETIC"},
            }
        else:
            unit = payload["units"][0]
            eid = payload["evidence"][0]["id"]
            output = {
                "reviews": [
                    {
                        "unit_id": unit["id"],
                        "verdict": "supported",
                        "reason": "SYNTHETIC",
                        "claims": [
                            {
                                "span": unit["text"],
                                "kind": "fact",
                                "verdict": "supported",
                                "reason": "SYNTHETIC",
                                "evidence": [{"evidence_id": eid, "segment_id": "S1"}],
                            }
                        ],
                        "issues": [],
                        "numeric_resolutions": [],
                    }
                ],
                "coverage": {"verdict": "complete", "reason": "SYNTHETIC"},
            }
        return ModelCompletion(
            content="SYNTHETIC",
            parsed=output,
            receipt=ModelCallReceipt(
                call_id=kwargs["call_id"],
                stage=kwargs["stage"],
                input_hash=kwargs["input_hash"],
                prompt_version=kwargs["prompt_version"],
                requested_model=config.semantic_llm_model,
                configured_model_revision=None,
                status="completed",
                started_at=datetime.now(UTC),
                finish_reason="stop",
                usage=ModelUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
                max_output_tokens=config.semantic_llm_max_output_tokens,
            ),
        )

    service = make()
    service.model_call = model

    def research(identifier, token, fixed, progress):
        key = progress["evidence"][0]["key"]
        draft = f"SYNTHETIC fixed observation [{key}]."
        result = check_biocheck(
            None,
            build_packet(fixed["question"], progress["evidence"]),
            draft,
            service._shared_callback(identifier, token, fixed, progress),
            loaded=local_biocheck,
            repair=False,
            verification_mode="paragraph",
        )
        progress["final_report"] = result
        service._checkpoint(identifier, token, progress)
        raise DomainError("analysis_model_budget", "SYNTHETIC stop after saving checked report", 422)

    service._research_result = research
    checkpoint = service._checkpoint

    def exit_before_report(identifier, token, progress, **kwargs):
        checkpoint(identifier, token, progress, **kwargs)
        if (
            any(
                c["stage"] == "biocheck_verify_coverage" and c["status"] == "received"
                for c in progress["calls"]
            )
            and "final_report" not in progress
        ):
            raise SQLAlchemyError("SYNTHETIC exit after received reply before report")

    service._checkpoint = exit_before_report
    with pytest.raises(RuntimeError):
        service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert "final_report" not in row.progress
        assert len(row.progress["calls"]) == 2
        assert all(c["status"] == "received" for c in row.progress["calls"])
    expire(database, accepted["id"])
    service = make()
    service.model_call = lambda *a, **k: pytest.fail("Persisted paragraph replies cannot be sent twice")
    service._research_result = research
    service.recover()
    service.execute(accepted["id"], accepted["input_hash"])
    with Session(database) as session:
        row = session.get(InvestigationAnalysisRecord, accepted["id"])
        assert row.progress["final_report"]["status"] == "PASS"
        assert (
            row.progress["final_report"]["final_text"]
            == row.progress["final_report"]["verification"]["draft"]
        )
        assert len(row.progress["calls"]) == 2
    assert sent == ["biocheck_verify_paragraph", "biocheck_verify_coverage"]
