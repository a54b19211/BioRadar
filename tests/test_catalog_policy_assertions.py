"""Finite offline controls for the explicit catalog exception; no scientific/model result."""

import copy
from datetime import UTC, datetime

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import search_analysis_records
from bioradar.research.investigations.analysis_service import AnalysisService
from bioradar.research.investigations.kdd import extend_context
from test_initial_language_retrieval import context, fixed, plans, progress, save_empty

from tests.query_policy_assertions import assert_query_policy


def test_actual_empty_alternatives_allow_one_fixed_catalog_but_not_ordinary_empty_terms():
    data, state, items = fixed(), progress(), plans()
    data["execution_version"] = "query-step-v1"
    data["query_policy"].update(
        requested="query_planner",
        effective="query_planner",
        version="offline-language-contract",
        training_status="heuristic_untrained",
        selector="information_gain_v1",
    )
    state["query_policy"]["initial_plans"] = items
    state["tool_usage"] = {"tool_calls": 0, "preparation_tool_calls": 0}
    state["calls"] = []
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.history.collection.v1",
                "items": [],
                "coverage": {},
                "next_cursor": "unread-next-page" if len(calls) == 7 else None,
            },
        )

    reader = FabricHistoryReader(
        httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)),
        authorization_ref="SYNTHETIC",
    )

    def observed():
        return [
            {
                "action_id": q["action_id"],
                "query_index": i,
                "status": q["status"],
                "evidence_ids": [],
                "new_artifact_refs": [],
            }
            for i, q in enumerate(state["queries"])
        ]

    for index in range(7):
        request = extend_context(data, state, context(data, state, items))
        state["query_policy"]["checks"] = copy.deepcopy(request["checks"])
        candidates = [
            item
            for item in request["candidates"]
            if item["kind"] == "document_search" and item["arguments"]["provider"] == "fabric"
        ]
        action = next(
            item for item in candidates if bool(item["arguments"].get("catalog_discovery")) is (index == 6)
        )
        args = action["arguments"]
        if index == 6:
            assert AnalysisService._catalog_discovery_plan(None, data, state, action) == args["plan"]
        found = search_analysis_records(
            reader,
            source_id=args["source_id"],
            is_demo=False,
            terms=args["terms"],
            cutoff=datetime(2026, 10, 31, tzinfo=UTC),
            limit=8,
            catalog_discovery=args.get("catalog_discovery") is True,
        )
        history = observed()
        query = save_empty(data, state, action)
        decision = state["query_policy"]["decisions"][-1]
        decision.update(
            step=index,
            decision="continue",
            reason="offline selected actual candidate",
            policy_request=request,
            state_summary={"observed_history": history},
        )
        query.update(
            has_more=found["next_cursor"] is not None,
            scope="fabric_bounded_original_catalog_not_keyword_match" if index == 6 else "literal-query",
            usage={"catalog_discovery": True, "coverage": found["coverage"]}
            if index == 6
            else {"request_status": "received"},
        )
        state["tool_usage"]["tool_calls"] += 1
    assert len(calls) == 7 and calls[-1].url.params.get_list("terms") == []
    state["query_policy"]["decisions"].append(
        {
            "step": 7,
            "decision": "insufficient",
            "action": None,
            "reason": "no_originals_adopted",
            "state_summary": {"observed_history": observed()},
        }
    )
    state["query_policy"].update(status="insufficient", pending_action=None)
    AnalysisService._sync_query_receipts(state)
    actual = {
        "id": "offline-catalog-analysis",
        "is_demo": False,
        "search_scope": data["search_scope"],
        "evidence": [],
        "queries": state["queries"],
        "calls": [],
        "result": None,
        "query_policy": AnalysisService._policy_view(data, state),
    }
    checked = assert_query_policy(actual)
    assert checked["action_tool_calls"] == 7 and checked["status"] == "insufficient"
    assert actual["queries"][-1]["has_more"] is True  # A returned cursor never starts a second page.
    for mutation in (
        "ordinary_empty",
        "old_version",
        "foreign_source",
        "unread_evidence",
        "unknown_response",
        "nonempty_response",
        "changed_cutoff",
        "wrong_limit",
        "keyword_claim",
        "actual_catalog_terms",
    ):
        bad = copy.deepcopy(actual)
        trace = bad["query_policy"]
        choice = trace["decisions"][6]
        args = choice["action"]["arguments"]
        if mutation == "ordinary_empty":
            args.pop("catalog_discovery")
        elif mutation == "old_version":
            trace.pop("initial_retrieval_version")
        elif mutation == "foreign_source":
            args["source_id"] = "another-source"
        elif mutation == "unread_evidence":
            choice["policy_request"]["evidence"] = [{"id": "earlier-evidence"}]
        elif mutation == "unknown_response":
            bad["queries"][0]["usage"]["request_status"] = "unknown"
        elif mutation == "nonempty_response":
            bad["queries"][0]["returned"] = 1
        elif mutation == "changed_cutoff":
            args["as_of"] = "2026-11-01T00:00:00+00:00"
        elif mutation == "wrong_limit":
            bad["queries"][-1]["usage"]["coverage"]["followup_limit"] = 20
        elif mutation == "actual_catalog_terms":
            bad["queries"][-1]["plan"]["terms"] = ["enzyme"]
        else:
            bad["queries"][-1]["scope"] = "keyword-match"
        with pytest.raises(AssertionError):
            assert_query_policy(bad)
