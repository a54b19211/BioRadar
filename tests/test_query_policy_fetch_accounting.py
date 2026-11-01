"""Strict receipt projections of offline source-fetch checkpoints; no real collector/model calls."""

import copy

import httpx
import pytest
from bioradar.adapters import fabric_fetch
from bioradar.api.errors import DomainError
from bioradar.research.investigations import fetch_execution
from bioradar.research.investigations.analysis_service import AnalysisService
from test_opus_source_adapters import Host, fetch_action, fetch_fixed, run

from tests.query_policy_assertions import assert_query_policy


def test_fetch_unknown_poll_retains_actual_known_prefix_and_rejects_forged_cost(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request.method)
        return httpx.Response(202 if request.method == "POST" else 200, json=run("running"))

    with httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)) as client:
        host = Host(client)
        submit = fabric_fetch.submit_fetch
        monkeypatch.setattr(fetch_execution, "submit_fetch", lambda *a, **kw: submit(*a, **kw, client=client))
        monkeypatch.setattr(fetch_execution, "_seconds", lambda f, p: 60)
        fixed, progress = fetch_fixed(), {"queries": [], "evidence": [], "calls": []}
        action = fetch_action(fixed)
        for _ in range(3):
            with pytest.raises(DomainError) as exc:
                fetch_execution.execute_source_fetch(
                    host, "SYNTHETIC-analysis", "lease", fixed, progress, action, 0
                )
            assert exc.value.code == "analysis_fetch_pending"
        saved = next(
            copy.deepcopy(item)
            for item in reversed(host.checkpoints)
            if item["queries"]
            and item["queries"][0]["status"] == "sent"
            and item["queries"][0]["cost"].get("known_tool_calls") == 2
        )
    assert calls == ["POST", "GET", "GET"]
    fixed["query_policy"].update(
        schema_version="query-planner.external.v2",
        requested="query_planner",
        effective="query_planner",
        version="offline-external",
        selector="heuristic",
        training_status="heuristic_untrained",
    )
    saved["query_policy"] = {
        "status": "blocked",
        "reason": "history_fetch_read_outcome_unconfirmed",
        "pending_action": action,
        "checks": [{"id": "source_scope", "status": "unknown"}],
        "decisions": [
            {
                "step": 0,
                "decision": "continue",
                "action": action,
                "reason": "offline actual checkpoint",
                "state_summary": {"observed_history": []},
            }
        ],
    }
    AnalysisService._sync_query_receipts(saved)
    actual = {
        "id": "SYNTHETIC-analysis",
        "is_demo": False,
        "queries": saved["queries"],
        "evidence": [],
        "calls": [],
        "result": None,
        "query_policy": AnalysisService._policy_view(fixed, saved),
    }
    checked = assert_query_policy(actual)
    assert checked["action_tool_calls"] == 2 and checked["reserved_tool_calls"] == 1
    assert actual["query_policy"]["budget"]["usage"]["tool_calls"] is None
    for corrupt in ("prefix", "reserve", "wrong_kind", "run", "scope", "acceptance_key"):
        bad = copy.deepcopy(actual)
        query = bad["queries"][0]
        if corrupt == "prefix":
            query["cost"]["known_tool_calls"] = 3
        elif corrupt == "reserve":
            query["cost"]["reserved_tool_calls"] = 2
        elif corrupt == "wrong_kind":
            query["kind"] = "document_search"
        elif corrupt == "run":
            query["usage"]["fetch"]["run"]["source_id"] = "who_don"
        elif corrupt == "scope":
            query["plan"]["scope"]["is_demo"] = True
        else:
            query["usage"]["fetch"]["call_key"] = "another-acceptance-key"
        bad["query_policy"]["decisions"][0]["receipt"]["cost"] = copy.deepcopy(query["cost"])
        with pytest.raises(AssertionError):
            assert_query_policy(bad)
