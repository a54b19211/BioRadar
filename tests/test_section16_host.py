"""Generic execution regressions; synthetic callbacks only, no model HTTP."""

from datetime import UTC, datetime

import pytest
from bioradar.adapters.reasoning_model import ModelCallReceipt, ModelCompletion, ModelUsage
from bioradar.api.config import Settings
from bioradar.research.investigations.analysis_models import ModelSynthesis
from bioradar.research.investigations.analysis_service import AnalysisService
from bioradar.research.investigations.query_policy import build_coverage_checks
from test_kdd_finishing_budget import state
from test_query_policy import fixed, progress


def test_next_request_is_not_blocked_by_four_future_worst_case_reservations():
    service, frozen, saved = state("pipeline")
    frozen["budget"]["max_tokens"] = 200000
    service._final_token_allowance = lambda *_: 500000
    sent = []

    def complete(settings, **kwargs):
        sent.append(kwargs["stage"])
        return ModelCompletion(
            content="{}",
            parsed={},
            receipt=ModelCallReceipt(
                call_id=kwargs["call_id"],
                stage=kwargs["stage"],
                input_hash=kwargs["input_hash"],
                prompt_version=kwargs["prompt_version"],
                requested_model="SYNTHETIC-model",
                configured_model_revision=None,
                status="completed",
                started_at=datetime.now(UTC),
                max_output_tokens=settings.semantic_llm_max_output_tokens,
                usage=ModelUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
            ),
        )

    service.model_call = complete
    service._call(
        "SYNTHETIC",
        "token",
        "synthesize",
        ModelSynthesis,
        frozen,
        saved,
        {"task": "synthesize", "material": "SYNTHETIC " * 7000},
    )
    assert sent == ["synthesize"]
    assert saved["calls"][0]["request_bytes"] > 0
    assert saved["calls"][0]["input_estimate"]["basis"] == "uncalibrated_byte_upper_bound"


@pytest.mark.parametrize("policy", ["baseline", "query_planner"])
def test_all_coverage_paths_keep_unknown_duplicate_labels_without_aborting(policy):
    frozen, saved = fixed(policy), progress()
    saved["evidence"] = [{"key": "E1"}]
    reading = {
        "coverage_checks": [
            {"id": "UNDECLARED", "status": "passed", "evidence_ids": ["E1"]},
            {"id": "answer_scope", "status": "passed", "evidence_ids": ["E1"]},
            {"id": "answer_scope", "status": "passed", "evidence_ids": ["E1"]},
        ]
    }
    checks = build_coverage_checks(frozen, saved, reading=reading)
    assert next(c for c in checks if c["id"] == "answer_scope")["status"] == "unknown"
    assert not any(c["id"] == "UNDECLARED" and c["status"] == "passed" for c in checks)


def test_kdd_search_count_excludes_pages_and_non_search_actions():
    _, frozen, saved = state("pipeline")
    frozen["budget"]["max_queries"] = 16
    saved["queries"] = [
        {
            "kind": "document_search",
            "status": "read",
            "pagination": {"page_number": 1},
            "cost": {"tool_calls": 1},
        },
        {
            "kind": "document_search",
            "status": "read",
            "pagination": {"page_number": 2},
            "cost": {"tool_calls": 1},
        },
        {"kind": "document_read", "status": "read", "cost": {"tool_calls": 1}},
        {"kind": "analysis_run", "status": "read", "cost": {"tool_calls": 0}},
    ]
    budget = AnalysisService._policy_budget(frozen, saved)
    assert budget["usage"]["queries"] == 1
    assert budget["remaining"]["queries"] == 15
    assert budget["usage"]["known_tool_calls"] == 3


def test_test_profile_accepts_hour_without_changing_product_default():
    assert Settings(environment="test", analysis_max_seconds=3600).analysis_max_seconds == 3600
    assert Settings().analysis_max_seconds == 1800


def test_named_object_is_read_only_from_adopted_body_not_title():
    from bioradar.research.investigations.query_policy import named_object_context

    frozen, saved = fixed("query_planner"), progress()
    frozen["question"] = "Compare Orion."
    frozen["evidence_time"]["cutoff"] = frozen["as_of"]
    frozen["is_demo"] = True
    frozen["query_policy"]["schema_version"] = "query-planner.external.v2"
    saved["query_policy"]["named_objects"] = [
        {"id": "object-1", "name": "Orion", "question_span": {"start": 8, "end": 13}}
    ]
    saved["evidence"] = [{"key": "E1", "title": "Orion", "text": "unrelated actual body"}]
    candidates = []
    objects = named_object_context(frozen, saved, candidates, [])
    assert objects[0]["status"] == "unread" and not objects[0]["evidence_ids"]
    assert candidates[0]["arguments"]["terms"] == ["Orion"]
    saved["evidence"][0]["text"] = "preOrionSuffix and Orion-2 are other object names."
    assert named_object_context(frozen, saved, [], [])[0]["status"] == "unread"
    saved["evidence"][0]["text"] = "Orion actual source body."
    assert named_object_context(frozen, saved, [], [])[0]["evidence_ids"] == ["E1"]


def test_saved_request_bytes_match_the_reduced_output_cap():
    from bioradar.adapters.reasoning_model import encode_request

    service, frozen, saved = state("pipeline")
    schema = {"type": "object"}
    payload = {"task": "SYNTHETIC"}
    prompt = "SYNTHETIC system"
    encoded = encode_request(
        service.settings, stage="synthetic", system_prompt=prompt, payload=payload, response_schema=schema
    )
    frozen["budget"]["max_tokens"] = len(encoded) + 1024 + 9999
    actual = []

    def model(config, **kwargs):
        actual.append(
            len(
                encode_request(
                    config,
                    stage=kwargs["stage"],
                    system_prompt=kwargs["system_prompt"],
                    payload=kwargs["payload"],
                    response_schema=kwargs["response_schema"],
                )
            )
        )
        return ModelCompletion(
            content="{}",
            parsed={},
            receipt=ModelCallReceipt(
                call_id=kwargs["call_id"],
                stage=kwargs["stage"],
                input_hash=kwargs["input_hash"],
                prompt_version=kwargs["prompt_version"],
                requested_model="SYNTHETIC-model",
                configured_model_revision=None,
                status="completed",
                started_at=datetime.now(UTC),
                max_output_tokens=config.semantic_llm_max_output_tokens,
                usage=ModelUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
            ),
        )

    service.model_call = model
    service._call("SYNTHETIC", "token", "synthetic", schema, frozen, saved, payload, system_prompt=prompt)
    assert saved["calls"][0]["model_parameters"]["max_output_tokens"] == 9999
    assert saved["calls"][0]["request_bytes"] == actual[0]
    assert saved["calls"][0]["input_estimate"]["request_bytes"] == actual[0]


def test_host_retains_unknown_coverage_issue_after_normalized_reading(monkeypatch):
    from test_optional_policy_reading import harness, reading

    service, frozen, saved = harness(monkeypatch)
    saved.setdefault("limitations", [])

    def model(config, **kwargs):
        value = reading()
        value["coverage_checks"] = [{"id": "unknown-model-check", "status": "passed", "evidence_ids": ["E1"]}]
        return ModelCompletion(
            content="SYNTHETIC",
            parsed=value,
            receipt=ModelCallReceipt(
                call_id=kwargs["call_id"],
                stage=kwargs["stage"],
                input_hash=kwargs["input_hash"],
                prompt_version=kwargs["prompt_version"],
                requested_model="SYNTHETIC-model",
                configured_model_revision=None,
                status="completed",
                started_at=datetime.now(UTC),
                max_output_tokens=config.semantic_llm_max_output_tokens,
                usage=ModelUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
            ),
        )

    service.model_call = model
    assert service._policy_reading("SYNTHETIC", "token", frozen, saved)
    assert saved["query_policy"]["coverage_issues"] == [
        {"check_id": "unknown-model-check", "source": "model_coverage", "status": "unknown"}
    ]
