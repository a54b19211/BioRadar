"""Readable display of already source-bound facts; no extraction or professional classification."""

_FIELDS = {
    "clinicaltrials": (
        "trial_id",
        "status",
        "phase",
        "why_stopped",
        "conditions",
        "interventions",
        "enrollment",
        "start_date",
        "start_date_type",
        "completion_date",
    ),
    "fda_crl": (
        "application_numbers",
        "letter_type",
        "letter_date",
        "file_name",
        "company_name",
        "approval_status",
    ),
    "europe_pmc": (
        "source",
        "id",
        "pmid",
        "pmcid",
        "doi",
        "publication_types",
        "correction_links",
        "publication_date",
        "date_of_revision",
        "license",
        "is_open_access",
    ),
    "external_archive": (
        "program_id",
        "trial_id",
        "lineage_id",
        "indication",
        "jurisdiction",
        "mapping_available_at",
        "phase",
        "status",
    ),
}
_BOUNDARIES = {
    "clinicaltrials": "登记停止不等于药物失败；COMPLETED不等于疗效成功；缺结果不能补作阴性。",
    "fda_crl": "保留原信函类型；APPROVAL与CRL分别显示；CRL不等于永久失败，签字日不是公开日。",
    "europe_pmc": "更正/撤稿关系按原字段显示，不自动判断其对具体命题的支持或反面；摘要不等于获授权全文。",
    "external_archive": "人工提供档案及映射审核仅用于声明队列；不冒充真实药物效果验证。",
}


def summarize_records(records, *, limit=60):
    if not 1 <= limit <= 100:
        raise ValueError("readable summary limit must be 1..100")
    rows, seen, questions = [], set(), []
    for record in records:
        if record["record_kind"] == "outcome" or record["version_id"] in seen:
            continue
        seen.add(record["version_id"])
        if len(rows) >= limit:
            continue
        facts, locators = record["facts"], record["field_locators"]
        source = record["source_id"]
        keys = _FIELDS.get(source, ())
        row = {
            "source_id": source,
            "source_record_id": record["source_record_id"],
            "version_id": record["version_id"],
            "raw_sha256": record["raw_sha256"],
            "title": record["title"],
            "source_url": record.get("source_url"),
            "fields": {
                key: {"value": facts[key], "locator": locators.get(key)} for key in keys if key in facts
            },
            "missing_fields": [key for key in keys if key not in facts],
            "classification": "program_context_not_professional_judgment",
            "boundary": _BOUNDARIES.get(source, "源投影字段整理，未作专业判断。"),
        }
        # This is a display excerpt from a bound source field, never a quotation
        # reconstructed from a cleaned string or a claim to have read full text.
        excerpt_key = "text" if source == "fda_crl" else "abstract" if source == "europe_pmc" else "summary"
        excerpt = facts.get(excerpt_key)
        if isinstance(excerpt, str) and excerpt.strip():
            row["source_field_excerpt"] = {
                "field": excerpt_key,
                "text": excerpt[:700],
                "truncated": len(excerpt) > 700,
                "original_character_count": len(excerpt),
                "locator": locators.get(excerpt_key),
            }
        else:
            row["source_field_excerpt"] = None
        rows.append(row)
        if source == "clinicaltrials":
            questions.append("核对登记状态、停止原因及独立结果披露；登记完成或停止均不直接代表疗效结论。")
        elif source == "fda_crl":
            questions.append("按原申请号查后续监管决定和适应证；当前信函不能证明整个项目永久失败。")
        elif source == "europe_pmc":
            questions.append(
                "逐项核更正/撤稿关联原文与具体命题；缺关系字段不等于没有更正，未取得授权全文则留待补证。"
            )
    return {
        "rows": rows,
        "available_versions": len(seen),
        "displayed_versions": len(rows),
        "truncated": len(seen) > len(rows),
        "next_questions": list(dict.fromkeys(questions)),
        "scope": "Only adopted, source-bound fields; no automatic support/oppose or efficacy inference.",
    }
