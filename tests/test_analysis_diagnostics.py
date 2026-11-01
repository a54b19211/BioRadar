"""Source-free delivery diagnostics from saved metadata; no research/model execution."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SECRET = "SYNTHETIC_DO_NOT_EXPORT_SOURCE_OR_PROMPT"


def build(view, **kwargs):
    module = ROOT / "services/bioradar/delivery/analysis_diagnostics.py"
    assert module.is_file(), "Saved failure metadata has no source-free diagnostic projection"
    from bioradar.delivery.analysis_diagnostics import build_analysis_diagnostics

    return build_analysis_diagnostics(view, **kwargs)


def saved():
    body = SECRET + " report"
    ref = {
        "provider": "fabric",
        "object_id": "record-7",
        "adopted_version": "fixed-v3",
        "authorization_ref": SECRET + "-grant",
    }
    view = {
        "id": "analysis-7",
        "investigation_id": "investigation-2",
        "state": "needs_review",
        "error_code": "analysis_passage_invalid",
        "is_demo": True,
        "question": SECRET,
        "result": {
            "report": {
                "status": "FAIL",
                "generation_error": SECRET,
                "final_text": body,
                "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "verification": {
                    "status": "FAIL",
                    "draft": body,
                    "units": [
                        {
                            "unit_id": "U2",
                            "sentence_index": 2,
                            "text": SECRET,
                            "issues": ["causal_overstatement", SECRET],
                            "claims": [
                                {
                                    "span": SECRET,
                                    "kind": "fact",
                                    "verdict": "contradicted",
                                    "evidence": [{"evidence_id": "E7", "segment_id": "S3", "quote": SECRET}],
                                }
                            ],
                        }
                    ],
                    "verification_errors": [
                        {"unit_id": "U2", "claim_index": 0, "code": "unknown_segment", "message": SECRET}
                    ],
                },
            }
        },
        "calls": [
            {
                "call_id": "call-1",
                "call_key": "read-1",
                "stage": "read_plan",
                "status": "received",
                "payload": {"evidence": [{"text": SECRET}]},
                "system_prompt": SECRET,
                "receipt": {
                    "status": "completed",
                    "request_bytes": 2048,
                    "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                },
            },
            {
                "call_id": "call-2",
                "call_key": "check-1",
                "stage": "biocheck_check",
                "status": "unconfirmed",
                "payload": {"draft": SECRET},
                "receipt": {
                    "status": "unconfirmed",
                    "error_code": "model_transport_unconfirmed",
                    "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
                },
            },
        ],
        "queries": [
            {
                "action_id": "search-1",
                "kind": "document_search",
                "provider": "fabric",
                "plan": {"source_id": "fixture", "terms": ["alpha", "cohort"], "reason": SECRET},
                "status": "empty",
                "pagination": {
                    "page_number": 1,
                    "has_more": True,
                    "next_cursor": "opaque-page-2",
                    "max_seq": 9,
                },
            },
            {
                "action_id": "search-2",
                "kind": "document_search",
                "provider": "fabric",
                "plan": {
                    "source_id": "fixture",
                    "terms": ["alpha", "cohort"],
                    "cursor": "opaque-page-2",
                    "previous_query_index": 0,
                },
                "status": "read",
                "pagination": {"page_number": 2, "has_more": False, "next_cursor": None, "max_seq": 9},
                "records": [{"text": SECRET}],
                "evidence_keys": ["E7"],
            },
        ],
        "progress": {},
    }
    progress = {
        "calls": copy.deepcopy(view["calls"]),
        "queries": copy.deepcopy(view["queries"]),
        "reading_repairs": {
            "read-1": {
                "reason": "analysis_passage_invalid",
                "status": "validated",
                "original_call_key": "read-1",
                "repair_call_key": "read-1:structure-repair",
                "repair_call_id": "repair-1",
                "field_paths": [["claims", 0, "evidence_links", 0]],
                "allowed_reference_ids": {"E7": ["E7:p001"]},
                "invalid_fields": [SECRET],
                "original_request_bytes": 10000,
                "repair_request_bytes": 1100,
                "max_input_ratio": 0.2,
            }
        },
        "narrative_extractions": {
            "extract-7": {
                "record_ref": ref,
                "status": "partial",
                "observations": [{"quote": SECRET}],
                "gaps": [
                    {
                        "observation_index": index,
                        "issues": [{"stage": stage, "reason": stage + "_binding_invalid"}],
                    }
                    for index, stage in enumerate(["unit", "period", "subject", "pairing", "metric_role"])
                ],
            }
        },
    }
    fixed = {
        "budget": {"max_calls": 32, "max_tokens": 1048576, "max_seconds": 3600},
        "execution_limits": {
            "document_search": 16,
            "document_read": 32,
            "pages_per_chain": 5,
            "investigations_per_batch": 1,
        },
        "evidence": [{"text": SECRET}],
        "private_config": {"api_key": SECRET},
    }
    return view, progress, fixed


def test_source_free_package_retains_failure_repair_numeric_rules_and_biocheck_positions():
    view, progress, fixed = saved()
    before = copy.deepcopy((view, progress, fixed))
    report = build(view, progress=progress, fixed=fixed)
    serialized = json.dumps(report, ensure_ascii=False)
    assert SECRET not in serialized
    assert report["analysis_id"] == "analysis-7" and report["error_code"] == "analysis_passage_invalid"
    repair = report["reading_repairs"][0]
    assert repair["reason"] == "analysis_passage_invalid"
    assert repair["field_paths"] == [["claims", 0, "evidence_links", 0]]
    assert repair["allowed_reference_ids"] == {"E7": ["E7:p001"]}
    assert "invalid_fields" not in repair
    assert {issue["stage"] for item in report["numeric_candidates"] for issue in item["issues"]} == {
        "unit",
        "period",
        "subject",
        "pairing",
        "metric_role",
    }
    assert len(report["numeric_candidates"]) == 5
    assert all(item["record_ref"]["object_id"] == "record-7" for item in report["numeric_candidates"])
    assert any(
        item.get("label") == "causal_overstatement" and item["sentence_index"] == 2
        for item in report["biocheck_issues"]
    )
    assert any(
        item.get("code") == "unknown_segment" and item["claim_index"] == 0
        for item in report["biocheck_issues"]
    )
    assert (view, progress, fixed) == before


def test_stage_usage_keeps_unknown_actual_bytes_and_does_not_count_failed_report_as_pass():
    view, progress, fixed = saved()
    report = build(view, progress=progress, fixed=fixed)
    usage = report["usage"]
    assert usage["total_tokens"] is None and usage["known_total_tokens"] == 70
    assert usage["unknown_usage_calls"] == 1
    stages = {item["stage"]: item for item in usage["stages"]}
    assert stages["read_plan"]["total_tokens"] == 70
    assert stages["read_plan"]["request_bytes"] == 2048
    assert stages["biocheck_check"]["request_bytes"] is None
    assert stages["biocheck_check"]["total_tokens"] is None
    assert usage["verified_pass_total_tokens"] is None
    assert report["execution_limits"]["document_search"] == 16
    assert report["execution_limits"]["max_tokens"] == 1048576
    progress["calls"][0]["receipt"]["request_bytes"] = None
    progress["calls"][0]["request_bytes"] = 1234
    actual = build(view, progress=progress, fixed=fixed)["usage"]["calls"][0]
    assert actual["request_bytes"] == 1234


def test_true_bound_pass_usage_is_exact_and_includes_all_actual_stages():
    view, progress, fixed = saved()
    report = view["result"]["report"]
    report["status"] = report["verification"]["status"] = "PASS"
    report["verification"]["verification_errors"] = []
    progress["calls"][1]["status"] = "received"
    progress["calls"][1]["receipt"].update(
        status="completed",
        request_bytes=4096,
        usage={"prompt_tokens": 80, "completion_tokens": 10, "total_tokens": 90},
    )
    diagnostics = build(view, progress=progress, fixed=fixed)
    assert diagnostics["usage"]["total_tokens"] == 160
    assert diagnostics["usage"]["verified_pass_total_tokens"] == 160
    report["verification"]["draft"] += " changed"
    assert build(view, progress=progress, fixed=fixed)["usage"]["verified_pass_total_tokens"] is None


def test_search_terms_page_number_and_continuation_receipt_remain_traceable_without_source():
    view, progress, fixed = saved()
    queries = build(view, progress=progress, fixed=fixed)["queries"]
    assert queries[0]["terms"] == ["alpha", "cohort"]
    assert queries[0]["pagination"]["has_more"] is True
    assert queries[1]["pagination"]["page_number"] == 2
    assert queries[1]["previous_query_index"] == 0
    assert queries[0]["pagination"]["next_cursor_sha256"] == queries[1]["cursor_sha256"]
    assert "opaque-page-2" not in json.dumps(queries)


def test_cli_exports_saved_file_only_and_does_not_overwrite_a_prior_package(tmp_path):
    script = ROOT / "scripts/export_analysis_diagnostics.py"
    assert script.is_file(), "No saved-data-only source-free diagnostics CLI exists"
    view, progress, fixed = saved()
    source, target = tmp_path / "saved.json", tmp_path / "diagnostics.json"
    source.write_text(json.dumps({"analysis": view, "progress": progress, "fixed_input": fixed}))
    result = subprocess.run(
        [sys.executable, str(script), "--input", str(source), "--output", str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    before = target.read_bytes()
    assert json.loads(before)["analysis_id"] == "analysis-7"
    assert SECRET.encode() not in before
    again = subprocess.run(
        [sys.executable, str(script), "--input", str(source), "--output", str(target)],
        capture_output=True,
        text=True,
    )
    assert again.returncode != 0 and target.read_bytes() == before


def test_program_and_model_numeric_indices_keep_their_distinct_candidate_origins():
    view, progress, fixed = saved()
    cached = progress["narrative_extractions"]["extract-7"]
    view["query_policy"] = {
        "artifact_results": {
            "calculation-7": {
                "data_preparation": {
                    "excluded_records": [
                        {
                            "reason": "narrative_observation_rejected",
                            "record_ref": cached["record_ref"],
                            "observation_index": 0,
                            "issues": cached["gaps"][0]["issues"],
                        }
                    ],
                }
            }
        }
    }
    candidates = build(view, progress=progress, fixed=fixed)["numeric_candidates"]
    assert {item["observation_index"] for item in candidates if item["origin"] == "model_extraction"} == set(
        range(5)
    )
    assert len(candidates) == 6
    assert len([item for item in candidates if item["origin"] == "artifact"]) == 1
    assert candidates[0]["artifact_id"] == "calculation-7"


def test_legacy_numeric_extraction_reason_remains_visible():
    view, progress, fixed = saved()
    progress["narrative_extractions"] = {
        "SYNTHETIC-v1": {
            "record_ref": {
                "source_id": "SYNTHETIC",
                "object_id": "SYNTHETIC-old",
                "adopted_version": "SYNTHETIC-v1",
            },
            "gaps": [
                {"observation_index": 0, "reason": "literal_metric_scope_period_or_value_requires_review"}
            ],
        }
    }
    result = build(view, progress=progress, fixed=fixed)
    row = next(item for item in result["numeric_candidates"] if item["origin"] == "model_extraction")
    assert row["issues"] == [
        {"stage": "scope", "reason": "literal_metric_scope_period_or_value_requires_review"}
    ]
