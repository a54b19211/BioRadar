"""Non-numeric material retention is independent of candidate/value admission."""

import copy

import pytest
from bioradar.research.datasets.build import build_dataset
from bioradar.research.datasets.intelligence import SOURCE_INTELLIGENCE_VERSION
from bioradar.research.engine.service import EngineService
from bioradar.research.mining.kdd import (
    SOURCE_INTELLIGENCE_COMPUTE_VERSION,
    SOURCE_INTELLIGENCE_METHOD_VERSION,
    kdd_versions,
    run_discovery,
    run_kdd,
)
from bioradar.research.mining.models import (
    DiscoveryPolicy,
    KddPolicy,
    MiningResult,
    SignalContext,
    SourceRelation,
    canonical_hash,
)
from test_mining import AS_OF, OLD, record, snapshot

pytestmark = pytest.mark.ENGINE


def policy(direction="public_health", *, enabled=True):
    values = {"as_of": AS_OF, "kdd_direction": direction}
    if enabled:
        values["source_intelligence_version"] = SOURCE_INTELLIGENCE_VERSION
    return KddPolicy(**values)


def advisory(name="advisory", **extra):
    return record(
        name,
        "SYNTHETIC 北岭医院医疗提醒：就诊请携带既往用药清单并按指引分诊。",
        source_id="external_archive",
        source_family=None,
        source_independence_status="unresolved",
        study_cluster_ref=None,
        study_independence_status="not_applicable",
        **extra,
    )


def outcome(result):
    return next(item for item in result.method_results if item.method_id == "public_health_source_claims")


def test_no_numeric_or_checkable_claim_still_has_exact_retained_method_output_without_candidates():
    data = snapshot([advisory()])
    before = data.model_dump(mode="json")
    dataset = build_dataset(data, policy())
    retained = [
        u for u in dataset.units if u["unit_kind"] in {"source_statement", "source_information_event"}
    ]
    result = run_kdd(data, policy())
    method = outcome(result)
    assert method.execution_status == "success" and method.readiness == "exploratory_only"
    assert method.method_version == SOURCE_INTELLIGENCE_METHOD_VERSION
    assert method.statistics["retained_source_units"] == sorted(retained, key=lambda u: u["unit_id"])
    assert method.statistics["retained_source_version_count"] == 1
    assert method.statistics["source_statement_count"] == 1
    assert method.statistics["source_information_event_count"] == 0
    assert method.statistics["retention_builds_candidates"] is False
    assert not method.candidates and not result.candidates
    assert not result.pattern_evaluations and not result.selection_decisions
    assert result.data_preparation["quantitative_qualified_count"] == 0
    assert result.data_preparation["quantitative_exclusions"]
    assert not result.data_preparation["excluded_records"]
    assert all(not u["independent_study"] for u in method.statistics["retained_source_units"])
    assert data.model_dump(mode="json") == before
    assert result.compute_version == SOURCE_INTELLIGENCE_COMPUTE_VERSION
    assert MiningResult.model_validate(result.model_dump(mode="json")) == result
    assert canonical_hash(result.model_dump(mode="json", exclude={"result_hash"})) == result.result_hash
    method.statistics["retained_source_units"][0]["features"]["claims"].append({"SYNTHETIC": True})
    assert dataset.units == tuple(retained)
    assert retained[0]["features"]["claims"] == []


def test_exact_correction_relation_and_historical_material_survive_without_new_event_candidates():
    original = advisory("prior-medical", published=OLD)
    correction = record(
        "correction",
        "SYNTHETIC：更正此前说明，原安排保持不变。",
        source_id="external_archive",
        source_family=None,
        source_independence_status="unresolved",
        study_cluster_ref=None,
        study_independence_status="not_applicable",
        signal_context=SignalContext(
            source_relations=(SourceRelation(kind="correction", target_ref=original.record_ref),),
        ),
    )
    data = snapshot([original, correction])
    before = copy.deepcopy(data.model_dump(mode="json"))
    result = run_kdd(data, policy())
    units = outcome(result).statistics["retained_source_units"]
    event = next(u for u in units if u["unit_kind"] == "source_information_event")
    assert event["features"]["event"]["target_ref"] == original.record_ref.model_dump(mode="json")
    assert event["features"]["event"]["event_type"] == "correction"
    prior = next(
        u for u in units if u["members"][0]["record_ref"] == original.record_ref.model_dump(mode="json")
    )
    assert prior["features"]["body_ref"]["text_hash"] == original.normalized_text_hash
    assert prior["features"]["window"] == "baseline"
    assert outcome(result).statistics["retained_source_version_count"] == 2
    assert not result.candidates and not outcome(result).candidates
    assert data.model_dump(mode="json") == before


def test_existing_checkable_claim_candidates_are_exact_and_material_units_do_not_add_candidates():
    item = record(
        "claim",
        "SYNTHETIC：网传北岭医院发生医护人员感染，原始来源尚未核实。",
        source_id="external_archive",
        source_family=None,
        source_independence_status="unresolved",
        study_cluster_ref=None,
        study_independence_status="not_applicable",
    )
    data = snapshot([item])
    old, new = run_kdd(data, policy(enabled=False)), run_kdd(data, policy())
    assert outcome(old).candidates and len(outcome(old).candidates) == len(outcome(new).candidates)
    fields = {"candidate_key", "method_version"}
    assert [c.model_dump(mode="json", exclude=fields) for c in outcome(old).candidates] == [
        c.model_dump(mode="json", exclude=fields) for c in outcome(new).candidates
    ]
    assert [c.model_dump(mode="json", exclude=fields) for c in old.candidates] == [
        c.model_dump(mode="json", exclude=fields) for c in new.candidates
    ]
    assert all(c.method_version == "attributed-source-claim-v1" for c in outcome(old).candidates)
    assert all(c.method_version == SOURCE_INTELLIGENCE_METHOD_VERSION for c in outcome(new).candidates)
    assert outcome(old).candidates[0].candidate_key != outcome(new).candidates[0].candidate_key
    EngineService._validate_supports(data, old)
    EngineService._validate_supports(data, new)
    assert len(outcome(new).statistics["retained_source_units"]) == 1
    assert outcome(new).candidates[0].evidence_status == "unverified"
    assert outcome(new).candidates[0].metrics["attention"]["confirmed_source_group_count"] == 0
    assert "retained_source_units" not in outcome(old).statistics


def test_future_version_only_changes_public_health_and_discovery_compute_markers():
    old, new = kdd_versions(policy(enabled=False)), kdd_versions(policy())
    assert old["__engine_compute__"] == "bioradar-mining-compute-v0.4"
    assert old["public_health_source_claims"] == "attributed-source-claim-v1"
    assert {
        key: value
        for key, value in new.items()
        if key not in {"__engine_compute__", "public_health_source_claims"}
    } == {
        key: value
        for key, value in old.items()
        if key not in {"__engine_compute__", "public_health_source_claims"}
    }
    assert new["__engine_compute__"] == SOURCE_INTELLIGENCE_COMPUTE_VERSION
    assert new["public_health_source_claims"] == SOURCE_INTELLIGENCE_METHOD_VERSION
    assert kdd_versions(policy("research")) == kdd_versions(policy("research", enabled=False))
    discovery = DiscoveryPolicy(
        as_of=AS_OF,
        discovery_directions=("research", "public_health"),
        source_intelligence_version=SOURCE_INTELLIGENCE_VERSION,
    )
    result = run_discovery(snapshot([advisory()]), discovery)
    assert result.compute_version == SOURCE_INTELLIGENCE_COMPUTE_VERSION
    assert kdd_versions(discovery)["public_health_source_claims"] == SOURCE_INTELLIGENCE_METHOD_VERSION
    assert "public_health" in result.data_preparation["direction_datasets"]
    assert outcome(result).statistics["retained_source_units"]
