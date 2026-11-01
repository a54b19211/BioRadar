"""Persisted topic fetch recovery: recorded pending/unknown work is never resent."""

from datetime import UTC, datetime, timedelta

import pytest
from bioradar.topics.models import TopicScopeRecord
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_engine_persistence import database as database  # noqa: F401
from test_topics import create
from test_topics import runtime as runtime  # noqa: F401

pytestmark = pytest.mark.ENGINE


def test_topic_fetch_checkpoint_recovers_known_run_and_preserves_unknown_without_post(runtime):
    make, _, _ = runtime
    identifier = create(make)
    service = make()
    with Session(service.engine.database) as session:
        scope = session.scalar(select(TopicScopeRecord).where(TopicScopeRecord.topic_id == identifier))
        scope_id = scope.id
    requests = []

    def interrupted(context, *, call_key, prior_receipt, checkpoint):
        assert prior_receipt is None and context["may_send"] is False  # Demo never submits a fetch.
        receipt = {
            "status": "received",
            "run_id": "SYNTHETIC-sealed-reader-run",
            "request_hash": "a" * 64,
            "result": {"status": "pending"},
        }
        checkpoint(receipt)
        requests.append("recorded_pending")
        raise RuntimeError("SYNTHETIC lost coordinator after saved checkpoint")

    service.fetch_hook = interrupted
    now = datetime.now(UTC) + timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="SYNTHETIC lost"):
        service._fetch_source(scope_id, "fixture-source", now=now)

    def reconciled(context, *, call_key, prior_receipt, checkpoint):
        assert prior_receipt["run_id"] == "SYNTHETIC-sealed-reader-run"
        assert context["may_send"] is False
        requests.append("reconcile_only")
        return {
            **prior_receipt,
            "result": {"status": "completed", "qualification": "sealed_fixture_no_fetch"},
        }

    recovered = make()
    recovered.fetch_hook = reconciled
    recovered._fetch_source(scope_id, "fixture-source", now=now + timedelta(seconds=1))
    with Session(service.engine.database) as session:
        saved = session.get(TopicScopeRecord, scope_id).progress["_fetch"]["fixture-source"]
        assert saved["receipt"]["result"]["status"] == "completed"
        original_key = saved["call_key"]

    # A separate topic models an uncertain request; never regress a terminal receipt.
    from bioradar.topics.models import TopicCreate
    from test_topics import definition

    _, second = service.create(
        service.actor, TopicCreate(is_demo=True, definition=definition()), "second-topic"
    )
    with Session(service.engine.database) as session:
        second_scope = session.scalar(
            select(TopicScopeRecord).where(TopicScopeRecord.topic_id == second["id"])
        )
        unknown_scope_id = second_scope.id

    def unknown(context, *, call_key, prior_receipt, checkpoint):
        assert prior_receipt is None and call_key != original_key
        return {"status": "unknown", "request_hash": "b" * 64, "reason": "no_response_no_run_id"}

    recovered.fetch_hook = unknown
    recovered._fetch_source(unknown_scope_id, "fixture-source", now=now + timedelta(seconds=2))

    def leave_unknown(context, *, call_key, prior_receipt, checkpoint):
        assert prior_receipt["status"] == "unknown" and "run_id" not in prior_receipt
        requests.append("unknown_reconcile_no_post")
        return prior_receipt

    restarted = make()
    restarted.fetch_hook = leave_unknown
    restarted._fetch_source(unknown_scope_id, "fixture-source", now=now + timedelta(seconds=3))
    assert requests == ["recorded_pending", "reconcile_only", "unknown_reconcile_no_post"]
