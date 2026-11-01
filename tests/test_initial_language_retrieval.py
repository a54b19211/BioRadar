"""Initial language alternatives and bounded directory eligibility; only synthetic HTTP."""

import copy
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import search_analysis_records
from bioradar.adapters.query_planner import decide_query
from bioradar.api.errors import DomainError
from bioradar.research.investigations.kdd import extend_context
from bioradar.research.investigations.query_policy import (
    BOUNDED_INITIAL_VERSION,
    build_policy_context,
    catalog_discovery_candidates,
    freeze_policy,
    question_plan_payload,
    semantic_key,
    validate_initial_plans,
)
from test_opus_source_adapters import original
from test_query_policy import fixed as legacy_fixed
from test_query_policy import plan, progress, receipt, write_planner

pytestmark = pytest.mark.ENGINE


def fixed():
    data = legacy_fixed("query_planner")
    data.update(
        question="SYNTHETIC 膜过滤设备的公开资料是否提供验证依据？",
        is_demo=False,
        as_of="2026-10-31T00:00:00+00:00",
        evidence_time={"mode": "current", "cutoff": "2026-10-31T00:00:00+00:00"},
        reading_limits={"search_limit": 8},
        budget={"max_queries": 16, "max_tool_calls": 64},
    )
    data["query_policy"].update(
        schema_version="query-planner.external.v2",
        research_contract_version="bioradar-research-input-v1",
        initial_retrieval_version=BOUNDED_INITIAL_VERSION,
        direction="research",
        selector="information_gain_v1",
        max_steps=32,
        action_limits={"document_search": 8},
    )
    return data


def plans():
    return [
        plan(term)
        for term in (
            "膜过滤", "membrane filtration", "membrane filter",
            "薄膜过滤", "filter membrane", "filtration equipment",
        )
    ]


def context(data, state, items):
    return build_policy_context(data, state, phase="initial", plans=items)


def save_empty(data, state, action):
    query = receipt(
        action, returned=0, cost={"tool_calls": 1, "tokens": 0, "relation_access": 0},
        usage={"request_status": "received"},
    )
    state["queries"].append(query)
    state["query_policy"]["decisions"].append({"action": copy.deepcopy(action)})
    return query


def exhausted():
    data, state, items = fixed(), progress(), plans()
    state["query_policy"]["initial_plans"] = items
    for index in range(len(items)):
        candidates = context(data, state, items)["candidates"]
        action = next(
            item for item in candidates
            if not item["arguments"].get("catalog_discovery")
            and item["arguments"]["index"] == index
        )
        save_empty(data, state, action)
    return data, state, items


def test_current_contract_limits_generated_queries_and_preserves_baseline_payload(tmp_path):
    settings = write_planner(tmp_path)
    settings.analysis_query_policy_v2_max_steps = 32
    assert freeze_policy(settings)["initial_retrieval_version"] == BOUNDED_INITIAL_VERSION
    current = freeze_policy(settings)
    assert current["narrative_contract_version"] == "literal-narrative-observations-v2"
    assert current["biocheck_input_version"] == "adopted-window-declared-audit-v1"
    assert current["narrative_proposal_version"] == "literal-narrative-proposal-v1"
    assert current["source_intelligence_version"] == "public-health-source-intelligence-v1"
    legacy = freeze_policy(settings, "baseline")
    assert "narrative_contract_version" not in legacy and "biocheck_input_version" not in legacy
    assert "narrative_proposal_version" not in legacy and "source_intelligence_version" not in legacy
    assert "initial_retrieval_version" not in freeze_policy(settings, "baseline")

    data = fixed()
    payload = question_plan_payload(data)
    assert payload["max_queries"] == 3
    assert payload["retrieval_strategy_version"] == BOUNDED_INITIAL_VERSION
    assert "OR备选" in payload["instruction"]
    assert "不同语言或同义词强行放进同一terms" in payload["instruction"]
    assert "待检索假设" in payload["instruction"]
    assert len(validate_initial_plans({"queries": plans()[:3]}, data)) == 3
    assert len(validate_initial_plans(plans(), data)) == 6
    data["budget"]["max_queries"] = 4
    with pytest.raises(DomainError) as error:
        validate_initial_plans({"queries": plans()}, data)
    assert error.value.code == "analysis_query_budget"

    data = fixed()
    data["query_policy"]["effective"] = "baseline"
    old = question_plan_payload(data)
    assert old == {
        "task": "question_plan", "question": data["question"],
        "search_scope": ["papers"], "max_queries": 2,
        "instruction": (
            "仅规划在允许来源内的最多两项字面查询，每项最多三个词。"
            "terms中的每个词是必须同时满足的字面条件（AND），不是任选词或语义扩展。"
            "先用辨识力强且问题中确实出现的研究对象词；对象词放第一项。"
            "首查通常只需一个药物、靶点、疾病或登记编号，不叠加机制、试验类型等修饰词。"
            "当前没有已读证据，不输出事实、命题或结论。"
        ),
    }
    with pytest.raises(DomainError):
        validate_initial_plans({"queries": plans()[:3]}, data)
    data["query_policy"]["initial_retrieval_version"] = BOUNDED_INITIAL_VERSION
    data["query_policy"]["effective"] = "baseline"
    assert question_plan_payload(data) == old
    with pytest.raises(DomainError):
        validate_initial_plans({"queries": plans()[:3]}, data)


def test_actual_empty_literal_http_does_not_remove_an_independent_language_alternative():
    data, state, items = fixed(), progress(), plans()[:2]
    before = copy.deepcopy(items)
    calls = []

    def handler(request):
        terms = request.url.params.get_list("terms")
        calls.append(terms)
        text = "SYNTHETIC membrane filtration equipment."
        found = [original("papers", text=text)] if all(term in text for term in terms) else []
        return httpx.Response(200, json={
            "schema_version": "datafabric.history.collection.v1", "items": found,
            "coverage": {}, "next_cursor": None,
        })

    reader = FabricHistoryReader(
        httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler)),
        authorization_ref="SYNTHETIC",
    )
    for index, expected in enumerate(("empty", "read")):
        action = next(
            candidate for candidate in context(data, state, items)["candidates"]
            if candidate["arguments"]["index"] == index
        )
        assert action["arguments"]["plan"]["reason"].startswith("待检索假设")
        found = search_analysis_records(
            reader, source_id="papers", is_demo=False, terms=action["arguments"]["terms"],
            cutoff=datetime(2026, 10, 31, tzinfo=UTC), limit=8,
        )
        query = save_empty(data, state, action)
        query.update(status="read" if found["items"] else "empty", returned=len(found["items"]))
        assert query["status"] == expected
        if index == 0:
            remaining = context(data, state, items)["candidates"]
            assert [item["arguments"]["terms"] for item in remaining] == [["membrane filtration"]]
    assert items == before
    assert calls == [["膜过滤"], ["membrane filtration"]]
    assert reader.requests_made == 2


def test_native_selector_executes_six_empty_alternatives_then_one_bounded_directory():
    project = os.environ.get("BIORADAR_QUERY_PLANNER_TEST_PROJECT_DIR")
    if not project or not (Path(project) / "src/integration/kdd.py").exists():
        pytest.skip("native v2 query-planner test directory is not configured")
    settings = SimpleNamespace(query_planner_project_dir=Path(project))
    data, state, items = fixed(), progress(), plans()
    state["query_policy"]["initial_plans"] = items
    requests = []

    def handler(request):
        requests.append(request.url.params.get_list("terms"))
        directory = not request.url.params.get_list("terms")
        return httpx.Response(200, json={
            "schema_version": "datafabric.history.collection.v1",
            "items": [original("papers")] if directory else [],
            "coverage": {}, "next_cursor": "SYNTHETIC-not-followed" if directory else None,
        })

    reader = FabricHistoryReader(
        httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler)),
        authorization_ref="SYNTHETIC",
    )
    for _ in range(6):
        request = extend_context(data, state, context(data, state, items))
        decision = decide_query(settings, request)
        assert decision["decision"] == "continue", decision
        action = decision["action"]
        assert not action["arguments"].get("catalog_discovery")
        assert len(request["history"]) == len(state["queries"])
        found = search_analysis_records(
            reader, source_id="papers", is_demo=False, terms=action["arguments"]["terms"],
            cutoff=datetime(2026, 10, 31, tzinfo=UTC), limit=8,
        )
        assert found["items"] == []
        save_empty(data, state, action)
    assert {tuple(item) for item in requests} == {tuple(item["terms"]) for item in items}
    assert reader.requests_made == 6
    assert len(state["evidence"]) == 0
    request = extend_context(data, state, context(data, state, items))
    decision = decide_query(settings, request)
    assert decision["decision"] == "continue", decision
    action = decision["action"]
    assert action["arguments"]["catalog_discovery"] is True
    found = search_analysis_records(
        reader, source_id=action["arguments"]["source_id"], is_demo=False, terms=[],
        cutoff=datetime.fromisoformat(action["arguments"]["as_of"]),
        mode=action["arguments"]["mode"], limit=action["arguments"]["limit"],
        catalog_discovery=True,
    )
    assert len(found["items"]) == 1 and found["coverage"]["complete"] is False
    assert found["coverage"]["has_more"] is True
    assert found["coverage"]["search_scope"] == "bounded_original_catalog_not_keyword_match"
    assert requests[-1] == [] and len(requests) == reader.requests_made == 7
    query = save_empty(data, state, action)
    query.update(status="read", returned=1, has_more=True, records=found["items"])
    remaining = context(data, state, items)["candidates"]
    assert remaining and all(item["kind"] == "document_read" for item in remaining)
    assert remaining[0]["arguments"]["records"] == [{
        "source_record_id": found["items"][0]["source_record_id"],
        "version_id": found["items"][0]["version_id"],
    }]


def test_directory_eligibility_requires_actual_empty_receipts_and_is_once_per_source():
    data, state, items = exhausted()
    checks = context(data, state, items)["checks"]
    candidate = catalog_discovery_candidates(data, state, items, checks)[0]
    args = candidate["arguments"]
    assert args["catalog_discovery"] is True and args["terms"] == [] and "query" not in args
    assert args["limit"] == 8
    assert args["as_of"] == data["evidence_time"]["cutoff"] == args["scope"]["as_of"]
    assert args["mode"] == args["scope"]["mode"] == "current"
    assert args["is_demo"] is args["scope"]["is_demo"] is False
    assert candidate["estimated_cost"]["tool_calls"] == 1

    for mutation in (
        {"status": "unconfirmed"}, {"status": "failed"},
        {"usage": {"request_status": "not_sent"}}, {"cost": {"tool_calls": None}},
        {"returned": 1},
    ):
        altered = copy.deepcopy(state)
        altered["queries"][-1].update(mutation)
        assert catalog_discovery_candidates(data, altered, items, checks) == []
    altered = copy.deepcopy(state)
    altered["query_policy"]["pending_action"] = candidate
    assert catalog_discovery_candidates(data, altered, items, checks) == []
    altered = copy.deepcopy(state)
    altered["evidence"] = [{"key": "SYNTHETIC-already-read"}]
    assert catalog_discovery_candidates(data, altered, items, checks) == []

    # Empty+known cost is an actual response even if the old Fabric receipt has no usage object.
    state["queries"][-1]["usage"] = None
    assert catalog_discovery_candidates(data, state, items, checks)

    # A second source still has an untried language candidate and cannot borrow these empty receipts.
    data["search_scope"].append("other")
    extra = plan("membrane filtration", source_id="other")
    only_first = catalog_discovery_candidates(data, state, items + [extra], checks)
    assert [item["arguments"]["source_id"] for item in only_first] == ["papers"]
    before = copy.deepcopy(state)
    save_empty(data, state, only_first[0])
    assert catalog_discovery_candidates(data, state, items + [extra], checks) == []
    data["search_scope"].remove("other")
    with pytest.raises(DomainError):
        catalog_discovery_candidates(data, before, items + [extra], checks)


def test_directory_identity_ignores_page_size_but_preserves_source_time_and_demo():
    data, state, items = exhausted()
    action = next(
        x for x in context(data, state, items)["candidates"]
        if x["arguments"].get("catalog_discovery")
    )
    args = action["arguments"]
    key = semantic_key("document_search", args)
    changed = copy.deepcopy(args)
    changed.update(limit=20, index=77, phase="followup", plan={"reason": "changed"})
    changed["scope"] = {"display_only": True}
    assert semantic_key("document_search", changed) == key
    for field, value in (
        ("source_id", "other"), ("as_of", "2026-10-30T00:00:00+00:00"),
        ("mode", "operational_replay"), ("is_demo", True),
    ):
        assert semantic_key("document_search", args | {field: value}) != key
    ordinary = args | {"catalog_discovery": False, "terms": ["membrane filtration"]}
    assert semantic_key("document_search", ordinary) != key
    with pytest.raises(DomainError):
        semantic_key("document_search", args | {"query": "invented placeholder"})
    with pytest.raises(DomainError):
        semantic_key("document_search", args | {"terms": ["invented placeholder"]})


def test_directory_never_turns_into_empty_query_live_fetch_or_old_baseline_candidate():
    data, state, items = exhausted()
    data["providers"] = {"fabric_fetch": {"available": True}}
    request = extend_context(data, state, context(data, state, items))
    assert any(x["arguments"].get("catalog_discovery") for x in request["candidates"])
    assert all(x["kind"] != "source_fetch" for x in request["candidates"])
    data["evidence_time"].update(
        mode="public_history_reconstruction", cutoff="2019-12-31T23:59:59+00:00",
    )
    historical, historical_state, historical_plans = copy.deepcopy(data), progress(), items
    for index in range(6):
        action = next(
            x for x in context(historical, historical_state, historical_plans)["candidates"]
            if x["arguments"]["index"] == index
        )
        save_empty(historical, historical_state, action)
    action = catalog_discovery_candidates(
        historical, historical_state, historical_plans,
        context(historical, historical_state, historical_plans)["checks"],
    )[0]
    assert action["arguments"]["as_of"] == "2019-12-31T23:59:59+00:00"
    assert action["arguments"]["mode"] == "public_history_reconstruction"
    data["query_policy"]["effective"] = "baseline"
    assert catalog_discovery_candidates(data, state, items, []) == []
    data["query_policy"].update(
        initial_retrieval_version=BOUNDED_INITIAL_VERSION, effective="baseline",
    )
    assert catalog_discovery_candidates(data, state, items, []) == []
