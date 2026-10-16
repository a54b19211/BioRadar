"""A bounded Data-Fabric investigation, saved as ordinary research records.

The two planning functions are pure. Only the coordinator uses the adapter and
existing PostgreSQL services; it creates no run type, queue, or human judgment.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from bioradar.adapters.data_fabric_research import PROTOCOL_PATHS, validate_document
from bioradar.api.errors import DomainError
from bioradar.research.investigations.models import InvestigationCreate, InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.models import canonical_hash

if TYPE_CHECKING:
    from bioradar.adapters.data_fabric_research import DataFabricResearchReader
    from bioradar.research.engine.auth import EngineActor
    from bioradar.research.engine.service import EngineService

STOPPED = frozenset({"TERMINATED", "WITHDRAWN", "SUSPENDED"})
OTHER_RECOGNIZED_STATUSES = frozenset(
    {
        "COMPLETED",
        "RECRUITING",
        "NOT_YET_RECRUITING",
        "ACTIVE_NOT_RECRUITING",
        "ENROLLING_BY_INVITATION",
    }
)
CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
GENERIC_CODES = frozenset({"car-t", "t-cell", "t-cells", "anti-bcma"})
QUESTIONS = (
    "执行首查及由真实材料触发的追加标题查找，并保存采用资料",
    "记录本批注册状态、阶段、停止原因及缺失字段",
    "取得足以评价疗效、安全性及战略价值的结果和专业核验",
)
LIMITATIONS = (
    "title_literal_and_current_db_only_not_full_text_or_complete_topic_search",
    "registered_status_and_stoppage_reason_are_source_reports_not_effectiveness_judgments",
    "has_results_is_not_exposed_results_availability_remains_unknown",
    "publication_availability_and_immutable_provider_versions_remain_unverified",
    "search_and_detail_reads_are_not_one_consistent_database_snapshot",
    "record_counts_and_distinct_registration_ids_are_not_independent_study_counts",
    "program_materials_are_context_or_uncertain_no_automatic_support_oppose_or_human_judgment",
)
FACT_PATHS = {field: "/protocolSection/" + path for field, path in PROTOCOL_PATHS.items()}
DATE_FIELDS = (
    "study_first_post_date",
    "last_update_post_date",
    "start_date",
    "primary_completion_date",
    "completion_date",
)


@dataclass(frozen=True)
class FollowUp:
    terms: tuple[str, ...]
    term: str
    document_uuid: str
    field_path: str
    field_value: str
    value_start: int
    value_end: int
    record_ref: dict | None = None
    snapshot_id: str | None = None
    material_entry_id: str | None = None
    value_offset_unit: str = "unicode_codepoint"


def _query_key(terms) -> tuple[str, ...]:
    return tuple(sorted({term.casefold() for term in terms}))


def _bound_facts(document: dict) -> dict:
    """Reuse the adapter's pure source/path/value binding; do not maintain a second parser."""
    validate_document(document, document["uuid"])
    projection = document["projection"]
    return projection["facts"] if projection["status"] == "available" else {}


def _literal_value(value):
    return sorted(value, key=canonical_hash) if isinstance(value, list) else value


def select_followups(
    seed_terms: tuple[str, ...],
    documents: list[dict],
    used_queries=(),
    *,
    max_followups: int = 2,
) -> list[FollowUp]:
    """Select literal identifier-like substrings, without resolving drug/target identities.

    A code containing a seed component may yield a new component (BCMA-GPRC5D
    yields GPRC5D). All offsets refer to the unchanged intervention-name value.
    The original topic terms remain required in every subsequent title query.
    """
    if not 0 <= max_followups <= 2:
        raise ValueError("at most two follow-up terms are permitted")
    if (
        not seed_terms
        or len(seed_terms) > 3
        or any(not isinstance(term, str) or not 2 <= len(term.strip()) <= 120 for term in seed_terms)
        or len(_query_key(seed_terms)) != len(seed_terms)
    ):
        raise ValueError("one to three explicit seed terms are required")
    if max_followups == 0:
        return []
    used = {_query_key(query) for query in used_queries}
    seeds = {term.casefold() for term in seed_terms}
    selected = []
    for document in sorted(documents, key=lambda row: row["uuid"]):
        projection = document.get("projection", {})
        if projection.get("status") != "available":
            continue
        facts = _bound_facts(document)
        path = projection["field_paths"].get("interventions")
        if not path:
            continue
        for index, intervention in enumerate(facts.get("interventions") or []):
            # A procedure or comparator is not automatically a drug candidate.
            if intervention.get("type") not in {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"}:
                continue
            value = intervention.get("name")
            if not isinstance(value, str) or not value or len(value) > 2000:
                continue
            for match in CODE.finditer(value):
                spans = [(match.start(), match.end())]
                parts = list(re.finditer(r"[A-Za-z0-9]+", match.group()))
                if any(part.group().casefold() in seeds for part in parts):
                    spans = [
                        (match.start() + part.start(), match.start() + part.end())
                        for part in parts
                        if part.group().casefold() not in seeds
                    ]
                for start, end in spans:
                    term = value[start:end]
                    if not 3 <= len(term) <= 64 or term.casefold() in seeds | GENERIC_CODES:
                        continue
                    if not any(char.isdigit() for char in term) and not ("-" in term and term.isupper()):
                        continue
                    terms = (*seed_terms, term)
                    key = _query_key(terms)
                    if key in used:
                        continue
                    selected.append(
                        FollowUp(
                            terms=terms,
                            term=term,
                            document_uuid=document["uuid"],
                            field_path=f"{path}/{index}/name",
                            field_value=value,
                            value_start=start,
                            value_end=end,
                            record_ref=document.get("_record_ref"),
                            snapshot_id=document.get("_snapshot_id"),
                            material_entry_id=document.get("_material_entry_id"),
                        )
                    )
                    used.add(key)
                    if len(selected) >= max_followups:
                        return selected
    return selected


def summarize_trials(documents: list[dict]) -> dict:
    """Compare declared registry fields; missing results never become negative outcomes."""
    rows, by_registration = [], {}
    groups = {field: {} for field in ("overall_status", "phases", "conditions", "interventions")}
    for document in sorted(documents, key=lambda row: row["uuid"]):
        projection = document.get("projection", {})
        facts = _bound_facts(document)
        gaps = ["results_availability_not_exposed", "professional_outcome_evaluation_not_connected"]
        if projection.get("status") != "available":
            gaps.append("protocol_projection_unavailable")
        for field in ("nct_id", "overall_status", "phases", "interventions", "conditions"):
            if facts.get(field) is None:
                gaps.append("missing_" + field)
        status = facts.get("overall_status")
        stopped = True if status in STOPPED else False if status in OTHER_RECOGNIZED_STATUSES else None
        if status is not None and stopped is None:
            gaps.append("registered_status_not_classifiable")
        if stopped and not facts.get("why_stopped"):
            gaps.append("stoppage_reason_not_supplied")
        row = {
            "document_uuid": document["uuid"],
            "title": document.get("title"),
            "nct_id": facts.get("nct_id"),
            "overall_status": status,
            "phases": facts.get("phases"),
            "conditions": facts.get("conditions"),
            "interventions": facts.get("interventions"),
            "why_stopped": facts.get("why_stopped"),
            "registered_stoppage_status": stopped,
            "results_availability": "unknown_not_exposed",
            "field_paths": projection.get("field_paths") or {},
            "gaps": gaps,
            "record_ref": document.get("_record_ref"),
            "snapshot_id": document.get("_snapshot_id"),
            "material_entry_id": document.get("_material_entry_id"),
            "adoption_status": "adopted" if document.get("_snapshot_id") else "not_adopted",
            **{field: facts.get(field) for field in DATE_FIELDS},
            "dates_are_source_declarations_not_verified_event_times": True,
        }
        rows.append(row)
        if row["nct_id"]:
            by_registration.setdefault(row["nct_id"], []).append(row)
        for field, values in groups.items():
            value = row[field]
            if value is not None:
                # Array ordering alone is not a research difference.
                comparable = _literal_value(value)
                group = values.setdefault(canonical_hash(comparable), {"value": comparable, "documents": []})
                group["documents"].append(row["document_uuid"])
    differences = [
        {
            "field": field,
            "observed_values": list(values.values()),
            "meaning": "literal_registry_field_variation_not_comparative_effectiveness",
        }
        for field, values in groups.items()
        if len(values) > 1
    ]
    duplicates = []
    for registration, members in sorted(by_registration.items()):
        if len(members) < 2:
            continue
        different = [
            field
            for field in ("overall_status", "phases", "why_stopped", "interventions")
            if len({canonical_hash(_literal_value(member[field])) for member in members}) > 1
        ]
        duplicates.append(
            {
                "nct_id": registration,
                "documents": [member["document_uuid"] for member in members],
                "differing_fields": different,
                "meaning": "same_registration_multiple_documents_check_versions_not_independent_evidence",
            }
        )
    return {
        "record_count": len(rows),
        "distinct_reported_registration_ids": len(by_registration),
        "rows": rows,
        "field_differences": differences,
        "same_registration_documents": duplicates,
        "registered_stoppage_record_count": sum(row["registered_stoppage_status"] is True for row in rows),
        "unclassified_stoppage_status_record_count": sum(
            row["registered_stoppage_status"] is None for row in rows
        ),
        "adopted_record_count": sum(row["adoption_status"] == "adopted" for row in rows),
        "limitations": list(LIMITATIONS),
    }


def _quote(document: dict) -> str | None:
    text = document["projection"].get("text")
    if not text:
        return None
    # Quote source bytes as adopted, never reformat a parsed JSON object.
    match = re.search(r'"overallStatus"\s*:\s*"(?:\\.|[^"\\])*"', text)
    return match.group() if match and len(match.group()) <= 4000 else text[:500]


def run_bounded_investigation(
    engine: EngineService,
    actor: EngineActor,
    adapter: DataFabricResearchReader,
    *,
    question: str,
    seed_terms: tuple[str, ...] = ("BCMA", "myeloma"),
    title: str = "Data-Fabric 有界注册研究调查",
    max_rounds: int = 3,
    per_round: int = 6,
    max_documents: int = 18,
    execution_id: str | None = None,
) -> dict:
    """Read through one adapter, immediately persist successes, and stop honestly.

    Source-read exceptions become saved gaps. PostgreSQL writes are deliberately
    outside those handlers: an uncertain write outcome propagates without retry.
    Byte and wall-clock limits belong to the existing bounded adapter.
    """
    if not (1 <= max_rounds <= 3 and 1 <= per_round <= 6 and 1 <= max_documents <= 18):
        raise ValueError("investigation budgets exceed the permitted bounded pass")
    select_followups(seed_terms, [])  # Validate topic constraints before creating a record.
    execution_id = execution_id or str(uuid4())
    runtime = InvestigationService(engine)
    _, view = runtime.create(
        actor,
        InvestigationCreate(
            title=title,
            question=question,
            questions=list(QUESTIONS),
            is_demo=False,
        ),
        "digging:" + execution_id,
    )
    # create() may replay its original revision-1 receipt. Read the current row
    # before deciding whether an execution has already begun.
    current = runtime.get(actor, view["id"])
    if current.revision > 1:
        raise DomainError(
            "digging_execution_already_started",
            "该执行已有调查记录，请先核对；本次没有重发查找。",
            409,
        )
    view = current.model_dump(mode="json")
    report = {
        "investigation_id": view["id"],
        "execution_id": execution_id,
        "completion_status": "in_progress",
        "queries": [],
        "snapshot_ids": [],
        "material_entry_ids": [],
        "limitations": list(LIMITATIONS),
        "budgets": {"max_rounds": max_rounds, "per_round": per_round, "max_documents": max_documents},
        "retrieval_connection": "not_checked",
    }

    def append(kind, **payload):
        nonlocal view
        request = InvestigationEntryCreate.model_validate(
            {
                "expected_revision": view["revision"],
                "kind": kind,
                **payload,
            }
        )
        _, view = runtime.append(actor, view["id"], request, str(uuid4()))

    def note(value):
        append(
            "note",
            text="程序检索记录（不是研究人员判断）：\n" + json.dumps(value, ensure_ascii=False, indent=2),
        )

    append(
        "status",
        status="open",
        text="程序开始有界查找；不生成研究人员判断。",
        question_updates=[
            {"question_id": view["questions"][0]["question_id"], "status": "checking"},
        ],
    )
    documents, seen, used = [], set(), []
    plan = None
    read_error = False
    followups_executed = 0
    for number in range(max_rounds):
        terms = seed_terms if plan is None else plan.terms
        query = {
            "round": number + 1,
            "terms": list(terms),
            "scope": "title_literal_and_current_db",
            "status": "started",
            "trigger": asdict(plan) if plan else None,
            "purpose": "按用户主题词初查已有标题"
            if plan is None
            else "按真实干预字段派生词追加标题查找，逐条核对注册状态与缺口",
            "adopted_records": 0,
            "reused_records": 0,
            "unavailable_projections": [],
        }
        note(query)  # If the process stops, the saved record remains visibly unfinished.
        report["queries"].append(query)
        used.append(terms)
        if plan is not None:
            followups_executed += 1
        error = None
        try:
            result = adapter.search(terms, limit=per_round)
        except Exception as exc:
            error = exc.code if isinstance(exc, DomainError) else "provider_search_failed"
        if error is not None:
            query.update(status="failed", error_code=error)
            report["retrieval_connection"] = (
                "not_available" if error == "provider_not_found" else "read_failed"
            )
            read_error = True
            note(query)
            break
        query["has_more"] = result["has_more"]
        query["source_observed_at"] = result.get("observed_at")
        report["retrieval_connection"] = "available"
        query["returned_records"] = len(result["items"])
        for item in result["items"][:per_round]:
            if item["uuid"] in seen:
                query["reused_records"] += 1
                continue
            if len(seen) >= max_documents:
                query["budget_stopped"] = True
                break
            seen.add(item["uuid"])
            error = None
            try:
                document = adapter.read_document(item["uuid"])
                _bound_facts(document)
                snapshot = (
                    adapter.snapshot_from_documents([document])
                    if document["projection"]["status"] == "available"
                    else None
                )
            except Exception as exc:
                error = exc.code if isinstance(exc, DomainError) else "provider_document_read_failed"
            if error is not None:
                query.update(status="partial" if query["adopted_records"] else "failed", error_code=error)
                read_error = True
                break
            if snapshot is None:
                query["unavailable_projections"].append(
                    {
                        "document_uuid": document["uuid"],
                        "reason": document["projection"].get("reason"),
                    }
                )
                documents.append(document)
                continue
            _, adopted = engine.adopt_snapshot(actor, snapshot, str(uuid4()))
            record = snapshot.records[0]
            facts = document["projection"].get("facts") or {}
            summary = {
                "program_record": "registered_fields_only_not_professional_evaluation",
                "nct_id": facts.get("nct_id"),
                "overall_status": facts.get("overall_status"),
                "phases": facts.get("phases"),
                "why_stopped": (facts.get("why_stopped") or "")[:1200] or None,
                "intervention_names_display": [
                    (item.get("name") or "")[:200] for item in (facts.get("interventions") or [])[:6]
                ],
                "display_limit": "完整值及全部干预以固定原文为准，此处仅摘要展示",
                "results_availability": "unknown_not_exposed",
            }
            append(
                "material",
                snapshot_id=adopted["id"],
                record_ref=record.record_ref.model_dump(mode="json"),
                quote=_quote(document),
                stance="context" if facts else "uncertain",
                text=json.dumps(summary, ensure_ascii=False, indent=2),
            )
            material_id = view["entries"][-1]["id"]
            adopted_document = {
                **document,
                "_snapshot_id": adopted["id"],
                "_material_entry_id": material_id,
                "_record_ref": record.record_ref.model_dump(mode="json"),
            }
            documents.append(adopted_document)
            query["adopted_records"] += 1
            report["snapshot_ids"].append(adopted["id"])
            report["material_entry_ids"].append(material_id)
        if query["status"] == "started":
            query["status"] = "completed" if result["items"] else "empty"
        note(query)
        if read_error or len(seen) >= max_documents:
            break
        choices = select_followups(seed_terms, documents, used)
        if not choices:
            report["no_further_query"] = "no_new_source_derived_literal_term_within_query_constraints"
            break
        plan = choices[0]

    summary = summarize_trials(documents)
    report["summary"] = summary
    report["followups_executed"] = followups_executed
    available = any(document["projection"]["status"] == "available" for document in documents)
    completed = followups_executed > 0 and not read_error and available
    report["completion_status"] = (
        "completed_with_gaps"
        if completed
        else "partial"
        if documents
        else "failed"
        if read_error
        else "partial"
    )
    note(
        {
            "本轮执行状态": report["completion_status"],
            "读取记录数": summary["record_count"],
            "成功采用记录数": summary["adopted_record_count"],
            "来源报告停止类状态的记录数": summary["registered_stoppage_record_count"],
            "存在原样字段差异": [item["field"] for item in summary["field_differences"]],
            "同注册号多文档组数": len(summary["same_registration_documents"]),
            "缺口类型与记录数": dict(Counter(gap for row in summary["rows"] for gap in row["gaps"])),
            "结果可得性": "上游本次投影未暴露hasResults，不能根据COMPLETED或缺字段推断有无结果",
            "范围": list(LIMITATIONS),
        }
    )
    append(
        "status",
        status="paused",
        text=(
            "本次有界查找已停止，执行状态："
            + report["completion_status"]
            + "。只整理已采用注册字段及未知，等待人工核查；未形成专业结论。"
        ),
        question_updates=[
            {
                "question_id": view["questions"][0]["question_id"],
                "status": "resolved" if completed else "blocked",
            },
            {
                "question_id": view["questions"][1]["question_id"],
                "status": "resolved" if available else "blocked",
            },
            {"question_id": view["questions"][2]["question_id"], "status": "blocked"},
        ],
    )
    report["final_revision"] = view["revision"]
    report["investigation_status"] = view["status"]
    return report
