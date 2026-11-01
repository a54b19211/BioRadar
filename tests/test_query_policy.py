"""Observable policy decisions, strict candidates and source versions; no paid model calls."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from bioradar.adapters.query_planner import (
    decide_query,
    load_query_planner,
    validate_decision,
)
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import freeze_payload
from bioradar.research.investigations.query_policy import (
    baseline_decide,
    build_policy_context,
    decide,
    freeze_policy,
    mechanism_query_context,
    semantic_key,
    validate_initial_plans,
)

pytestmark = pytest.mark.ENGINE


def fixed(policy="baseline"):
    return {
        "question": "SYNTHETIC pipeline for TARGET",
        "as_of": "2026-10-30T00:00:00+00:00",
        "search_scope": ["papers"],
        "budget": {"max_queries": 6},
        "query_policy": {"effective": policy, "max_steps": 12},
        "evidence_time": {"mode": "current"},
    }


def plan(*terms, followup=False, **extra):
    result = {"source_id": "papers", "terms": list(terms), "reason": "SYNTHETIC query"}
    if followup:
        result.update(
            gap_id="G1",
            target_claim_id="C1",
            anchoring_evidence_keys=["E1"],
            expected_discrimination="SYNTHETIC distinguish the evidence",
        )
    return result | extra


def evidence():
    return {
        "key": "E1",
        "source_id": "papers",
        "record_ref": {"adopted_version": "v1"},
        "title": "SYNTHETIC original",
        "passages": [{"id": "E1:p001", "text": "SYNTHETIC."}],
    }


def progress():
    return {"queries": [], "evidence": [], "calls": [], "query_policy": {"checks": [], "decisions": []}}


def receipt(action, status="empty", **extra):
    args = action["arguments"]
    return {
        "action_id": action["id"],
        "phase": args["phase"],
        "index": args["index"],
        "provider": args["provider"],
        "plan": copy.deepcopy(args["plan"]),
        "status": status,
        "evidence_keys": [],
        "new_versions": 0,
    } | extra


def context(data=None, state=None, plans=None, **extra):
    return build_policy_context(
        data or fixed(),
        state or progress(),
        phase=extra.pop("phase", "initial"),
        plans=plans if plans is not None else [plan("TARGET")],
        **extra,
    )


def test_context_is_pure_and_costs_are_observed_separately_from_query_count():
    data, state = fixed(), progress()
    state["query_policy"]["budget"] = {
        "limits": {"tool_calls": 64, "queries": 6},
        "remaining": {"tool_calls": 61, "queries": 5},
    }
    before = copy.deepcopy((data, state))
    request = context(data, state)
    assert (data, state) == before
    assert request["budget"] == {"limits": {"tool_calls": 64}, "remaining": {"tool_calls": 61}}
    assert request["host_budget"]["remaining"]["queries"] == 5
    decision = baseline_decide(request)
    assert decision["action"] == request["candidates"][0]
    assert decision["policy"]["legacy_sequence"] is True


def test_baseline_redecides_after_each_receipt_and_runs_originals_before_one_relaxation():
    data, state = fixed(), progress()
    plans = [plan("ABSENT", "TARGET"), plan("SECOND")]
    first = baseline_decide(context(data, state, plans))["action"]
    state["queries"].append(receipt(first))
    second = baseline_decide(context(data, state, plans))["action"]
    assert second["arguments"]["terms"] == ["SECOND"]
    state["queries"].append(receipt(second, "read"))
    relaxed = baseline_decide(context(data, state, plans))["action"]
    assert relaxed["arguments"]["terms"] == ["TARGET"]
    assert relaxed["arguments"]["index"] == 2
    assert relaxed["arguments"]["source_id"] == "papers"
    assert relaxed["arguments"]["relaxed_from"]["terms"] == ["ABSENT", "TARGET"]
    state["query_relaxation"] = relaxed["relaxation"]
    state["queries"].append(receipt(relaxed, "read"))
    followup = plan("ABSENT", "TARGET", followup=True)
    action = baseline_decide(context(data, state, [followup], phase="followup"))["action"]
    state["queries"].append(receipt(action))
    done = baseline_decide(context(data, state, [followup], phase="followup"))
    assert done["decision"] == "insufficient" and done["action"] is None
    assert len(state["queries"]) == 4


def test_read_without_new_versions_is_not_an_empty_search_and_does_not_relax():
    data, state = fixed(), progress()
    plans = [plan("ABSENT", "TARGET")]
    action = baseline_decide(context(data, state, plans))["action"]
    state["queries"].append(receipt(action, "read", new_versions=0))
    assert context(data, state, plans)["candidates"] == []


def test_legacy_relaxation_field_resumes_same_stable_index():
    data, state = fixed(), progress()
    plans = [plan("ABSENT", "TARGET")]
    action = baseline_decide(context(data, state, plans))["action"]
    state["queries"].append(receipt(action))
    relaxed = baseline_decide(context(data, state, plans))["action"]
    state["initial_relaxation"] = relaxed["relaxation"]
    resumed = baseline_decide(context(data, state, plans))["action"]
    assert resumed == relaxed


def test_followup_changed_plan_gets_new_index_and_same_plan_cannot_reuse_old_receipt():
    data, state = fixed(), progress()
    old = plan("OLD", followup=True)
    action = baseline_decide(context(data, state, [old], phase="followup"))["action"]
    state["queries"].append(receipt(action, "read"))
    changed = plan("NEW", followup=True)
    next_action = baseline_decide(context(data, state, [changed], phase="followup"))["action"]
    assert next_action["arguments"]["index"] == 1
    assert next_action["arguments"]["terms"] == ["NEW"]
    assert context(data, state, [old], phase="followup")["candidates"] == []


def test_semantic_identity_ignores_ids_phases_reasons_and_and_term_order():
    first = {
        "provider": "fabric",
        "source_id": "papers",
        "terms": [" TARGET ", "Drug"],
        "phase": "initial",
        "index": 0,
        "plan": {"reason": "first"},
        "as_of": "cutoff",
    }
    second = first | {
        "terms": ["drug", "target", "target"],
        "phase": "followup",
        "index": 5,
        "plan": {"reason": "a new reason"},
    }
    assert semantic_key("document_search", first) == semantic_key("document_search", second)
    assert semantic_key("document_search", first) != semantic_key(
        "document_search", second | {"provider": "weknora"}
    )
    assert semantic_key("document_search", first) != semantic_key(
        "document_search", second | {"as_of": "a different cutoff"}
    )


@pytest.mark.parametrize("status", ["prepared", "sent", "unconfirmed"])
def test_pending_receipt_blocks_a_new_phase_globally(status):
    data, state = fixed(), progress()
    action = baseline_decide(context(data, state))["action"]
    state["queries"].append(receipt(action, status))
    request = context(data, state, [plan("DIFFERENT", followup=True)], phase="followup")
    decision = baseline_decide(request)
    assert decision["decision"] == "insufficient"
    assert decision["reason"] == "pending_execution_or_reconciliation"
    assert decision["state_summary"]["pending_action_ids"] == [action["id"]]


def test_pending_adoption_blocks_selection_even_after_search_receipt_is_saved():
    data, state = fixed(), progress()
    action = baseline_decide(context(data, state))["action"]
    state["query_policy"]["pending_action"] = action
    state["queries"].append(receipt(action, "read"))
    assert context(data, state)["history"][0]["status"] == "prepared"
    assert baseline_decide(context(data, state))["action"] is None


def test_external_deduplicates_completed_cross_phase_while_baseline_preserves_legacy_comparison():
    data, state = fixed(), progress()
    initial = baseline_decide(context(data, state))["action"]
    state["queries"].append(receipt(initial, "read"))
    same = [plan("TARGET", followup=True)]
    assert context(data, state, same, phase="followup")["candidates"]
    data["query_policy"]["effective"] = "query_planner"
    assert context(data, state, same, phase="followup")["candidates"] == []


def test_external_merges_unexecuted_initial_plans_with_current_semantic_gap_plan():
    data, state = fixed("query_planner"), progress()
    state["query_policy"]["initial_plans"] = [plan("INITIAL")]
    request = context(data, state, [plan("NEW", followup=True)], phase="followup")
    assert {item["arguments"]["phase"] for item in request["candidates"]} == {"initial", "followup"}


def test_pipeline_coverage_requires_current_evidence_and_never_follows_covers_or_empty():
    data, state = fixed(), progress()
    state["evidence"] = [evidence()]
    request = context(data, state)
    checks = {item["id"]: item for item in request["checks"]}
    assert checks["source_materials"]["status"] == "passed"
    for key in ("support", "counterevidence", "competition", "analogous_failure", "technology", "patent"):
        assert checks[key]["status"] == "unknown" and checks[key]["evidence_ids"] == []
    state["query_policy"]["checks"] = request["checks"]
    reading = {
        "coverage_checks": [
            {"id": "support", "status": "passed", "evidence_ids": ["E1"], "reason": "SYNTHETIC original"},
            {
                "id": "patent",
                "status": "passed",
                "evidence_ids": ["FUTURE"],
                "reason": "invalid future reference",
            },
        ]
    }
    checks = {item["id"]: item for item in context(data, state, reading=reading)["checks"]}
    assert checks["support"]["status"] == "passed"
    assert checks["patent"]["status"] == "unknown" and checks["patent"]["evidence_ids"] == []
    # The existing baseline ignores undeclared coverage decoration and continues;
    # it must not manufacture a successful scientific check from that decoration.
    unknown = context(data, state, reading={"coverage_checks": [{"id": "invented", "status": "passed"}]})
    by_id = {item["id"]: item for item in unknown["checks"]}
    assert "invented" not in by_id
    assert by_id["support"]["status"] == "unknown"
    assert by_id["source_materials"]["status"] == "passed"


def test_mechanism_gaps_and_opposing_observations_become_real_declared_query_coverage():
    data, state = fixed("query_planner"), progress()
    state["evidence"] = [evidence()]
    mechanism = {
        "status": "completed",
        "conflicts": [],
        "critical_gaps": [
            {"reason": "SYNTHETIC causal link untested", "missing_experiment": "TARGET inhibition"}
        ],
        "claims": [{"id": "MC1", "opposing_observation_ids": ["O2"]}],
    }
    state["mechanism_evaluation"] = mechanism
    initial = context(data, state)
    mech_ids = [item["id"] for item in initial["checks"] if item["id"].startswith("mech:")]
    assert len(mech_ids) == 2 and "mech:opposing:MC1" in mech_ids
    assert initial["mechanism_context"]["opposing_observations"][0]["opposing_observation_ids"] == ["O2"]
    reading = {
        "gaps": [{"id": "G1", "question": "SYNTHETIC causal experiment", "why_it_matters": "distinguish"}],
        "next_queries": [],
    }
    query = plan("TARGET", "inhibition", followup=True, coverage_check_ids=mech_ids)
    request = context(data, state, [query], phase="followup", reading=reading)
    assert set(mech_ids).issubset(request["candidates"][0]["covers"])
    payload = freeze_payload(
        "read_plan",
        data,
        state["evidence"],
        query_context={
            "coverage_checks": request["checks"],
            "query_history": request["history"],
            "mechanism_evaluation": mechanism,
        },
    )
    assert payload["mechanism_query_context"]["critical_gaps"][0]["missing_experiment"] == "TARGET inhibition"
    assert payload["mechanism_query_context"]["opposing_observations"]
    assert "空conflicts不代表无反证" in payload["coverage_instruction"]
    assert payload["coverage_checks"] == request["checks"]


@pytest.mark.parametrize("mode", ["operational_replay", "public_history_reconstruction"])
def test_historical_mode_never_generates_current_weknora_candidate(mode):
    data, state = fixed("query_planner"), progress()
    data["evidence_time"]["mode"] = mode
    request = context(data, state, provider_status={"weknora": {"available": True}})
    assert all(item["arguments"]["provider"] == "fabric" for item in request["candidates"])


def test_weknora_is_one_explicit_query_with_unknown_token_cost_and_only_once():
    data, state = fixed(), progress()
    action = baseline_decide(context(data, state))["action"]
    state["queries"].append(receipt(action, "read"))
    request = context(data, state, provider_status={"weknora": {"available": True}})
    weknora = baseline_decide(request)["action"]
    assert weknora["arguments"]["provider"] == "weknora"
    assert weknora["estimated_cost"]["tokens"] is None
    state["queries"].append(receipt(weknora, "unconfirmed"))
    request = context(data, state, provider_status={"weknora": {"available": True}})
    assert request["history"][-1]["actual_cost"]["tokens"] is None
    assert baseline_decide(request)["action"] is None


def test_unknown_limited_budget_is_not_zero_or_unlimited():
    state = progress()
    state["query_policy"]["budget"] = {
        "limits": {"tool_calls": 64},
        "remaining": {"tool_calls": None},
    }
    with pytest.raises(DomainError, match="预算尚未确认"):
        context(state=state)


def test_no_graph_is_fabricated_and_only_explicit_graph_kinds_are_supported():
    data = fixed("query_planner")
    request = context(data)
    assert all(item["kind"] == "document_search" for item in request["candidates"])
    graph = {
        "id": "fixed-graph",
        "kind": "graph_neighbors",
        "arguments": {"binding": {"version_id": "G1"}, "seed_ids": ["NODE1"]},
        "covers": ["technology"],
        "estimated_cost": {"tool_calls": 1, "relation_access": 3},
    }
    request = context(data, graph_candidates=[graph])
    assert graph in request["candidates"]
    graph["kind"] = "graph_expand"
    with pytest.raises(DomainError, match="固定图工具"):
        context(data, graph_candidates=[graph])


def write_planner(path):
    (path / "src").mkdir()
    (path / "src" / "method.py").write_text("METHOD = 'SYNTHETIC no network'\n")
    (path / "radar_policy.py").write_text(
        "import hashlib\nfrom pathlib import Path\n"
        "root=Path(__file__).parent/'src'\n"
        "POLICY_VERSION=hashlib.sha256(Path(__file__).read_bytes()+b''.join("
        "str(p.relative_to(root)).encode()+b'\\0'+p.read_bytes() "
        "for p in sorted(root.rglob('*.py')))).hexdigest()\n"
        "SCHEMA_VERSION='query-planner.external.v1'\n"
        "def decide(request):\n"
        " return {'schema_version':SCHEMA_VERSION,'decision':'continue',"
        "'action':request['candidates'][0],'reason':'SYNTHETIC method',"
        "'policy':{'version':POLICY_VERSION,'selector':'heuristic','stop':'rule',"
        "'training_status':'heuristic_untrained'},'state_summary':{}}\n"
    )
    return SimpleNamespace(
        query_planner_project_dir=path,
        analysis_query_policy="query_planner",
        analysis_query_policy_max_steps=12,
    )


def test_controlled_adapter_freezes_actual_source_and_preserves_candidate(tmp_path):
    settings = write_planner(tmp_path)
    before = list(sys.path)
    loaded = load_query_planner(settings)
    assert sys.path == before
    frozen = freeze_policy(settings)
    assert frozen["version"] == loaded[1]
    assert frozen["training_status"] == "heuristic_untrained"
    request = context(fixed("query_planner"))
    result = decide(request, policy="query_planner", planner=lambda req: decide_query(settings, req))
    assert result["action"] == request["candidates"][0]
    (tmp_path / "src/method.py").write_text("METHOD = 'SYNTHETIC changed'\n")
    with pytest.raises(DomainError, match="固定版本"):
        decide_query(settings, request, expected_version=frozen["version"])
    with pytest.raises(DomainError, match="源码已变化"):
        decide_query(settings, request, loaded=loaded)


def test_missing_planner_does_not_silently_fall_back():
    settings = SimpleNamespace(query_planner_project_dir=None, analysis_query_policy="query_planner")
    with pytest.raises(DomainError, match="未配置"):
        freeze_policy(settings)
    with pytest.raises(DomainError, match="尚不可用"):
        decide(context(), policy="query_planner")


def result_for(request):
    return {
        "schema_version": request["schema_version"],
        "decision": "continue",
        "action": copy.deepcopy(request["candidates"][0]),
        "reason": "SYNTHETIC selection",
        "policy": {
            "version": "fixed-source",
            "selector": "heuristic",
            "stop": "rule",
            "training_status": "heuristic_untrained",
        },
        "state_summary": {},
    }


def test_adapter_rejects_new_arguments_even_under_an_existing_candidate_id():
    request = context(fixed("query_planner"))
    result = result_for(request)
    result["action"]["arguments"]["source_id"] = "unauthorized"
    with pytest.raises(DomainError, match="候选以外"):
        validate_decision(request, result)


def test_adapter_rejects_retry_hidden_by_new_phase_or_action_id():
    data, state = fixed("query_planner"), progress()
    request = context(data, state)
    result = result_for(request)
    old = copy.deepcopy(result["action"]["arguments"])
    old.update(phase="an-old-stage", index=8, plan={"reason": "different"})
    request["history"] = [
        {
            "action_id": "old-id",
            "kind": "document_search",
            "arguments": old,
            "status": "completed",
            "new_evidence_ids": [],
        }
    ]
    with pytest.raises(DomainError, match="同义动作"):
        validate_decision(request, result)


def test_adapter_rejects_unknown_cost_in_limited_dimension_and_unearned_complete():
    request = context(fixed("query_planner"))
    request["budget"]["limits"]["tokens"] = 100
    request["budget"]["remaining"]["tokens"] = 100
    request["candidates"][0]["estimated_cost"]["tokens"] = None
    with pytest.raises(DomainError, match="费用未知"):
        validate_decision(request, result_for(request))
    result = result_for(request)
    result.update(decision="complete", action=None)
    with pytest.raises(DomainError, match="覆盖检查"):
        validate_decision(request, result)


def test_explicit_initial_queries_keep_scope_and_model_plan_limit():
    data = fixed()
    assert validate_initial_plans([plan("TARGET")], data)[0]["terms"] == ["TARGET"]
    with pytest.raises(DomainError, match="授权来源"):
        validate_initial_plans([plan("TARGET", source_id="unauthorized")], data)
    with pytest.raises(DomainError, match="固定查询预算"):
        validate_initial_plans({"queries": [plan("TARGET")] * 3}, data)


def test_ordinary_question_requires_source_bound_answer_scope_even_when_model_lists_no_gaps():
    data, state = fixed("query_planner"), progress()
    data["question"] = "SYNTHETIC: did this record report a termination reason?"
    state["evidence"] = [evidence()]
    request = context(data, state, plans=[], reading={"gaps": [], "coverage_checks": []})
    checks = {item["id"]: item for item in request["checks"]}
    assert checks["source_materials"]["status"] == "passed"
    assert checks["answer_scope"]["status"] == "unknown"
    assert baseline_decide(request)["decision"] == "insufficient"
    result = {
        "schema_version": request["schema_version"],
        "decision": "complete",
        "action": None,
        "reason": "SYNTHETIC completion",
        "policy": {
            "version": "source",
            "selector": "heuristic",
            "stop": "rule",
            "training_status": "heuristic_untrained",
        },
        "state_summary": {},
    }
    with pytest.raises(DomainError, match="覆盖检查"):
        validate_decision(request, result)
    reading = {
        "coverage_checks": [
            {
                "id": "answer_scope",
                "status": "passed",
                "evidence_ids": ["E1"],
                "reason": "SYNTHETIC original answers this limited question",
            }
        ]
    }
    request = context(data, state, plans=[], reading=reading)
    assert validate_decision(request, result)["decision"] == "complete"
    assert all(item["status"] == "passed" for item in request["checks"])


@pytest.mark.parametrize("defect", ["nan", "missing_action", "bool_cost"])
def test_adapter_requires_exact_finite_json_and_explicit_action(defect):
    request = context(fixed("query_planner"))
    result = result_for(request)
    if defect == "nan":
        result["action"]["arguments"]["threshold"] = float("nan")
    elif defect == "missing_action":
        result.pop("action")
    else:
        result["action"]["estimated_cost"]["tool_calls"] = True
    with pytest.raises(DomainError):
        validate_decision(request, result)


def test_real_q020_feedback_keeps_dereferenceable_claims_joins_opposing_evidence_and_versions():
    snapshot = json.loads((Path(__file__).parent / "fixtures/mechevidence_q020_feedback.json").read_text())
    evaluation = snapshot["evaluation"]
    result = mechanism_query_context(evaluation)
    assert result["scope"] == evaluation["scope"]
    assert result["conflicts"] == []
    claims = {claim["id"]: claim for claim in result["claims"]}
    joins = {join["id"]: join for join in result["joins"]}
    observations = {item["id"]: item for item in result["evidence"]}
    assert claims["C2"]["opposing_observation_ids"] == ["L2"]
    assert "No elimination of nonactivated T cells" in claims["C2"]["text"]
    assert observations["L2"]["relation"] == "opposes"
    reference = observations["L2"]["references"][0]
    assert reference["material_id"] == "M1" and len(reference["version"]) == 64
    assert set(reference) == {"id", "material_id", "version"}
    assert "text" not in reference
    for gap in result["critical_gaps"]:
        assert set(gap["claim_ids"]).issubset(claims)
        assert set(gap["join_ids"]).issubset(joins)
    for claim in claims.values():
        assert set(claim["opposing_observation_ids"]).issubset(observations)
    assert "unresolved_counterevidence" in result
    # Synthetic mutation of the real snapshot tests the same field on joins.
    changed = copy.deepcopy(evaluation)
    changed["joins"][0]["opposing_observation_ids"] = ["L2"]
    changed_result = mechanism_query_context(changed)
    assert any(
        item.get("join_id") == "J1" and item["opposing_observation_ids"] == ["L2"]
        for item in changed_result["opposing_observations"]
    )


def test_quality_gaps_and_fixed_graph_background_reach_planning_without_passing_checks():
    data, state = fixed("query_planner"), progress()
    state["evidence"] = [evidence()]
    state["quality_feedback"] = {
        "machine_status": "PASS",
        "repair_units": [],
        "review_required": [],
        "evidence_gaps": [
            {
                "kind": "evidence_selection",
                "unit_id": "U1",
                "claim_index": 0,
                "span": "SYNTHETIC claim",
                "reason": "SYNTHETIC selected passage is insufficient",
            }
        ],
    }
    state["query_policy"]["graph_context"] = [{"qualification": "background only", "version": "fixed-G"}]
    state["query_policy"]["graph_history"] = [{"status": "resolved", "entity_id": "grounded-1"}]
    request = context(data, state)
    gaps = [item for item in request["checks"] if item["id"].startswith("quality:")]
    assert len(gaps) == 1 and gaps[0]["status"] == "unknown"
    reading = {"gaps": [{"id": "G1", "question": "SYNTHETIC missing support", "why_it_matters": "answer"}]}
    query = plan("TARGET", followup=True, coverage_check_ids=[gaps[0]["id"]])
    request = context(data, state, [query], phase="followup", reading=reading)
    assert gaps[0]["id"] in request["candidates"][0]["covers"]
    payload = freeze_payload(
        "read_plan",
        data,
        state["evidence"],
        query_context={
            "quality_feedback": request["quality_feedback"],
            "graph_context": request["graph_context"],
            "graph_history": request["graph_history"],
            "coverage_checks": request["checks"],
        },
    )
    assert payload["quality_feedback"]["evidence_gaps"] == state["quality_feedback"]["evidence_gaps"]
    assert payload["graph_context"] and payload["graph_history"]
    assert "不是临床或机制支持证据" in payload["graph_context_qualification"]
    data["quality_feedback"] = state["quality_feedback"]
    assert (
        freeze_payload("synthesize", data, state["evidence"])["quality_feedback"] == state["quality_feedback"]
    )


def test_baseline_preserves_old_query_bound_instead_of_external_step_limit():
    request = context()
    request["max_steps"] = 1
    request["history"] = [{"action_id": "completed", "status": "completed"}]
    assert baseline_decide(request)["decision"] == "continue"


@pytest.mark.parametrize("extra", ["", None, "a meaningful qualifier"])
def test_policy_reading_keeps_raw_and_only_ignores_empty_extra(extra):
    from bioradar.research.investigations.analysis import parse_policy_reading

    raw = {
        "claims": [
            {
                "id": "C1",
                "kind": "source_statement",
                "text": "SYNTHETIC bounded statement.",
                "scope": "SYNTHETIC",
                "status": "supported",
                "conditions": [],
                "evidence_links": [
                    {
                        "evidence_key": "E1",
                        "passage_id": "E1:p001",
                        "role": "supports",
                        "reason": "SYNTHETIC original text.",
                        "unused_annotation": extra,
                    }
                ],
            }
        ],
        "explanations": [],
        "gaps": [],
        "next_queries": [],
        "coverage_checks": [],
        "conditional_answer": {"answer": "SYNTHETIC.", "conditions": [], "unknowns": []},
    }
    if extra:
        with pytest.raises(DomainError):
            parse_policy_reading(raw)
    else:
        normalized, changes = parse_policy_reading(raw)
        assert "unused_annotation" not in normalized["claims"][0]["evidence_links"][0]
        assert changes == [
            {
                "path": ["claims", 0, "evidence_links", 0, "unused_annotation"],
                "ignored_value": extra,
                "reason": "empty_undeclared_field",
            }
        ]
    assert raw["claims"][0]["evidence_links"][0]["unused_annotation"] == extra
    del raw["claims"][0]["evidence_links"][0]["reason"]
    with pytest.raises(DomainError):
        parse_policy_reading(raw)


def test_external_gap_namespace_is_idempotent_across_rounds_without_merging_different_questions():
    data, state = fixed("query_planner"), progress()
    question = "Are the original results available?"
    for identifier in ("g_results", "gap:g_results", "gap:gap:g_results"):
        if identifier == "gap:gap:g_results":
            duplicate = copy.deepcopy(
                next(item for item in state["query_policy"]["checks"] if item["id"] == "gap:g_results")
            )
            duplicate["id"] = "gap:gap:g_results"
            state["query_policy"]["checks"].append(duplicate)
        reading = {
            "gaps": [{"id": identifier, "question": question, "why_it_matters": "Original results matter."}]
        }
        before = copy.deepcopy((state, reading))
        request = context(
            data, state, [plan("TARGET", followup=True, gap_id=identifier)], phase="followup", reading=reading
        )
        assert (state, reading) == before
        gaps = [item for item in request["checks"] if item["id"].startswith("gap:")]
        assert [(item["id"], item["question"]) for item in gaps] == [("gap:g_results", question)]
        assert "gap:g_results" in request["candidates"][0]["covers"]
        state["query_policy"]["checks"] = request["checks"]
    different = "What were the distinct safety findings?"
    distinct_id = None
    for identifier in ("gap:g_results", "gap:g_results"):
        reading = {"gaps": [{"id": identifier, "question": different}]}
        request = context(
            data, state, [plan("TARGET", followup=True, gap_id=identifier)], phase="followup", reading=reading
        )
        gaps = [item for item in request["checks"] if item["id"].startswith("gap:")]
        assert len(gaps) == 2
        assert {item["question"] for item in gaps} == {question, different}
        other = next(item["id"] for item in gaps if item["question"] == different)
        assert other != "gap:g_results"
        assert distinct_id is None or other == distinct_id
        distinct_id = other
        assert other in request["candidates"][0]["covers"]
        state["query_policy"]["checks"] = request["checks"]
    long_identifier = "g" * 80
    long_keys = []
    for identifier, long_question in (
        (long_identifier, "First distinct long-ID question"),
        (long_identifier, "Second distinct long-ID question"),
        (long_identifier, "Second distinct long-ID question"),
    ):
        reading = {"gaps": [{"id": identifier, "question": long_question}]}
        request = context(
            data,
            state,
            [plan("TARGET", followup=True, gap_id=identifier)],
            phase="followup",
            reading=reading,
        )
        key = next(item["id"] for item in request["checks"] if item["question"] == long_question)
        assert len(key) <= 80 and key in request["candidates"][0]["covers"]
        long_keys.append(key)
        state["query_policy"]["checks"] = request["checks"]
    assert long_keys[0] != long_keys[1] == long_keys[2]
    echoed = context(
        data,
        state,
        [plan("TARGET", followup=True, gap_id=long_keys[1])],
        phase="followup",
        reading={"gaps": [{"id": long_keys[1], "question": "Second distinct long-ID question"}]},
    )
    assert echoed["checks"] == state["query_policy"]["checks"]
    assert long_keys[1] in echoed["candidates"][0]["covers"]
    legacy = context(
        fixed("baseline"), progress(), reading={"gaps": [{"id": "gap:g_results", "question": question}]}
    )
    assert any(item["id"] == "gap:gap:g_results" for item in legacy["checks"])
