"""The public HTTP ledger reconciles disjoint costs without leaking the saved RPC envelope."""

import copy

import pytest
from bioradar.research.investigations.analysis_service import AnalysisService

from scripts.query_policy_assertions import assert_professional_http_accounting


def operation(action_id, count, *, outcomes=None):
    exchanges = [
        {
            "attempts": 1,
            "http_attempted_indices": [1],
            "status": "received",
            "request": {"arguments": {"private": "SYNTHETIC-SECRET"}, "Authorization": "SYNTHETIC-SECRET"},
            "response": {
                "provider": "mech_evidence" if action_id else "biocheck",
                "status": "model_required",
            },
        }
        for _ in range(count)
    ]
    exchanges.extend(outcomes or [])
    return {
        "action_id": action_id,
        "binding": {
            "provider": "mech_evidence" if action_id else "biocheck",
            "operation": "evaluate" if action_id else "verify",
            "arguments": {"permit": "SYNTHETIC-SECRET"},
        },
        "exchanges": exchanges,
    }


def view(progress):
    fixed = {
        "execution_version": "query-step-v1",
        "query_policy": {},
        "budget": {"max_tool_calls": 64, "max_queries": 12},
    }
    return {
        "query_policy": AnalysisService._policy_view(fixed, progress),
        "queries": progress["queries"],
        "calls": progress["calls"],
    }


def test_projection_is_safe_disjoint_and_checker_rejects_omission_or_duplicate_charge():
    progress = {
        "queries": [
            {
                "action_id": "route-mech",
                "provider": "professional",
                "status": "read",
                "cost": {"tool_calls": 5, "reserved_tool_calls": 0},
            },
            {"action_id": "route-source", "provider": "fabric", "status": "read", "cost": {"tool_calls": 2}},
        ],
        "calls": [{"status": "unconfirmed"}],
        "professional_operations": {"route-op": operation("route-mech", 5), "report-op": operation(None, 6)},
        "tool_usage": {"preparation_tool_calls": 5, "tool_calls": 18, "professional_tool_calls": 11},
    }
    before = copy.deepcopy(progress)
    result = view(progress)
    assert progress == before and "SYNTHETIC-SECRET" not in repr(result["query_policy"])
    reconciled = assert_professional_http_accounting(result)
    assert reconciled["known_total_tool_calls"] == 5 + 7 + 6 == 18
    assert reconciled["professional_http_unknown_outcomes"] == 0
    assert result["query_policy"]["budget"]["usage"]["model_unconfirmed_calls"] == 1
    for corrupt in ("omit", "duplicate", "leak", "charge_route_twice"):
        bad = copy.deepcopy(result)
        trace = bad["query_policy"]
        if corrupt == "omit":
            trace["professional_http_ledger"].pop()
        elif corrupt == "duplicate":
            trace["professional_http_ledger"].append(trace["professional_http_ledger"][0])
        elif corrupt == "leak":
            trace["professional_http_ledger"][0]["request"] = {"Authorization": "SECRET"}
        else:
            trace["budget"]["usage"]["known_tool_calls"] += 5
        with pytest.raises(AssertionError):
            assert_professional_http_accounting(bad)


def test_http_unknown_reservation_is_separate_from_model_unknown_and_legacy_gap_is_not_rewritten():
    progress = {
        "queries": [
            {"action_id": "source", "provider": "fabric", "status": "read", "cost": {"tool_calls": 1}},
            {
                "action_id": "legacy",
                "provider": "weknora",
                "status": "failed",
                "error_code": "weknora_kdd_http_error",
                "cost": {"tool_calls": 0},
            },
        ],
        "calls": [],
        "professional_operations": {
            "report": operation(
                None,
                1,
                outcomes=[
                    {"attempts": 1, "http_attempted_indices": [1], "status": "unknown"},
                    {"attempts": 1, "http_attempted_indices": [], "status": "sent"},
                    {"attempts": 0, "http_attempted_indices": [], "status": "not_sent"},
                ],
            )
        },
        "tool_usage": {
            "preparation_tool_calls": 2,
            "tool_calls": 5,
            "professional_tool_calls": 2,
            "professional_reserved_tool_calls": 1,
        },
    }
    before = copy.deepcopy(progress)
    result = view(progress)
    accounting = assert_professional_http_accounting(result)
    assert accounting["known_total_tool_calls"] == 5
    assert accounting["non_route_reserved_tool_calls"] == 1
    assert accounting["professional_http_unknown_outcomes"] == 2
    usage = result["query_policy"]["budget"]["usage"]
    assert usage["model_unconfirmed_calls"] == 0 and usage["tool_calls"] is None
    assert usage["budgeted_tool_calls"] == 6
    assert result["query_policy"]["data_quality"][0]["code"] == "legacy_weknora_http_attempt_unaccounted"
    assert progress == before and progress["queries"][1]["cost"]["tool_calls"] == 0


@pytest.mark.parametrize(
    "attempts,response_kind,known,reserved",
    [
        (1, "received", 1, 0),
        (3, "received", 1, 2),
        (1, "missing", 0, 1),
        (0, "received", 0, 0),
        (1, "malformed", 0, 1),
    ],
)
def test_legacy_received_without_measured_indices_uses_only_provable_response(
    attempts, response_kind, known, reserved
):
    response = {
        "schema_version": "bioradar.professional.v1",
        "provider": "biocheck",
        "provider_version": "SYNTHETIC-v1",
        "operation_id": "legacy-report",
        "status": "completed",
        "result": {"qualification": "SYNTHETIC saved response"},
    }
    if response_kind == "missing":
        response = None
    elif response_kind == "malformed":
        response = "SYNTHETIC non-object response format"
    progress = {
        "queries": [
            {"action_id": "source", "provider": "fabric", "status": "read", "cost": {"tool_calls": 7}}
        ],
        "calls": [{"status": "unconfirmed"}],
        "professional_operations": {
            "legacy-report": operation(
                None,
                0,
                outcomes=[
                    {
                        "attempts": attempts,
                        "http_attempted_indices": [],
                        "status": "received",
                        "response": response,
                    }
                    for _ in range(6)
                ],
            )
        },
        "tool_usage": {"preparation_tool_calls": 8, "tool_calls": 15},
    }
    for previously_reserved in (0, 6 * attempts):
        progress["tool_usage"]["professional_reserved_tool_calls"] = previously_reserved
        before = copy.deepcopy(progress)
        result = view(progress)
        accounting = assert_professional_http_accounting(result)
        assert accounting["known_total_tool_calls"] == 15 + 6 * known
        assert accounting["non_route_professional_tool_calls"] == 6 * known
        assert accounting["non_route_reserved_tool_calls"] == 6 * reserved
        usage = result["query_policy"]["budget"]["usage"]
        assert usage["recorded_known_tool_calls"] == 15
        assert usage["reserved_tool_calls"] == 6 * reserved
        assert usage["budgeted_tool_calls"] == 15 + 6 * (known + reserved)
        assert usage["model_unconfirmed_calls"] == 1
        for item in result["query_policy"]["professional_http_ledger"]:
            assert item["http_attempted_indices"] == []
            assert item["known_tool_calls"] == known and item["reserved_tool_calls"] == reserved
            assert item["accounting_basis"] == (
                "legacy_received_response" if known else "unmeasured_attempts"
            )
        assert len(result["query_policy"]["data_quality"]) == 6
        assert progress == before
