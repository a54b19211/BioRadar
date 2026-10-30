"""Pure KDD fixtures: eligibility and value are separate from queue capacity."""

import copy
from datetime import UTC, datetime

import pytest
from bioradar.research.datasets.build import build_dataset
from bioradar.research.mining.kdd import kdd_versions, run_kdd
from bioradar.research.mining.models import (
    Assessment,
    Embedding,
    KddPolicy,
    MetricObservation,
    MiningPolicy,
    MiningResult,
    ResourceLimits,
    ValuePolicy,
    canonical_hash,
)
from bioradar.research.mining.runner import run_mining
from pydantic import ValidationError
from test_mining import AS_OF, OLD, context, entity, provenance, record, ref, snapshot

pytestmark = pytest.mark.ENGINE


def policy(direction, **extra):
    return KddPolicy(as_of=AS_OF, kdd_direction=direction, **extra)


def metric(name, value, period, *, domain="business", subject="company", **extra):
    starts = [datetime(2026, month, 1, tzinfo=UTC) for month in (1, 4, 7, 10)]
    data = {
        "observation_id": name,
        "domain": domain,
        "subject_ref": ref(subject),
        "metric_id": "synthetic_revenue" if domain == "business" else "synthetic_notifications",
        "metric_version": "definition-v1",
        "value_lower": str(value),
        "value_upper": str(value),
        "unit": "currency" if domain == "business" else "notifications",
        "value_kind": "monetary" if domain == "business" else "count",
        "currency": "USD" if domain == "business" else None,
        "reporting_scope": "synthetic consolidated" if domain == "business" else "synthetic surveillance",
        "population": "synthetic enrolled population",
        "region": "synthetic region",
        "period_start": starts[period],
        "period_end": starts[period + 1],
        "measurement_basis": "reported",
        "binding_status": "source_bound",
        "field_locators": {"value": {"type": "json_pointer", "pointer": "/synthetic/value"}},
        "provenance": provenance(name),
    }
    data.update(extra)
    return MetricObservation(**data)


def metric_record(name, value, period, *, domain="business", subject="company", **extra):
    return record(
        name,
        "SYNTHETIC typed metric fixture " + name,
        canonical_entity_refs=(entity(subject),),
        metric_observations=(metric(name, value, period, domain=domain, subject=subject, **extra),),
    )


def method(result, name):
    return next(value for value in result.method_results if value.method_id == name)


def test_old_policy_and_record_wire_do_not_acquire_kdd_defaults():
    old = MiningPolicy(as_of=AS_OF)
    assert "kdd_direction" not in old.model_dump(mode="json")
    assert "metric_observations" not in record("old", "SYNTHETIC old text").model_dump(mode="json")
    result = run_mining(
        snapshot([record("old", "SYNTHETIC old text")]),
        old.model_copy(update={"method_ids": ("pipeline_landscape",)}),
    )
    assert (
        not {"data_preparation", "pattern_evaluations", "selection_decisions"}
        & result.model_dump(mode="json").keys()
    )
    assert canonical_hash(result.model_dump(mode="json", exclude={"result_hash"})) == result.result_hash
    assert MiningResult.model_validate(result.model_dump(mode="json")) == result
    with pytest.raises(ValidationError):
        KddPolicy(as_of=AS_OF)


def test_business_comparable_period_changes_are_computed_and_full_candidates_retained():
    data = snapshot([metric_record("q1", 100, 0), metric_record("q2", 150, 1), metric_record("q3", 210, 2)])
    before = copy.deepcopy(data.model_dump(mode="json"))
    result = run_kdd(data, policy("business"))
    outcome = method(result, "business_metric_change")
    assert len(outcome.candidates) == 2
    assert sorted(candidate.metrics["effect"]["lower"] for candidate in outcome.candidates) == [50, 60]
    assert all(value["eligible"] for value in result.pattern_evaluations)
    assert result.data_preparation["qualified_count"] == 3
    assert result.data_preparation["denominator"]["population_total"] is None
    assert data.model_dump(mode="json") == before
    assert canonical_hash(result.model_dump(mode="json", exclude={"result_hash"})) == result.result_hash


def test_zero_change_is_value_excluded_not_lost_or_capacity_deferred():
    result = run_kdd(snapshot([metric_record("a", 100, 0), metric_record("b", 100, 1)]), policy("business"))
    assert len(result.method_results[0].candidates) == 1
    assert not result.candidates
    assert result.selection_decisions[0]["decision"] == "value_excluded"
    assert (
        "no_directional_change_or_overlapping_value_intervals"
        in result.pattern_evaluations[0]["value_reasons"]
    )


def test_currency_reporting_scope_and_overlapping_periods_never_fake_growth():
    data = snapshot(
        [
            metric_record("usd", 100, 0),
            metric_record("cny", 1000, 1, currency="CNY"),
            metric_record("restated_scope", 500, 2, reporting_scope="different consolidated scope"),
        ]
    )
    result = run_kdd(data, policy("business"))
    assert len(result.method_results[0].candidates) == 3
    assert not result.candidates
    assert all(not evaluation["eligible"] for evaluation in result.pattern_evaluations)
    overlap = metric_record("overlap", 200, 1, period_start=datetime(2026, 3, 1, tzinfo=UTC))
    result = run_kdd(snapshot([metric_record("q1", 100, 0), overlap]), policy("business"))
    assert result.method_results[0].candidates[0].metrics["comparable"] is False


def test_conflicting_same_period_values_are_preserved_and_not_averaged():
    result = run_kdd(
        snapshot(
            [
                metric_record("q1", 100, 0),
                metric_record("conflict1", 150, 1),
                metric_record("conflict2", 190, 1),
            ]
        ),
        policy("business"),
    )
    assert result.data_preparation["qualified_count"] == 2
    candidate = result.method_results[0].candidates[0]
    assert candidate.metrics["conflicting_values"] is True
    assert candidate.metrics["effect"] is None
    assert len(candidate.supports) == 3
    assert not result.candidates


def test_source_unbound_forecast_missing_currency_and_future_metrics_are_explicit_exclusions():
    rows = [
        metric_record("declared", 100, 0, binding_status="declared"),
        metric_record("forecast", 100, 0, measurement_basis="forecast"),
        metric_record("currency", 100, 0, currency=None),
        metric_record(
            "future", 100, 0, provenance=provenance("future", available=datetime(2027, 1, 1, tzinfo=UTC))
        ),
    ]
    result = run_kdd(snapshot(rows), policy("business"))
    assert result.data_preparation["qualified_count"] == 0
    assert len(result.data_preparation["excluded_records"]) == 4
    assert not result.candidates
    assert result.method_results[0].execution_status == "unsupported"
    assert canonical_hash(result.model_dump(mode="json", exclude={"result_hash"})) == result.result_hash


def test_public_health_notification_change_does_not_become_incidence_and_rate_needs_denominator():
    result = run_kdd(
        snapshot(
            [
                metric_record("p1", 100, 0, domain="public_health", subject="region"),
                metric_record("p2", 200, 1, domain="public_health", subject="region"),
            ]
        ),
        policy("public_health"),
    )
    assert result.candidates
    assert (
        result.candidates[0].metrics["qualification"]
        == "notification_or_measurement_change_not_incidence_or_outbreak"
    )
    assert result.candidates[0].metrics["denominator_known"] is False
    rate = metric_record("rate", 2, 0, domain="public_health", value_kind="ratio")
    result = run_kdd(snapshot([rate]), policy("public_health"))
    assert (
        result.data_preparation["excluded_records"][0]["reason"] == "public_health_rate_denominator_required"
    )


def test_research_documents_can_explore_without_fabricating_independent_studies():
    rows = [
        record("d1", "SYNTHETIC alpha receptor binding mechanism."),
        record("d2", "SYNTHETIC alpha receptor binding pathway."),
    ]
    rows = [
        row.model_copy(update={"study_cluster_ref": None, "study_independence_status": "unresolved"})
        for row in rows
    ]
    result = run_kdd(snapshot(rows), policy("research"))
    assert result.data_preparation["qualified_count"] == 2
    assert all(unit["unit_kind"] == "exploratory_document" for unit in result.data_preparation["units"])
    assert method(result, "research_tfidf_topics").execution_status == "success"
    assert method(result, "research_tfidf_topics").candidates
    assert method(result, "research_evidence_balance").execution_status == "unsupported"
    assert all(
        candidate.metrics["independent_study_count"] is None
        and candidate.metrics["confirmed_independent_study_count"] == 0
        for candidate in method(result, "research_tfidf_topics").candidates
    )


def test_duplicate_reports_do_not_count_as_two_studies_and_conditions_are_explicit():
    shared = ref("one-study", "study")
    assessment = Assessment(
        assessment_ref=ref("assessment"),
        study_cluster_ref=shared,
        stage="animal",
        review_status="reviewed",
        stance="supportive",
        context=context(),
        provenance=provenance(),
    )
    rows = [
        record(
            name,
            "SYNTHETIC alpha receptor " + name,
            study_cluster_ref=shared,
            context=context(),
            assessments=(assessment,),
        )
        for name in ("report1", "report2")
    ]
    result = run_kdd(snapshot(rows), policy("research"))
    assert result.data_preparation["qualified_count"] == 1
    outcome = method(result, "research_evidence_balance")
    assert outcome.execution_status == "unsupported"
    assert not outcome.candidates
    assert len(outcome.statistics["assessment_inventory"]) == 1
    assert outcome.statistics["assessment_inventory"][0]["assessment_stances"] == ["supportive"]
    assert len(result.data_preparation["units"][0]["members"]) == 2


def test_tfidf_fit_uses_only_baseline_when_comparing_novelty():
    base = record("base", "SYNTHETIC alpha receptor baseline mechanism.", published=OLD)
    new = record("new", "SYNTHETIC unseen beta technology.")
    result = run_kdd(snapshot([base, new]), policy("research"))
    outcome = method(result, "research_tfidf_topics")
    fit = outcome.statistics["fit_unit_ids"]
    assert len(fit) == 1
    by_id = {unit["unit_id"]: unit for unit in result.data_preparation["units"]}
    assert by_id[fit[0]]["members"][0]["record_ref"] == base.record_ref.model_dump(mode="json")
    changed = run_kdd(
        snapshot([base, record("different", "SYNTHETIC a completely different future technology.")]),
        policy("research"),
    )
    assert (
        method(changed, "research_tfidf_topics").statistics["model_hash"] == outcome.statistics["model_hash"]
    )


def with_embedding(row, vector, version="embedding-v1"):
    embedding = Embedding(
        provider="synthetic-fixed-vector",
        model_version=version,
        dimension=len(vector),
        vector=vector,
        text_hash=row.normalized_text_hash,
        provenance=provenance("embedding"),
    )
    return row.model_copy(update={"embedding": embedding})


def test_embedding_clusters_and_novelty_keep_model_spaces_and_tfidf_control_separate():
    rows = [
        with_embedding(record("old1", "SYNTHETIC alpha binding.", published=OLD), (1, 0)),
        with_embedding(record("new1", "SYNTHETIC beta binding."), (0, 1)),
        with_embedding(record("new2", "SYNTHETIC beta pathway."), (0, 1)),
        with_embedding(record("other1", "SYNTHETIC another model."), (1, 0, 0), "other-v1"),
        with_embedding(record("other2", "SYNTHETIC other model peer."), (1, 0, 0), "other-v1"),
    ]
    result = run_kdd(snapshot(rows), policy("research"))
    outcome = method(result, "research_embedding_clusters")
    assert len(outcome.statistics["model_spaces"]) == 2
    assert outcome.statistics["different_model_spaces_never_compared"]
    assert any(candidate.metrics.get("cosine_distance") == 1 for candidate in outcome.candidates)
    assert any(
        candidate.metrics["representation"] == "embedding" and candidate.metrics["analysis_unit_count"] >= 2
        for candidate in outcome.candidates
    )
    assert method(result, "research_tfidf_topics").candidates


def test_pipeline_splits_indications_and_does_not_count_registrations_as_drug_programs():
    ctx = context().model_copy(update={"disease_refs": (ref("disease1"), ref("disease2"))})
    rows = [
        record(
            name,
            "SYNTHETIC protocol " + name,
            context=ctx,
            project_ref=ref(name),
            project_stage="phase2",
            canonical_entity_refs=(entity(name),),
        )
        for name in ("project1", "project2")
    ]
    result = run_kdd(snapshot(rows), policy("pipeline"))
    assert result.data_preparation["qualified_count"] == 4
    assert len(result.method_results[0].candidates) == 2
    assert all(candidate.metrics["project_count"] == 2 for candidate in result.method_results[0].candidates)
    native = record(
        "native",
        "SYNTHETIC native protocol",
        trial_refs=(ref("NCT00000001", "trial"),),
        project_stage="phase1",
    )
    result = run_kdd(snapshot([native]), policy("pipeline"))
    assert result.data_preparation["units"][0]["unit_kind"] == "native_registration"
    assert result.method_results[0].candidates[0].metrics["drug_program_count"] == 0


def test_capacity_deferred_candidates_are_saved_with_identity_and_not_value_excluded():
    rows = [
        metric_record(f"{company}-{period}", 100 if period == 0 else 200, period, subject=company)
        for company in ("a", "b", "c")
        for period in (0, 1)
    ]
    limits = ResourceLimits(review_capacity=1, max_candidates_per_method=1)
    result = run_kdd(snapshot(rows), policy("business", limits=limits))
    assert len(result.method_results[0].candidates) == 3
    assert len(result.candidates) == 1
    assert sum(item["decision"] == "capacity_deferred" for item in result.selection_decisions) == 2
    summary = result.data_preparation["selection_summary"]
    assert summary["value_eligible_count"] == 3
    assert summary["value_excluded_count"] == 0
    assert len(summary["deferred_candidate_keys"]) == 2


def test_algorithm_input_cap_keeps_deferred_unit_identity_and_partial_scope(monkeypatch):
    monkeypatch.setattr("bioradar.research.mining.kdd.MAX_CLUSTER_UNITS", 2)
    rows = [with_embedding(record(str(i), "SYNTHETIC vector peer " + str(i)), (1, 0)) for i in range(3)]
    result = run_kdd(snapshot(rows), policy("research"))
    outcome = method(result, "research_embedding_clusters")
    assert outcome.execution_status == "partial"
    assert outcome.truncated_count == 1
    assert len(outcome.statistics["algorithm_input_deferred_unit_ids"]) == 1
    assert len(result.data_preparation["units"]) == 3


def test_reordering_input_preserves_dataset_policy_versions_and_result_hash():
    rows = [metric_record("q1", 100, 0), metric_record("q2", 200, 1)]
    first = run_mining(snapshot(rows), policy("business"))
    second = run_mining(snapshot(list(reversed(rows))), policy("business"))
    assert first.result_hash == second.result_hash
    assert first.compute_version == "bioradar-mining-compute-v0.4"
    assert kdd_versions(policy("business"))["__engine_compute__"] == first.compute_version
    manifest = copy.deepcopy(first.data_preparation)
    expected = manifest.pop("dataset_hash")
    manifest.pop("selection_summary")
    manifest.pop("dataset_hash_scope")
    assert canonical_hash(manifest) == expected
    assert build_dataset(snapshot(rows), policy("business")).manifest()["dataset_hash"] == expected


def test_value_threshold_is_frozen_and_does_not_modify_generated_pattern():
    data = snapshot([metric_record("q1", 100, 0), metric_record("q2", 110, 1)])
    permissive = run_kdd(data, policy("business"))
    stricter = run_kdd(data, policy("business", value_policy=ValuePolicy(relative_change_threshold=0.5)))
    assert permissive.candidates and not stricter.candidates
    assert permissive.method_results == stricter.method_results
    assert permissive.policy_hash != stricter.policy_hash


def test_native_protocol_source_state_history_does_not_invent_drug_failure_or_future_state():
    native_ref = ref("NCT00000001", "trial")
    before = record(
        "protocol",
        "SYNTHETIC registered phase one.",
        published=OLD,
        trial_refs=(native_ref,),
        context=context(),
        project_stage="phase1",
        project_status="RECRUITING",
        record_ref=ref("protocol", "document", "v1"),
    )
    after = record(
        "protocol",
        "SYNTHETIC registered phase two.",
        trial_refs=(native_ref,),
        context=context(),
        project_stage="phase2",
        project_status="TERMINATED",
        record_ref=ref("protocol", "document", "v2"),
    )
    result = run_kdd(snapshot([before, after]), policy("pipeline"))
    candidate = result.method_results[0].candidates[0]
    assert result.data_preparation["qualified_count"] == 2
    assert candidate.metrics["project_count"] == 1
    assert candidate.metrics["drug_program_count"] == 0
    assert len(candidate.metrics["transitions"]) == 1
    assert candidate.metrics["transitions"][0]["time_kind"] == "source_state_observed_not_clinical_event"
    assert "clinical_failure" not in str(candidate.metrics)
    future = after.model_copy(update={"available_at": datetime(2027, 1, 1, tzinfo=UTC)})
    past = run_kdd(snapshot([before, future]), policy("pipeline"))
    assert not past.method_results[0].candidates[0].metrics["transitions"]
    assert any(
        row["reason"] == "pipeline_state_not_known_by_cutoff"
        for row in past.data_preparation["excluded_records"]
    )


def test_reviewed_assessment_of_another_study_is_not_adopted_by_matching_conditions():
    shared = ref("study-A", "study")
    other = Assessment(
        assessment_ref=ref("wrong-study-assessment"),
        study_cluster_ref=ref("study-B", "study"),
        stage="animal",
        review_status="reviewed",
        stance="supportive",
        context=context(),
        provenance=provenance(),
    )
    row = record(
        "study-record",
        "SYNTHETIC study A fixed text.",
        study_cluster_ref=shared,
        context=context(),
        assessments=(other,),
    )
    dataset = build_dataset(snapshot([row]), policy("research"))
    assert dataset.units[0]["features"]["assessments"] == []
    assert any(value["reason"] == "assessment_study_identity_mismatch" for value in dataset.exclusions)
    assert not method(run_kdd(snapshot([row]), policy("research")), "research_evidence_balance").candidates


def test_replay_future_study_independence_mapping_downgrades_to_document_exploration():
    shared = ref("study-A", "study")
    mapping = entity("study-A").model_copy(
        update={
            "canonical_entity_ref": shared,
            "mapping_available_at": datetime(2027, 1, 1, tzinfo=UTC),
        }
    )
    row = record(
        "study-record",
        "SYNTHETIC study A fixed text.",
        study_cluster_ref=shared,
        context=context(),
        canonical_entity_refs=(mapping,),
    )
    dataset = build_dataset(snapshot([row]), policy("research", time_mode="availability_replay"))
    assert dataset.units[0]["unit_kind"] == "exploratory_document"
    assert dataset.units[0]["independent_study"] is False
    assert "study_independence_mapping_not_available_at_cutoff" in dataset.gaps


def test_single_native_who_notice_remains_usable_without_case_counts_or_population():
    row = record("native-who", "SYNTHETIC official notice text.", source_id="who_don")
    result = run_kdd(snapshot([row], coverage=()), policy("public_health"))
    assert result.data_preparation["qualified_count"] == 1
    assert result.data_preparation["units"][0]["unit_kind"] == "native_notice_publication"
    assert method(result, "public_health_metric_change").execution_status == "unsupported"
    outcome = method(result, "public_health_notifications")
    assert outcome.execution_status == "success"
    assert outcome.statistics["analysis_mode"] == "notification_descriptive"
    assert result.candidates[0].metrics["new_epidemic_claim_permitted"] is False
    assert result.candidates[0].metrics["region"] is None
    assert result.candidates[0].metrics["regional_or_population_rate_permitted"] is False


def test_who_historical_backfill_is_described_as_stock_not_new_epidemic():
    row = record(
        "native-who",
        "SYNTHETIC old official notice text.",
        published=OLD,
        source_id="who_don",
        ingestion_mode="historical_backfill",
    )
    result = run_kdd(snapshot([row], coverage=()), policy("public_health"))
    candidate = method(result, "public_health_notifications").candidates[0]
    assert candidate.kind == "association"
    assert candidate.urgency == "routine"
    assert candidate.metrics["freshness"] == "stock_or_backfilled_notice"
    assert candidate.metrics["new_epidemic_claim_permitted"] is False


def test_year_precision_notice_is_not_silently_assigned_to_january():
    row = record(
        "year-only-who",
        "SYNTHETIC year precision official notice.",
        source_id="who_don",
        published_at=None,
        published_time_raw="2026",
        time_precision="year",
    )
    result = run_kdd(snapshot([row], coverage=()), policy("public_health"))
    outcome = method(result, "public_health_notifications")
    assert len(outcome.candidates) == 1
    assert outcome.statistics["publication_period_counts"] == {}
    assert len(outcome.statistics["unknown_publication_month_unit_ids"]) == 1
    assert outcome.candidates[0].metrics["new_epidemic_claim_permitted"] is False


def test_pipeline_incomplete_conditions_do_not_turn_same_version_into_history():
    ctx = context().model_copy(
        update={"population": None, "disease_refs": (ref("disease1"), ref("disease2"))}
    )
    row = record(
        "multi-condition",
        "SYNTHETIC native protocol fixed once",
        trial_refs=(ref("NCT00000001", "trial"),),
        context=ctx,
        project_stage="phase1",
        project_status="RECRUITING",
    )
    result = run_kdd(snapshot([row]), policy("pipeline"))
    candidates = method(result, "conditional_pipeline_landscape").candidates
    assert result.data_preparation["qualified_count"] == 2
    assert len(candidates) == 2
    assert all(item.metrics["observed_comparisons"] == 0 for item in candidates)
    assert all(item.metrics["unchanged"] is False for item in candidates)
    assert all(
        item.metrics["history_status"] == "insufficient_distinct_ordered_fixed_versions"
        for item in candidates
    )
    assert all("distinct_ordered_version_history_required" in item.data_gaps for item in candidates)


def test_pipeline_compares_distinct_versions_only_within_the_same_partial_condition():
    ctx = context().model_copy(
        update={"population": None, "disease_refs": (ref("disease1"), ref("disease2"))}
    )
    native = ref("NCT00000001", "trial")
    rows = [
        record(
            "protocol",
            "SYNTHETIC first state",
            published=OLD,
            record_ref=ref("protocol", "document", "v1"),
            trial_refs=(native,),
            context=ctx,
            project_stage="phase1",
        ),
        record(
            "protocol",
            "SYNTHETIC second state",
            record_ref=ref("protocol", "document", "v2"),
            trial_refs=(native,),
            context=ctx,
            project_stage="phase2",
        ),
    ]
    result = run_kdd(snapshot(rows), policy("pipeline"))
    candidates = method(result, "conditional_pipeline_landscape").candidates
    assert len(candidates) == 2
    assert all(item.metrics["observed_comparisons"] == 1 for item in candidates)
    assert all(len(item.metrics["transitions"]) == 1 for item in candidates)
    assert sum(item.metrics["observed_comparisons"] for item in candidates) == 2


def test_pipeline_versions_with_same_observation_time_are_not_ordered_by_hash():
    native = ref("NCT00000001", "trial")
    rows = [
        record(
            "protocol",
            "SYNTHETIC equal-time state one",
            record_ref=ref("protocol", "document", "v1"),
            trial_refs=(native,),
            context=context(),
            project_stage="phase1",
        ),
        record(
            "protocol",
            "SYNTHETIC equal-time state two",
            record_ref=ref("protocol", "document", "v2"),
            trial_refs=(native,),
            context=context(),
            project_stage="phase2",
        ),
    ]
    result = run_kdd(snapshot(rows), policy("pipeline"))
    candidate = method(result, "conditional_pipeline_landscape").candidates[0]
    assert candidate.metrics["observed_comparisons"] == 0
    assert candidate.metrics["unchanged"] is False
    assert "fixed_version_observation_order_ambiguous" in candidate.data_gaps


def test_metric_calendar_spans_are_partitioned_before_comparing_annual_quarter_and_half_year():
    spans = [
        ("annual-2024", 100, (2024, 1), (2025, 1)),
        ("quarter-2024-1", 20, (2024, 1), (2024, 4)),
        ("quarter-2024-2", 25, (2024, 4), (2024, 7)),
        ("half-2024", 45, (2024, 1), (2024, 7)),
        ("annual-2025", 150, (2025, 1), (2026, 1)),
        ("half-2025", 60, (2025, 1), (2025, 7)),
    ]
    rows = [
        metric_record(
            name,
            value,
            0,
            period_start=datetime(*start, 1, tzinfo=UTC),
            period_end=datetime(*end, 1, tzinfo=UTC),
        )
        for name, value, start, end in spans
    ]
    result = run_kdd(snapshot(rows), policy("business"))
    outcome = method(result, "business_metric_change")
    assert outcome.statistics["series_count"] == 3
    annual = [
        candidate for candidate in outcome.candidates if candidate.metrics["period_basis"].get("months") == 12
    ]
    assert len(annual) == 1 and annual[0].metrics["effect"]["lower"] == 50
    assert {support.record_ref.object_id for support in annual[0].supports} == {"annual-2024", "annual-2025"}
    assert {candidate.metrics["period_basis"]["months"] for candidate in outcome.candidates} == {3, 6, 12}
    assert all(
        candidate.metrics["seasonal_adjustment"] == "not_performed" for candidate in outcome.candidates
    )
    assert outcome.statistics["source_fy_fp_labels_used_for_comparability"] is False


def test_annual_intervals_with_different_start_months_remain_separate_sequences():
    rows = [
        metric_record(
            f"annual-{year}-{month}",
            year,
            0,
            period_start=datetime(year, month, 1, tzinfo=UTC),
            period_end=datetime(year + 1, month, 1, tzinfo=UTC),
        )
        for month in (1, 4)
        for year in (2024, 2025)
    ]
    result = run_kdd(snapshot(rows), policy("business"))
    outcome = method(result, "business_metric_change")
    assert outcome.statistics["series_count"] == 2
    assert len(outcome.candidates) == 2
    assert {candidate.metrics["period_basis"]["start_month_phase"] for candidate in outcome.candidates} == {
        0,
        3,
    }


def test_research_lexical_tags_are_removed_without_changing_fixed_source():
    from bioradar.research.mining.kdd import _research_analyzer

    text = "<h4>Purpose</h4> &amp; <b>BRCA1</b> kinase research evidence."
    tokens = _research_analyzer(text)
    assert "h4" not in tokens and "amp" not in tokens
    assert "purpose" in tokens and "brca1" in tokens
    data = snapshot([record("html", text)])
    before = copy.deepcopy(data.model_dump(mode="json"))
    result = run_kdd(data, policy("research"))
    outcome = method(result, "research_tfidf_topics")
    assert "html_entities_decoded" in outcome.statistics["lexical_preprocessing"]
    assert all("h4" not in candidate.metrics["terms"] for candidate in outcome.candidates)
    assert data.model_dump(mode="json") == before


def test_single_stock_document_is_explicit_content_followup_without_invented_novelty():
    data = snapshot([record("single", "SYNTHETIC kinase mechanism with cellular evidence.")])
    permitted = run_kdd(data, policy("research"))
    candidate = method(permitted, "research_tfidf_topics").candidates[0]
    evaluation = next(
        value for value in permitted.pattern_evaluations if value["candidate_key"] == candidate.candidate_key
    )
    assert candidate.kind == "hypothesis" and candidate.metrics["cosine_distance"] is None
    assert evaluation["eligible"] and evaluation["eligibility_kind"] == "exploratory_content_followup"
    assert evaluation["value_score"] is None and evaluation["novelty"] is None
    assert evaluation["qualification"] == "exploratory_content_followup_not_supported_pattern"
    assert permitted.data_preparation["selection_summary"]["exploratory_followup_eligible_count"] == 1
    strict = run_kdd(data, policy("research", value_policy=ValuePolicy(allow_singleton_exploration=False)))
    assert not strict.candidates
    assert method(strict, "research_tfidf_topics").candidates == (candidate,)
