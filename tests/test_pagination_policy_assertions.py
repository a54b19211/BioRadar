"""Strict finite cursor checks from actual synthetic HTTP pages and runtime projections."""

import copy
from datetime import UTC, datetime

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import search_analysis_records
from bioradar.research.investigations.analysis_service import AnalysisService
from bioradar.research.investigations.kdd import extend_context
from bioradar.research.investigations.query_policy import BOUNDED_INITIAL_VERSION, PAGINATION_VERSION
from test_initial_language_retrieval import context, fixed, plans, progress, save_empty

from scripts.query_policy_assertions import _search_key, assert_query_policy


def actual_page_view(catalog):
    data, state = fixed(), progress()
    items = plans()[:3] if catalog else plans()[:1]
    data["execution_version"] = "query-step-v1"
    data["query_policy"].update(
        requested="query_planner",
        effective="query_planner",
        version="SYNTHETIC-paged-policy",
        training_status="heuristic_untrained",
        selector="information_gain_v1",
        initial_retrieval_version=BOUNDED_INITIAL_VERSION,
        pagination_version=PAGINATION_VERSION,
    )
    state["query_policy"]["initial_plans"] = items
    state["tool_usage"] = {"tool_calls": 0, "preparation_tool_calls": 0}
    state["calls"] = []
    requests = []
    first_page = len(items) + 1 if catalog else 1

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "schema_version": "datafabric.history.collection.v1",
                "items": [],
                "coverage": {"max_seq": 17},
                "next_cursor": "SYNTHETIC-opaque-page-2" if len(requests) == first_page else None,
            },
        )

    def observed():
        return [
            {
                "action_id": query["action_id"],
                "query_index": index,
                "status": query["status"],
                "evidence_ids": [],
                "new_artifact_refs": [],
            }
            for index, query in enumerate(state["queries"])
        ]

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler)) as client:
        reader = FabricHistoryReader(client, authorization_ref="SYNTHETIC")
        for index in range(first_page + 1):
            request = extend_context(data, state, context(data, state, items))
            state["query_policy"]["checks"] = copy.deepcopy(request["checks"])
            if index == first_page:
                action = next(item for item in request["candidates"] if "cursor" in item["arguments"])
            elif catalog and index == len(items):
                action = next(
                    item
                    for item in request["candidates"]
                    if item["arguments"].get("catalog_discovery") is True
                )
            else:
                action = next(
                    item
                    for item in request["candidates"]
                    if item["arguments"].get("source_id") == items[index]["source_id"]
                    and item["arguments"].get("terms") == items[index]["terms"]
                    and not item["arguments"].get("relaxed_from")
                    and "cursor" not in item["arguments"]
                )
            args = action["arguments"]
            if "cursor" in args or args.get("catalog_discovery"):
                assert AnalysisService._catalog_discovery_plan(None, data, state, action) == args["plan"]
            found = search_analysis_records(
                reader,
                source_id=args["source_id"],
                is_demo=False,
                terms=args["terms"],
                cutoff=datetime(2026, 10, 31, tzinfo=UTC),
                limit=args["limit"],
                catalog_discovery=args.get("catalog_discovery") is True,
                cursor=args.get("cursor"),
                max_seq=args.get("max_seq"),
                pagination=True,
            )
            history = observed()
            query = save_empty(data, state, action)
            state["query_policy"]["decisions"][-1].update(
                step=index,
                decision="continue",
                reason="SYNTHETIC actual candidate",
                policy_request=request,
                state_summary={"observed_history": history},
            )
            query.update(
                has_more=bool(found["next_cursor"]),
                scope="fabric_bounded_original_catalog_not_keyword_match"
                if args.get("catalog_discovery") is True
                else "literal-query",
                pagination={
                    "has_more": bool(found["next_cursor"]),
                    "next_cursor": found["next_cursor"],
                    "max_seq": found["coverage"]["max_seq"],
                    "page_number": args.get("page_number", 1),
                },
                usage={"catalog_discovery": True, "coverage": found["coverage"]}
                if args.get("catalog_discovery") is True
                else {"request_status": "received"},
            )
            state["tool_usage"]["tool_calls"] += 1
    state["query_policy"]["decisions"].append(
        {
            "step": len(state["queries"]),
            "decision": "insufficient",
            "action": None,
            "reason": "SYNTHETIC no originals adopted",
            "state_summary": {"observed_history": observed()},
        }
    )
    state["query_policy"].update(status="insufficient", pending_action=None)
    AnalysisService._sync_query_receipts(state)
    return {
        "id": "SYNTHETIC-cursor-analysis",
        "is_demo": False,
        "search_scope": data["search_scope"],
        "queries": state["queries"],
        "evidence": [],
        "calls": [],
        "result": None,
        "query_policy": AnalysisService._policy_view(data, state),
    }, requests


@pytest.mark.parametrize("catalog", [False, True])
def test_measured_pages_pass_but_forged_or_metadata_only_retries_do_not(catalog):
    actual, requests = actual_page_view(catalog)
    checked = assert_query_policy(actual, expected_policy="query_planner")
    assert checked["action_tool_calls"] == len(requests) == (5 if catalog else 2)
    assert requests[-1].url.params["cursor"] == "SYNTHETIC-opaque-page-2"
    original = actual["query_policy"]["decisions"][-2]["action"]
    metadata = copy.deepcopy(original)
    metadata["arguments"].update(index=999, phase="another-phase", reason="another-label", page_number=8)
    assert _search_key(metadata) == _search_key(original)
    for mutation in [
        "old_flag",
        "cursor",
        "sequence",
        "page_number",
        "limit",
        "source",
        "cutoff",
        "unknown_predecessor",
        "failed_predecessor",
        "missing_history",
        "actual_terms",
        "metadata_rescan",
        "received_sequence",
        "cursor_self_loop",
    ]:
        bad = copy.deepcopy(actual)
        trace = bad["query_policy"]
        decision = trace["decisions"][-2]
        action, query = decision["action"], bad["queries"][-1]
        args = action["arguments"]
        if mutation == "old_flag":
            trace.pop("pagination_version")
        elif mutation == "cursor":
            args["cursor"] = "SYNTHETIC-guessed-cursor"
        elif mutation == "sequence":
            args["max_seq"] += 1
        elif mutation == "page_number":
            args["page_number"] += 1
        elif mutation == "limit":
            args["limit"] += 1
        elif mutation == "source":
            args["source_id"] = "unselected-source"
        elif mutation == "cutoff":
            args["as_of"] = "2027-01-01T00:00:00+00:00"
        elif mutation in {"unknown_predecessor", "failed_predecessor"}:
            previous = next(q for q in bad["queries"] if q["action_id"] == args["previous_action_id"])
            if mutation == "unknown_predecessor":
                previous["usage"]["request_status"] = "unknown"
            else:
                previous["status"] = "failed"
        elif mutation == "missing_history":
            decision["policy_request"]["history"] = []
        elif mutation == "actual_terms":
            query["plan"]["terms"] = ["SYNTHETIC substituted search"]
        elif mutation == "metadata_rescan":
            action["id"] = "page:another-metadata-label"
            args.update(index=999, phase="changed-label")
        elif mutation == "received_sequence":
            query["pagination"]["max_seq"] += 1
        else:
            query["has_more"] = query["pagination"]["has_more"] = True
            query["pagination"]["next_cursor"] = args["cursor"]
        with pytest.raises(AssertionError):
            assert_query_policy(bad)
