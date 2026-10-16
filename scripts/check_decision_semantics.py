"""Independent synthetic counterexamples; no provider/network/DB calls.

Run from the repository: .venv/bin/python scripts/check_decision_semantics.py
This is separate from owner tests and is not clinical/policy evaluation.
"""

import hashlib
import itertools
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.decision.evidence import validate_evidence
from bioradar.research.decision.models import DecisionInput, DecisionPolicy, NumericInterval
from bioradar.research.decision.runner import evaluate_decision
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.decision_service import DecisionRuntime
from bioradar.research.engine.schemas import DecisionRunCreate
from bioradar.research.mining.models import EngineRecord, NormalizedSnapshot, canonical_hash
from bioradar.research.mining.prepare import snapshot_hash

CHECKS = []


def basis():
    return {
        "kind": "declared_assumption",
        "claimed_author": "SYNTHETIC researcher declaration",
        "authored_at": "2026-10-01T00:00:00Z",
        "rationale": "SYNTHETIC input, not a factual or professional recommendation",
    }


def point(value):
    return {"lower": str(value), "upper": str(value)}


def raw():
    metric = {
        "metric_id": "cost",
        "label": "SYNTHETIC cost",
        "direction": "minimize",
        "unit": "TEST currency",
        "definition": "SYNTHETIC annual cost",
        "population": "TEST units",
        "geography": "TEST region",
        "jurisdiction": "TEST jurisdiction",
        "period_start": "2027-01-01",
        "period_end": "2027-12-31",
        "price_base": "TEST 2026 nominal",
        "comparability_basis": "One shared declared scope",
    }
    return {
        "input_id": "SYNTHETIC-INTEGRATION-PROBE",
        "is_demo": True,
        "brief": {
            "title": "SYNTHETIC decision preparation, not real advice",
            "audience": "university",
            "decision_question": "Compare declared alternatives within a declared budget",
            "mandate": "SYNTHETIC mandate, not validated authority",
            "as_of": "2026-10-14T00:00:00Z",
            "decision_deadline": "2026-11-01T00:00:00Z",
            "horizon_start": "2027-01-01",
            "horizon_end": "2027-12-31",
            "baseline_option_id": "base",
            "scope_description": "TEST only",
        },
        "metrics": [metric],
        "options": [
            {"option_id": name, "label": name, "description": "SYNTHETIC alternative"}
            for name in ("base", "alternative")
        ],
        "scenarios": [
            {
                "scenario_id": "s",
                "label": "TEST scenario",
                "description": "No scenario probability",
                "basis": basis(),
            }
        ],
        "outcomes": [
            {
                "option_id": name,
                "scenario_id": "s",
                "metric_id": "cost",
                "value": point(value),
                "basis": basis(),
            }
            for name, value in (("base", 10), ("alternative", 20))
        ],
        "constraints": [
            {
                "constraint_id": "budget",
                "label": "TEST budget",
                "metric_id": "cost",
                "operator": "le",
                "threshold": point(50),
                "basis": basis(),
            }
        ],
    }


def policy():
    return DecisionPolicy(pareto_metric_ids=("cost",), regret_metric_id="cost")


def evaluate(value):
    input = DecisionInput.model_validate(value)
    return input, evaluate_decision(input, policy())


def expect_error(call, error=ValueError, code=None):
    try:
        call()
    except error as caught:
        if code:
            assert getattr(caught, "code", str(caught)) == code
        return
    raise AssertionError("invalid input/result was accepted")


def attach_observation(value, *, available_at="2026-10-13T00:00:00Z", quote_only=False, flags=()):
    ref = {
        "provider": "fixture",
        "object_type": "document",
        "object_id": "SYNTHETIC-source",
        "adopted_version": "v1",
        "version_source": "provider",
        "content_hash": "SYNTHETIC source hash",
        "authorization_ref": "development:development:sources",
    }
    record = EngineRecord.model_validate(
        {
            "record_ref": ref,
            "document_hash": hashlib.sha256(b"SYNTHETIC source text").hexdigest(),
            "source_instance": "fixture",
            "source_id": "fixture-source",
            "observed_at": "2026-10-13T00:00:00Z",
            "available_at": "2026-10-13T00:00:00Z",
            "ingestion_mode": "fixture",
            "text_status": "inline_authorized",
            "normalized_text": "SYNTHETIC source text",
            "normalization_version": "fixture-probe-v1",
            "record_flags": flags,
            "regional_indicators": [
                {
                    "indicator": "SYNTHETIC annual cost",
                    "region": "TEST region",
                    "period": "2025",
                    "unit": "TEST currency",
                    "value": 10,
                    "population_scope": "TEST units",
                    "jurisdiction": "TEST jurisdiction",
                    "provenance": {
                        "provider_ref": ref,
                        "available_at": available_at,
                        "schema_version": "SYNTHETIC probe.v1",
                    },
                }
            ],
        }
    )
    snapshot = NormalizedSnapshot(
        snapshot_id="SYNTHETIC-source",
        is_demo=True,
        source_instance="fixture",
        coverage_mode="manual_package",
        observed_at=datetime(2026, 10, 13, tzinfo=UTC),
        records=(record,),
    )
    frozen_hash = snapshot_hash(snapshot)
    value["evidence_snapshot_hashes"] = [frozen_hash]
    value["metrics"][0].update(period_start="2025-01-01", period_end="2025-12-31")
    link = {"snapshot_hash": frozen_hash, "record_ref": ref, "quote": "SYNTHETIC source text"}
    if not quote_only:
        link["field_path"] = "/regional_indicators/0/value"
    value["outcomes"][0]["basis"] = dict(basis(), kind="observed", evidence=[link])
    return snapshot


def main():
    original, reference = evaluate(raw())
    for audience in ("central", "provincial", "university", "enterprise"):
        value = raw()
        value["brief"]["audience"] = audience
        result = evaluate(value)[1]
        excluded = {"input_hash", "result_hash"}
        assert canonical_hash(reference.model_dump(mode="json", exclude=excluded)) == canonical_hash(
            result.model_dump(mode="json", exclude=excluded)
        )
    CHECKS.append("audience_only_does_not_change_evidence_or_numeric_comparison")

    value = raw()
    value["outcomes"][0]["value"] = point(60)
    result = evaluate(value)[1]
    assert result.feasibility[1].option_id == "base" and result.feasibility[1].status == "fail"
    assert any(
        item.option_id == "base" and item.difference.lower == item.difference.upper == 0
        for item in result.baseline_differences
    )
    CHECKS.append("failed_status_quo_retained_with_zero_self_difference")

    value = raw()
    value["outcomes"][1]["value"] = {"lower": "40", "upper": "60"}
    result = evaluate(value)[1]
    assert result.excluded_from_comparison["alternative"]
    assert result.conditional_comparison_set == ("base",)
    assert result.regret.status == "partial"
    assert result.regret.worst_case_bounds["base"].lower == result.regret.worst_case_bounds["base"].upper == 0
    CHECKS.append("uncertain_feasibility_not_hidden_as_global_optimum")

    value = raw()
    value["constraints"] = []
    value["outcomes"][1].update(value=None, unknown_reason="TEST missing competitor")
    result = evaluate(value)[1]
    assert result.regret.status == "unsupported" and not result.regret.worst_case_bounds
    assert any(item.status == "unknown" for item in result.pareto_comparisons)
    CHECKS.append("missing_competitor_not_zero_or_silently_discarded")

    value = raw()
    for cell in value["outcomes"]:
        cell["value"] = point(80)
    result = evaluate(value)[1]
    assert not result.conditional_comparison_set and result.regret.status == "unsupported"
    CHECKS.append("no_feasible_alternative_explicit")

    value = raw()
    value["constraints"] = []
    value["outcomes"][0]["value"] = {"lower": "0", "upper": "2"}
    value["outcomes"][1]["value"] = {"lower": "1", "upper": "4"}
    result = evaluate(value)[1]
    for a, b in itertools.product((Decimal(0), Decimal(2)), (Decimal(1), Decimal(4))):
        expected = {"base": max(Decimal(0), a - b), "alternative": max(Decimal(0), b - a)}
        for option, regret in expected.items():
            bound = result.regret.worst_case_bounds[option]
            assert bound.lower <= regret <= bound.upper
    assert result.regret.interpretation == "conservative_outer_bounds"
    CHECKS.append("interval_regret_bounds_cover_enumerated_extremes_without_probabilities")

    value = raw()
    source = attach_observation(value)
    observed = DecisionInput.model_validate(value)
    validate_evidence(observed, (source,))
    invalid_scope = observed.model_copy(
        update={"metrics": (observed.metrics[0].model_copy(update={"unit": "INCOMPATIBLE UNIT"}),)}
    )
    expect_error(
        lambda: validate_evidence(invalid_scope, (source,)),
        code="decision_numeric_fact_scope_or_unit_mismatch",
    )
    value["outcomes"][0]["value"] = point(999)
    expect_error(
        lambda: validate_evidence(DecisionInput.model_validate(value), (source,)),
        code="decision_numeric_fact_differs_from_adopted_value",
    )
    value = raw()
    source = attach_observation(value, quote_only=True)
    expect_error(
        lambda: validate_evidence(DecisionInput.model_validate(value), (source,)),
        code="decision_observed_numeric_requires_bound_typed_value",
    )
    value = raw()
    source = attach_observation(value, available_at="2027-01-01T00:00:00Z")
    expect_error(
        lambda: validate_evidence(DecisionInput.model_validate(value), (source,)),
        code="decision_typed_facet_available_after_cutoff",
    )
    value = raw()
    source = attach_observation(value, flags=("retracted",))
    expect_error(
        lambda: validate_evidence(DecisionInput.model_validate(value), (source,)),
        code="decision_observation_uses_retracted_or_deleted_record",
    )
    value["outcomes"][0]["basis"]["kind"] = "declared_assumption"
    assert any(
        gap.startswith("declared_input_cites_inactive_source:")
        for gap in validate_evidence(DecisionInput.model_validate(value), (source,))
    )
    CHECKS.append("typed_observation_matches_value_scope_and_time_quote_alone_rejected")

    DecisionRuntime._validate_output_ids(original, policy(), reference)
    bad = reference.model_copy(update={"conditional_comparison_set": ("UNDECLARED",)})
    expect_error(
        lambda: DecisionRuntime._validate_output_ids(original, policy(), bad),
        DomainError,
        "decision_result_binding_invalid",
    )
    first = reference.feasibility[0]
    tampered = first.checks[0].model_copy(update={"value": NumericInterval(lower=999, upper=999)})
    bad = reference.model_copy(
        update={"feasibility": (first.model_copy(update={"checks": (tampered,)}), *reference.feasibility[1:])}
    )
    expect_error(
        lambda: DecisionRuntime._validate_output_ids(original, policy(), bad),
        DomainError,
        "decision_result_binding_invalid",
    )
    bad = reference.model_copy(
        update={"feasibility": (first.model_copy(update={"checks": ()}), *reference.feasibility[1:])}
    )
    expect_error(
        lambda: DecisionRuntime._validate_output_ids(original, policy(), bad),
        DomainError,
        "decision_result_binding_invalid",
    )
    CHECKS.append("runtime_rejects_unknown_option_tampered_numeric_and_omitted_gate")

    value = raw()
    value["brief"].update(as_of="2035-01-01T00:00:00Z", decision_deadline="2035-02-01T00:00:00Z")
    settings = Settings(environment="test")
    runtime = DecisionRuntime(SimpleNamespace(settings=settings, database=None))
    body = DecisionRunCreate(input=DecisionInput.model_validate(value), policy=policy(), snapshot_ids=[])

    class NoDatabaseSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def begin(self):
            return self

    with patch("bioradar.research.engine.decision_service.Session", lambda _: NoDatabaseSession()):
        expect_error(
            lambda: runtime.create(DevelopmentAccess(settings).actor(), body, "SYNTHETIC-IDEMPOTENCY"),
            DomainError,
            "decision_future_cutoff",
        )
    CHECKS.append("future_appraisal_cutoff_rejected_before_source_read_or_database_mutation")

    files = [
        "services/bioradar/research/decision/models.py",
        "services/bioradar/research/decision/evidence.py",
        "services/bioradar/research/decision/algorithms.py",
        "services/bioradar/research/decision/runner.py",
        "services/bioradar/research/engine/decision_service.py",
    ]
    print(
        json.dumps(
            {
                "probe_format": "bioradar.decision-independent-semantics.v1",
                "captured_at": datetime.now(UTC).isoformat(),
                "synthetic_only": True,
                "scope": (
                    "Pure declared inputs, frozen synthetic source, static output binding "
                    "and no-DB runtime cutoff; not professional validation"
                ),
                "checks": {name: "passed" for name in CHECKS},
                "source_sha256": {
                    name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in files
                },
                "compute_version": reference.compute_version,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
