"""Source-free diagnostics from saved analysis metadata, without model or source access."""

import copy
import hashlib
import json
import re

_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,159}")
_VERDICTS = {
    "supported",
    "contradicted",
    "uncertain",
    "unsupported",
    "overstated",
    "not_supported",
    "mixed",
    "not_checkable",
}
_REF_KEYS = (
    "provider",
    "source_id",
    "source_record_id",
    "object_type",
    "object_id",
    "adopted_version",
    "version_id",
    "version_source",
    "content_hash",
)


def _object(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _fields(value, names):
    value = _object(value)
    return {name: copy.deepcopy(value[name]) for name in names if name in value}


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _code(value):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None


def _reference(value):
    return _fields(value, _REF_KEYS)


def _repair_rows(progress):
    source = progress.get("reading_repairs", _object(progress.get("diagnostics")).get("reading_repairs"))
    source = list(source.values()) if isinstance(source, dict) else _rows(source)
    names = (
        "reason",
        "error_code",
        "original_call_key",
        "repair_call_key",
        "repair_call_id",
        "status",
        "repair_error_code",
        "field_paths",
        "allowed_reference_ids",
        "allowed_claim_ids",
        "original_request_bytes",
        "repair_request_bytes",
        "original_input_token_estimate",
        "repair_input_token_estimate",
        "max_input_ratio",
    )
    result = []
    for item in source:
        row = _fields(item, names)
        for field in ("reason", "error_code", "repair_error_code"):
            if field in row:
                original = row[field]
                row[field] = _code(original)
                if original and row[field] is None:
                    row[field + "_sha256"] = _digest(original)
        if "field_paths" not in row:
            row["field_paths"] = [
                error["path"] for error in _rows(item.get("reference_errors")) if "path" in error
            ]
        result.append(row)
    return result


def _reading_validation_rows(progress):
    source = progress.get(
        "reading_validation_errors", _object(progress.get("diagnostics")).get("reading_validation_errors")
    )
    source = source if isinstance(source, dict) else {}
    return [
        {
            "original_call_key": key,
            **_fields(item, ("original_request_sha256", "original_output_sha256")),
            "error_code": _code(item.get("error_code")),
            "field_errors": [
                {"path": copy.deepcopy(error.get("path")), "error_code": _code(error.get("error_code"))}
                for error in _rows(item.get("field_errors"))
                if isinstance(error, dict)
            ],
        }
        for key, item in source.items()
        if isinstance(item, dict)
    ]


def _queries(queries):
    result = []
    for index, item in enumerate(queries):
        plan, pagination = _object(item.get("plan")), _object(item.get("pagination"))
        row = _fields(
            item,
            (
                "action_id",
                "kind",
                "provider",
                "status",
                "error_code",
                "returned",
                "new_versions",
                "has_more",
                "evidence_keys",
                "decision_step",
            ),
        )
        row.update(
            query_index=index,
            source_id=plan.get("source_id"),
            terms=copy.deepcopy(plan.get("terms")),
            previous_query_index=plan.get("previous_query_index"),
            cursor_sha256=_digest(plan.get("cursor")),
        )
        if pagination:
            row["pagination"] = _fields(pagination, ("page_number", "has_more", "max_seq"))
            row["pagination"]["next_cursor_sha256"] = _digest(pagination.get("next_cursor"))
        if item.get("kind") == "document_read":
            row["records"] = [
                _fields(record, ("source_record_id", "version_id", "window_start"))
                for record in _rows(plan.get("records"))
            ]
        result.append(row)
    return result


def _biocheck_issues(report):
    result = []
    reviews = [
        ("verification", _object(report.get("verification"))),
        ("initial_verification", _object(report.get("initial_verification"))),
        ("support_audit", _object(report.get("support_audit"))),
    ]
    for round_index, item in enumerate(_rows(report.get("rounds"))):
        saved = _object(item.get("report"))
        reviews.append(("round:" + str(round_index), _object(saved.get("verification"))))
    for source, review in reviews:
        for unit_index, unit in enumerate(_rows(review.get("units"))):
            base = {
                "source": source,
                "unit_id": unit.get("unit_id"),
                "unit_index": unit_index,
                "sentence_index": _number(unit.get("sentence_index")),
            }
            for issue_index, issue in enumerate(_rows(unit.get("issues"))):
                original = (
                    issue if isinstance(issue, str) else json.dumps(issue, ensure_ascii=False, sort_keys=True)
                )
                label = _code(issue) if isinstance(issue, str) else _code(issue.get("label"))
                result.append(
                    {
                        **base,
                        "issue_index": issue_index,
                        "label": label,
                        "description_present": bool(original),
                        "description_sha256": _digest(original),
                    }
                )
            for claim_index, claim in enumerate(_rows(unit.get("claims"))):
                row = {
                    **base,
                    "claim_index": claim_index,
                    "verdict": claim.get("verdict") if claim.get("verdict") in _VERDICTS else None,
                    "label": claim.get("verdict") if claim.get("verdict") in _VERDICTS else None,
                    "kind": _code(claim.get("kind")),
                }
                if _number(claim.get("sentence_index")) is not None:
                    row["sentence_index"] = claim["sentence_index"]
                row["evidence_ids"] = [
                    _fields(ref, ("evidence_id", "segment_id", "version"))
                    for ref in _rows(claim.get("evidence"))
                ]
                result.append(row)
        for issue in _rows(review.get("verification_errors")):
            row = _fields(
                issue,
                (
                    "unit_id",
                    "claim_index",
                    "sentence_index",
                    "evidence_id",
                    "segment_id",
                    "path",
                    "rule_id",
                    "code",
                    "error_code",
                ),
            )
            row.update(source=source, reason=_code(issue.get("reason")))
            result.append(row)
        for check in _rows(review.get("checks")):
            result.append({"source": source, **_fields(check, ("unit_id", "claim_index", "verdict"))})
    return result


def _numeric_candidates(analysis, progress):
    result = []
    policy = _object(analysis.get("query_policy")) or _object(
        _object(analysis.get("result")).get("query_policy")
    )
    for artifact_id, output in _object(policy.get("artifact_results")).items():
        data = _object(_object(output).get("data_preparation"))
        manifests = [data, *_object(data.get("direction_datasets")).values()]
        for manifest in manifests:
            for item in _rows(_object(manifest).get("excluded_records")):
                if item.get("reason") != "narrative_observation_rejected":
                    continue
                result.append(
                    {
                        "origin": "artifact",
                        "artifact_id": artifact_id,
                        "record_ref": _reference(item.get("record_ref")),
                        "observation_index": item.get("observation_index"),
                        "reason": "narrative_observation_rejected",
                        "issues": [
                            {"stage": _code(issue.get("stage")), "reason": _code(issue.get("reason"))}
                            for issue in _rows(item.get("issues"))
                        ],
                    }
                )
    for key, item in _object(progress.get("narrative_extractions")).items():
        for gap in _rows(item.get("gaps")):
            result.append(
                {
                    "origin": "model_extraction",
                    "extraction_id": key,
                    "record_ref": _reference(item.get("record_ref")),
                    "observation_index": gap.get("observation_index"),
                    "reason": "narrative_observation_rejected",
                    "issues": [
                        {"stage": _code(issue.get("stage")), "reason": _code(issue.get("reason"))}
                        for issue in _rows(gap.get("issues"))
                    ]
                    or ([{"stage": "scope", "reason": _code(gap["reason"])}] if gap.get("reason") else []),
                }
            )
    if not result:
        for item in _rows(_object(progress.get("diagnostics")).get("numeric_candidates")):
            result.append(
                {
                    **_fields(
                        item, ("origin", "artifact_id", "extraction_id", "observation_index", "reason")
                    ),
                    "record_ref": _reference(item.get("record_ref")),
                    "issues": [_fields(issue, ("stage", "reason")) for issue in _rows(item.get("issues"))],
                }
            )
    return result


def _total(rows):
    known = [row["actual_tokens"] for row in rows if row["attempted"] and row["actual_tokens"] is not None]
    unknown = sum(row["attempted"] and row["actual_tokens"] is None for row in rows)
    sizes = [row["request_bytes"] for row in rows if row["request_bytes"] is not None]
    return {
        "call_count": len(rows),
        "attempted_calls": sum(row["attempted"] for row in rows),
        "total_tokens": None if unknown else sum(known),
        "known_total_tokens": sum(known),
        "unknown_usage_calls": unknown,
        "request_bytes": sum(sizes) if len(sizes) == len(rows) else None,
        "known_request_bytes": sum(sizes),
        "unknown_request_bytes_calls": len(rows) - len(sizes),
    }


def _usage(calls, report):
    rows = []
    for item in calls:
        receipt = _object(item.get("receipt"))
        usage = _object(receipt.get("usage"))
        raw = {
            name: _number(usage.get(name)) for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        total = raw["total_tokens"]
        basis = "receipt_total" if total is not None else None
        if total is None and raw["prompt_tokens"] is not None and raw["completion_tokens"] is not None:
            total = raw["prompt_tokens"] + raw["completion_tokens"]
            basis = "sum_of_receipt_components"
        attempted = receipt.get("status") != "not_sent" and item.get("status") != "prepared"
        row = _fields(item, ("call_id", "call_key", "stage", "status", "request_signature", "input_hash"))
        row.update(
            receipt_status=receipt.get("status"),
            error_code=receipt.get("error_code"),
            usage=raw,
            request_bytes=(
                _number(receipt.get("request_bytes"))
                if _number(receipt.get("request_bytes")) is not None
                else _number(item.get("request_bytes"))
            ),
            request_bytes_basis="encoded_model_request_utf8",
            actual_tokens=total,
            actual_tokens_basis=basis,
            attempted=attempted,
            finish_reason=receipt.get("finish_reason"),
            model=receipt.get("provider_model") or receipt.get("requested_model"),
            elapsed_ms=_number(receipt.get("elapsed_ms")),
        )
        rows.append(row)
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get("stage"), []).append(row)
    totals = _total(rows)
    body = report.get("final_text")
    verification = _object(report.get("verification"))
    bound_pass = (
        report.get("status") == "PASS"
        and verification.get("status") == "PASS"
        and isinstance(body, str)
        and bool(body)
        and verification.get("draft") == body
        and report.get("text_sha256") == _digest(body)
        and not verification.get("verification_errors")
    )
    return {
        **totals,
        "calls": rows,
        "stages": [{"stage": stage, **_total(items)} for stage, items in grouped.items()],
        "verified_pass_total_tokens": totals["total_tokens"] if bound_pass else None,
        "verified_pass_report": bound_pass,
        "monetary_cost": None,
        "qualification": (
            "调用用量来自原回执；字节是编码输入大小而非token。未知不补零；"
            "PASS费用仅对应逐字绑定的文字核验PASS稿，不代表科学结论成立。"
        ),
    }


def build_analysis_diagnostics(analysis, *, progress=None, fixed=None):
    """Pure allowlist projection; callers retain source authorization and saved rows unchanged."""
    progress = _object(progress) if progress is not None else _object(analysis.get("progress"))
    fixed = _object(fixed)
    report = _object(_object(analysis.get("result")).get("report"))
    calls = _rows(progress.get("calls", analysis.get("calls")))
    queries = _rows(progress.get("queries", analysis.get("queries")))
    limits = {}
    for values in (
        _object(_object(analysis.get("budget")).get("limits")),
        fixed.get("budget"),
        fixed.get("execution_limits"),
        progress.get("execution_limits"),
        _object(progress.get("diagnostics")).get("execution_limits"),
    ):
        limits.update({key: value for key, value in _object(values).items() if _number(value) is not None})
    execution = _object(progress.get("execution_diagnostic"))
    return {
        "analysis_id": analysis.get("id"),
        "investigation_id": analysis.get("investigation_id"),
        "state": analysis.get("state"),
        "error_code": analysis.get("error_code"),
        "is_demo": analysis.get("is_demo"),
        "report_status": report.get("status"),
        "report_text_sha256": report.get("text_sha256"),
        "report_errors": {
            "error_code": report.get("error_code"),
            "generation_error_present": bool(report.get("generation_error")),
            "generation_error_sha256": _digest(report.get("generation_error")),
            "verification_error_present": bool(report.get("verification_error")),
            "verification_error_sha256": _digest(report.get("verification_error")),
        },
        "execution_diagnostic": {
            "exception_type": execution.get("exception_type"),
            "frames": [
                _fields(item, ("file", "function", "line")) for item in _rows(execution.get("frames"))
            ],
        },
        "queries": _queries(queries),
        "reading_repairs": _repair_rows(progress),
        "reading_validation_errors": _reading_validation_rows(progress),
        "biocheck_issues": _biocheck_issues(report),
        "numeric_candidates": _numeric_candidates(analysis, progress),
        "usage": _usage(calls, report),
        "execution_limits": limits,
        "qualification": (
            "诊断记录不含原文、提示、引句或自由文本原因；"
            "编号、检索词、规则码、位置及用量用于复核失败，不是原核验正文。"
        ),
    }
