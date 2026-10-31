"""Independent checks of delivery metadata and the entire exported Markdown."""

import hashlib
import re
from urllib.parse import quote

_NAMES = {
    "clinicaltrials": "ClinicalTrials 临床登记",
    "europe_pmc": "Europe PMC 文献",
    "who_don": "WHO 通报",
    "sec_edgar": "SEC 公司披露",
    "fda_crl": "FDA 审评信函",
    "legacy_documents": "Fabric 已采用资料",
    "external_archive": "已导入原始档案",
}


def _escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def expected_delivery_markdown(view, delivery, legacy_markdown):
    assert delivery.get("schema_version") == "bioradar-report-delivery-v1", "unknown delivery schema"
    report = view["result"]["report"]
    body = report["final_text"]
    digest = hashlib.sha256(body.encode()).hexdigest()
    assert delivery["body_text_sha256"] == digest
    assert delivery["saved_text_sha256"] == report.get("text_sha256")
    assert delivery["saved_hash_matches"] == (
        digest == report["text_sha256"] if isinstance(report.get("text_sha256"), str) else None
    )
    assert delivery["verification_status"] == report.get("status"), "delivery upgraded verification"
    verification = report.get("verification") or {}
    assert delivery["verification_text_matches"] == (
        verification.get("draft") == body if verification else None
    )
    assert legacy_markdown.endswith(body)
    prefix = legacy_markdown[: -len(body)] if body else legacy_markdown
    assert delivery["scope_prefix"] == prefix
    result = view["result"]
    binding = delivery.get("assessment_binding")
    if result.get("assessment_binding") is not None:
        assert binding == result["assessment_binding"], "delivery changed assessment binding"
    claim_ids = {claim["id"] for claim in result.get("claims", [])}
    sidecars = [result.get("warning_assessment") or {}, *(result.get("competing_hypotheses") or [])]
    risk = result.get("topic_risk_reassessment") or {}
    sidecars.append(risk)
    references = [
        reference
        for item in sidecars
        for field in ("supporting_claim_ids", "opposing_claim_ids", "scope_control_claim_ids")
        for reference in item.get(field, [])
    ]
    if risk.get("decision_claim_id"):
        references.append(risk["decision_claim_id"])
    if set(references) - claim_ids:
        assert binding and binding["status"] == "unbound", "dangling final claim references hidden"
    if binding and binding["status"] == "bound":
        assert not set(references) - claim_ids, "bound assessment contains missing claim IDs"
    assessment_prefix = (
        "> 系统记录：结构化研究判断未绑定本版命题；原稿与原判断保留，不作为已绑定评分或自动行动依据。\n\n"
        if binding and binding["status"] == "unbound"
        else ""
    )
    assert delivery.get("assessment_prefix", "") == assessment_prefix
    impact = (view.get("progress") or {}).get("material_update_impact") or {}
    impact_prefix = ""
    if impact:
        notes = {
            "equivalent_inputs": "本次已比较的分析输入等同，未重新生成或核验报告。",
            "affects_dependency": "原分析依据发生变化，新版复查另行保存；本稿尚未据此更新。",
        }
        note = notes.get(impact.get("status"), "有新版本等待判断；本稿尚未据此更新。")
        cutoff = _escape(impact.get("report_as_of") or "未提供，未补成当前时点")
        parent = _escape(impact.get("parent_analysis_id") or "未记录")
        impact_prefix = (
            f"> 材料更新记录：{note} 本稿仍绑定原固定材料。"
            f"本次影响比较依据报告：{parent}；该报告资料截止：{cutoff}。\n\n"
        )
        assert delivery["report_currentness"] == "prior_fixed_materials"
    else:
        assert "material_update_prefix" not in delivery and "report_currentness" not in delivery
    assert delivery.get("material_update_prefix", "") == impact_prefix
    policy = view.get("query_policy") or (view.get("result") or {}).get("query_policy") or {}
    basis = policy.get("synthesis_reading_basis") or {}
    reading_prefix = ""
    if basis.get("cannot_use_as_current_conclusion"):
        missing = basis.get("not_incorporated") or {}
        lines = [
            "> 保留的早期阅读稿：尚未纳入后续结果，不能作为当前结论。",
            "> 原阅读记录：" + _escape(basis.get("call_key") or basis.get("call_id") or "未记录")
            + "；原查询边界：" + _escape(
                str(basis["after_query_index"]) if basis.get("after_query_index") is not None else "未记录"
            ),
            "> 原输入：" + str(len(basis.get("input_evidence", []))) + " 份材料选段、"
            + str(len(basis.get("input_artifact_refs", []))) + " 份分析产物。",
        ]
        for item in missing.get("evidence", []):
            ref = item.get("record_ref") or {}
            lines.append(
                "> 尚未纳入原件：" + _escape(item.get("evidence_key") or ref.get("object_id"))
                + "；来源：" + _escape(item.get("source_id") or "未记录")
                + "；原件ID：" + _escape(ref.get("object_id") or "未记录")
                + "；固定版本：" + _escape(ref.get("adopted_version"))
            )
        for item in missing.get("artifacts", []):
            lines.append(
                "> 尚未纳入产物：" + _escape(item.get("id")) + "；版本：" + _escape(item.get("version"))
                + "；原状态：" + _escape(item.get("status") or "未记录") + "。完成不代表核验通过。"
            )
        if view.get("investigation_id") and view.get("id"):
            inv, identifier = (quote(str(view[key]), safe="") for key in ("investigation_id", "id"))
            lines.append(
                f"> 可读入口：[本版完整调查与产物记录]"
                f"(/api/engine/investigations/{inv}/analyses/{identifier}/export)。"
            )
        reading_prefix = "\n".join(lines) + "\n\n"
    else:
        assert "reading_coverage_prefix" not in delivery
    assert delivery.get("reading_coverage_prefix", "") == reading_prefix
    assert delivery["body_range"] == {
        "start": len(prefix) + len(assessment_prefix) + len(impact_prefix) + len(reading_prefix),
        "end": len(prefix) + len(assessment_prefix) + len(impact_prefix) + len(reading_prefix) + len(body),
        "unit": "unicode_code_points",
    }
    evidence = {item["key"]: item for item in view["evidence"]}
    mapping = report.get("evidence_map") or {}
    tokens = list(
        dict.fromkeys(
            match[0]
            for match in re.finditer(r"\[(record:)?([A-Za-z][A-Za-z0-9_-]{0,127})\]", body)
            if match[1] or match[2].startswith("q") or match[2] in evidence or match[2] in mapping
        )
    )
    rows = delivery["source_index"]
    assert [item["citation"] for item in rows] == tokens, "citation index missing, reordered, or invented"
    unbound = []
    lines = [
        "",
        "",
        "---",
        "",
        "## 来源索引（系统补充，未纳入正文核验）",
        "",
        "以下标签仅方便回到本版固定原文；不是原报告正文或新增核验结果。",
        "全文核验、所选片段复核、机制评价和人工决定分别判断。转述准确不等于传言属实。",
        f"原保存正文 SHA-256：{digest}",
        "",
    ]
    if not rows:
        lines.append("正文未找到可识别的编号引用；不能据此推断引用完整或事实已经核实。")
    for row in rows:
        token = row["citation"]
        citation_id = token[1:-1].removeprefix("record:")
        assert row["citation_id"] == citation_id
        binding = mapping.get(citation_id) or {}
        item = evidence.get(binding.get("key", citation_id))
        reference = binding.get("record_ref")
        if item is None and isinstance(reference, dict):
            exact = [entry for entry in evidence.values() if entry["record_ref"] == reference]
            item = exact[0] if len(exact) == 1 else None
        if item is not None and reference is not None and item["record_ref"] != reference:
            item = None
        if (
            item is not None
            and "version" in binding
            and binding["version"] != item["record_ref"].get("adopted_version")
        ):
            item = None
        if item is None or not item["record_ref"].get("adopted_version"):
            assert row["binding_status"] == "unbound" and not row.get("evidence_key")
            unbound.append(token)
            lines.append(f"- {_escape(token)}：无法绑定本版固定原文，未猜测对应来源。")
            continue
        assert row["binding_status"] == "bound"
        assert row["evidence_key"] == item["key"] and row["record_ref"] == item["record_ref"]
        assert row["title"] == (item.get("title") or "标题未记录")
        assert row["publication_time"] == item.get("publication_time")
        source_id = item.get("source_id") or item["record_ref"].get("source_id") or ""
        assert row["source_id"] == source_id
        assert row["source_name"] == _NAMES.get(source_id, source_id or "来源未记录")
        inv_id, analysis_id, evidence_key = (
            quote(value, safe="") for value in (view["investigation_id"], view["id"], item["key"])
        )
        url = f"/api/engine/investigations/{inv_id}/analyses/{analysis_id}/evidence/{evidence_key}"
        assert row["fixed_original_url"] == url, "index does not open this version's original"
        assert row["evidence_role"] == item.get("evidence_role", "source_material")
        label = " · ".join(
            _escape(value)
            for value in (row["source_name"], row.get("publication_time") or "公开时间未记录", row["title"])
        )
        role = "（图谱背景，不替代原文证据）" if row["evidence_role"] == "graph_background" else ""
        lines.append(
            f"- {_escape(token)}：[{label}]({url}){role}。"
            f"固定版本：{_escape(row['record_ref']['adopted_version'])}。"
        )
    appendix = "\n".join(lines) + "\n"
    assert delivery["unbound_citations"] == unbound
    assert delivery["source_index_markdown"] == appendix, (
        "source appendix differs from independently checked metadata"
    )
    return prefix + assessment_prefix + impact_prefix + reading_prefix + body + appendix
