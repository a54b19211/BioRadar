"""Fixed-version delta review and its definition/fairness boundaries; no external calls."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from bioradar.research.datasets.build import build_dataset
from bioradar.research.mining.evaluation import evaluate_patterns
from bioradar.research.mining.kdd import run_kdd
from bioradar.research.mining.models import ResourceLimits
from test_kdd_mining import metric, metric_record, policy
from test_mining import snapshot

pytestmark = pytest.mark.ENGINE


def fixed_versions():
    old = metric_record("same-financial-record", 100, 0)
    new_ref = old.record_ref.model_copy(
        update={"adopted_version": "second-fixed-version", "content_hash": "second-content"}
    )

    def observations(last_year, ref):
        rows = []
        for year in range(2014, last_year + 1):
            value = 100 * 2 ** (year - 2014) if year <= 2022 else 30000 if year == 2023 else 35000
            obs = metric(
                "same-financial-record",
                value,
                0,
                observation_id="period-" + str(year),
                period_start=datetime(year, 1, 1, tzinfo=UTC),
                period_end=datetime(year + 1, 1, 1, tzinfo=UTC),
            )
            rows.append(
                obs.model_copy(update={"provenance": obs.provenance.model_copy(update={"provider_ref": ref})})
            )
        return tuple(rows)

    old = old.model_copy(update={"metric_observations": observations(2022, old.record_ref)})
    new_text = old.normalized_text + " SYNTHETIC later disclosure contains two additional periods"
    new = old.model_copy(
        update={
            "record_ref": new_ref,
            "evidence_refs": (new_ref,),
            "available_at": old.available_at + timedelta(seconds=1),
            "observed_at": old.observed_at + timedelta(seconds=1),
            "metric_observations": observations(2024, new_ref),
            "normalized_text": new_text,
            "normalized_text_hash": sha256(new_text.encode()).hexdigest(),
            "document_hash": sha256(new_text.encode()).hexdigest(),
        }
    )
    return old, new


def test_new_periods_need_same_fixed_record_baseline_and_outrank_historical_stock():
    old, new = fixed_versions()
    fixed = snapshot([old, new])
    before = fixed.model_dump(mode="json")
    limits = ResourceLimits(review_capacity=2, max_candidates_per_group=2, max_candidates_per_method=100)
    ordinary_policy = policy("business", limits=limits)
    assert "monitor_changed_refs" not in ordinary_policy.model_dump(mode="json")
    ordinary = run_kdd(fixed, ordinary_policy)
    incremental = run_kdd(fixed, policy("business", limits=limits, monitor_changed_refs=(new.record_ref,)))
    evaluations = {row["candidate_key"]: row for row in incremental.pattern_evaluations}
    assert len(incremental.candidates) == 2
    assert all(
        evaluations[c.candidate_key]["incremental_context"]["classification"] == "new_period"
        for c in incremental.candidates
    )
    assert {
        evaluations[c.candidate_key]["incremental_context"]["current_period_start"][:4]
        for c in incremental.candidates
    } == {"2023", "2024"}
    assert incremental.method_results == ordinary.method_results
    assert {
        e["candidate_key"]: (e["eligible"], e["value_score"]) for e in incremental.pattern_evaluations
    } == {e["candidate_key"]: (e["eligible"], e["value_score"]) for e in ordinary.pattern_evaluations}
    assert {c.candidate_key for c in ordinary.candidates} != {c.candidate_key for c in incremental.candidates}
    assert len(incremental.method_results[0].candidates) > len(incremental.candidates)
    assert incremental.data_preparation["selection_summary"]["all_generated_candidates_retained"]
    assert any(
        e["incremental_context"]["classification"] == "existing_fixed_period"
        for e in incremental.pattern_evaluations
    )
    missing = run_kdd(
        snapshot([new]), policy("business", limits=limits, monitor_changed_refs=(new.record_ref,))
    )
    assert missing.pattern_evaluations and all(
        e["incremental_context"]["classification"] == "missing_prior_fixed_version_baseline"
        for e in missing.pattern_evaluations
    )
    other = metric_record("another-company", 100, 0, subject="different-company")
    mixed = run_kdd(
        snapshot([other, new]), policy("business", limits=limits, monitor_changed_refs=(new.record_ref,))
    )
    temporal = [
        e
        for e in mixed.pattern_evaluations
        if e["incremental_context"]["classification"] != "no_qualified_numeric_period_mapping"
    ]
    assert temporal and all(
        e["incremental_context"]["classification"] == "missing_prior_fixed_version_baseline" for e in temporal
    )
    assert fixed.model_dump(mode="json") == before


def test_changed_metric_definition_cannot_borrow_prior_version_period_baseline():
    old, new = fixed_versions()
    v1 = old.model_copy(
        update={
            "metric_observations": tuple(
                obs for obs in old.metric_observations if obs.period_start.year in (2021, 2022)
            )
        }
    )
    v2 = new.model_copy(
        update={
            "metric_observations": tuple(
                obs.model_copy(update={"metric_version": "different-definition-v2"})
                for obs in new.metric_observations
                if obs.period_start.year in (2022, 2023)
            )
        }
    )
    result = run_kdd(snapshot([v1, v2]), policy("business", monitor_changed_refs=(new.record_ref,)))
    assert result.pattern_evaluations
    assert all(
        e["incremental_context"]["classification"] == "missing_prior_fixed_version_baseline"
        for e in result.pattern_evaluations
    )
    assert result.data_preparation["selection_summary"]["incremental_review"]["priority_applied"] is False


def test_no_new_period_mapping_keeps_original_high_importance_fair_buckets():
    old, new = fixed_versions()
    fixed = snapshot([old, new])
    limits = ResourceLimits(review_capacity=2, max_candidates_per_group=2)
    ordinary_policy = policy("business", limits=limits, exploration_fraction=0)
    base = run_kdd(fixed, ordinary_policy).method_results[0].candidates[0]
    metrics = {key: value for key, value in base.metrics.items() if key != "period_unit_ids"}
    candidates = [
        base.model_copy(
            update={
                "candidate_key": "a" * 64,
                "method_id": "a",
                "importance": "low",
                "urgency": "routine",
                "data_gaps": (),
                "metrics": metrics,
            }
        ),
        base.model_copy(
            update={
                "candidate_key": "b" * 64,
                "method_id": "b",
                "importance": "high",
                "urgency": "routine",
                "data_gaps": (),
                "metrics": metrics,
            }
        ),
        base.model_copy(
            update={
                "candidate_key": "c" * 64,
                "method_id": "b",
                "importance": "high",
                "urgency": "routine",
                "data_gaps": (),
                "metrics": metrics,
            }
        ),
    ]
    expected = evaluate_patterns(candidates, ordinary_policy)[0]
    context_policy = policy(
        "business", limits=limits, exploration_fraction=0, monitor_changed_refs=(new.record_ref,)
    )
    actual, evaluations, _, summary = evaluate_patterns(
        candidates, context_policy, dataset=build_dataset(fixed, context_policy)
    )
    assert {c.candidate_key for c in expected} == {"b" * 64, "c" * 64}
    assert actual == expected
    assert all(
        e["incremental_context"]["classification"] == "no_qualified_numeric_period_mapping"
        for e in evaluations
    )
    assert summary["incremental_review"]["priority_applied"] is False
