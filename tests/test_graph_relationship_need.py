"""Explicit question intent may discover graph background; every name/text is synthetic."""

import copy

import pytest
from bioradar.adapters.fixed_graph import question_relationship_need
from bioradar.adapters.query_graph import build_graph_candidates
from test_graph_package_discovery import execute, setup_graph

pytestmark = pytest.mark.BOOT


def question_input(question):
    settings, fixed, progress, reader, calls, catalogs = setup_graph()
    fixed["question"] = question
    fixed["query_policy"].update(
        effective="query_planner",
        schema_version="query-planner.external.v2",
        direction="research",
        discovery_version="bounded-required-gap-discovery-v1",
    )
    name = "SYNTHETIC B"
    start = question.index(name)
    objects = [{"id": "b", "name": name, "question_span": {"start": start, "end": start + len(name)}}]
    progress["query_policy"]["named_objects"] = copy.deepcopy(objects)
    return settings, fixed, progress, reader, calls, catalogs, objects


@pytest.mark.parametrize(
    "question",
    [
        "SYNTHETIC B的作用机制是什么？",
        "What is the binding relationship of SYNTHETIC B?",
    ],
)
def test_single_named_relationship_declares_a_need_and_a_bounded_graph_candidate(question):
    settings, fixed, progress, reader, calls, catalogs, objects = question_input(question)
    before = copy.deepcopy((fixed, progress, objects))
    need = question_relationship_need(fixed, objects)
    assert need and need["id"].startswith("question_relation:")
    assert "不能当作实验支持" in need["question"]
    assert (fixed, progress, objects) == before
    checks = [{**need, "status": "unknown"}]
    actions = build_graph_candidates(settings, fixed, progress, None, checks=checks)
    assert [c["arguments"]["operation"] for c in actions] == ["catalog"]
    assert actions[0]["covers"] == [need["id"]]
    assert not calls and not catalogs
    output = execute(settings, reader, actions[0])
    assert output["status"] == "read" and output["actual_cost"]["tool_calls"] == 1
    assert output["records"] == [] and not progress.get("evidence")
    assert checks[0]["status"] == "unknown"
    assert "不自动确认" in output["usage"]["qualification"]
    assert not build_graph_candidates(settings, fixed, progress, None, checks=[{**need, "status": "passed"}])
    assert not build_graph_candidates(settings, {**fixed, "graph_scope": []}, progress, None, checks=checks)
    settings.engine_source_id_allowlist = {"fixture": []}
    assert not build_graph_candidates(settings, fixed, progress, None, checks=checks)
    reader.client.close()


@pytest.mark.parametrize(
    "question",
    [
        "SYNTHETIC B药物的安全性和适用人群有哪些证据？",
        "Which populations and safety outcomes are reported for SYNTHETIC B?",
        "SYNTHETIC B靶点材料的来源类型是否明确？",
        "SYNTHETIC B的机制已有背景说明，但本题不查该关系。",
        "SYNTHETIC B：不研究其作用机制，只核对安全终点。",
        "Do not investigate the mechanism of SYNTHETIC B; check safety outcomes only.",
        "SYNTHETIC B的机制有背景报道，本题仅核对材料来源类型。",
        "Background reports SYNTHETIC B binding; evaluate only the sample sources.",
    ],
)
def test_a_name_drug_or_mechanism_background_does_not_override_question_scope(question):
    _, fixed, _, reader, _, _, objects = question_input(question)
    assert question_relationship_need(fixed, objects) is None
    reader.client.close()


@pytest.mark.parametrize(
    "question",
    [
        "Does SYNTHETIC B not bind its reported partner?",
        "能否排除SYNTHETIC B与其因子的相互作用？",
    ],
)
def test_negative_relationship_hypothesis_is_not_a_request_to_skip_it(question):
    _, fixed, _, reader, _, _, objects = question_input(question)
    assert question_relationship_need(fixed, objects) is not None
    reader.client.close()


def test_legacy_baseline_other_directions_and_missing_named_objects_keep_old_behavior():
    settings, fixed, progress, reader, _, _, objects = question_input("SYNTHETIC B的关系如何查证？")
    legacy = copy.deepcopy(fixed)
    legacy["query_policy"].pop("discovery_version")
    legacy_check = [{"id": "technology", "status": "unknown"}]
    old_actions = build_graph_candidates(settings, legacy, progress, None, checks=legacy_check)
    assert question_relationship_need(legacy, objects) is None
    assert build_graph_candidates(settings, legacy, progress, None, checks=legacy_check) == old_actions
    for field, value in (("effective", "baseline"), ("direction", "business")):
        changed = copy.deepcopy(fixed)
        changed["query_policy"][field] = value
        assert question_relationship_need(changed, objects) is None
    assert question_relationship_need(fixed, []) is None
    reader.client.close()
