"""Evidence/heat/lifecycle regressions with explicit synthetic origin identity."""

from datetime import timedelta

import pytest
from bioradar.research.mining.models import SignalContext, SourceRelation
from bioradar.research.mining.signals import assess_records
from bioradar.topics.tracking import update_tracking
from test_mining import AS_OF, record

pytestmark = pytest.mark.ENGINE
DEFINITION = {"domain": "public_health", "cadence_hours": 24}


def value(row):
    return {
        "matched": True,
        "record_ref": row.record_ref.model_dump(mode="json"),
        "source_id": row.source_id,
        "source_record_id": row.record_ref.object_id,
        "unit": row.record_ref.object_id,
        "study_confirmed": False,
        "source_family": row.source_family,
        "source_independence_status": row.source_independence_status,
        "signal_context": row.signal_context.model_dump(mode="json") if row.signal_context else None,
        "attention": assess_records([row]),
        "received_at": AS_OF.isoformat(),
        "facts": {},
    }


def test_tracking_reuses_repost_groups_and_does_not_turn_silence_into_risk_resolution():
    first = record("origin", "北江医院收治7例不明原因肺炎。", source_family="hospital-a")
    repost = record(
        "repost",
        "转发：北江医院收治7例不明原因肺炎。",
        source_family="news-b",
        signal_context=SignalContext(
            source_relations=(SourceRelation(kind="repost", target_ref=first.record_ref),)
        ),
    )
    initial = update_tracking([value(first)], DEFINITION, {}, as_of=AS_OF)
    updated = update_tracking(
        [value(first), value(repost)], DEFINITION, initial, as_of=AS_OF + timedelta(hours=1)
    )
    assert updated["evidence"]["confirmed_source_groups"] == 1
    assert updated["evidence"]["level"] != "independent_sources"
    assert not updated["model_update"]["needed"]
    quiet = update_tracking(
        [value(first), value(repost)], DEFINITION, updated, as_of=AS_OF + timedelta(hours=2)
    )
    assert quiet["risk"]["status"] == "unresolved"
    assert quiet["lifecycle"] not in {"dormant", "closed"}
    assert quiet["cadence_hours"] == 1
    assert not quiet["model_update"]["needed"]


def test_same_source_new_facts_trigger_update_but_identical_republication_does_not():
    first = record("first", "北江医院收治7例不明原因肺炎。", source_family="hospital-a")
    changed = record("changed", "北江医院收治9例不明原因肺炎，传播方式尚未确认。", source_family="hospital-a")
    initial = update_tracking([value(first)], DEFINITION, {}, as_of=AS_OF)
    updated = update_tracking([value(changed)], DEFINITION, initial, as_of=AS_OF + timedelta(hours=1))
    assert updated["model_update"]["needed"]
    assert updated["heat"]["new_source_groups"] == 0
    again = update_tracking([value(changed)], DEFINITION, updated, as_of=AS_OF + timedelta(hours=2))
    assert not again["model_update"]["needed"]


def test_unknown_anonymous_posts_never_become_independent_confirmation_or_wake_evidence():
    row = record(
        "anonymous",
        "据称北江医院收治7例不明原因肺炎。",
        source_family=None,
        source_independence_status="unresolved",
        signal_context=SignalContext(attribution="anonymous"),
    )
    state = update_tracking([value(row)], DEFINITION, {"lifecycle": "dormant"}, as_of=AS_OF)
    assert state["evidence"]["confirmed_source_groups"] == 0
    assert state["evidence"]["independent_study_count"] is None
    assert state["heat"]["score"] == 0 and state["heat"]["provisional_exposure_score"] > 0
    assert state["lifecycle"] != "warming"


def test_host_queued_budget_wait_is_visible_and_hypotheses_remain_parallel():
    from bioradar.topics.service import analysis_budget_wait
    from bioradar.topics.tracking import hypothesis_comparison

    due = (AS_OF + timedelta(days=1)).isoformat()
    assert analysis_budget_wait(
        {"state": "queued", "progress": {"budget_wait": {"next_budget_at": due}}}
    ) == (True, due)
    assert analysis_budget_wait({"state": "queued", "progress": {}}) == (False, None)
    assert analysis_budget_wait({"state": "waiting_budget", "progress": {"next_budget_at": due}}) == (
        True,
        due,
    )
    rows = [
        {"id": "H1", "supporting_claim_ids": ["C1"], "opposing_claim_ids": ["C2"], "winner": True},
        {"id": "H2", "supporting_claim_ids": [], "opposing_claim_ids": [], "gaps": ["not_tested"]},
    ]
    compared = hypothesis_comparison(rows, [{"claim_id": "C1"}, {"claim_id": "C2"}])
    assert compared["selection"] == "not_automatically_ranked"
    assert all("winner" not in h for h in compared["hypotheses"])
    assert compared["hypotheses"][1]["gaps"] == ["not_tested"]
    assert rows[0]["winner"] is True  # Inputs remain fixed.
    with pytest.raises(ValueError, match="undeclared"):
        hypothesis_comparison([{**rows[0], "supporting_claim_ids": ["C99"]}], [{"claim_id": "C1"}])
