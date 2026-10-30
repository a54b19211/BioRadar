"""PostgreSQL feedback on fixed generated candidates, including capacity deferrals."""

import pytest
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.schemas import FeedbackCreate, RunCreate
from bioradar.research.engine.service import EngineService
from bioradar.research.mining.models import ResourceLimits
from bioradar.research.mining.runner import run_mining
from test_engine_persistence import database as database  # noqa: F401
from test_engine_persistence import service as service  # noqa: F401
from test_engine_persistence import settings as settings  # noqa: F401
from test_engine_persistence import tokens
from test_kdd_mining import metric_record, policy
from test_mining import snapshot

pytestmark = pytest.mark.ENGINE


def test_unselected_fixed_candidate_feedback_is_persistent_and_scope_bound(service, settings, database):
    data = snapshot(
        [metric_record("q1", 100, 0), metric_record("q2", 150, 1), metric_record("q3", 210, 2)]
    )
    actor = DevelopmentAccess(settings).actor()
    _, adopted = service.adopt_snapshot(actor, data, "snapshot")
    _, accepted = service.create_run(
        actor,
        RunCreate(
            snapshot_id=adopted["id"],
            policy=policy("business", limits=ResourceLimits(review_capacity=1)),
        ),
        "run",
    )
    current = tokens(database, accepted["id"])
    fixed = service.start_run(accepted["id"], *current)
    assert fixed is not None
    result = run_mining(*fixed)
    assert service.finish_run(accepted["id"], *current, result)
    selected = {candidate.candidate_key for candidate in result.candidates}
    deferred = {
        item["candidate_key"]
        for item in result.selection_decisions
        if item["decision"] == "capacity_deferred"
    }
    assert len(selected) == 1 and len(deferred) == 1
    candidate_key = deferred.pop()
    assert candidate_key not in selected
    assert candidate_key in {
        candidate.candidate_key for method in result.method_results for candidate in method.candidates
    }
    original = service.get_run(actor, accepted["id"]).result.model_dump(mode="json")
    body = FeedbackCreate(
        candidate_key=candidate_key,
        result_hash=result.result_hash,
        decision="needs_check",
        reason="SYNTHETIC negative-case review of a capacity-deferred fixed candidate",
    )
    first = service.add_feedback(actor, accepted["id"], body, "review")
    assert first[0] == 201 and first[1]["candidate_key"] == candidate_key
    assert service.add_feedback(actor, accepted["id"], body, "review") == first
    saved = service.get_run(actor, accepted["id"])
    assert len(saved.feedback) == 1 and saved.feedback[0].id == first[1]["id"]
    assert saved.result.model_dump(mode="json") == original
    for changed, code in [
        (body.model_copy(update={"result_hash": "0" * 64}), "result_hash_conflict"),
        (body.model_copy(update={"candidate_key": "not-a-fixed-candidate"}), "candidate_not_found"),
    ]:
        with pytest.raises(DomainError) as invalid:
            service.add_feedback(actor, accepted["id"], changed, "invalid")
        assert invalid.value.code == code
    narrowed = settings.model_copy(update={"engine_source_id_allowlist": {"fixture": ["other-source"]}})
    restricted = EngineService(database, narrowed)
    with pytest.raises(DomainError) as hidden:
        restricted.add_feedback(DevelopmentAccess(narrowed).actor(), accepted["id"], body, "hidden")
    assert hidden.value.status_code == 404
