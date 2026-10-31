import copy
import hashlib

import pytest
from bioradar.delivery.research_report import build_report_delivery, render_report_markdown

from scripts.report_delivery_assertions import expected_delivery_markdown


def _view():
    body = "# 原稿\n\n数值🧬保持不变。[qabc123] 后文 [record:S7] 与 [qmissing]。\n"
    reference = {"source_id": "who_don", "source_record_id": "notice-1", "adopted_version": "fixed-v1"}
    return {
        "id": "analysis-1",
        "investigation_id": "investigation-1",
        "question": "固定问题",
        "result": {
            "report": {
                "final_text": body,
                "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "status": "FAIL",
                "verification": {"draft": body, "status": "FAIL"},
                "evidence_map": {"S7": {"key": "qabc123", "record_ref": reference}},
            }
        },
        "evidence": [
            {
                "key": "qabc123",
                "source_id": "who_don",
                "title": "原文 [标题]\n第二行",
                "publication_time": "2022-07-10",
                "record_ref": reference,
            }
        ],
        "progress": {},
    }


def test_delivery_keeps_exact_failed_body_and_separate_fixed_source_index():
    view = _view()
    original = copy.deepcopy(view)
    delivery = build_report_delivery(view)
    markdown = render_report_markdown(view)
    body = view["result"]["report"]["final_text"]
    assert view == original
    assert delivery["verification_status"] == "FAIL"
    assert delivery["verification_text_matches"] is True
    assert delivery["saved_hash_matches"] is True
    assert [item.get("evidence_key") for item in delivery["source_index"]] == ["qabc123", "qabc123", None]
    assert delivery["unbound_citations"] == ["[qmissing]"]
    start, end = delivery["body_range"]["start"], delivery["body_range"]["end"]
    assert markdown[start:end] == body
    assert hashlib.sha256(markdown[start:end].encode()).hexdigest() == delivery["body_text_sha256"]
    assert markdown == delivery["machine_verification_prefix"] + body + delivery["source_index_markdown"]
    assert "所选支持片段复核：未记录，不推定已执行" in delivery["machine_verification_prefix"]
    assert expected_delivery_markdown(view, delivery, body) == markdown
    forged = copy.deepcopy(delivery)
    forged["source_index"][0]["publication_time"] = "2099-01-01"
    with pytest.raises(AssertionError):
        expected_delivery_markdown(view, forged, body)
    assert "来源索引（系统补充，未纳入正文核验）" in markdown[end:]
    assert "原文 \\[标题\\] 第二行" in markdown[end:]
    assert "/api/engine/investigations/investigation-1/analyses/analysis-1/evidence/qabc123" in markdown[end:]


def test_delivery_does_not_guess_conflicting_or_unversioned_citations():
    view = _view()
    view["result"]["report"]["evidence_map"]["qabc123"] = {
        "key": "qabc123",
        "record_ref": {"adopted_version": "different"},
    }
    rows = build_report_delivery(view)["source_index"]
    assert rows[0]["binding_status"] == "unbound"
    assert rows[1]["binding_status"] == "bound"
    report = view["result"]["report"]
    report["evidence_map"]["S7"] = {"key": "qabc123", "version": "different-fixed-version"}
    delivery = build_report_delivery(view)
    assert delivery["source_index"][1]["binding_status"] == "unbound"
    expected_delivery_markdown(view, delivery, report["final_text"])
    report["evidence_map"]["S7"]["version"] = "fixed-v1"
    assert build_report_delivery(view)["source_index"][1]["binding_status"] == "bound"
    report["evidence_map"]["S7"]["record_ref"] = {"adopted_version": "different"}
    assert build_report_delivery(view)["source_index"][1]["binding_status"] == "unbound"
    view["evidence"][0]["record_ref"].pop("adopted_version")
    assert all(item["binding_status"] == "unbound" for item in build_report_delivery(view)["source_index"])


def test_scope_prefix_and_system_index_do_not_change_verification_or_body_hash():
    view = _view()
    view["progress"]["material_scope"] = {
        "fixed_version_count": 2,
        "read_version_count": 1,
        "unread_versions": [{"source_id": "who_don", "source_record_id": "notice-2", "version_id": "v2"}],
        "source_failures": [],
    }
    view["result"]["report"]["verification"]["draft"] += " changed"
    view["result"]["report"]["text_sha256"] = "incorrect-saved-hash"
    delivery = build_report_delivery(view)
    assert delivery["saved_hash_matches"] is False
    assert delivery["verification_text_matches"] is False
    assert delivery["body_range"]["start"] > 0
    markdown = render_report_markdown(view)
    assert (
        markdown
        == delivery["scope_prefix"]
        + delivery["machine_verification_prefix"]
        + view["result"]["report"]["final_text"]
        + delivery["source_index_markdown"]
    )


def test_structured_fallback_is_explicitly_not_a_verified_professional_report():
    view = _view()
    view["result"] = {
        "conditional_answer": {"answer": "保存的结构稿", "conditions": [], "unknowns": []},
        "claims": [],
        "gaps": [],
        "explanations": [],
        "update": {"reasons": []},
    }
    delivery = build_report_delivery(view)
    assert delivery["body_kind"] == "structured_draft"
    assert delivery["verification_status"] is None
    assert delivery["verification_text_matches"] is None
    assert delivery["saved_hash_matches"] is None
    assert "保存的结构稿" in render_report_markdown(view)


def test_legacy_unbound_assessment_is_only_a_system_prefix_and_body_remains_exact():
    view = _view()
    view["result"].update(
        claims=[{"id": "current-claim", "text": "SYNTHETIC current claim"}],
        warning_assessment={"decision": "watch", "supporting_claim_ids": ["OLD"], "opposing_claim_ids": []},
    )
    original = copy.deepcopy(view)
    body = view["result"]["report"]["final_text"]
    delivery = build_report_delivery(view)
    markdown = render_report_markdown(view)
    assert delivery["assessment_binding"]["status"] == "unbound"
    assert delivery["assessment_prefix"].startswith("> 系统记录：")
    start, end = delivery["body_range"]["start"], delivery["body_range"]["end"]
    assert markdown[start:end] == body
    assert hashlib.sha256(markdown[start:end].encode()).hexdigest() == view["result"]["report"]["text_sha256"]
    assert expected_delivery_markdown(view, delivery, body) == markdown
    assert delivery["verification_status"] == "FAIL" and delivery["verification_text_matches"] is True
    assert view == original
    forged = copy.deepcopy(delivery)
    forged["assessment_prefix"] = "PASS\n"
    with pytest.raises(AssertionError):
        expected_delivery_markdown(view, forged, body)


@pytest.mark.parametrize("status", ["impact_pending", "affects_dependency", "equivalent_inputs"])
def test_material_update_currentness_stays_outside_saved_body_and_preserves_old_failure(status):
    view = _view()
    view["progress"]["material_update_impact"] = {
        "version": "dependency-aware-material-update-v1",
        "status": status,
        "parent_analysis_id": "SYNTHETIC-earlier-completed-analysis",
        "checked_revision": 4,
        "report_as_of": "2025-01-01T00:00:00+00:00",
        "report_currentness": "prior_fixed_materials",
        "decisions": [],
    }
    original = copy.deepcopy(view)
    body = view["result"]["report"]["final_text"]
    delivery = build_report_delivery(view)
    markdown = render_report_markdown(view)
    bounds = delivery["body_range"]
    assert markdown[bounds["start"] : bounds["end"]] == body
    assert delivery["body_text_sha256"] == view["result"]["report"]["text_sha256"]
    assert delivery["verification_status"] == "FAIL" and delivery["verification_text_matches"]
    assert "本稿仍绑定原固定材料" in delivery["material_update_prefix"]
    assert "影响比较依据报告：SYNTHETIC-earlier-completed-analysis" in delivery["material_update_prefix"]
    assert "2025-01-01T00:00:00+00:00" in delivery["material_update_prefix"]
    assert expected_delivery_markdown(view, delivery, body) == markdown
    assert view == original
    altered = copy.deepcopy(delivery)
    altered["material_update_prefix"] = "已根据最新材料完成核验。"
    with pytest.raises(AssertionError):
        expected_delivery_markdown(view, altered, body)


def test_early_reading_export_lists_unincorporated_professional_result_without_rewriting_the_body():
    view = _view()
    view["query_policy"] = {
        "synthesis_reading_basis": {
            "coverage_version": "retained-reading-coverage-v1",
            "cannot_use_as_current_conclusion": True,
            "call_key": "SYNTHETIC-reading-1",
            "after_query_index": 0,
            "input_evidence": [{"evidence_key": "qabc123"}],
            "input_artifact_refs": [],
            "not_incorporated": {
                "evidence": [
                    {
                        "evidence_key": "SYNTHETIC-new",
                        "source_id": "SYNTHETIC-feed",
                        "record_ref": {"object_id": "SYNTHETIC-original", "adopted_version": "v2"},
                    }
                ],
                "artifacts": [
                    {"id": "professional:SYNTHETIC", "version": "actual-fixed-version", "status": "partial"}
                ],
            },
        }
    }
    original = copy.deepcopy(view)
    body = view["result"]["report"]["final_text"]
    delivery = build_report_delivery(view)
    markdown = render_report_markdown(view)
    prefix = delivery["reading_coverage_prefix"]
    assert "尚未纳入后续结果，不能作为当前结论" in prefix
    assert "SYNTHETIC-reading-1" in prefix and "SYNTHETIC-new" in prefix
    assert "原查询边界：0" in prefix
    assert "来源：SYNTHETIC-feed" in prefix and "原件ID：SYNTHETIC-original" in prefix
    assert "份材料选段" in prefix
    assert "原状态：partial" in prefix and "actual-fixed-version" in prefix
    assert "/api/engine/investigations/investigation-1/analyses/analysis-1/export" in prefix
    bounds = delivery["body_range"]
    assert markdown[bounds["start"] : bounds["end"]] == body
    assert delivery["verification_status"] == "FAIL" and delivery["verification_text_matches"]
    assert delivery["saved_hash_matches"] and view == original
    assert expected_delivery_markdown(view, delivery, body) == markdown
    altered = copy.deepcopy(delivery)
    altered["reading_coverage_prefix"] = altered["reading_coverage_prefix"].replace("partial", "PASS")
    with pytest.raises(AssertionError):
        expected_delivery_markdown(view, altered, body)
