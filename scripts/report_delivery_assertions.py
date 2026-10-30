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
    assert delivery["body_range"] == {
        "start": len(prefix),
        "end": len(prefix) + len(body),
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
    return legacy_markdown + appendix
