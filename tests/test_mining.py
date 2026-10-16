"""Algorithm gold examples and scientific failure cases; all data are fixtures."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from bioradar.research.mining.methods_pipeline import cohort_event_table
from bioradar.research.mining.models import (
    Assessment,
    ClaimDependency,
    Embedding,
    EngineRecord,
    Event,
    MiningEntity,
    MiningLabels,
    MiningPolicy,
    NormalizedSnapshot,
    ProjectTimeline,
    ProviderRef,
    RegionalIndicator,
    Relation,
    ResourceLimits,
    SectionProvenance,
    SnapshotCoverage,
    StateAssertion,
    StudyContext,
    TimeExpression,
    canonical_hash,
)
from bioradar.research.mining.prepare import prepare, ref_key, snapshot_hash
from bioradar.research.mining.runner import run_mining
from bioradar.research.mining.temporal import batch_bursts
from pydantic import ValidationError

pytestmark = pytest.mark.ENGINE
AS_OF = datetime(2026, 10, 14, tzinfo=UTC)
OLD = datetime(2026, 4, 15, tzinfo=UTC)
NEW = datetime(2026, 10, 1, tzinfo=UTC)
GRANT = "development:development:sources"


def ref(name, kind="entity", version="v1"):
    return ProviderRef(
        provider="fixture",
        object_type=kind,
        object_id=name,
        adopted_version=version,
        version_source="provider",
        content_hash=sha256(name.encode()).hexdigest(),
        authorization_ref=GRANT,
    )


def provenance(name="section", available=OLD):
    return SectionProvenance(
        schema_version="bioradar.engine.fixture-section.v1",
        provider_ref=ref(name, "section"),
        available_at=available,
    )


def instant(time):
    return TimeExpression(raw=time.isoformat(), precision="instant", instant=time)


def entity(name, kind="entity"):
    return MiningEntity(
        temporary_entity_ref=ref(f"temporary-{name}"),
        canonical_entity_ref=ref(name, kind),
        entity_type=kind,
        mapping_status="confirmed",
        mapping_ref=ref(f"map-{name}", "mapping"),
        mapping_version="mapping-fixture-v1",
        mapping_available_at=OLD,
    )


def context():
    return StudyContext(
        disease_refs=(ref("D", "disease"),),
        population="P",
        endpoint="E",
        treatment_line="L",
        geography="J",
        study_design="synthetic",
        provenance=provenance("context"),
    )


def record(name, text, published=NEW, **extra):
    data = dict(
        record_ref=ref(name, "document"),
        document_hash=sha256(text.encode()).hexdigest(),
        source_instance="fixture",
        source_id="fixture-source",
        observed_at=published,
        available_at=published,
        first_seen_at=published,
        published_at=published,
        time_precision="instant",
        published_time_raw=published.isoformat(),
        publication_time_verified=True,
        source_family="fixture-alpha" if len(name) % 2 else "fixture-beta",
        source_independence_status="confirmed",
        study_cluster_ref=ref(f"study-{name}", "study"),
        study_independence_status="confirmed",
        ingestion_mode="fixture",
        text_status="inline_authorized",
        normalized_text=text,
        normalization_version="identity-fixture-v1",
        evidence_refs=(ref(name, "document"),),
    )
    data.update(extra)
    return EngineRecord(**data)


def snapshot(records, **extra):
    data = dict(
        snapshot_id="fixture",
        is_demo=True,
        source_instance="fixture",
        coverage_mode="complete",
        observed_at=AS_OF,
        records=tuple(records),
        coverage=(
            SnapshotCoverage(
                source_instance="fixture",
                source_id="fixture-source",
                mode="complete",
                scope_description="synthetic adopted fixture cohort, not real intelligence",
                record_count=len({r.record_ref.identity_key for r in records}),
                is_consistent_snapshot=True,
            ),
        ),
    )
    data.update(extra)
    return NormalizedSnapshot(**data)


def full_fixture():
    baseline = record(
        "baseline",
        "Technology K improves endpoint E for disease D. Baseline blue circuit platform.",
        OLD,
        canonical_entity_refs=(entity("D", "disease"), entity("K"), entity("P0", "project")),
        labels=MiningLabels(
            technology_form=("J",),
            disease_refs=(ref("D", "disease"),),
            label_provider_ref=ref("labels", "section"),
        ),
        project_ref=ref("P0", "project"),
        project_stage="alpha",
        context=context(),
        state_assertions=(
            StateAssertion(
                assertion_ref=ref("old-state", "assertion"),
                entity_ref=ref("P0", "project"),
                property="stage",
                value="alpha",
                effective_time=instant(OLD),
                source_ref=ref("baseline", "document"),
            ),
        ),
    )
    dependency = ClaimDependency(
        judgment_id="J0",
        claim_id="C0",
        claim_text="Technology K improves endpoint E for disease D",
        entity_refs=(ref("K"),),
        evidence_refs=(baseline.record_ref,),
        context=context(),
        provenance=provenance("claim"),
    )
    baseline = baseline.model_copy(update={"dependencies": (dependency,)})
    records = [baseline]
    for index in range(3):
        text = (
            f"Amber lattice cascade connects technology K with disease D. Specific synthetic marker {index}."
        )
        extra = dict(
            canonical_entity_refs=(entity("D", "disease"), entity("K"), entity("P0", "project")),
            labels=MiningLabels(
                technology_form=("K",),
                disease_refs=(ref("D", "disease"),),
                label_provider_ref=ref("labels", "section"),
            ),
            context=context(),
        )
        if index == 0:
            extra.update(
                project_ref=ref("P0", "project"),
                project_stage="beta",
                company_refs=(ref("F1"), ref("F2")),
                entry_time=instant(NEW),
                state_assertions=(
                    StateAssertion(
                        assertion_ref=ref("new-state", "assertion"),
                        entity_ref=ref("P0", "project"),
                        property="stage",
                        value="beta",
                        effective_time=instant(NEW),
                        source_ref=ref(f"current-{index}", "document"),
                    ),
                ),
            )
        records.append(record(f"current-{index}", text, **extra))
    records.append(
        record(
            "negative",
            "Technology K did not improve endpoint E for disease D. Negative result.",
            canonical_entity_refs=(entity("K"), entity("D", "disease")),
            context=context(),
        )
    )
    records.append(
        record(
            "notice",
            "Synthetic source notice changes the previous evidence version.",
            events=(
                Event(
                    event_ref=ref("withdrawal-event", "event"),
                    event_type="retraction",
                    object_refs=(baseline.record_ref,),
                    effective_time=instant(NEW),
                    evidence_refs=(ref("notice", "document"),),
                    source_status="supported",
                    provenance=provenance("withdrawal", NEW),
                ),
            ),
        )
    )
    for name, time, stage in (("assessment-old", OLD, "in_vitro"), ("assessment-new", NEW, "animal")):
        records.append(
            record(
                name,
                f"Synthetic reviewed stage {stage}. Independent technical exercise.",
                time,
                assessments=(
                    Assessment(
                        assessment_ref=ref(name, "assessment"),
                        study_cluster_ref=ref(name, "study"),
                        stage=stage,
                        review_status="reviewed",
                        assessor="fixture-reviewer",
                        stance="supportive",
                        context=context(),
                        evidence_refs=(ref(name, "document"),),
                        provenance=provenance(name, time),
                    ),
                ),
            )
        )
    for name, time, edges in (
        ("network-old", OLD, (("A", "B"), ("B", "E"), ("C", "D-org"))),
        ("network-new", NEW, (("B", "C"),)),
    ):
        relations = tuple(
            Relation(
                relation_ref=ref(f"co-{a}-{b}", "relation"),
                subject_ref=ref(a, "organization"),
                object_ref=ref(b, "organization"),
                relation_type="coauthored",
                directed=False,
                identity_status="confirmed",
                effective_time=instant(time),
                evidence_refs=(ref(name, "document"),),
                study_cluster_ref=ref(f"paper-{a}-{b}", "study"),
                provenance=provenance(name, time),
            )
            for a, b in edges
        )
        records.append(
            record(
                name, "Synthetic collaboration events with controlled identities.", time, relations=relations
            )
        )
    for name, days, kind, event_days in (
        ("dwell-1", 600, "no_event", None),
        ("dwell-2", 500, "stage_advance", 100),
        ("dwell-3", 500, "commercial_exit", 150),
    ):
        start = AS_OF - timedelta(days=days)
        project_ref = ref(name, "project")
        timeline = ProjectTimeline(
            project_ref=project_ref,
            stage="alpha",
            entered_at=instant(start),
            observation_end=AS_OF,
            event_time=instant(start + timedelta(days=event_days)) if event_days else None,
            event_kind=kind,
            observation_coverage="continuous",
            context=context(),
            evidence_refs=(ref(name, "document"),),
            provenance=provenance(name),
        )
        records.append(
            record(
                name,
                "Synthetic stage timeline with explicit observation end.",
                project_ref=project_ref,
                canonical_entity_refs=(entity(name, "project"),),
                timeline=timeline,
            )
        )
    risk_relations = tuple(
        Relation(
            relation_ref=ref(f"supply-{a}-{b}", "relation"),
            subject_ref=ref(a),
            object_ref=ref(b),
            relation_type="supplies",
            directed=True,
            identity_status="confirmed",
            effective_time=instant(OLD),
            evidence_refs=(ref("risk", "document"),),
            provenance=provenance("risk"),
        )
        for a, b in (("SupplyS", "ProductX"), ("ProductX", "BuyerY"))
    )
    records.append(
        record(
            "risk",
            "Synthetic supported safety action with hypothetical supply exposures.",
            relations=risk_relations,
            events=(
                Event(
                    event_ref=ref("risk-event", "event"),
                    event_type="regulatory_safety_action",
                    object_refs=(ref("SupplyS"),),
                    jurisdiction="J",
                    effective_time=instant(NEW),
                    evidence_refs=(ref("risk", "document"),),
                    source_status="supported",
                    provenance=provenance("risk-event", NEW),
                ),
            ),
        )
    )
    for region, value, denominator in (("R1", 20, 100), ("R2", 30, 300)):
        records.append(
            record(
                f"region-{region}",
                "Synthetic same-scope regional observations.",
                regional_indicators=(
                    RegionalIndicator(
                        indicator="I",
                        region=region,
                        period="2025",
                        unit="count",
                        value=value,
                        denominator=denominator,
                        population_scope="P",
                        jurisdiction="J",
                        evidence_refs=(ref(f"region-{region}", "document"),),
                        provenance=provenance(region),
                    ),
                ),
            )
        )
    return snapshot(records), MiningPolicy(as_of=AS_OF)


def method_result(snapshot_, method, **extra):
    return run_mining(snapshot_, MiningPolicy(as_of=AS_OF, method_ids=(method,), **extra)).method_results[0]


def test_all_registered_methods_have_real_computations_and_controller_questions():
    snap, policy = full_fixture()
    computed = run_mining(snap, policy)
    assert len(computed.method_results) == 11
    assert all(m.execution_status in {"success", "partial"} for m in computed.method_results)
    assert all(m.candidates for m in computed.method_results)
    assert all(
        c.next_questions and c.next_data_requirements for m in computed.method_results for c in m.candidates
    )
    assert computed.result_hash == canonical_hash(computed.model_dump(mode="json", exclude={"result_hash"}))


def test_reordering_duplicates_and_snapshot_labels_preserve_result_hash():
    snap, policy = full_fixture()
    altered = snapshot(tuple(reversed(snap.records)) + (snap.records[0],), snapshot_id="different-label")
    assert snapshot_hash(snap) == snapshot_hash(altered)
    assert run_mining(snap, policy).result_hash == run_mining(altered, policy).result_hash


def test_quotes_are_literal_spans_in_the_fixed_text_and_have_its_hash():
    snap, policy = full_fixture()
    texts = {r.record_ref.version_key: r for r in snap.records}
    for method in run_mining(snap, policy).method_results:
        for c in method.candidates:
            for support in c.supports:
                if support.quote is not None:
                    original = texts[support.record_ref.version_key]
                    assert (
                        original.normalized_text[support.normalized_start : support.normalized_end]
                        == support.quote
                    )
                    assert support.normalized_text_hash == original.normalized_text_hash
                    assert support.quote_status == "normalized_only"


def test_unresolved_identity_does_not_block_text_or_invent_confirmed_counts():
    rows = [
        record(
            str(i),
            f"Amber lattice cascade unrelated marker {i}.",
            source_family=None,
            source_independence_status="unresolved",
            study_cluster_ref=None,
            study_independence_status="unresolved",
        )
        for i in range(2)
    ]
    output = method_result(snapshot(rows), "lexical_emergence")
    assert output.readiness == "exploratory_only" and output.candidates
    assert all(c.metrics.get("confirmed_study_count", 0) == 0 for c in output.candidates)
    assert all(c.metrics.get("growth_confirmable", False) is False for c in output.candidates)


def test_document_duplicates_do_not_increase_independent_contributions():
    snap, _ = full_fixture()
    source = snap.records[1]
    duplicate = source.model_copy(update={"record_ref": ref("syndication", "document")})
    one = method_result(snapshot((snap.records[0], source)), "entity_combinations")
    two = method_result(snapshot((snap.records[0], source, duplicate)), "entity_combinations")
    assert [r["current_count"] for r in one.statistics["combination_rows"]] == [
        r["current_count"] for r in two.statistics["combination_rows"]
    ]


def test_stock_and_unverified_raw_dates_never_become_recent_growth():
    rows = [
        record(
            str(i),
            f"Amber lattice cascade marker {i}.",
            OLD,
            published_at=None,
            published_time_raw="2026-10-01",
            time_precision="day",
            publication_time_verified=False,
        )
        for i in range(2)
    ]
    output = method_result(snapshot(rows), "lexical_emergence")
    assert output.candidates
    assert all(c.metrics.get("current_count", 0) == 0 for c in output.candidates)
    assert all(c.metrics.get("growth_confirmable", False) is False for c in output.candidates)


def test_availability_replay_rejects_todays_export_and_future_enrichment():
    snap, _ = full_fixture()
    r = snap.records[1]
    policy = MiningPolicy(as_of=AS_OF, time_mode="availability_replay", method_ids=("entity_combinations",))
    future = r.model_copy(update={"observed_at": AS_OF + timedelta(days=1)})
    assert not prepare(snapshot((future,)), policy).records
    missing_mapping = r.model_copy(
        update={
            "canonical_entity_refs": tuple(
                e.model_copy(update={"mapping_available_at": None}) for e in r.canonical_entity_refs
            )
        }
    )
    assert (
        run_mining(snapshot((missing_mapping,)), policy).method_results[0].execution_status == "unsupported"
    )
    future_coverage = snapshot((r,), observed_at=AS_OF + timedelta(days=1))
    prepared = prepare(future_coverage, policy)
    assert not prepared.coverage_complete and not prepared.records[0].growth_eligible


def test_backfill_and_source_onboarding_can_be_explored_but_not_confirm_growth():
    rows = [
        record(
            str(i), f"Amber lattice cascade marker {i}.", record_flags=("historical_backfill", "new_source")
        )
        for i in range(2)
    ]
    prepared = prepare(snapshot(rows), MiningPolicy(as_of=AS_OF))
    assert all(not r.growth_eligible for r in prepared.records)
    assert method_result(snapshot(rows), "lexical_emergence").candidates


def test_tombstones_do_not_generate_active_text_discovery():
    rows = [record(str(i), "Amber lattice cascade.", record_flags=("retracted",)) for i in range(2)]
    assert method_result(snapshot(rows), "lexical_emergence").execution_status == "unsupported"
    assert method_result(snapshot(rows), "text_topics").execution_status == "unsupported"


def test_k_d_combination_does_not_require_a_known_target():
    snap, _ = full_fixture()
    output = method_result(snap, "entity_combinations")
    assert any(c.metrics["target_dimension"] == "unknown_optional_enrichment" for c in output.candidates)
    assert all(c.metrics["target_keys"] is None for c in output.candidates)
    assert all(c.metrics["r2_threshold_met"] is False for c in output.candidates)


def test_future_state_is_a_plan_and_not_an_observed_change():
    snap, _ = full_fixture()
    row = snap.records[1]
    plan = row.state_assertions[0].model_copy(
        update={
            "assertion_ref": ref("planned", "assertion"),
            "value": "planned",
            "effective_time": instant(datetime(2027, 1, 1, tzinfo=UTC)),
        }
    )
    row = row.model_copy(update={"state_assertions": (*row.state_assertions, plan)})
    output = method_result(snapshot((snap.records[0], row)), "state_transitions")
    planned = [
        c for c in output.candidates if "future_effective_assertion_is_not_observed_progress" in c.reasons
    ]
    assert len(planned) == 1 and planned[0].kind == "hypothesis" and not planned[0].metrics["is_recent"]
    assert not any(c.metrics.get("after") == "planned" for c in output.candidates)


def test_supported_withdrawal_notice_matches_old_version_without_notice_flags():
    snap, _ = full_fixture()
    output = method_result(snap, "counterevidence_impact")
    assert output.statistics["direct_matches"] == 1
    assert output.statistics["unreferenced_matches"] >= 1


def test_competing_exit_estimator_does_not_treat_commercial_exit_as_censoring():
    table = cohort_event_table([(100, "stage_advance"), (150, "commercial_exit"), (500, "no_event")])
    assert table[-1]["competing_event_cumulative_incidence"]["stage_advance"] == pytest.approx(1 / 3)
    assert table[-1]["competing_event_cumulative_incidence"]["commercial_exit"] == pytest.approx(1 / 3)
    assert table[-1]["no_recorded_exit_survival"] == pytest.approx(1 / 3)


def test_registration_without_reviewed_results_is_not_maturity():
    row = record("registered", "Registered phase with no results.", project_stage="phase_3")
    assert method_result(snapshot((row,)), "maturity_shift").execution_status == "unsupported"


def test_old_event_in_new_article_is_not_urgent():
    snap, _ = full_fixture()
    row = next(r for r in snap.records if r.record_ref.object_id == "risk")
    old_event = row.events[0].model_copy(update={"effective_time": instant(OLD)})
    output = method_result(snapshot((row.model_copy(update={"events": (old_event,)}),)), "event_exposure")
    assert output.candidates and all(c.urgency == "routine" for c in output.candidates)


def test_graph_edge_without_evidence_cannot_propagate():
    snap, _ = full_fixture()
    row = next(r for r in snap.records if r.record_ref.object_id == "risk")
    row = row.model_copy(
        update={"relations": tuple(e.model_copy(update={"evidence_refs": ()}) for e in row.relations)}
    )
    output = method_result(snapshot((row,)), "event_exposure")
    assert all(c.metrics["hop_count"] == 0 for c in output.candidates)
    assert output.statistics["cross_object_variant_status"] == "unsupported"


def test_invalid_embeddings_conflicting_versions_coverage_and_unbounded_policy_fail():
    with pytest.raises(ValidationError):
        Embedding(
            provider="S03",
            model_version="m",
            dimension=2,
            vector=(1, 2, 3),
            text_hash="h",
            provenance=provenance(),
        )
    with pytest.raises(ValidationError):
        MiningPolicy(as_of=AS_OF, method_ids=("text_topics", "text_topics"))
    with pytest.raises(ValidationError):
        MiningPolicy(as_of=AS_OF, baseline_start=datetime(1, 1, 1, tzinfo=UTC))
    row = record("x", "Text.")
    with pytest.raises(ValueError):
        prepare(
            snapshot((row, row.model_copy(update={"project_stage": "changed"}))), MiningPolicy(as_of=AS_OF)
        )
    coverage = snapshot((row,)).coverage[0]
    with pytest.raises(ValidationError):
        snapshot((row,), coverage=(coverage, coverage))
    assert ref_key(ref("a:b", "c")) != ref_key(ref("b", "a:c"))


def test_burst_dp_finds_rate_increase_and_no_exposure_is_unknown():
    output = batch_bursts([1] * 20 + [70] * 5, [100] * 25)
    assert output["intervals"] and output["states"][-1] > 0
    assert batch_bursts([0], [0])["states"] == [None]


def test_resource_truncation_and_missing_inputs_are_explicit():
    snap, policy = full_fixture()
    output = run_mining(snap, policy.model_copy(update={"limits": ResourceLimits(max_records=2)}))
    assert any(m.execution_status == "partial" and m.truncated_count for m in output.method_results)
    empty = method_result(snapshot(()), "lexical_emergence")
    assert empty.execution_status == "unsupported" and empty.readiness == "unsupported"


def test_json_schema_keys_are_not_cleaned_research_text():
    rows = [
        record(str(i), '{"briefTitle":"fixture","designModule":{"designInfo":"value"}}') for i in range(2)
    ]
    assert method_result(snapshot(rows), "lexical_emergence").execution_status == "unsupported"
    assert method_result(snapshot(rows), "text_topics").execution_status == "unsupported"


def test_processing_budget_disables_growth_confirmation_and_lists_excluded_records():
    snap, _ = full_fixture()
    policy = MiningPolicy(as_of=AS_OF, limits=ResourceLimits(max_records=2))
    prepared = prepare(snap, policy)
    assert not prepared.coverage_complete
    assert all(not item.growth_eligible for item in prepared.records)
    omitted = [e for e in prepared.excluded_records if e.reason == "record_budget_exhausted"]
    assert len(omitted) == len(snap.records) - 2
    output = run_mining(snap, policy).method_results[0]
    assert output.readiness != "ready"
    assert all(not c.metrics.get("growth_confirmable", False) for c in output.candidates)


def test_burst_relevance_tracks_supporting_records_not_whole_study_history():
    from bioradar.research.mining.methods_text import _bins

    a = record("old-same-study", "Old background without new term.", OLD)
    b = record("new-same-study", "Amber lattice cascade.", study_cluster_ref=a.study_cluster_ref)
    prepared = prepare(snapshot((a, b)), MiningPolicy(as_of=AS_OF))
    output = _bins(list(prepared.records), {b.record_ref.version_key}, prepared)
    assert sum(output["relevant_counts"]) == 1


def test_label_and_context_facets_require_their_own_replay_provenance():
    snap, _ = full_fixture()
    policy = MiningPolicy(as_of=AS_OF, time_mode="availability_replay", method_ids=("entity_combinations",))
    row = snap.records[1]
    labels = row.labels.model_copy(update={"provenance": provenance("labels", AS_OF + timedelta(days=1))})
    output = run_mining(snapshot((row.model_copy(update={"labels": labels}),)), policy)
    assert output.method_results[0].execution_status == "unsupported"
    future_context = row.context.model_copy(
        update={"provenance": provenance("context", AS_OF + timedelta(days=1))}
    )
    output = run_mining(
        snapshot((row.model_copy(update={"context": future_context}),)),
        policy.model_copy(update={"method_ids": ("pipeline_landscape",)}),
    )
    assert output.method_results[0].statistics["cohorts"][0]["competitive_comparability"] == "undetermined"


def test_same_assertion_or_assessment_version_with_conflicting_payload_does_not_select_first():
    snap, _ = full_fixture()
    row = snap.records[1]
    other = row.model_copy(
        update={
            "record_ref": ref("other", "document"),
            "state_assertions": (row.state_assertions[0].model_copy(update={"value": "conflicting"}),),
        }
    )
    assert method_result(snapshot((row, other)), "state_transitions").execution_status == "failed"
    reviewed = next(r for r in snap.records if r.assessments)
    duplicate = reviewed.model_copy(update={"record_ref": ref("assessment-duplicate", "document")})
    output = method_result(snapshot((reviewed, duplicate)), "maturity_shift")
    assert output.statistics["contexts"][0]["study_count"] == 1
    assert sum(output.statistics["contexts"][0]["stage_counts"].values()) == 1
    conflicting = duplicate.model_copy(
        update={"assessments": (reviewed.assessments[0].model_copy(update={"stage": "regulatory"}),)}
    )
    assert method_result(snapshot((reviewed, conflicting)), "maturity_shift").execution_status == "failed"


def test_unresolved_company_identity_does_not_create_known_hhi_share():
    snap, _ = full_fixture()
    row = snap.records[1]
    output = method_result(snapshot((row,)), "pipeline_landscape")
    cohort = output.statistics["cohorts"][0]
    assert cohort["company_fractional_shares"] == {}
    assert cohort["unknown_ownership_share"] == 1


def test_normalized_duplicate_abstracts_have_one_topic_weight_and_all_support_refs():
    original = record("original", "Amber lattice cascade specific discovery.", OLD)
    copied = record(
        "copied", original.normalized_text, NEW, document_hash=sha256(b"different-frontmatter").hexdigest()
    )
    output = method_result(snapshot((original, copied)), "text_topics")
    assert output.statistics["text_units"] == 1
    assert output.statistics["normalized_text_duplicates_merged"] == 1
    assert len(output.candidates[0].supports) == 2
    assert output.candidates[0].kind == "hypothesis"


def test_english_subphrase_merging_respects_tokens():
    rows = [record(str(i), f"KRAS mutation marker {i}. RAS mutation context {i}.") for i in range(2)]
    output = method_result(snapshot(rows), "lexical_emergence")
    phrases = {c.metrics.get("phrase") for c in output.candidates}
    assert any(phrase and "kras mutation" in phrase for phrase in phrases)
    assert any(phrase and "ras mutation" in phrase and "kras" not in phrase for phrase in phrases)


def test_fair_review_rounds_do_not_compare_project_count_with_text_distance():
    from bioradar.research.mining.common import candidate
    from bioradar.research.mining.runner import select_candidates

    prepared = prepare(snapshot((record("x", "Fixture."),)), MiningPolicy(as_of=AS_OF))
    candidates = []
    for index in range(4):
        for method, score in (("pipeline_landscape", 10000), ("text_topics", 0.1)):
            candidates.append(
                candidate(method, {"index": index}, "Fixture", list(prepared.records), score=score)
            )
    policy = MiningPolicy(
        as_of=AS_OF,
        exploration_fraction=0,
        limits=ResourceLimits(review_capacity=4, max_candidates_per_group=4),
    )
    selected = select_candidates(candidates, policy)
    assert [c.method_id for c in selected].count("pipeline_landscape") == 2
    assert [c.method_id for c in selected].count("text_topics") == 2


def test_nullable_labels_provenance_has_precise_hash_compatibility():
    snap = snapshot((record("x", "Fixture."),))
    value = snap.model_dump(mode="json", exclude={"snapshot_id"})
    value["records"][0]["labels"].pop("provenance")
    assert snapshot_hash(snap) == canonical_hash(value)
    richer = snap.records[0].model_copy(
        update={"labels": snap.records[0].labels.model_copy(update={"provenance": provenance("labels")})}
    )
    assert snapshot_hash(snapshot((richer,))) != snapshot_hash(snap)


def test_same_regional_values_with_different_provenance_keep_both_sources():
    snap, _ = full_fixture()
    first = next(r for r in snap.records if r.record_ref.object_id == "region-R1")
    second = next(r for r in snap.records if r.record_ref.object_id == "region-R2")
    duplicate = first.model_copy(
        update={
            "record_ref": ref("regional-other-source", "document"),
            "regional_indicators": (
                first.regional_indicators[0].model_copy(
                    update={"provenance": provenance("other-regional-source")}
                ),
            ),
        }
    )
    output = method_result(snapshot((first, second, duplicate)), "regional_comparison")
    assert output.statistics["comparison_groups"][0]["rows"][0]["support_record_count"] == 2
    assert len(output.candidates[0].supports) == 3


def test_pending_exposure_paths_respect_total_budget_and_are_partial():
    snap, _ = full_fixture()
    row = next(r for r in snap.records if r.record_ref.object_id == "risk")
    edges = tuple(
        row.relations[0].model_copy(
            update={"relation_ref": ref(f"edge-{i}", "relation"), "object_ref": ref(f"target-{i}")}
        )
        for i in range(100)
    )
    output = method_result(
        snapshot((row.model_copy(update={"relations": edges}),)),
        "event_exposure",
        limits=ResourceLimits(max_paths=5),
    )
    assert output.execution_status == "partial"
    assert output.statistics["paths_evaluated"] == 5
    assert output.statistics["pending_path_budget_drops"] > 0


def test_naive_time_expressions_and_empty_embedding_versions_fail():
    with pytest.raises(ValidationError):
        TimeExpression(precision="day", instant=datetime(2026, 10, 1))
    with pytest.raises(ValidationError):
        Embedding(
            provider="S03",
            model_version="",
            dimension=1,
            vector=(1,),
            text_hash="text",
            provenance=provenance(),
        )


def test_numeric_state_format_changes_are_not_new_facts_and_booleans_stay_distinct():
    snap, _ = full_fixture()
    old, new = snap.records[:2]
    old = old.model_copy(
        update={"state_assertions": (old.state_assertions[0].model_copy(update={"value": 1}),)}
    )
    new = new.model_copy(
        update={"state_assertions": (new.state_assertions[0].model_copy(update={"value": 1.0}),)}
    )
    output = method_result(snapshot((old, new)), "state_transitions")
    assert output.statistics["format_only"] == 1 and not output.candidates
    boolean = new.model_copy(
        update={"state_assertions": (new.state_assertions[0].model_copy(update={"value": True}),)}
    )
    assert method_result(snapshot((old, boolean)), "state_transitions").candidates
