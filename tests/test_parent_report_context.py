"""Parent wording is comparison context, without changing previously frozen verifier input."""
import copy
import hashlib

from bioradar.research.investigations.analysis_service import AnalysisService


def test_parent_report_context_is_bound_and_only_added_before_first_verification():
    text = "SYNTHETIC parent draft: amounts from 2017 and 2020 were described together."
    fixed = {
        "query_context_version": "semantic-v2",
        "query_policy": {"effective": "query_planner", "schema_version": "query-planner.external.v2"},
        "parent_result": {"report": {"final_text": text, "status": "FAIL"}},
        "evidence": [],
    }
    progress = {"query_policy": {"checks": []}, "queries": [], "evidence": [], "calls": []}
    result = {"claims": [], "explanations": [], "gaps": [], "conditional_answer": {}, "update": {}}
    original = copy.deepcopy(fixed)
    first = AnalysisService._report_context(fixed, progress, result, freeze=True)
    prior = first["research_scope"]["prior_report"]
    assert prior["text"] == text and prior["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert prior["verification_status"] == "FAIL"
    assert "不是新的原始证据" in prior["qualification"]
    assert first.get("evidence") is None and fixed == original
    fixed["parent_result"]["report"]["final_text"] = "SYNTHETIC changed object must not affect frozen scope."
    restored = AnalysisService._report_context(fixed, progress, result)
    assert restored["research_scope"]["prior_report"] == prior
    legacy = {"query_policy": {"checks": []}, "queries": [], "evidence": [],
              "calls": [{"stage": "biocheck_plan"}]}
    prior_verifier = AnalysisService._report_context(original, legacy, result)
    assert "prior_report" not in prior_verifier.get("research_scope", {})
