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
    assert markdown == body + delivery["source_index_markdown"]
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
