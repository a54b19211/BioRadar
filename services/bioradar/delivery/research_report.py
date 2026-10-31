"""Formatting and source labels outside the unchanged saved research text."""

import copy
import hashlib
import re
from urllib.parse import quote

from bioradar.research.investigations.analysis import render_report
from bioradar.research.investigations.kdd import scope_markdown
from bioradar.research.investigations.research_assessment import (
    assessment_binding_note,
    assessment_result_view,
)
from bioradar.research.mining.models import canonical_hash

SCHEMA_VERSION = "bioradar-report-delivery-v1"
SOURCE_NAMES = {
    "clinicaltrials": "ClinicalTrials 临床登记",
    "europe_pmc": "Europe PMC 文献",
    "who_don": "WHO 通报",
    "sec_edgar": "SEC 公司披露",
    "fda_crl": "FDA 审评信函",
    "legacy_documents": "Fabric 已采用资料",
    "external_archive": "已导入原始档案",
}
_CITATION = re.compile(r"\[(?P<record>record:)?(?P<key>[A-Za-z][A-Za-z0-9_-]{0,127})\]")


def _object(value):
    return value if isinstance(value, dict) else {}


def human_review_context(analysis, entries):
    """Read-only projection of actual judgment entries, scoped to the exact saved object."""
    from bioradar.research.investigations.models import HumanReview

    rows, general_count = [], 0
    for original in entries:
        entry = original.model_dump(mode="json") if hasattr(original, "model_dump") else original
        if not isinstance(entry, dict) or entry.get("investigation_id") != analysis.get("investigation_id"):
            continue
        payload = _object(entry.get("payload"))
        if payload.get("kind") != "judgment":
            continue
        if not payload.get("review"):
            general_count += 1
            continue
        review = HumanReview.model_validate(payload["review"]).model_dump(mode="json")
        target = review["target"]
        binding = "different_target"
        if target.get("analysis_id") == analysis.get("id"):
            if target["type"] == "report":
                text = _object(_object(analysis.get("result")).get("report")).get("final_text")
                matches = (
                    isinstance(text, str)
                    and hashlib.sha256(text.encode()).hexdigest() == target["text_sha256"]
                )
            else:
                claims = [
                    c
                    for c in _object(analysis.get("result")).get("claims", [])
                    if c.get("id") == target["claim_id"]
                ]
                matches = len(claims) == 1 and canonical_hash(claims[0]) == target["claim_sha256"]
            if review.get("analysis_input_hash") is not None:
                matches = matches and review["analysis_input_hash"] == analysis.get("input_hash")
            binding = "exact_target" if matches else "target_changed"
        rows.append(
            {
                "entry_id": entry["id"],
                "review_revision": entry["revision"],
                "reviewer_actor": entry["created_by"],
                "reviewed_at": entry["created_at"],
                "binding_status": binding,
                "text": payload["text"],
                "conditions": payload.get("conditions", ""),
                "basis_entry_ids": list(payload.get("basis_entry_ids", [])),
                **review,
                **(
                    {"assessment_axes": copy.deepcopy(payload["assessment_axes"])}
                    if payload.get("assessment_axes")
                    else {}
                ),
            }
        )
    return {
        "status": "recorded" if any(r["binding_status"] == "exact_target" for r in rows) else "not_reviewed",
        "analysis_id": analysis.get("id"),
        "reviews": rows,
        "investigation_only_judgment_count": general_count,
        "qualification": (
            "人工记录仅适用于所列固定对象和范围；不改变机器核验，不签发对外警报，也不自动适用于新版。"
        ),
    }


def human_review_prefix(context):
    if context is None:
        return ""
    labels = {
        "needs_verification": "待人工核实",
        "dismissed": "范围内不采纳",
        "accepted_within_scope": "范围内人工采纳",
    }
    lines = ["> 人工核实记录（系统记录，未纳入原正文核验）", "> " + _escape(context["qualification"])]
    matched = [row for row in context.get("reviews", []) if row["binding_status"] == "exact_target"]
    if not matched:
        lines.append("> 尚无针对本稿固定对象的人工核实记录；调查级笔记或其他版本的判断不覆盖本稿。")
    for row in context.get("reviews", []):
        target = row["target"]
        version = target.get("text_sha256") or target.get("claim_sha256") or target.get("result_hash")
        identity = target.get("claim_id") or target.get("analysis_id") or target.get("candidate_key")
        lines.append(
            "> "
            + " · ".join(
                _escape(x)
                for x in (
                    labels[row["decision"]],
                    row["binding_status"],
                    row["reviewer_actor"],
                    row["reviewed_at"],
                    row["entry_id"],
                    target["type"],
                    identity,
                    version,
                )
            )
        )
        lines.append("> 适用范围：" + _escape(row["scope"]) + "；人工判断：" + _escape(row["text"]))
        lines.append("> 条件与下一步：" + _escape(row.get("conditions") or "未记录"))
        references = row.get("basis_record_refs") or []
        basis = "；".join(
            " / ".join(
                _escape(ref.get(key) or "未记录") for key in ("provider", "object_id", "adopted_version")
            )
            for ref in references
        )
        lines.append("> 人工依据的固定原件：" + (basis or "未选择，仅为待核提醒"))
    return "\n".join(lines) + "\n\n"


def assessment_axes_prefix(axes):
    if not isinstance(axes, dict):
        return ""
    source = axes.get("source_reliability") or []
    information = axes.get("information_credibility") or []
    lines = ["> 来源与信息评定（系统记录，未纳入原正文核验）"]
    for name, values, fallback in (
        ("来源可靠性（A–F）", source, "F"),
        ("具体信息可信度（1–6）", information, "6"),
    ):
        if not values:
            lines.append(f"> {name}：{fallback} · 未评定，无充分依据。")
        for item in values:
            reference = _object(item.get("record_ref"))
            parts = (
                item.get("grade"),
                item.get("assessment_status"),
                item.get("claim_id") or item.get("publisher_scope"),
                reference.get("object_id"),
                reference.get("adopted_version"),
                item.get("assessed_by"),
                item.get("assessed_at"),
                item.get("native_review_entry_id"),
            )
            lines.append("> " + name + "：" + " · ".join(_escape(x) for x in parts if x is not None))
            if item.get("assessment_basis"):
                lines.append("> 评级依据：" + _escape(item["assessment_basis"]))
    lines.append("> 证据等级：" + _escape(axes.get("evidence_level") or "未记录"))
    score = _object(axes.get("heat")).get("score")
    lines.append("> 曝光热度：" + (str(score) if type(score) in (int, float) else "未记录"))
    lines.append("> 四项分别记录，不合成信用分；F／6 表示未能评定，不代表虚假，也不是 50% 概率。")
    return "\n".join(lines) + "\n\n"


def _body(analysis):
    result = _object(analysis.get("result"))
    report = _object(result.get("report"))
    text = report.get("final_text")
    if isinstance(text, str) and text:
        return text, report, "professional_report"
    return render_report(result, analysis.get("question", "")), report, "structured_draft"


def _citation_rows(body, analysis, report):
    evidence = {
        item["key"]: item
        for item in analysis.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    mapping = _object(report.get("evidence_map"))
    rows = []
    seen = set()
    for match in _CITATION.finditer(body):
        citation_id = match["key"]
        if not (
            match["record"]
            or citation_id.startswith("q")
            or citation_id in evidence
            or citation_id in mapping
        ):
            continue
        token = match[0]
        if token in seen:
            continue
        seen.add(token)
        binding = _object(mapping.get(citation_id))
        key = binding.get("key", citation_id)
        item = evidence.get(key)
        reference = binding.get("record_ref")
        if item is None and isinstance(reference, dict):
            exact = [entry for entry in evidence.values() if entry.get("record_ref") == reference]
            if len(exact) == 1:
                item = exact[0]
                key = item["key"]
        if item is not None and reference is not None and item.get("record_ref") != reference:
            item = None
        if (
            item is not None
            and "version" in binding
            and binding["version"] != _object(item.get("record_ref")).get("adopted_version")
        ):
            item = None
        row = {"citation": token, "citation_id": citation_id, "binding_status": "unbound"}
        if item is not None:
            ref = _object(item.get("record_ref"))
            version = ref.get("adopted_version")
            if isinstance(version, str) and version:
                source_id = item.get("source_id") or ref.get("source_id") or ""
                ids = (analysis.get("investigation_id"), analysis.get("id"), key)
                url = None
                if all(isinstance(value, str) and value for value in ids):
                    inv_id, analysis_id, evidence_key = (quote(value, safe="") for value in ids)
                    url = (
                        f"/api/engine/investigations/{inv_id}/analyses/{analysis_id}/evidence/{evidence_key}"
                    )
                row.update(
                    binding_status="bound",
                    evidence_key=key,
                    source_id=source_id,
                    source_name=SOURCE_NAMES.get(source_id, source_id or "来源未记录"),
                    title=item.get("title") or "标题未记录",
                    publication_time=item.get("publication_time"),
                    record_ref=ref,
                    fixed_original_url=url,
                    evidence_role=item.get("evidence_role", "source_material"),
                    text_basis=item.get("text_basis"),
                )
        rows.append(row)
    return rows


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


def _index_markdown(rows, digest):
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
    for item in rows:
        citation = _escape(item["citation"])
        if item["binding_status"] != "bound":
            lines.append(f"- {citation}：无法绑定本版固定原文，未猜测对应来源。")
            continue
        label = " · ".join(
            (
                _escape(item["source_name"]),
                _escape(item.get("publication_time") or "公开时间未记录"),
                _escape(item["title"]),
            )
        )
        url = item.get("fixed_original_url")
        link = f"[{label}]({url})" if url else label
        role = "（图谱背景，不替代原文证据）" if item["evidence_role"] == "graph_background" else ""
        lines.append(
            f"- {citation}：{link}{role}。固定版本：{_escape(item['record_ref']['adopted_version'])}。"
        )
    return "\n".join(lines) + "\n"


def _material_impact_prefix(impact):
    if not impact:
        return ""
    status = impact.get("status")
    note = (
        "本次已比较的分析输入等同，未重新生成或核验报告。"
        if status == "equivalent_inputs"
        else "原分析依据发生变化，新版复查另行保存；本稿尚未据此更新。"
        if status == "affects_dependency"
        else "有新版本等待判断；本稿尚未据此更新。"
    )
    cutoff = _escape(impact.get("report_as_of") or "未提供，未补成当前时点")
    parent = _escape(impact.get("parent_analysis_id") or "未记录")
    return (
        f"> 材料更新记录：{note} 本稿仍绑定原固定材料。"
        f"本次影响比较依据报告：{parent}；该报告资料截止：{cutoff}。\n\n"
    )


def _reading_coverage_prefix(analysis):
    policy = _object(analysis.get("query_policy")) or _object(
        _object(analysis.get("result")).get("query_policy")
    )
    basis = _object(policy.get("synthesis_reading_basis"))
    if not basis.get("cannot_use_as_current_conclusion"):
        return ""
    missing = _object(basis.get("not_incorporated"))
    lines = [
        "> 保留的早期阅读稿：尚未纳入后续结果，不能作为当前结论。",
        "> 原阅读记录："
        + _escape(basis.get("call_key") or basis.get("call_id") or "未记录")
        + "；原查询边界："
        + _escape(
            str(basis["after_query_index"]) if basis.get("after_query_index") is not None else "未记录"
        ),
        "> 原输入："
        + str(len(basis.get("input_evidence", [])))
        + " 份材料选段、"
        + str(len(basis.get("input_artifact_refs", [])))
        + " 份分析产物。",
    ]
    for item in missing.get("evidence", []):
        ref = _object(item.get("record_ref"))
        lines.append(
            "> 尚未纳入原件："
            + _escape(item.get("evidence_key") or ref.get("object_id"))
            + "；来源："
            + _escape(item.get("source_id") or "未记录")
            + "；原件ID："
            + _escape(ref.get("object_id") or "未记录")
            + "；固定版本："
            + _escape(ref.get("adopted_version"))
        )
    for item in missing.get("artifacts", []):
        lines.append(
            "> 尚未纳入产物："
            + _escape(item.get("id"))
            + "；版本："
            + _escape(item.get("version"))
            + "；原状态："
            + _escape(item.get("status") or "未记录")
            + "。完成不代表核验通过。"
        )
    if analysis.get("investigation_id") and analysis.get("id"):
        inv, identifier = (quote(str(analysis[key]), safe="") for key in ("investigation_id", "id"))
        lines.append(
            f"> 可读入口：[本版完整调查与产物记录]"
            f"(/api/engine/investigations/{inv}/analyses/{identifier}/export)。"
        )
    return "\n".join(lines) + "\n\n"


def machine_verification_prefix(report):
    """Display the saved, separate machine statuses outside the checked text."""
    if not report:
        return ""
    audit = _object(report.get("support_audit"))
    full_status = _escape(report.get("status") or "未记录，不推定已执行")
    audit_status = _escape(audit.get("status") or "未记录，不推定已执行")
    explanation = audit.get("not_run_reason") or audit.get("error_code")
    suffix = "；说明：" + _escape(explanation) if explanation else ""
    return (
        "> 机器核验状态（系统记录，不属于已核正文）\n"
        f"> 全文核验：{full_status}。\n"
        f"> 所选支持片段复核：{audit_status}{suffix}。"
        "仅针对所选支持片段；PASS不代表整稿通过，也不证明传言属实或人工批准。\n\n"
    )


def build_report_delivery(analysis, *, human_review=None):
    """Pure JSON metadata; never changes the analysis, report text, or evidence identities."""
    if human_review is None:
        human_review = analysis.get("human_review_context")
    body, report, kind = _body(analysis)
    digest = hashlib.sha256(body.encode()).hexdigest()
    saved_hash = report.get("text_sha256")
    verification = _object(report.get("verification"))
    machine_prefix = machine_verification_prefix(report)
    progress = _object(analysis.get("progress"))
    prefix = scope_markdown(progress.get("material_scope"))
    impact = _object(progress.get("material_update_impact"))
    impact_prefix = _material_impact_prefix(impact)
    reading_prefix = _reading_coverage_prefix(analysis)
    projected = assessment_result_view(_object(analysis.get("result")))
    binding = projected.get("assessment_binding")
    note = assessment_binding_note(projected)
    assessment_prefix = "> " + note + "\n\n" if note else ""
    rows = _citation_rows(body, analysis, report)
    review_prefix = human_review_prefix(human_review)
    axes_prefix = assessment_axes_prefix(_object(analysis.get("result")).get("assessment_axes"))
    body_start = (
        len(prefix)
        + len(machine_prefix)
        + len(assessment_prefix)
        + len(impact_prefix)
        + len(reading_prefix)
        + len(review_prefix)
        + len(axes_prefix)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "body_kind": kind,
        "body_text_sha256": digest,
        "saved_text_sha256": saved_hash,
        "saved_hash_matches": digest == saved_hash if isinstance(saved_hash, str) else None,
        "verification_status": report.get("status"),
        "verification_text_matches": verification.get("draft") == body if verification else None,
        "body_range": {
            "start": body_start,
            "end": body_start + len(body),
            "unit": "unicode_code_points",
        },
        "source_index": rows,
        "unbound_citations": [row["citation"] for row in rows if row["binding_status"] == "unbound"],
        "scope_prefix": prefix,
        **(
            {
                "machine_verification_prefix": machine_prefix,
                "support_audit_status": _object(report.get("support_audit")).get("status"),
            }
            if machine_prefix
            else {}
        ),
        **({"assessment_axes_prefix": axes_prefix} if axes_prefix else {}),
        **(
            {"human_review_context": copy.deepcopy(human_review), "human_review_prefix": review_prefix}
            if human_review is not None
            else {}
        ),
        **(
            {"assessment_binding": binding, "assessment_prefix": assessment_prefix}
            if binding is not None
            else {}
        ),
        **(
            {"material_update_prefix": impact_prefix, "report_currentness": "prior_fixed_materials"}
            if impact
            else {}
        ),
        **({"reading_coverage_prefix": reading_prefix} if reading_prefix else {}),
        "source_index_markdown": _index_markdown(rows, digest),
        "qualification": "系统来源索引未纳入原正文核验；文字核验通过不等于传言属实、证据覆盖完整或人工批准。",
    }


def render_report_markdown(analysis, *, human_review=None):
    """Scope prefix + byte-identical saved body + separately labelled system source index."""
    body, _, _ = _body(analysis)
    delivery = build_report_delivery(analysis, human_review=human_review)
    return (
        delivery["scope_prefix"]
        + delivery.get("machine_verification_prefix", "")
        + delivery.get("assessment_prefix", "")
        + delivery.get("material_update_prefix", "")
        + delivery.get("reading_coverage_prefix", "")
        + delivery.get("human_review_prefix", "")
        + delivery.get("assessment_axes_prefix", "")
        + body
        + delivery["source_index_markdown"]
    )
