"""One existing RQ job performs pure mining and atomically adopts its result."""

import logging

from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.data.database import create_database
from bioradar.research.engine.decision_service import DecisionExecution
from bioradar.research.engine.service import EngineService

logger = logging.getLogger(__name__)


def execute_run(identifier: str, attempt: int, token: str, input_hash: str) -> str:
    settings = Settings()
    database = create_database(settings)
    if database is None:
        raise RuntimeError("Research database is not configured")
    engine = EngineService(database, settings)
    try:
        fixed_input = engine.start_run(identifier, attempt, token, input_hash)
        if fixed_input is None:
            return identifier
        if isinstance(fixed_input, DecisionExecution):
            from bioradar.research.decision.runner import evaluate_decision

            result = evaluate_decision(fixed_input.input, fixed_input.policy, fixed_input.snapshots)
        else:
            from bioradar.research.mining.runner import run_mining

            result = run_mining(*fixed_input)
        engine.finish_run(identifier, attempt, token, input_hash, result)
        return identifier
    except Exception as exc:
        logger.warning(
            "Engine computation failed: run=%s attempt=%s type=%s", identifier, attempt, type(exc).__name__
        )
        try:
            # Only the known permanent budget failure exposes its static safe diagnosis.
            code = (
                "result_budget_exceeded"
                if isinstance(exc, DomainError) and exc.code == "result_budget_exceeded"
                else "computation_failed"
            )
            engine.fail_run(identifier, attempt, token, input_hash, code=code)
        except Exception:
            logger.warning("Engine failure state unavailable: run=%s attempt=%s", identifier, attempt)
        # RQ receives a sanitized failure, not provider/document/credential text.
        raise RuntimeError("Research computation failed; see persisted run state") from None
    finally:
        database.dispose()
