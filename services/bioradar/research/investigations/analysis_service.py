"""One bounded semantic research record; PostgreSQL owns input, progress and output."""

import copy
import hashlib
import json
import math
import secrets
import time
import traceback
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    cast,
    func,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from bioradar.adapters.reasoning_model import model_parameters, normalize_model_parameters
from bioradar.api.errors import DomainError
from bioradar.data.database import Base
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.models import EngineIdempotency
from bioradar.research.engine.service import SOURCE_READ_UNAVAILABLE, EngineService
from bioradar.research.investigations.analysis import (
    MAX_CHARACTERS,
    MAX_EVIDENCE,
    SYSTEM_PROMPT,
    analysis_budget,
    basis_hash,
    evidence_basis_identity,
    freeze_passages,
    freeze_payload,
    material_context_identity,
    material_context_payload,
    needs_revision,
    program_update,
    readable_result,
    relaxed_query_terms,
    render_report,
    report_context,
    source_excerpt,
    validate_reading,
    validate_synthesis,
    validate_update,
    validated_result,
)
from bioradar.research.investigations.analysis_models import (
    ANALYSIS_VERSION,
    PROMPT_VERSION,
    AnalysisBudget,
    AnalysisCreate,
    AnalysisEvidence,
    AnalysisEvidenceRead,
    AnalysisList,
    AnalysisQueryReceipt,
    AnalysisResult,
    AnalysisSummary,
    AnalysisUpdate,
    AnalysisView,
    GenericReview,
    InitialQuery,
    ModelReading,
    ModelReview,
    ModelSynthesis,
    PlannedQuery,
    PolicyPlannedQuery,
    PolicyReading,
    QuestionPlan,
)
from bioradar.research.investigations.execution_budget import (
    WAIT_CODE,
    finish_budget_wait,
    reading_limits,
    reserve_automatic_call,
)
from bioradar.research.investigations.models import InvestigationEntryCreate
from bioradar.research.investigations.research_assessment import (
    assessment_repair_payload,
    assessment_result_view,
    claim_assessment_errors,
    final_assessment_context,
    final_reference_repair_payload,
    synthesis_schema,
    validate_assessment_repair,
    validate_final_reference_repair,
)
from bioradar.research.investigations.service import (
    InvestigationEntryRecord,
    InvestigationRecord,
    InvestigationService,
)
from bioradar.research.mining.models import NormalizedSnapshot, canonical_hash


class InvestigationAnalysisRecord(Base):
    __tablename__ = "investigation_analyses"
    __table_args__ = (UniqueConstraint("workspace_id", "investigation_id", "input_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), nullable=False)
    parent_analysis_id: Mapped[str | None] = mapped_column(ForeignKey("investigation_analyses.id"))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    is_demo: Mapped[bool] = mapped_column(nullable=False)
    based_on_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fixed_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    dispatch_pending: Mapped[bool] = mapped_column(nullable=False)
    rq_job_id: Mapped[str] = mapped_column(String(100), nullable=False)
    execution_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note_entry_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def execution_seconds(fixed_input):
    parameters = fixed_input["model_parameters"]
    return parameters.get(
        "job_timeout_seconds", max(240, parameters.get("max_calls", 3) * parameters["timeout_seconds"] + 60)
    )


def minimum_send_seconds(fixed, progress, stage):
    """Freeze a floor, then respect observed latency of this same report stage."""
    timeout = fixed["model_parameters"]["timeout_seconds"]
    floor = fixed["model_parameters"].get("min_send_seconds", min(60, timeout))
    aliases = {"biocheck_recheck": "biocheck_check", "recheck": "check"}
    target = aliases.get(stage, stage)
    observed = [
        math.ceil((call.get("receipt") or {}).get("elapsed_ms", 0) / 1000)
        for call in progress.get("calls", [])
        if call.get("status") == "received" and aliases.get(call["stage"], call["stage"]) == target
    ]
    return min(timeout, max(1, floor, *observed))


def _known_optional_read_failure(call):
    receipt = (call or {}).get("receipt") or {}
    if (call or {}).get("status") != "failed":
        return False
    if receipt.get("status") == "not_sent":
        return receipt.get("error_code") in {
            "analysis_model_budget",
            "analysis_token_budget",
            "analysis_time_budget",
            "analysis_query_budget",
            "analysis_tool_budget",
            "model_input_budget",
        }
    return (
        receipt.get("status") == "failed"
        and receipt.get("http_status") == 200
        and receipt.get("error_code")
        in {"model_output_truncated", "model_output_invalid", "model_protocol_error"}
    )


def semantic_enabled(settings):
    return bool(
        settings.semantic_llm_enabled
        and settings.analysis_auto_enabled
        and settings.semantic_llm_base_url
        and settings.semantic_llm_api_key
        and settings.semantic_llm_model
    )


class AnalysisService:
    def __init__(self, engine, reader=None, model_call=None):
        self.engine, self.settings = engine, engine.settings
        self.actor = DevelopmentAccess(self.settings).actor()
        self.reader, self.model_call = reader, model_call
        if reader is not None:
            from bioradar.adapters.fabric_history import FabricHistoryReader

            # Current permission checks must also work when only saving a completed
            # report after its research read/time budget has expired.
            def current_sources():
                return FabricHistoryReader(
                    reader.client,
                    authorization_ref=reader.authorization_ref,
                    source_instance=reader.source_instance,
                    max_seconds=3,
                    max_bytes=1_000_000,
                ).sources()

            self.engine.history_source_reader = current_sources

    def _source_instance(self, is_demo):
        if is_demo:
            return (
                getattr(self.reader, "demo_source_instance", None)
                or self.settings.data_fabric_demo_source_instance
                or "fixture"
            )
        return getattr(self.reader, "source_instance", self.settings.data_fabric_source_instance)

    def _row(self, session, actor, identifier, *, lock=False):
        InvestigationService(self.engine)._actor(actor)
        statement = select(InvestigationAnalysisRecord).where(
            InvestigationAnalysisRecord.id == identifier,
            InvestigationAnalysisRecord.workspace_id == actor.workspace_id,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = session.scalar(statement)
        if row is None:
            raise DomainError("not_found", "未找到可访问的机器分析", 404)
        self._authorize(session, actor, row)
        return row

    def _authorize(self, session, actor, row):
        # This set includes every parent input and every read result, not only cited previews.
        for dependency in row.fixed_input.get("dependency_sources", []):
            instance, source = dependency["instance"], dependency["source_id"]
            if (
                instance not in actor.source_allowlist
                or source not in self.settings.engine_source_id_allowlist.get(instance, [])
            ):
                raise DomainError("history_source_revoked", "机器分析依赖来源当前无读取许可", 403)
            if dependency.get("history"):
                self.engine.authorize_history_source(source)
        for identifier in row.fixed_input.get("snapshot_ids", []):
            self.engine._snapshot(session, actor, identifier)

    def _search_scope(self, actor, is_demo):
        instance = self._source_instance(is_demo)
        if instance not in actor.source_allowlist:
            return []
        allowed = set(self.settings.engine_source_id_allowlist.get(instance, []))
        result = []
        for source in self.settings.analysis_search_source_ids:
            if source in self.settings.analysis_graph_source_ids or source not in allowed:
                continue
            try:
                self.engine.authorize_history_source(source)
            except DomainError as exc:
                if exc.code in SOURCE_READ_UNAVAILABLE:
                    continue
                raise
            result.append(source)
        return result

    def _graph_scope(self, actor, is_demo):
        if not self.settings.data_fabric_graph_query_token:
            return []
        instance = self._source_instance(is_demo)
        if instance not in actor.source_allowlist:
            return []
        allowed = set(self.settings.engine_source_id_allowlist.get(instance, []))
        result = []
        for source in self.settings.analysis_graph_source_ids:
            if source not in allowed:
                continue
            try:
                self.engine.authorize_history_source(source)
            except DomainError as exc:
                if exc.code in SOURCE_READ_UNAVAILABLE:
                    continue
                raise
            result.append(source)
        return result

    def options(self, actor, *, is_demo=False):
        from bioradar.adapters.reasoning_model import model_status

        InvestigationService(self.engine)._actor(actor)
        status = model_status(self.settings)
        return {
            "enabled": status.enabled,
            "configured": status.configured,
            "retrieval_sources": sorted(
                set(self._search_scope(actor, is_demo)) | set(self._graph_scope(actor, is_demo))
            ),
            "graph_sources": self._graph_scope(actor, is_demo),
            "limits": {
                **self._budget_limits(),
                "manual_analyses_per_hour": self.settings.analysis_manual_per_hour,
                "evidence_versions": self.settings.analysis_v2_max_evidence
                if self.settings.analysis_kdd_enabled
                else MAX_EVIDENCE,
                "evidence_characters": self.settings.analysis_v2_max_characters
                if self.settings.analysis_kdd_enabled
                else MAX_CHARACTERS,
                "records_per_query": self.settings.analysis_v2_records_per_query
                if self.settings.analysis_kdd_enabled
                else 3,
                "document_search": self.settings.analysis_v2_action_limits.get(
                    "document_search", self.settings.analysis_max_queries
                ),
                "document_read": self.settings.analysis_v2_action_limits.get(
                    "document_read", self.settings.analysis_query_policy_v2_max_steps
                ),
                "pages_per_chain": 5,
                "monitor_investigations_per_batch": self.settings.monitor_investigations_per_batch,
                "support_audit_calls": 1,
            },
            "limits_by_policy": {
                "baseline": {
                    "evidence_versions": MAX_EVIDENCE,
                    "evidence_characters": MAX_CHARACTERS,
                    "records_per_query": 3,
                    "document_search": self.settings.analysis_max_queries,
                    "document_read": 0,
                    "pages_per_chain": 1,
                },
                "query_planner": {
                    "evidence_versions": self.settings.analysis_v2_max_evidence
                    if self.settings.analysis_kdd_enabled
                    else MAX_EVIDENCE,
                    "evidence_characters": self.settings.analysis_v2_max_characters
                    if self.settings.analysis_kdd_enabled
                    else MAX_CHARACTERS,
                    "records_per_query": self.settings.analysis_v2_records_per_query
                    if self.settings.analysis_kdd_enabled
                    else 3,
                    "document_search": min(
                        self.settings.analysis_max_queries,
                        self.settings.analysis_v2_action_limits.get(
                            "document_search", self.settings.analysis_max_queries
                        ),
                    )
                    if self.settings.analysis_kdd_enabled
                    else self.settings.analysis_max_queries,
                    "document_read": self.settings.analysis_v2_action_limits.get(
                        "document_read", self.settings.analysis_query_policy_v2_max_steps
                    )
                    if self.settings.analysis_kdd_enabled
                    else 0,
                    "pages_per_chain": 5 if self.settings.analysis_kdd_enabled else 1,
                },
            },
            "providers": self._provider_status(),
            "runtime": {
                "environment": self.settings.environment,
                "workspace_id": actor.workspace_id,
                "database_name": self.engine.database.url.database
                if self.settings.environment == "test"
                else None,
            },
        }

    def _budget_limits(self, requested=None):
        limits = {
            "max_calls": self.settings.analysis_max_calls,
            "max_tokens": self.settings.analysis_max_tokens,
            "max_seconds": self.settings.analysis_max_seconds,
            "max_queries": self.settings.analysis_max_queries,
            "max_tool_calls": self.settings.analysis_max_tool_calls,
        }
        if requested:
            for name, value in requested.model_dump(exclude_none=True).items():
                if value > limits[name]:
                    raise DomainError("analysis_budget_invalid", "请求预算不能超过服务上限", 422)
                limits[name] = value
        return limits

    def _provider_status(self, *, kdd=False, direction="research", is_demo=False):
        from bioradar.adapters.weknora import provider_status as retrieval_status
        from bioradar.research.investigations.professional import provider_status

        retrieval = retrieval_status(self.settings)
        if kdd:
            from bioradar.adapters.weknora_kdd import search_status

            retrieval = search_status(self.settings, direction=direction, is_demo=is_demo)
        result = {
            "biocheck": provider_status(self.settings, "biocheck"),
            "mech_evidence": provider_status(self.settings, "mech_evidence"),
            "weknora": retrieval,
        }
        if kdd:
            from bioradar.adapters.fabric_fetch import FETCH_VERSION, demo_fetch_available

            result["fabric_fetch"] = {
                "provider": "data_fabric",
                "version": FETCH_VERSION,
                "available": bool(
                    demo_fetch_available(self.settings)
                    if is_demo
                    else self.settings.data_fabric_url and self.settings.data_fabric_history_fetch_token
                ),
                "scope": "isolated_simulation" if is_demo else "current_real_sources_only",
                **(
                    {"demo_instance_id": self.settings.data_fabric_demo_fetch_instance_id}
                    if is_demo and demo_fetch_available(self.settings)
                    else {}
                ),
            }
        return result

    @staticmethod
    def _summary(row):
        fixed = row.fixed_input
        return AnalysisSummary(
            id=row.id,
            investigation_id=row.investigation_id,
            parent_analysis_id=row.parent_analysis_id,
            state=row.state,
            trigger=row.trigger,
            created_at=row.created_at,
            completed_at=row.completed_at,
            question=fixed["question"],
            based_on_revision=row.based_on_revision,
            version=fixed["version"],
            evidence_count=len(row.progress.get("evidence", fixed["evidence"])),
            error_code=row.error_code,
            result_summary=row.result["conditional_answer"]["answer"] if row.result else None,
        )

    @classmethod
    def _view(cls, row, *, fixed_version_refs=None):
        from bioradar.delivery.analysis_diagnostics import build_analysis_diagnostics
        from bioradar.research.investigations.kdd import material_scope

        progress = row.progress
        policy_view = cls._policy_view(row.fixed_input, progress)
        result = assessment_result_view(row.result) if row.result else None
        if result is not None and policy_view is not None:
            # This read projection includes report HTTP receipts saved after retrieval.
            # The stored result, source text and original provider receipts stay immutable.
            result["query_policy"] = copy.deepcopy(policy_view)
        return AnalysisView(
            **cls._summary(row).model_dump(),
            is_demo=row.is_demo,
            input_hash=row.input_hash,
            prompt_version=row.fixed_input["prompt_version"],
            search_scope=row.fixed_input["search_scope"],
            evidence=progress.get("evidence", row.fixed_input["evidence"]),
            queries=[{k: v for k, v in q.items() if k != "records"} for q in progress.get("queries", [])],
            calls=[
                {k: v for k, v in call.items() if k not in {"payload", "output"}}
                for call in progress["calls"]
            ],
            result=AnalysisResult.model_validate(result) if result else None,
            initial_draft=progress.get("initial_draft"),
            limitations=progress.get("limitations", row.fixed_input["limitations"]),
            note_entry_id=row.note_entry_id,
            progress={
                "stage": progress.get("stage", row.state),
                "started_at": progress.get("started_at"),
                "updated_at": progress.get("updated_at"),
                "waiting_reason": row.error_code,
                "budget_pool": row.fixed_input.get("execution_budget", {}).get("pool"),
                "budget_wait": copy.deepcopy(progress.get("budget_wait")),
                "waiting_fetch": copy.deepcopy(progress.get("waiting_fetch")),
                **(
                    {"material_update_impact": copy.deepcopy(progress["material_update_impact"])}
                    if "material_update_impact" in progress
                    else {}
                ),
                "model_output_adjustments": copy.deepcopy(progress.get("model_output_adjustments", {})),
                "diagnostics": build_analysis_diagnostics(
                    {"id": row.id, "state": row.state, "result": result, "query_policy": policy_view},
                    progress=progress,
                    fixed=row.fixed_input,
                ),
                **(
                    {
                        "material_scope": material_scope(
                            row.fixed_input, progress, fixed_version_refs=fixed_version_refs
                        )
                    }
                    if (
                        row.fixed_input.get("query_policy", {}).get("schema_version")
                        == "query-planner.external.v2"
                    )
                    else {}
                ),
            },
            budget=analysis_budget(
                row.fixed_input,
                progress,
                datetime.fromisoformat(progress["finished_at"])
                if row.state in {"completed", "needs_review", "blocked", "failed"}
                and progress.get("finished_at")
                else datetime.now(UTC),
            ),
            providers=progress.get("providers", row.fixed_input.get("providers", {})),
            query_policy=policy_view,
        )

    def get(self, actor, investigation_id, identifier):
        with Session(self.engine.database) as session:
            row = self._row(session, actor, identifier)
            if row.investigation_id != investigation_id:
                raise DomainError("not_found", "机器分析不属于本调查", 404)
            fixed_version_refs = None
            if (
                row.fixed_input.get("query_policy", {}).get("schema_version") == "query-planner.external.v2"
                and "kdd_source_version_refs" not in row.progress
            ):
                # Old completed rows keep their saved model inputs. Only this read
                # projection gets the full, authorized frozen snapshot identities.
                fixed_version_refs = [
                    record["record_ref"]
                    for snapshot_id in row.fixed_input.get("snapshot_ids", [])
                    for record in self.engine._snapshot(session, actor, snapshot_id).payload["records"]
                ]
            return self._view(row, fixed_version_refs=fixed_version_refs)

    def list(self, actor, investigation_id, *, cursor=None, limit=20):
        from bioradar.adapters.reasoning_model import model_status

        status = model_status(self.settings)
        with Session(self.engine.database) as session:
            view = InvestigationService(self.engine).get(actor, investigation_id)
            statement = select(InvestigationAnalysisRecord).where(
                InvestigationAnalysisRecord.investigation_id == view.id,
                InvestigationAnalysisRecord.workspace_id == actor.workspace_id,
            )
            if cursor:
                boundary = session.get(InvestigationAnalysisRecord, cursor)
                if boundary is None or boundary.investigation_id != view.id:
                    raise DomainError("cursor_invalid", "机器分析翻页位置不属于本调查", 422)
                statement = statement.where(
                    tuple_(InvestigationAnalysisRecord.created_at, InvestigationAnalysisRecord.id)
                    < (boundary.created_at, boundary.id)
                )
            rows = list(
                session.scalars(
                    statement.order_by(
                        InvestigationAnalysisRecord.created_at.desc(), InvestigationAnalysisRecord.id.desc()
                    ).limit(limit + 1)
                )
            )
            items, unavailable = [], 0
            for row in rows[:limit]:
                try:
                    self._authorize(session, actor, row)
                except DomainError as exc:
                    if exc.code not in SOURCE_READ_UNAVAILABLE:
                        raise
                    unavailable += 1
                    continue
                items.append(self._summary(row))
        return AnalysisList(
            enabled=status.enabled,
            configured=status.configured,
            unavailable_reason=None if status.enabled and status.configured else "模型未配置或尚未启用",
            limits={
                **self._budget_limits(),
                "model_calls": self.settings.analysis_max_calls,
                "normal_model_calls": 3,  # Legacy generic reading/synthesis/check baseline.
                "conditional_revision_calls": 2,
                "support_audit_calls": 1,
                "queries": 2,
                "records_per_query": 3,
                "evidence_versions": 10,
                "evidence_characters": 40000,
                "manual_analyses_per_hour": self.settings.analysis_manual_per_hour,
            },
            retrieval_sources=self._search_scope(actor, view.is_demo),
            items=items,
            next_cursor=rows[limit - 1].id if len(rows) > limit else None,
            unavailable_count=unavailable,
        )

    def _initial_evidence(self, session, actor, view, selected_ids, *, allow_empty=False, limits=None):
        limits = limits or reading_limits({})
        # A machine-selected material still binds an original snapshot; machine notes do not.
        materials = [e for e in view.entries if e.kind == "material"]
        if selected_ids:
            selected = set(selected_ids)
            if len(selected) != len(selected_ids) or not selected.issubset({e.id for e in materials}):
                raise DomainError("analysis_basis_invalid", "依据须为本调查已有的外来材料", 422)
            materials = [e for e in materials if e.id in selected]
        materials.reverse()
        evidence, snapshots, dependencies, seen, limitations = [], set(), {}, set(), []
        for entry in materials:
            binding = entry.payload
            adopted = self.engine._snapshot(session, actor, binding.snapshot_id)
            snapshot = NormalizedSnapshot.model_validate(adopted.payload)
            record = next(r for r in snapshot.records if r.record_ref == binding.support.record_ref)
            identity = record.record_ref.version_key
            if identity in seen:
                continue
            seen.add(identity)
            if len(evidence) >= limits["max_versions"]:
                limitations.append(f"材料超过{limits['max_versions']}个版本，余下固定记录保留在补读清单。")
                break
            if not record.normalized_text:
                limitations.append("有材料缺少可供解读的授权文本，未补造内容。")
                continue
            snapshots.add(binding.snapshot_id)
            dependency = {
                "instance": record.source_instance,
                "source_id": record.source_id,
                "history": record.record_ref.provider == "data_fabric_history",
            }
            dependencies[canonical_hash(dependency)] = dependency
            raw_binding = None
            if dependency["history"]:
                source, native, demo = json.loads(record.record_ref.object_id)
                raw_binding = {
                    "source_id": source,
                    "source_record_id": native,
                    "version_id": record.record_ref.adopted_version,
                    "raw_sha256": record.record_ref.content_hash,
                    "is_demo": demo,
                }
            content = record.normalized_text[:6000]
            evidence.append(
                AnalysisEvidence(
                    key="e" + canonical_hash(record.record_ref)[:12],
                    material_entry_id=entry.id,
                    material_context={
                        "entry_id": entry.id,
                        "stance": binding.stance,
                        "note": binding.text,
                        "origin": "user_supplied",
                        "qualification": (
                            "用户提供的待验证上下文：立场和备注是用户的解释或待核问题，不是来源原文或已证结论。"
                            "须结合固定原件核对；oppose提示应检查反证，不能自动采信或改写来源。"
                        ),
                    }
                    if not getattr(binding, "machine_analysis_id", None)
                    else None,
                    evidence_role="graph_background"
                    if record.source_id in {"reviewed_graph", "primekg_graph"}
                    else "source_material",
                    snapshot_id=binding.snapshot_id,
                    record_ref=record.record_ref,
                    source_id=record.source_id,
                    history_binding=raw_binding,
                    title=record.source_url or record.record_ref.object_id,
                    text=content,
                    start=0,
                    end=len(content),
                    text_sha256=record.normalized_text_hash,
                    publication_time=record.published_time_raw,
                    source_url=record.source_url,
                    record_flags=list(record.record_flags),
                    limitations=["片段截取，未读取全文。"]
                    if len(content) < len(record.normalized_text)
                    else [],
                ).model_dump(mode="json")
            )
        if not evidence and not allow_empty:
            raise DomainError("analysis_no_material", "先加入至少一份有授权正文的材料，再开始机器分析", 422)
        return evidence, sorted(snapshots), list(dependencies.values()), sorted(set(limitations))

    def _manual_count(self, session, *, pool="manual"):
        return session.scalar(
            select(func.count())
            .select_from(InvestigationAnalysisRecord)
            .where(
                InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                InvestigationAnalysisRecord.trigger.in_(("manual", "material_update", "history")),
                func.coalesce(
                    InvestigationAnalysisRecord.fixed_input["execution_budget"]["pool"].astext, "manual"
                )
                == pool,
                func.coalesce(InvestigationAnalysisRecord.progress["manual_budget_pending"].astext, "false")
                != "true",
                func.coalesce(
                    cast(
                        InvestigationAnalysisRecord.progress["budget_reserved_at"].astext,
                        DateTime(timezone=True),
                    ),
                    InvestigationAnalysisRecord.created_at,
                )
                >= datetime.now(UTC) - timedelta(hours=1),
            )
        )

    def request_material_update(self, investigation_id):
        with Session(self.engine.database) as session:
            parent = session.scalar(
                select(InvestigationAnalysisRecord)
                .where(
                    InvestigationAnalysisRecord.investigation_id == investigation_id,
                    InvestigationAnalysisRecord.result.is_not(None),
                    InvestigationAnalysisRecord.state.in_(("completed", "needs_review")),
                )
                .order_by(InvestigationAnalysisRecord.created_at.desc())
                .limit(1)
            )
            latest = session.scalar(
                select(InvestigationAnalysisRecord)
                .where(InvestigationAnalysisRecord.investigation_id == investigation_id)
                .order_by(
                    InvestigationAnalysisRecord.created_at.desc(), InvestigationAnalysisRecord.id.desc()
                )
                .limit(1)
            )
            if (
                latest is None
                or latest.fixed_input.get("evidence_time", {}).get("mode", "current") != "current"
            ):
                return None
            parent_id = parent.id if parent else None
            basis = copy.deepcopy((parent or latest).fixed_input)
            question = basis["question"]
            prior_id = (parent or latest).id
            prior_revision = (parent or latest).based_on_revision
            prior_progress = copy.deepcopy((parent or latest).progress)
            prior_result = copy.deepcopy((parent or latest).result)
            latest_id, latest_state = latest.id, latest.state
        view = InvestigationService(self.engine).get(self.actor, investigation_id)
        if view.status != "open":
            return None
        prior_policy = basis.get("query_policy") or {}
        version = prior_policy.get("material_impact_version")
        updates = [
            entry
            for entry in view.entries
            if entry.kind == "material"
            and entry.revision > prior_revision
            and not entry.payload.machine_analysis_id
        ]
        if (
            version is not None
            and latest_state not in {"queued", "running"}
            and updates
            and all(
                entry.payload.automation is not None and entry.payload.stance == "context"
                for entry in updates
            )
        ):
            from bioradar.research.investigations.material_impact import evaluate_material_impact

            with Session(self.engine.database) as session:
                prior_sources = sorted(
                    set(basis.get("snapshot_ids", []))
                    | {item["snapshot_id"] for item in prior_progress.get("evidence", [])}
                )
                new_sources = sorted({entry.payload.snapshot_id for entry in updates})

                def records(identifiers):
                    return [
                        record
                        for identifier in identifiers
                        for record in NormalizedSnapshot.model_validate(
                            self.engine._snapshot(session, self.actor, identifier).payload
                        ).records
                    ]

                impact = evaluate_material_impact(
                    basis,
                    prior_progress,
                    prior_result,
                    [
                        {
                            "entry_id": entry.id,
                            "revision": entry.revision,
                            "new_ref": entry.payload.support.record_ref.model_dump(mode="json"),
                        }
                        for entry in updates
                    ],
                    records(prior_sources),
                    records(new_sources),
                )
            impact["parent_analysis_id"] = prior_id
            with Session(self.engine.database) as session, session.begin():
                saved = session.get(InvestigationAnalysisRecord, latest_id, with_for_update=True)
                if saved.state != latest_state or saved.state in {"queued", "running"}:
                    return None
                metadata = copy.deepcopy(saved.progress)
                metadata["material_update_impact"] = impact
                if impact["status"] != "affects_dependency":
                    metadata["material_update_checked_revision"] = impact["checked_revision"]
                saved.progress = metadata
            if impact["status"] != "affects_dependency":
                return None
        requested_budget = basis.get("requested_budget")
        body = AnalysisCreate(
            expected_revision=view.revision,
            parent_analysis_id=parent_id,
            question=question,
            auto_retrieve=bool(basis.get("retrieval", {}).get("auto_retrieve")),
            source_ids=sorted(set(basis.get("search_scope", [])) | set(basis.get("graph_scope", []))),
            query_policy=prior_policy.get("requested") or prior_policy.get("effective"),
            kdd_direction=basis.get("requested_direction") or prior_policy.get("direction"),
            budget=AnalysisBudget.model_validate(requested_budget) if requested_budget is not None else None,
        )
        key = "analysis-material-update:" + canonical_hash(
            ["inherited-request-v2", investigation_id, parent_id, body.model_dump(mode="json")]
        )
        return self.start(self.actor, investigation_id, body, key, defer_on_budget=True)[1]["id"]

    @staticmethod
    def _same_historical_context(left, right):
        if not left or not right or left.get("mode") == "current" or right.get("mode") == "current":
            return False
        try:
            same_cutoff = datetime.fromisoformat(left["cutoff"]) == datetime.fromisoformat(right["cutoff"])
        except (KeyError, TypeError, ValueError):
            return False
        return same_cutoff and all(
            left.get(key) == right.get(key) for key in ("study_id", "mode", "run_id", "history_request_id")
        )

    def start(
        self,
        actor,
        investigation_id,
        body,
        key,
        *,
        origin=None,
        defer_on_budget=False,
        historical_context=None,
    ):
        from bioradar.adapters.reasoning_model import model_status
        from bioradar.research.investigations.query_policy import freeze_policy

        if body.historical_scope is not None:
            if historical_context is not None:
                raise DomainError("analysis_history_invalid", "历史范围只能从一个入口指定", 422)
            if body.historical_scope.cutoff > datetime.now(UTC):
                raise DomainError("analysis_history_invalid", "历史时点不能晚于当前时间", 422)
            historical_context = {
                **body.historical_scope.model_dump(mode="json"),
                "history_request_id": canonical_hash(
                    [
                        investigation_id,
                        body.historical_scope.model_dump(mode="json"),
                    ]
                ),
            }
        policy = {**freeze_policy(self.settings, body.query_policy), "context_version": "semantic-v2"}
        evidence_time = {"mode": "current", "cutoff": datetime.now(UTC).isoformat()}
        if historical_context is not None:
            cutoff = historical_context["cutoff"]
            cutoff = datetime.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
            if cutoff.tzinfo is None or historical_context["mode"] not in {
                "current",
                "operational_replay",
                "public_history_reconstruction",
            }:
                raise DomainError("analysis_history_invalid", "历史分析须提供固定模式与带时区时点", 422)
            evidence_time = {
                "mode": historical_context["mode"],
                "cutoff": cutoff.isoformat(),
                "study_id": historical_context.get("study_id"),
                "run_id": historical_context.get("run_id"),
                **(
                    {"history_request_id": historical_context["history_request_id"]}
                    if historical_context.get("history_request_id")
                    else {}
                ),
            }
        time_identity = evidence_time if historical_context is not None else {"mode": "current"}
        status = model_status(self.settings)
        if not status.enabled or not status.configured:
            raise DomainError("analysis_not_configured", "机器分析尚未启用或模型未配置，未调用模型", 503)
        runtime = InvestigationService(self.engine)
        view = runtime.get(actor, investigation_id)
        if view.status != "open":
            raise DomainError("investigation_closed", "调查已暂停或关闭，未启动机器分析", 409)
        search_scope = self._search_scope(actor, view.is_demo)
        graph_scope = [] if historical_context else self._graph_scope(actor, view.is_demo)
        if body.source_ids:
            if not set(body.source_ids).issubset(set(search_scope) | set(graph_scope)):
                raise DomainError("analysis_query_invalid", "初查来源不在当前可补查许可范围", 422)
            search_scope = sorted(set(body.source_ids) & set(search_scope))
            graph_scope = sorted(set(body.source_ids) & set(graph_scope))
        if any(query.source_id not in search_scope for query in body.initial_queries):
            raise DomainError("analysis_query_invalid", "初始查询超出本次来源范围", 422)
        if body.initial_queries and not body.auto_retrieve:
            raise DomainError("analysis_query_invalid", "初始查询须启用自动取资料", 422)
        budget = self._budget_limits(body.budget)
        if len(body.initial_queries) > min(2, budget["max_queries"]):
            raise DomainError("analysis_query_budget", "初始查询最多两项，且不得超过本次查询预算", 422)
        question = body.question or view.question
        if policy.get("schema_version") == "query-planner.external.v2":
            from bioradar.research.investigations.kdd import direction_for

            policy["direction"] = direction_for(question, getattr(body, "kdd_direction", None))
            policy["direction_basis"] = (
                "explicit" if getattr(body, "kdd_direction", None) else "question_rule"
            )
            if view.origin is not None:
                policy["mining_origin"] = view.origin.model_dump(mode="json")
        risk_scope = (origin or {}).get("topic_risk_scope")
        if risk_scope is not None and policy.get("final_assessment_version"):
            # A fresh observation/scope must not deduplicate to a previous risk decision.
            policy["topic_risk_scope_sha256"] = canonical_hash(risk_scope)
        providers = self._provider_status(
            kdd=policy.get("schema_version") == "query-planner.external.v2",
            direction=policy.get("direction", "research"),
            is_demo=view.is_demo,
        )
        new_research = policy.get("research_contract_version") == "bioradar-research-input-v1"
        read_limits = (
            {
                "max_versions": self.settings.analysis_v2_max_evidence,
                "max_characters": self.settings.analysis_v2_max_characters,
                "search_limit": self.settings.analysis_v2_records_per_query,
            }
            if new_research
            else reading_limits({})
        )
        budget_pool = origin["source"] if origin else "material_update" if defer_on_budget else "manual"
        retrieval = {
            "auto_retrieve": body.auto_retrieve,
            "initial_queries": [q.model_dump(mode="json") for q in body.initial_queries],
            "budget": budget,
        }
        if new_research:
            retrieval["reading_limits"] = copy.deepcopy(read_limits)
        retrieval["budget_pool"] = budget_pool
        with Session(self.engine.database) as session:
            evidence, snapshots, dependencies, limitations = self._initial_evidence(
                session,
                actor,
                view,
                body.material_entry_ids,
                allow_empty=body.auto_retrieve,
                limits=read_limits,
            )
            parent = None
            if origin:
                previous_origin = session.scalar(
                    select(InvestigationAnalysisRecord)
                    .where(
                        InvestigationAnalysisRecord.workspace_id == actor.workspace_id,
                        InvestigationAnalysisRecord.fixed_input["origin"]["reservation_key"].astext
                        == origin["reservation_key"],
                    )
                    .limit(1)
                )
                if previous_origin is not None:
                    self._authorize(session, actor, previous_origin)
                    return 200, self._view(previous_origin).model_dump(mode="json")
            retry = None
            if body.retry_of_analysis_id:
                retry = self._row(session, actor, body.retry_of_analysis_id)
                if retry.investigation_id != view.id or retry.state not in {
                    "blocked",
                    "failed",
                    "needs_review",
                }:
                    raise DomainError("analysis_retry_invalid", "仅可明确另起一次未成功或待核的尝试", 409)
                if body.question is None:
                    question = retry.fixed_input["question"]
            parent_id = body.parent_analysis_id
            if retry is not None and parent_id is None:
                parent_id = retry.parent_analysis_id
            if parent_id:
                parent = self._row(session, actor, parent_id)
                if parent.investigation_id != view.id or parent.result is None:
                    raise DomainError("analysis_parent_invalid", "父分析须为本调查已有的完成草稿", 422)
            elif retry is None:
                parent = session.scalar(
                    select(InvestigationAnalysisRecord)
                    .where(
                        InvestigationAnalysisRecord.investigation_id == view.id,
                        InvestigationAnalysisRecord.result.is_not(None),
                        InvestigationAnalysisRecord.state.in_(("completed", "needs_review")),
                    )
                    .order_by(InvestigationAnalysisRecord.created_at.desc())
                    .limit(1)
                )
            if historical_context is not None:
                if retry is not None and not self._same_historical_context(
                    retry.fixed_input.get("evidence_time"), evidence_time
                ):
                    raise DomainError("analysis_retry_invalid", "重试与固定历史研究时点不相容", 422)
                if parent is not None and not self._same_historical_context(
                    parent.fixed_input.get("evidence_time"), evidence_time
                ):
                    if body.parent_analysis_id or retry is not None:
                        raise DomainError("analysis_parent_invalid", "父版不属于同一固定历史研究时点", 422)
                    # Automatic historical comparison can only see the same historical input.
                    candidates = session.scalars(
                        select(InvestigationAnalysisRecord)
                        .where(
                            InvestigationAnalysisRecord.investigation_id == view.id,
                            InvestigationAnalysisRecord.result.is_not(None),
                            InvestigationAnalysisRecord.state.in_(("completed", "needs_review")),
                        )
                        .order_by(InvestigationAnalysisRecord.created_at.desc())
                    )
                    parent = next(
                        (
                            candidate
                            for candidate in candidates
                            if self._same_historical_context(
                                candidate.fixed_input.get("evidence_time"), evidence_time
                            )
                        ),
                        None,
                    )
            inherited = retry or parent
            if (
                historical_context is None
                and inherited is not None
                and inherited.fixed_input.get("evidence_time", {}).get("mode", "current") != "current"
            ):
                graph_scope = []
                evidence_time = copy.deepcopy(inherited.fixed_input["evidence_time"])
                historical_context = copy.deepcopy(evidence_time)
                time_identity = evidence_time
                if body.source_ids and not set(body.source_ids).issubset(
                    inherited.fixed_input["search_scope"]
                ):
                    raise DomainError("analysis_query_invalid", "历史追问不得扩大已冻结来源", 422)
                search_scope = sorted(set(search_scope) & set(inherited.fixed_input["search_scope"]))
                admitted = {canonical_hash(e["record_ref"]) for e in inherited.progress.get("evidence", [])}
                evidence = [e for e in evidence if canonical_hash(e["record_ref"]) in admitted]
            if evidence_time["mode"] != "current":
                if "fabric_fetch" in providers:
                    providers["fabric_fetch"]["available"] = False
                providers["weknora"] = {
                    **providers["weknora"],
                    "available": False,
                    "status": "history_current_index_unavailable",
                }
                admitted = [item for item in evidence if item.get("history_binding")]
                if len(admitted) != len(evidence):
                    limitations.append("历史分析已排除没有固定历史原件身份的材料。")
                evidence = admitted
            parameters = model_parameters(self.settings)
            if self.settings.analysis_token_stage_ratios:
                parameters["token_calibration"] = {
                    "version": self.settings.analysis_token_calibration_version,
                    "ratios": dict(self.settings.analysis_token_stage_ratios),
                    "margin": 1.2,
                    "model": parameters["model"],
                    "model_revision": parameters["model_revision"],
                }
            parameters["min_send_seconds"] = min(
                self.settings.analysis_min_call_seconds, parameters["timeout_seconds"]
            )
            transport_parameters = {
                key: value for key, value in parameters.items() if key not in {"model", "model_revision"}
            }
            signature = canonical_hash(
                [
                    question,
                    retrieval,
                    providers,
                    evidence_basis_identity(evidence),
                    parameters,
                    search_scope,
                    graph_scope,
                    policy,
                    time_identity,
                ]
            )
            completed_signature = None
            if (
                parent is not None
                and {canonical_hash([item["record_ref"], item["snapshot_id"]]) for item in evidence}
                == {
                    canonical_hash([item["record_ref"], item["snapshot_id"]])
                    for item in parent.progress.get("evidence", [])
                }
                and material_context_identity(evidence)
                == material_context_identity(parent.progress.get("evidence", []))
            ):
                # Automatic originals are part of the parent's completed basis, even
                # when they were not present at its initial admission.
                previous = parent.fixed_input
                completed_signature = canonical_hash(
                    [
                        previous["question"],
                        previous.get("retrieval", {}),
                        previous.get("providers", {}),
                        evidence_basis_identity(evidence),
                        {
                            **normalize_model_parameters(
                                {
                                    "model": previous["model"],
                                    "model_revision": previous["model_revision"],
                                    **previous["model_parameters"],
                                }
                            ),
                            **(
                                {"min_send_seconds": previous["model_parameters"]["min_send_seconds"]}
                                if "min_send_seconds" in previous["model_parameters"]
                                else {}
                            ),
                        },
                        previous["search_scope"],
                        previous.get("graph_scope", []),
                        previous.get("query_policy"),
                        previous.get("evidence_time")
                        if previous.get("evidence_time", {}).get("mode", "current") != "current"
                        else {"mode": "current"},
                    ]
                )
            reuse_id = None
            if (
                parent is not None
                and not body.retry_of_analysis_id
                and signature in {parent.fixed_input["basis_signature"], completed_signature}
                and parent.fixed_input["version"] == ANALYSIS_VERSION
                and parent.fixed_input["prompt_version"] == PROMPT_VERSION
            ):
                self._authorize(session, actor, parent)
                reuse_id = parent.id
            if parent:
                self._authorize(session, actor, parent)
                snapshots = sorted(set(snapshots) | set(parent.fixed_input["snapshot_ids"]))
                for d in parent.fixed_input["dependency_sources"]:
                    if d not in dependencies:
                        dependencies.append(d)
            if origin and origin["source"] == "monitor":
                run = self.engine._run(session, actor, origin["run_id"])
                adopted = self.engine._snapshot(session, actor, run.snapshot_id)
                snapshots = sorted(set([*snapshots, adopted.id]))
                for record in NormalizedSnapshot.model_validate(adopted.payload).records:
                    dependency = {
                        "instance": record.source_instance,
                        "source_id": record.source_id,
                        "history": record.record_ref.provider == "data_fabric_history",
                    }
                    if dependency not in dependencies:
                        dependencies.append(dependency)
            if origin and origin["source"] == "topic":
                from bioradar.topics.service import TopicService

                evaluation, scope = TopicService(self.engine)._evaluation_row(
                    session, actor, origin["topic_id"], origin["evaluation_id"]
                )
                for source in set(evaluation.result.get("dependency_source_ids", [])) | set(
                    scope.definition["source_ids"]
                ):
                    dependency = {
                        "instance": scope.definition["source_instance"],
                        "source_id": source,
                        "history": True,
                    }
                    if dependency not in dependencies:
                        dependencies.append(dependency)
            parent_id = parent.id if parent else None
            input_hash = canonical_hash(
                [
                    basis_hash(
                        question,
                        evidence,
                        parent_id,
                        self.settings.semantic_llm_model,
                        self.settings.semantic_llm_model_revision,
                        search_scope,
                    ),
                    transport_parameters,
                    providers,
                    retrieval,
                    body.retry_of_analysis_id,
                    graph_scope,
                    policy,
                    time_identity,
                ]
            )
            max_calls = budget["max_calls"]
            fixed = {
                "question": question,
                "retrieval": retrieval,
                "budget": budget,
                **({"reading_limits": read_limits} if new_research else {}),
                "execution_budget": {
                    "version": "automatic-buckets-v1",
                    "pool": budget_pool,
                    "topic_id": (origin or {}).get("topic_id"),
                    "automatic_daily_tokens": self.settings.automatic_daily_model_tokens,
                    "topic_daily_tokens": self.settings.topic_daily_model_tokens,
                    "day_timezone": "UTC",
                },
                "requested_budget": body.budget.model_dump(exclude_none=True) if body.budget else None,
                "requested_direction": body.kdd_direction,
                "providers": providers,
                "basis_signature": signature,
                "as_of": evidence_time["cutoff"],
                "execution_version": "query-step-v1",
                "query_context_version": "semantic-v2",
                "query_policy": policy,
                "evidence_time": evidence_time,
                "version": ANALYSIS_VERSION,
                "prompt_version": PROMPT_VERSION,
                "evidence": evidence,
                "snapshot_ids": snapshots,
                "dependency_sources": dependencies,
                "search_scope": search_scope,
                "graph_scope": graph_scope,
                "parent_result": copy.deepcopy(parent.result) if parent else None,
                "origin": copy.deepcopy(origin),
                **(
                    {"kdd_origin": copy.deepcopy(policy.get("mining_origin"))}
                    if policy.get("schema_version") == "query-planner.external.v2"
                    else {}
                ),
                "model": parameters["model"],
                "model_revision": parameters["model_revision"],
                "model_parameters": {
                    **transport_parameters,
                    "max_calls": max_calls,
                    "job_timeout_seconds": budget["max_seconds"] + 60,
                },
                "limitations": [*limitations, "本次独立补查范围不改变专题关注范围；检索不是全网。"],
                "retry_of_analysis_id": body.retry_of_analysis_id,
            }
        with Session(self.engine.database) as session, session.begin():
            # Serialize the small manual admission check, never an external operation.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": actor.workspace_id + ":investigation-analysis"},
            )

            def mutate():
                if origin:
                    prior = session.scalar(
                        select(InvestigationAnalysisRecord)
                        .where(
                            InvestigationAnalysisRecord.workspace_id == actor.workspace_id,
                            InvestigationAnalysisRecord.fixed_input["origin"]["reservation_key"].astext
                            == origin["reservation_key"],
                        )
                        .limit(1)
                    )
                    if prior is not None:
                        self._authorize(session, actor, prior)
                        return 200, self._view(prior).model_dump(mode="json")
                if reuse_id:
                    existing = self._row(session, actor, reuse_id)
                    return 200, self._view(existing).model_dump(mode="json")
                existing = session.scalar(
                    select(InvestigationAnalysisRecord).where(
                        InvestigationAnalysisRecord.workspace_id == actor.workspace_id,
                        InvestigationAnalysisRecord.investigation_id == investigation_id,
                        InvestigationAnalysisRecord.input_hash == input_hash,
                    )
                )
                if existing is not None:
                    self._authorize(session, actor, existing)
                    return 200, self._view(existing).model_dump(mode="json")
                row, _ = runtime._row(session, actor, investigation_id, lock=True)
                if row.revision != body.expected_revision:
                    raise DomainError("revision_conflict", "调查已更新，请读最新材料后另行确认", 409)
                budget_pending = False
                if origin is None:
                    from bioradar.research.monitor.models import MonitorState

                    monitor_control = session.get(MonitorState, actor.workspace_id)
                    paused = monitor_control is not None and monitor_control.paused
                    if paused and not defer_on_budget:
                        raise DomainError("analysis_paused", "后台已暂停，尚未预约新的机器分析", 409)
                    if (
                        paused
                        or self._manual_count(session, pool=budget_pool)
                        >= self.settings.analysis_manual_per_hour
                    ):
                        if not defer_on_budget:
                            raise DomainError("analysis_manual_budget", "本小时手动机器分析额度已用尽", 429)
                        budget_pending = True
                else:
                    reservation = session.scalar(
                        select(EngineIdempotency).where(
                            EngineIdempotency.workspace_id == actor.workspace_id,
                            EngineIdempotency.actor_id == actor.actor_id,
                            EngineIdempotency.operation == "monitor.dig.start",
                            EngineIdempotency.key == origin["reservation_key"],
                        )
                    )
                    if reservation is None or reservation.response_code is None:
                        raise DomainError(
                            "analysis_reservation_required", "自动研判缺少既有调查预算回执", 409
                        )
                identifier = str(uuid4())
                saved = InvestigationAnalysisRecord(
                    id=identifier,
                    workspace_id=actor.workspace_id,
                    investigation_id=investigation_id,
                    parent_analysis_id=parent_id,
                    input_hash=input_hash,
                    state="queued",
                    trigger=origin["source"]
                    if origin
                    else (
                        "history"
                        if historical_context
                        else "material_update"
                        if parent or defer_on_budget
                        else "manual"
                    ),
                    is_demo=view.is_demo,
                    based_on_revision=row.revision,
                    fixed_input=fixed,
                    progress={
                        "calls": [],
                        "queries": [],
                        "evidence": evidence,
                        "limitations": fixed["limitations"],
                        "expected_revision": row.revision,
                        "budget_reserved_at": None if budget_pending else datetime.now(UTC).isoformat(),
                        "manual_budget_pending": budget_pending,
                        "tool_usage": {"tool_calls": 0},
                        "query_policy": {
                            "phase": "initial",
                            "checks": [],
                            "decisions": [],
                            "pending_action": None,
                            "reading": None,
                            "reading_evidence_hash": None,
                            "status": "running",
                        },
                    },
                    error_code="analysis_manual_budget" if budget_pending else None,
                    dispatch_pending=True,
                    rq_job_id="investigation-analysis-" + identifier,
                )
                session.add(saved)
                session.flush()
                return 202, self._view(saved).model_dump(mode="json")

            code, payload = self.engine._idempotent(
                session, actor, "investigation.analysis:" + investigation_id, key, body, mutate
            )
            self._row(session, actor, payload["id"])
        self.dispatch(payload["id"])
        return code, payload

    def dispatch(self, identifier):
        if self.settings.redis_url is None:
            return False
        with Session(self.engine.database) as session:
            row = session.get(InvestigationAnalysisRecord, identifier)
            if (
                row is None
                or row.state != "queued"
                or row.progress.get("manual_budget_pending")
                or row.progress.get("budget_wait")
                or row.progress.get("waiting_fetch")
                or not self.settings.semantic_llm_enabled
            ):
                return False
            from bioradar.research.monitor.models import MonitorState

            monitor = session.get(MonitorState, self.actor.workspace_id)
            if monitor and monitor.paused:
                return False
            if row.fixed_input.get("origin") and not semantic_enabled(self.settings):
                return False
            job_id, input_hash = row.rq_job_id, row.input_hash
            job_timeout = execution_seconds(row.fixed_input)
        connection = Redis.from_url(
            self.settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
        )
        try:
            try:
                job = Job.fetch(job_id, connection=connection)
                state = str(getattr(job.get_status(), "value", job.get_status()))
                if state not in {"queued", "started", "deferred", "scheduled"}:
                    # Only a known finished local job gets another dispatch id; input/stages stay fixed.
                    with Session(self.engine.database) as session, session.begin():
                        row = session.get(InvestigationAnalysisRecord, identifier, with_for_update=True)
                        if row.state == "queued" and row.rq_job_id == job_id:
                            row.rq_job_id = (
                                "investigation-analysis-" + identifier + "-" + secrets.token_hex(4)
                            )
                    return False
            except NoSuchJobError:
                Queue(self.settings.queue_prefix + ":routine", connection=connection).enqueue(
                    execute_analysis,
                    identifier,
                    input_hash,
                    job_id=job_id,
                    job_timeout=job_timeout,
                    result_ttl=86400,
                    failure_ttl=604800,
                )
            with Session(self.engine.database) as session, session.begin():
                row = session.get(InvestigationAnalysisRecord, identifier, with_for_update=True)
                row.dispatch_pending = False
            return True
        except Exception:
            return False
        finally:
            connection.close()

    def recover(self, limit=4):
        if self.settings.semantic_llm_enabled:
            with Session(self.engine.database) as session:
                latest = (
                    select(
                        InvestigationAnalysisRecord.investigation_id.label("id"),
                        func.max(InvestigationAnalysisRecord.based_on_revision).label("revision"),
                        func.max(
                            cast(
                                InvestigationAnalysisRecord.progress["material_update_checked_at"].astext,
                                DateTime(timezone=True),
                            )
                        ).label("checked"),
                        func.max(
                            func.coalesce(
                                cast(
                                    InvestigationAnalysisRecord.progress[
                                        "material_update_checked_revision"
                                    ].astext,
                                    Integer,
                                ),
                                0,
                            )
                        ).label("impact_revision"),
                    )
                    .where(InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id)
                    .group_by(InvestigationAnalysisRecord.investigation_id)
                    .subquery()
                )
                material = (
                    select(
                        InvestigationEntryRecord.investigation_id.label("id"),
                        func.max(InvestigationEntryRecord.revision).label("revision"),
                    )
                    .where(
                        InvestigationEntryRecord.kind == "material",
                        InvestigationEntryRecord.payload["machine_analysis_id"].astext.is_(None),
                        InvestigationEntryRecord.investigation_id.in_(
                            select(InvestigationRecord.id).where(
                                InvestigationRecord.workspace_id == self.actor.workspace_id
                            )
                        ),
                    )
                    .group_by(InvestigationEntryRecord.investigation_id)
                    .subquery()
                )
                changed = list(
                    session.scalars(
                        select(latest.c.id)
                        .join(material, latest.c.id == material.c.id)
                        .where(
                            material.c.revision > func.greatest(latest.c.revision, latest.c.impact_revision)
                        )
                        .order_by(
                            latest.c.checked.asc().nullsfirst(), material.c.revision.desc(), latest.c.id
                        )
                        .limit(limit)
                    )
                )
            for investigation_id in changed:
                try:
                    self.request_material_update(investigation_id)
                except DomainError:
                    pass
                finally:
                    with Session(self.engine.database) as session, session.begin():
                        checked = session.scalar(
                            select(InvestigationAnalysisRecord)
                            .where(
                                InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                                InvestigationAnalysisRecord.investigation_id == investigation_id,
                            )
                            .order_by(
                                InvestigationAnalysisRecord.created_at.desc(),
                                InvestigationAnalysisRecord.id.desc(),
                            )
                            .limit(1)
                            .with_for_update()
                        )
                        if checked is not None:
                            metadata = copy.deepcopy(checked.progress)
                            metadata["material_update_checked_at"] = datetime.now(UTC).isoformat()
                            checked.progress = metadata
        now = datetime.now(UTC)
        with Session(self.engine.database) as session, session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": self.actor.workspace_id + ":investigation-analysis"},
            )
            waiting = list(
                session.scalars(
                    select(InvestigationAnalysisRecord)
                    .where(
                        InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                        InvestigationAnalysisRecord.state == "queued",
                        InvestigationAnalysisRecord.progress["manual_budget_pending"].astext == "true",
                    )
                    .order_by(InvestigationAnalysisRecord.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            daily_waiting = list(
                session.scalars(
                    select(InvestigationAnalysisRecord)
                    .where(
                        InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                        InvestigationAnalysisRecord.state == "queued",
                        InvestigationAnalysisRecord.progress["budget_wait"]["reason"].astext == WAIT_CODE,
                    )
                    .order_by(InvestigationAnalysisRecord.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            fetch_waiting = list(
                session.scalars(
                    select(InvestigationAnalysisRecord)
                    .where(
                        InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                        InvestigationAnalysisRecord.state == "queued",
                        InvestigationAnalysisRecord.progress["waiting_fetch"]["next_poll_at"].astext.is_not(
                            None
                        ),
                    )
                    .order_by(InvestigationAnalysisRecord.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            from bioradar.research.monitor.models import MonitorState

            control = session.get(MonitorState, self.actor.workspace_id)
            if self.settings.semantic_llm_enabled and not (control is not None and control.paused):
                for pending in daily_waiting:
                    resumed = copy.deepcopy(pending.progress)
                    if finish_budget_wait(resumed, now):
                        pending.progress = resumed
                        pending.dispatch_pending = True
                        pending.error_code = None
            if self.settings.semantic_llm_enabled and not (control is not None and control.paused):
                for pending in fetch_waiting:
                    resumed = copy.deepcopy(pending.progress)
                    if datetime.fromisoformat(resumed["waiting_fetch"]["next_poll_at"]) <= now:
                        resumed.pop("waiting_fetch")
                        pending.progress, pending.dispatch_pending, pending.error_code = resumed, True, None
            for pending in waiting:
                if not self.settings.semantic_llm_enabled or control is not None and control.paused:
                    break
                inquiry = session.get(InvestigationRecord, pending.investigation_id)
                if inquiry is None or inquiry.status != "open":
                    pending.state, pending.error_code = "blocked", "investigation_closed"
                    continue
                if (
                    self._manual_count(
                        session, pool=pending.fixed_input.get("execution_budget", {}).get("pool", "manual")
                    )
                    >= self.settings.analysis_manual_per_hour
                ):
                    continue
                state = copy.deepcopy(pending.progress)
                state.update(manual_budget_pending=False, budget_reserved_at=now.isoformat())
                pending.progress, pending.error_code = state, None
                session.flush()
            rows = list(
                session.scalars(
                    select(InvestigationAnalysisRecord)
                    .where(
                        InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                        InvestigationAnalysisRecord.state == "running",
                        InvestigationAnalysisRecord.lease_until < now,
                    )
                    .order_by(InvestigationAnalysisRecord.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                uncertain = [c for c in row.progress["calls"] if c["status"] in {"sent", "unconfirmed"}]
                query_uncertain = row.fixed_input.get("execution_version") == "query-step-v1" and any(
                    q["status"] in {"sent", "unconfirmed"} for q in row.progress.get("queries", [])
                )
                comparison_only = bool(
                    uncertain
                    and all(c["stage"] == "compare" for c in uncertain)
                    and row.result
                    and row.result["review"]["status"] == "passed"
                )
                row.state = (
                    "needs_review"
                    if comparison_only
                    else ("blocked" if uncertain or query_uncertain else "queued")
                )
                row.error_code = (
                    "model_call_outcome_unconfirmed"
                    if uncertain
                    else "analysis_query_outcome_unconfirmed"
                    if query_uncertain
                    else None
                )
                if query_uncertain:
                    state = copy.deepcopy(row.progress)
                    policy_state = state.setdefault("query_policy", {})
                    policy_state.update(status="blocked", reason=row.error_code)
                    for index, query in enumerate(state.get("queries", [])):
                        if query["status"] not in {"sent", "unconfirmed"}:
                            continue
                        query.update(status="unconfirmed", error_code=row.error_code)
                        for decision in policy_state.get("decisions", []):
                            if (decision.get("action") or {}).get("id") == query.get("action_id"):
                                decision["receipt"] = {
                                    "action_id": query["action_id"],
                                    "query_index": index,
                                    "status": query["status"],
                                    "error_code": row.error_code,
                                    "evidence_ids": list(query.get("evidence_keys", [])),
                                    "cost": copy.deepcopy(query.get("cost")),
                                }
                    row.progress = state
                    if row.result is not None:
                        saved_result = copy.deepcopy(row.result)
                        saved_result["query_policy"] = self._policy_view(row.fixed_input, state)
                        row.result = saved_result
                if comparison_only:
                    state = copy.deepcopy(row.progress)
                    state["comparison_error_code"] = row.error_code
                    row.progress, row.completed_at = state, now
                row.execution_token, row.lease_until, row.dispatch_pending = (
                    None,
                    None,
                    not (uncertain or query_uncertain),
                )
            queued = list(
                session.scalars(
                    select(InvestigationAnalysisRecord.id)
                    .where(
                        InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                        InvestigationAnalysisRecord.state == "queued",
                    )
                    .order_by(InvestigationAnalysisRecord.created_at)
                    .limit(limit)
                )
            )
        return sum(bool(self.dispatch(identifier)) for identifier in queued)

    def _guard(self, identifier, token, *, check_deadline=False):
        # Time limits stop new upstream work, while confirmed receipts can still be saved locally.
        from bioradar.research.monitor.models import MonitorState

        self.engine._history_source_ids = None
        self.engine._history_permission_failure = None
        with Session(self.engine.database) as session:
            row = self._row(session, self.actor, identifier)
            if not self.settings.semantic_llm_enabled:
                raise DomainError("analysis_paused", "语义功能已停用，回执保留，未继续后续动作", 409)
            if (
                row.fixed_input["version"] != ANALYSIS_VERSION
                or row.fixed_input["prompt_version"] != PROMPT_VERSION
            ):
                raise DomainError(
                    "analysis_model_changed", "分析或提示版本已改变，旧回执保留，请明确另起尝试", 409
                )
            frozen = normalize_model_parameters(
                {
                    "model": row.fixed_input["model"],
                    "model_revision": row.fixed_input["model_revision"],
                    **row.fixed_input["model_parameters"],
                }
            )
            if frozen != model_parameters(self.settings):
                raise DomainError(
                    "analysis_model_changed", "模型配置已改变，回执保留，请明确另起一次尝试", 409
                )
            if any(c["status"] in {"sent", "unconfirmed"} for c in row.progress.get("calls", [])):
                raise DomainError(
                    "model_call_outcome_unconfirmed", "已有模型请求结果未知，未继续其他动作", 409
                )
            budget = analysis_budget(row.fixed_input, row.progress, datetime.now(UTC))
            remaining_seconds = budget["usage"]["remaining_seconds"]
            if check_deadline and remaining_seconds <= 0:
                raise DomainError("analysis_time_budget", "本轮研究达到固定时间预算，已保留进度", 409)
            if check_deadline and self.reader is not None:
                self.reader.deadline = min(self.reader.deadline, time.monotonic() + remaining_seconds)
            if (
                row.execution_token != token
                or row.state != "running"
                or row.lease_until is None
                or row.lease_until <= datetime.now(UTC)
            ):
                raise DomainError("analysis_execution_lost", "此机器分析已由其他执行接管", 409)
            investigation = InvestigationService(self.engine).get(self.actor, row.investigation_id)
            expected_revision = row.progress.get("expected_revision", row.based_on_revision)
            for intent in row.progress.get("append_intents", []):
                saved = self._saved("investigation.append:" + row.investigation_id, intent["key"])
                if saved:
                    expected_revision = max(expected_revision, saved["revision"])
            if investigation.revision != expected_revision:
                raise DomainError("revision_conflict", "调查材料或人工记录已变化，本版停止并保留回执", 409)
            if investigation.status != "open":
                raise DomainError("investigation_closed", "调查已暂停或关闭，停止后续自动动作", 409)
            monitor = session.get(MonitorState, self.actor.workspace_id)
            if monitor and monitor.paused:
                raise DomainError("analysis_paused", "后台已暂停，模型回执保留，未继续后续动作", 409)
            origin = row.fixed_input.get("origin")
            if origin:
                if not semantic_enabled(self.settings):
                    raise DomainError("analysis_paused", "自动机器分析已暂停，回执保留", 409)
                if origin["source"] == "topic":
                    from bioradar.topics.models import TopicEvaluationRecord, TopicScopeRecord

                    evaluation = session.get(TopicEvaluationRecord, origin["evaluation_id"])
                    scope = session.get(TopicScopeRecord, evaluation.scope_id) if evaluation else None
                    if scope is None or not scope.is_current or scope.paused:
                        raise DomainError("analysis_paused", "专题暂停或范围已更新，未继续后续动作", 409)
            return copy.deepcopy(row.fixed_input), copy.deepcopy(row.progress)

    @staticmethod
    def _sync_query_receipts(progress):
        """Keep the public decision trace aligned even when adoption or sending stops."""
        decisions = progress.get("query_policy", {}).get("decisions", [])
        for index, receipt in enumerate(progress.get("queries", [])):
            if receipt.get("status") == "prepared" or not receipt.get("action_id"):
                continue
            decision = next(
                (item for item in decisions if (item.get("action") or {}).get("id") == receipt["action_id"]),
                None,
            )
            if decision is not None:
                decision["receipt"] = {
                    "action_id": receipt["action_id"],
                    "query_index": index,
                    "status": receipt["status"],
                    "error_code": receipt.get("error_code"),
                    "evidence_ids": list(receipt.get("evidence_keys", [])),
                    "cost": copy.deepcopy(receipt.get("cost")),
                    **(
                        {
                            "new_artifact_refs": copy.deepcopy(receipt.get("artifact_refs", [])),
                            "check_updates": copy.deepcopy(receipt.get("check_updates", [])),
                        }
                        if receipt.get("kind") in {"dataset_prepare", "analysis_run", "professional_assess"}
                        else {}
                    ),
                }

    def _checkpoint(self, identifier, token, progress, *, fixed=None, result=None):
        self._sync_query_receipts(progress)
        # A received model response must survive pause/revocation before any further business write.
        with Session(self.engine.database) as session, session.begin():
            row = session.get(InvestigationAnalysisRecord, identifier, with_for_update=True)
            if row is None or row.execution_token != token:
                raise DomainError("analysis_execution_lost", "执行资格已变化，未覆盖机器回执", 409)
            progress["updated_at"] = datetime.now(UTC).isoformat()
            saved_progress = copy.deepcopy(progress)
            if row.progress.get("material_update_checked_at"):
                saved_progress["material_update_checked_at"] = row.progress["material_update_checked_at"]
            row.progress = saved_progress
            if fixed is not None:
                row.fixed_input = copy.deepcopy(fixed)
            if result is not None:
                row.result = copy.deepcopy(result)

    def _prepare(self, identifier, token, fixed, progress):
        if progress.get("prepared"):
            return progress
        remaining = reading_limits(fixed)["max_characters"]
        evidence = []
        for item in fixed["evidence"]:
            self._guard(identifier, token, check_deadline=True)
            value = copy.deepcopy(item)
            if value.get("history_binding") and self.reader is not None:
                mode, cutoff = self._evidence_time(fixed)
                before = self._tool_start(fixed, progress)
                try:
                    original = self.reader.read_version(value["history_binding"], mode=mode, cutoff=cutoff)
                finally:
                    spent = self._tool_finish(progress, before)
                    usage = progress.setdefault("tool_usage", {})
                    usage["preparation_tool_calls"] = usage.get("preparation_tool_calls", 0) + spent
                    self._checkpoint(identifier, token, progress)
                if original["raw_sha256"] != value["history_binding"]["raw_sha256"]:
                    raise DomainError("analysis_source_mismatch", "固定原件哈希不一致", 502)
                from bioradar.adapters.query_graph import remember_graph_package

                remember_graph_package(fixed, progress, original)
                value.update(source_excerpt(original, remaining))
                value["title"] = original["title"]
            full_length = len(value["text"])
            field_characters = len(json.dumps(value.get("facts", {}), ensure_ascii=False))
            value["text"] = value["text"][: max(0, min(6000, remaining - field_characters))]
            value["end"] = value["start"] + len(value["text"])
            remaining -= len(value["text"]) + field_characters
            if len(value["text"]) < full_length:
                value["limitations"].append("按本轮字符预算截取；未读取的内容不作为已核证据。")
            if not value["text"]:
                progress["limitations"].append("材料字符预算已满，部分依据未输入模型。")
                continue
            evidence.append(freeze_passages(value))
        progress["evidence"], progress["prepared"] = evidence, True
        self._checkpoint(identifier, token, progress)
        return progress

    @staticmethod
    def _report_context(fixed, progress, result, *, freeze=False):
        context = report_context(result, progress.get("mechanism_evaluation"))
        if not (
            fixed.get("query_context_version") == "semantic-v2"
            and fixed.get("query_policy", {}).get("effective") == "query_planner"
        ):
            return context
        context.update(material_context_payload(progress.get("evidence", [])))
        round_key = str(progress.get("active_report_round", 0))
        saved = progress.get("report_research_scopes", {})
        if round_key in saved:
            context["research_scope"] = {
                key: copy.deepcopy(value)
                for key, value in saved[round_key].items()
                if key != "analysis_results"
            }
            if "analysis_results" in saved[round_key]:
                context["analysis_results"] = copy.deepcopy(saved[round_key]["analysis_results"])
            for name in ("source_updates", "unresolved_source_updates"):
                if name in saved[round_key]:
                    context[name] = copy.deepcopy(saved[round_key][name])
            return context
        prefix = "report-round:" + round_key + ":" if round_key != "0" else ""
        if any(
            c["stage"].startswith("biocheck_")
            and (
                c.get("call_key", c["stage"]).startswith(prefix)
                if prefix
                else not c.get("call_key", c["stage"]).startswith("report-round:")
            )
            for c in progress.get("calls", [])
        ):
            # Earlier reports retain their actual inputs; do not claim a scope was
            # supplied to a verifier that has already returned without it.
            return context
        from bioradar.research.investigations.analysis import compact_query_context

        state = progress.get("query_policy", {})
        compact = compact_query_context(AnalysisService._reading_context(progress))
        checks = compact["coverage_checks"]
        originals = {item["id"]: item for item in state.get("checks", [])}
        for check in checks:
            if "mechanism_gap_index" in check:
                check["question"] = (
                    "专业缺口见assessment.mechanism_evaluation.critical_gaps["
                    + str(check["mechanism_gap_index"])
                    + "]，保留原条件和待查实验。"
                )
            reason = originals[check["id"]].get("reason")
            if reason:
                check["reason"] = reason
        queries = [
            {
                "source_id": q.get("plan", q.get("arguments", {})).get("source_id"),
                "arguments": copy.deepcopy(q.get("arguments", {})),
                "status": q.get("status"),
                "returned": q.get("returned"),
                "new_versions": q.get("new_versions"),
                "has_more": q.get("has_more"),
                "error_code": q.get("error_code"),
            }
            for q in compact["query_history"]
        ]
        from bioradar.research.investigations.kdd import material_scope, source_update_context

        updates = source_update_context(fixed, progress)
        context.update(copy.deepcopy(updates))
        reading_scope = material_scope(fixed, progress)
        if reading_scope is not None:
            reading_scope = copy.deepcopy(reading_scope)
            for item in reading_scope["unread_versions"]:
                # source/id/version already bind the entry; omit repeated transport
                # identity and action bookkeeping only from the model projection.
                item.pop("record_ref", None)
                item.pop("action_ids", None)
                item.pop("query_action_ids", None)
        findings = result if synthesis_schema(fixed) is not ModelSynthesis else state.get("reading") or {}
        research_findings = {
            name: copy.deepcopy(findings[name])
            for name in (
                "warning_assessment",
                "competing_hypotheses",
                "current_judgment",
                "topic_risk_reassessment",
            )
            if findings.get(name)
        }
        if synthesis_schema(fixed) is not ModelSynthesis and (fixed.get("origin") or {}).get(
            "topic_risk_scope"
        ):
            research_findings["topic_risk_scope"] = copy.deepcopy(fixed["origin"]["topic_risk_scope"])
            research_findings["topic_risk_definition"] = copy.deepcopy(
                fixed["origin"].get("topic_risk_definition")
            )
        scope = {
            **copy.deepcopy(updates),
            **research_findings,
            "coverage_issues": copy.deepcopy(state.get("coverage_issues", [])),
            **({"material_scope": reading_scope} if reading_scope is not None else {}),
            "source_ids": list(fixed.get("search_scope", [])),
            "graph_source_ids": list(fixed.get("graph_scope", [])),
            "evidence_time": copy.deepcopy(fixed.get("evidence_time", {})),
            "queries": queries,
            "coverage_checks": checks,
            "unresolved_checks": [
                {"id": c["id"], "status": c.get("status")} for c in checks if c.get("status") != "passed"
            ],
            "source_failures": [
                q
                for q in queries
                if q["error_code"] or q["status"] in {"failed", "not_sent", "sent", "unconfirmed"}
            ],
            "stop_status": state.get("status"),
            "stop_reason": state.get("reason"),
            "qualification": (
                "这是本轮实际授权和取证范围。空结果、成功查询及已取得资料均不代表覆盖完整；"
                "has_more表示仍有未读结果。图谱只作背景，未查资料不能解释为不存在。"
            ),
        }
        prior = (fixed.get("parent_result") or {}).get("report") or {}
        prior_text = prior.get("final_text")
        if isinstance(prior_text, str) and prior_text.strip():
            scope["prior_report"] = {
                "text": prior_text,
                "text_sha256": hashlib.sha256(prior_text.encode()).hexdigest(),
                "verification_status": prior.get("status"),
                "professional_approval": prior.get("professional_approval", "not_reviewed"),
                "identity_basis": "fixed_input.parent_result.report",
                "qualification": (
                    "这是父版实际保存的机器报告，仅供核对旧稿表达、年份和判断变化，"
                    "不是新的原始证据；旧稿错误须按本轮原件纠正，待核状态不能升级为事实。"
                ),
            }
        if compact.get("analysis_results"):
            scope["analysis_results"] = copy.deepcopy(compact["analysis_results"])
            context["analysis_results"] = copy.deepcopy(compact["analysis_results"])
        if freeze:
            progress.setdefault("report_research_scopes", {})[round_key] = copy.deepcopy(scope)
        context["research_scope"] = {key: value for key, value in scope.items() if key != "analysis_results"}
        return context

    def _final_token_allowance(self, fixed, progress):
        # Later inputs do not exist yet. Each actual request is priced in _call.
        return 0

    def _optional_budget(self, fixed, progress, calls, *, following=0, request_tokens=0, following_tokens=0):
        budget = analysis_budget(fixed, progress, datetime.now(UTC))
        return (
            len(progress["calls"]) + calls <= budget["limits"]["max_calls"]
            and (budget["limits"]["max_tokens"] or 10_000_000) - budget["usage"]["budgeted_tokens"]
            >= request_tokens
        )

    def _not_sent_call(self, identifier, token, fixed, progress, call, code, message, *, started_at=None):
        from bioradar.adapters.reasoning_model import ModelCallError, ModelCallReceipt

        now = datetime.now(UTC)
        started = started_at or now
        receipt = ModelCallReceipt(
            call_id=call["call_id"],
            stage=call["stage"],
            input_hash=call["input_hash"],
            prompt_version=call["prompt_version"],
            requested_model=fixed["model"],
            configured_model_revision=fixed["model_revision"],
            status="not_sent",
            error_code=code,
            started_at=started,
            completed_at=now,
            elapsed_ms=max(0, round((now - started).total_seconds() * 1000)),
            max_output_tokens=call["model_parameters"]["max_output_tokens"],
        )
        call["status"], call["receipt"] = "failed", receipt.model_dump(mode="json")
        self._checkpoint(identifier, token, progress)
        raise ModelCallError(code, message, receipt, 409)

    def _call(
        self,
        identifier,
        token,
        stage,
        schema,
        fixed,
        progress,
        payload,
        *,
        system_prompt=None,
        call_key=None,
        reserve_tokens=0,
    ):
        from bioradar.adapters.reasoning_model import (
            ModelCallError,
            ModelCallReceipt,
            complete_json,
            encode_request,
        )

        self._guard(identifier, token)
        prompt = system_prompt or SYSTEM_PROMPT
        key = call_key or stage
        report_round = progress.get("active_report_round", 0)
        if report_round and (
            stage.startswith("biocheck_")
            or stage in {"synthesize", "check", "revise", "recheck", "compare", "final_check"}
        ):
            key = "report-round:" + str(report_round) + ":" + key
        response_schema = copy.deepcopy(schema if isinstance(schema, dict) else schema.model_json_schema())
        if stage in {"synthesize", "revise"}:
            response_schema["$defs"]["AnalysisUpdate"]["properties"]["stance"] = {
                "type": "string",
                "const": "initial",
            }
            response_schema["$defs"]["AnalysisUpdate"]["properties"]["changed_claims"]["maxItems"] = 0
        elif stage == "compare":
            response_schema["properties"]["stance"]["enum"] = ["maintained", "revised", "unable_to_compare"]
        signature = canonical_hash([prompt, payload, response_schema, fixed["model_parameters"]])
        call = next((c for c in progress["calls"] if c.get("call_key", c["stage"]) == key), None)
        if call:
            if call_key is not None and call.get("request_signature", signature) != signature:
                raise DomainError(
                    "analysis_call_input_changed", "同一步骤的固定输入不同，未套用旧模型输出", 409
                )
            if call["status"] == "received":
                return call["output"], progress
            if call["status"] == "failed":
                saved_receipt = call.get("receipt") or {}
                if saved_receipt.get("error_code"):
                    try:
                        receipt = ModelCallReceipt.model_validate(saved_receipt)
                    except ValueError:
                        pass
                    else:
                        raise ModelCallError(
                            receipt.error_code,
                            "本阶段模型调用已停止；原回执保留，未自动重发",
                            receipt,
                            409,
                        )
                raise DomainError("model_call_failed", "本阶段模型调用已失败，未自动重发", 409)
            if call["status"] in {"sent", "unconfirmed"}:
                raise DomainError("model_call_outcome_unconfirmed", "上次模型调用结果未知，未自动重发", 409)
        # Reading a confirmed receipt and saving its report do not send another request.
        self._guard(identifier, token, check_deadline=True)
        if call is None:
            parameters = dict(fixed["model_parameters"])
            budget = analysis_budget(fixed, progress, datetime.now(UTC))
            if len(progress["calls"]) >= parameters["max_calls"]:
                raise DomainError("analysis_model_budget", "达到本轮实际模型调用上限", 422)
            from bioradar.research.investigations.execution_budget import request_estimate

            stage_cap = {
                "read_plan_repair": 2048,
                "biocheck_verify_paragraph": 12288,
                "biocheck_recheck_paragraph": 12288,
                "biocheck_verify_paragraph_retry": 16384,
                "biocheck_recheck_paragraph_retry": 16384,
                "biocheck_verify_coverage": 8192,
                "biocheck_recheck_coverage": 8192,
            }.get(stage, parameters["max_output_tokens"])
            output_cap = min(parameters["max_output_tokens"], stage_cap)
            try:
                request_bytes = len(
                    encode_request(
                        self.settings.model_copy(update={"semantic_llm_max_output_tokens": output_cap}),
                        stage=stage,
                        system_prompt=prompt,
                        payload=payload,
                        response_schema=response_schema,
                    )
                )
                estimate = request_estimate(fixed, stage, request_bytes)
                input_upper = estimate["estimated_input_tokens"]
            except (ValueError, TypeError, RecursionError):
                raise DomainError("model_input_invalid", "模型输入不是受支持的有限JSON对象", 422) from None
            remaining = (budget["limits"]["max_tokens"] or 10_000_000) - budget["usage"]["budgeted_tokens"]
            output_cap = min(output_cap, remaining - input_upper)
            if output_cap < 128:
                raise DomainError("analysis_token_budget", "剩余token预算不足以发送下一次有界请求", 422)
            parameters["max_output_tokens"] = output_cap
            # The final cap changes the wire JSON too; retain its exact byte count.
            request_bytes = len(
                encode_request(
                    self.settings.model_copy(update={"semantic_llm_max_output_tokens": output_cap}),
                    stage=stage,
                    system_prompt=prompt,
                    payload=payload,
                    response_schema=response_schema,
                )
            )
            estimate = request_estimate(fixed, stage, request_bytes)
            input_upper = estimate["estimated_input_tokens"]
            call = {
                "stage": stage,
                "call_key": key,
                "status": "prepared",
                "call_id": canonical_hash([identifier, key]),
                "request_signature": signature,
                "input_hash": canonical_hash([prompt, payload, response_schema, parameters]),
                "payload": copy.deepcopy(payload),
                "system_prompt": prompt,
                "response_schema": response_schema,
                "prompt_version": fixed["prompt_version"],
                "model_parameters": parameters,
                "token_upper_bound": input_upper + output_cap,
                "request_bytes": request_bytes,
                "input_estimate": estimate,
                **(
                    {"action_id": progress["active_professional_action"]}
                    if progress.get("active_professional_action")
                    else {}
                ),
            }
            progress["calls"].append(call)
            progress["stage"] = stage
            self._checkpoint(identifier, token, progress)
        self._guard(identifier, token)
        invoke = self.model_call or complete_json
        budget = analysis_budget(fixed, progress, datetime.now(UTC))
        seconds_left = budget["usage"]["remaining_seconds"]
        required_seconds = minimum_send_seconds(fixed, progress, stage)
        call["required_send_seconds"] = required_seconds
        if seconds_left < required_seconds:
            self._not_sent_call(
                identifier,
                token,
                fixed,
                progress,
                call,
                "analysis_time_budget",
                "剩余总时间不足本阶段固定及已观察到的发送窗口，模型请求未发送",
            )
        reserve_automatic_call(self, identifier, token, fixed, progress, call, datetime.now(UTC))
        call["model_parameters"]["timeout_seconds"] = min(
            call["model_parameters"]["timeout_seconds"], max(1, int(seconds_left))
        )
        actual_settings = self.settings.model_copy(
            update={
                "semantic_llm_max_output_tokens": call["model_parameters"]["max_output_tokens"],
                "semantic_llm_timeout_seconds": call["model_parameters"]["timeout_seconds"],
            }
        )
        call["input_hash"] = canonical_hash(
            [call["system_prompt"], call["payload"], call["response_schema"], call["model_parameters"]]
        )
        intent_at = datetime.now(UTC)
        call["status"] = "sent"
        self._checkpoint(identifier, token, progress)
        # The durable intent itself may take longer than the remaining execution time.
        now = datetime.now(UTC)
        budget = analysis_budget(fixed, progress, now)
        seconds_left = budget["usage"]["remaining_seconds"]
        if seconds_left < required_seconds:
            self._not_sent_call(
                identifier,
                token,
                fixed,
                progress,
                call,
                "analysis_time_budget",
                "发送意图保存后剩余时间不足本阶段必要窗口，模型请求未发送",
                started_at=intent_at,
            )
        # Timeout is a transport cap, not part of encode_request's wire body. Keep
        # the durable intent/hash stable and record the tighter actual cap separately.
        call["transport_timeout_seconds"] = min(
            call["model_parameters"]["timeout_seconds"], int(seconds_left)
        )
        actual_settings = actual_settings.model_copy(
            update={"semantic_llm_timeout_seconds": call["transport_timeout_seconds"]}
        )
        try:
            completion = invoke(
                actual_settings,
                stage=stage,
                system_prompt=call["system_prompt"],
                payload=call["payload"],
                response_schema=call["response_schema"],
                call_id=call["call_id"],
                input_hash=call["input_hash"],
                prompt_version=call["prompt_version"],
            )
        except ModelCallError as exc:
            call["status"] = "unconfirmed" if exc.receipt.status == "unconfirmed" else "failed"
            call["receipt"] = exc.receipt.model_dump(mode="json")
            self._checkpoint(identifier, token, progress)
            raise
        call["status"], call["output"] = "received", completion.parsed
        call["receipt"] = completion.receipt.model_dump(mode="json")
        self._checkpoint(identifier, token, progress)
        self._guard(identifier, token)
        return completion.parsed, progress

    @staticmethod
    def _evidence_time(fixed):
        frozen = fixed.get("evidence_time")
        if frozen:
            return frozen["mode"], datetime.fromisoformat(frozen["cutoff"])
        return "current", datetime.now(UTC)

    @staticmethod
    def _policy_budget(fixed, progress):
        from bioradar.research.investigations.professional import http_ledger
        from bioradar.research.investigations.query_policy import search_query_count

        query_count = search_query_count(fixed, progress)
        ledger = http_ledger(progress)
        by_action = {}
        for item in ledger:
            if item["action_id"] is not None:
                by_action[item["action_id"]] = by_action.get(item["action_id"], 0) + item["known_tool_calls"]
        route_calls = sum(
            cost["tool_calls"]
            if cost.get("tool_calls") is not None
            else by_action.get(query.get("action_id"), 0)
            if query.get("provider") == "professional"
            else cost.get("known_tool_calls", 0)
            for query in progress.get("queries", [])
            for cost in [query.get("cost") or {}]
        )
        non_route_calls = sum(item["known_tool_calls"] for item in ledger if item["action_id"] is None)
        limits = fixed.get("budget", {})
        usage = progress.get("tool_usage", {})
        uncertain = [
            q
            for q in progress.get("queries", [])
            if q["status"] in {"sent", "unconfirmed"} and (q.get("cost") or {}).get("tool_calls") is None
        ]
        reserved = sum(
            (q.get("cost") or {}).get("reserved_tool_calls", 1)
            for q in uncertain
            if q.get("provider") != "professional"
        ) + (
            sum(item["reserved_tool_calls"] for item in ledger)
            if ledger
            else usage.get("professional_reserved_tool_calls", 0)
        )
        # Keep the stored aggregate visible; this projection uses disjoint actual
        # receipts and legacy response evidence without filling any attempt index.
        known = usage.get("preparation_tool_calls", 0) + route_calls + non_route_calls
        unmeasured = sum(
            item["outcome"] == "received" and item["accounting_basis"] == "unmeasured_attempts"
            for item in ledger
        )
        return {
            "limits": {
                "tool_calls": limits.get("max_tool_calls", 64),
                "queries": limits.get("max_queries", 6),
            },
            "usage": {
                "tool_calls": None if uncertain or reserved or unmeasured else known,
                "known_tool_calls": known,
                "recorded_known_tool_calls": usage.get("tool_calls", 0),
                "unmeasured_professional_http_exchanges": unmeasured,
                "unknown_tool_calls": len(uncertain),
                "reserved_tool_calls": reserved,
                "budgeted_tool_calls": known + reserved,
                "queries": query_count,
                "relation_access": usage.get("relation_access", 0),
                "preparation_tool_calls": usage.get("preparation_tool_calls", 0),
                "route_action_tool_calls": route_calls,
                "non_route_professional_tool_calls": non_route_calls,
                "professional_http_unknown_outcomes": sum(
                    item["attempts"] > 0 and item["outcome"] in {"sent", "unknown"} for item in ledger
                ),
                "model_unconfirmed_calls": sum(
                    call.get("status") in {"sent", "unconfirmed"} for call in progress.get("calls", [])
                ),
            },
            "remaining": {
                "tool_calls": max(0, limits.get("max_tool_calls", 64) - known - reserved),
                "queries": max(0, limits.get("max_queries", 6) - query_count),
            },
        }

    @classmethod
    def _policy_view(cls, fixed, progress):
        if fixed.get("execution_version") != "query-step-v1":
            return None
        from bioradar.research.investigations.professional import http_ledger

        state = progress.get("query_policy", {})
        return {
            **copy.deepcopy(fixed.get("query_policy", {})),
            "execution_version": fixed["execution_version"],
            "evidence_time": copy.deepcopy(fixed.get("evidence_time")),
            "status": state.get("status", "running"),
            "reason": state.get("reason"),
            "checks": copy.deepcopy(state.get("checks", [])),
            "coverage_issues": copy.deepcopy(state.get("coverage_issues", [])),
            "reading_manifest": copy.deepcopy(state.get("reading_manifest", [])),
            **(
                {
                    "synthesis_stop_reason": progress["synthesis_stop_reason"],
                    "synthesis_reading_basis": copy.deepcopy(progress["synthesis_reading_basis"]),
                }
                if progress.get("synthesis_stop_reason")
                else {}
            ),
            "decisions": copy.deepcopy(state.get("decisions", [])),
            "phase_transitions": copy.deepcopy(state.get("phase_transitions", [])),
            "pending_action": copy.deepcopy(state.get("pending_action")),
            "budget": cls._policy_budget(fixed, progress),
            "professional_http_ledger": http_ledger(progress),
            "data_quality": [
                {
                    "code": "legacy_weknora_http_attempt_unaccounted",
                    "action_id": query.get("action_id"),
                    "query_index": index,
                    "error_code": query["error_code"],
                    "recorded_tool_calls": 0,
                    "qualification": "旧回执确认HTTP错误但记录请求次数为0；保留原账，未推算或补写费用。",
                }
                for index, query in enumerate(progress.get("queries", []))
                if query.get("provider") == "weknora"
                and query.get("error_code") == "weknora_kdd_http_error"
                and (query.get("cost") or {}).get("tool_calls") == 0
            ]
            + [
                {
                    "code": "legacy_professional_http_attempt_indices_missing",
                    "operation_id": item["operation_id"],
                    "exchange_index": item["exchange_index"],
                    "known_tool_calls": item["known_tool_calls"],
                    "reserved_tool_calls": item["reserved_tool_calls"],
                    "accounting_basis": item["accounting_basis"],
                    "qualification": (
                        "旧HTTP回执缺尝试索引；已有有效响应仅确认1次，其余未测尝试保守预留，未补写索引。"
                        if item["accounting_basis"] == "legacy_received_response"
                        else "旧接收标记缺可用响应或有效尝试计数，未推算已知次数；只对确定的尝试数保守预留。"
                    ),
                }
                for item in http_ledger(progress)
                if item["outcome"] == "received" and item["accounting_basis"] != "http_attempted_indices"
            ],
            **(
                {
                    "artifacts": copy.deepcopy(progress.get("kdd_artifacts", [])),
                    "artifact_results": copy.deepcopy(progress.get("kdd_results", {})),
                    "execution": copy.deepcopy(progress.get("kdd_execution", [])),
                }
                if fixed.get("query_policy", {}).get("schema_version") == "query-planner.external.v2"
                else {}
            ),
        }

    def _tool_start(self, fixed, progress, *, reserve=0):
        if fixed.get("execution_version") != "query-step-v1":
            return None
        remaining = self._policy_budget(fixed, progress)["remaining"]["tool_calls"] - reserve
        if remaining <= 0:
            raise DomainError("analysis_tool_budget", "本轮来源请求预算已用尽，已保留实际回执", 422)
        before = getattr(self.reader, "requests_made", 0)
        if self.reader is not None:
            self.reader.max_requests = before + remaining
        return before

    def _tool_finish(self, progress, before, *, extra=0):
        if before is None:
            return 0
        count = max(0, getattr(self.reader, "requests_made", 0) - before) + extra
        usage = progress.setdefault("tool_usage", {"tool_calls": 0})
        usage["tool_calls"] = usage.get("tool_calls", 0) + count
        return count

    @staticmethod
    def _saved_policy_reading(state):
        schema = PolicyReading if state.get("reading_kind") == "policy" else ModelReading
        return schema.model_validate(state["reading"])

    @staticmethod
    def _reading_context(progress):
        from bioradar.research.investigations.kdd import analysis_results

        state = progress["query_policy"]
        return {
            "coverage_checks": state.get("checks", []),
            "query_history": [{k: v for k, v in q.items() if k != "records"} for q in progress["queries"]],
            "mechanism_evaluation": progress.get("mechanism_evaluation"),
            "quality_feedback": state.get("quality_feedback"),
            "graph_context": progress.get("graph_context", []),
            **({"analysis_results": analysis_results(progress)} if progress.get("kdd_results") else {}),
        }

    def _reading_payload_tokens(self, fixed, payload, schema):
        from bioradar.adapters.reasoning_model import encode_request
        from bioradar.research.investigations.execution_budget import estimate_input_tokens

        return (
            estimate_input_tokens(
                fixed,
                payload["task"],
                len(
                    encode_request(
                        self.settings,
                        stage=payload["task"],
                        system_prompt=SYSTEM_PROMPT,
                        payload=payload,
                        response_schema=schema.model_json_schema(),
                    )
                ),
            )
            + 128
        )

    def _policy_reading(self, identifier, token, fixed, progress, *, optional=False):
        from bioradar.research.investigations.query_policy import build_policy_context

        state = progress["query_policy"]
        previous = state.get("reading")
        declared = build_policy_context(
            fixed,
            progress,
            phase=state["phase"],
            plans=state.get("initial_plans", [])
            if state["phase"] == "initial"
            else (previous or {}).get("next_queries", []),
            reading=previous,
            provider_status=progress.get("providers", fixed.get("providers", {})),
        )
        state["checks"] = copy.deepcopy(declared.get("checks", state.get("checks", [])))
        context = {
            "coverage_checks": state.get("checks", []),
            "query_history": [{k: v for k, v in q.items() if k != "records"} for q in progress["queries"]],
            "mechanism_evaluation": progress.get("mechanism_evaluation"),
            "quality_feedback": state.get("quality_feedback"),
        }
        fingerprint = canonical_hash(
            [
                [[e["key"], e["selection_hash"], e["text_sha256"]] for e in progress["evidence"]],
                context["mechanism_evaluation"],
                context["quality_feedback"],
                progress.get("graph_context"),
            ]
        )
        external = fixed["query_policy"].get("effective") == "query_planner" or bool(
            state.get("quality_round")
        )
        compact_mode = external and fixed.get("query_context_version") == "semantic-v2"
        if compact_mode:
            from bioradar.research.investigations.analysis import (
                compact_query_context,
                semantic_reading_state,
            )

            context = self._reading_context(progress)
            fingerprint = canonical_hash(semantic_reading_state(progress["evidence"], context))
            context = compact_query_context(context)
        key = "policy-read:" + fingerprint if external else "read_plan"
        saved = next((c for c in progress["calls"] if c.get("call_key", c["stage"]) == key), None)

        def retain_previous(reason, failed=None):
            state["refresh_stopped_reason"] = reason
            state["refresh_not_semantically_reviewed"] = True
            if failed is not None:
                entry = {
                    "call_key": failed.get("call_key", failed["stage"]),
                    "call_id": failed["call_id"],
                    "error_code": (failed.get("receipt") or {}).get("error_code"),
                    "receipt_status": (failed.get("receipt") or {}).get("status"),
                    "http_status": (failed.get("receipt") or {}).get("http_status"),
                    "finish_reason": (failed.get("receipt") or {}).get("finish_reason"),
                }
                failures = state.setdefault("optional_read_failures", [])
                if entry not in failures:
                    failures.append(entry)
            note = (
                "可选语义重读未执行或未形成有效新解读，保留上一已校验解读；"
                "补查回执和新增专业缺口未因此自动视为已覆盖。"
            )
            if note not in progress.setdefault("limitations", []):
                progress["limitations"].append(note)
            self._checkpoint(identifier, token, progress)
            return False

        if optional and previous is not None and _known_optional_read_failure(saved):
            return retain_previous((saved.get("receipt") or {})["error_code"], saved)
        if (
            optional
            and previous is not None
            and saved is not None
            and saved["status"] == "prepared"
            and saved["model_parameters"]["max_output_tokens"]
            < fixed["model_parameters"]["max_output_tokens"]
        ):
            try:
                self._not_sent_call(
                    identifier,
                    token,
                    fixed,
                    progress,
                    saved,
                    "analysis_token_budget",
                    "已准备的可选重读不足正常输出额度，保留原解读且未发送请求",
                )
            except DomainError as exc:
                return retain_previous(exc.code, saved)
        payload = (
            freeze_payload("read_plan", fixed, progress["evidence"], query_context=context)
            if external
            else freeze_payload("read_plan", fixed, progress["evidence"])
        )
        request_tokens = following_tokens = 0
        if compact_mode or optional:
            request_tokens = self._reading_payload_tokens(fixed, payload, PolicyReading)
        if compact_mode:
            following_payload = self._synthesis_payload(fixed, progress)
            following_tokens = self._reading_payload_tokens(fixed, following_payload, synthesis_schema(fixed))
        if (
            optional
            and saved is None
            and not self._optional_budget(
                fixed,
                progress,
                1,
                following=1,
                request_tokens=request_tokens - 128 + fixed["model_parameters"]["max_output_tokens"],
                following_tokens=following_tokens,
            )
        ):
            return retain_previous("analysis_finishing_budget_reserved")
        try:
            raw, progress = self._call(
                identifier,
                token,
                "read_plan",
                PolicyReading if external else ModelReading,
                fixed,
                progress,
                payload,
                call_key=key if external else None,
                **(
                    {"reserve_tokens": following_tokens + self._final_token_allowance(fixed, progress)}
                    if compact_mode and optional
                    else {}
                ),
            )
        except DomainError as exc:
            failed = next((c for c in progress["calls"] if c.get("call_key", c["stage"]) == key), None)
            if (
                not optional
                or previous is None
                or not (self._report_budget_stop(exc) or _known_optional_read_failure(failed))
            ):
                raise
            return retain_previous(exc.code, failed)
        if external:
            from bioradar.research.investigations.reading_execution import validated_policy_output

            try:
                reading = validated_policy_output(
                    self,
                    identifier,
                    token,
                    fixed,
                    progress,
                    raw,
                    payload,
                    key,
                    declared.get("checks", []),
                    optional=optional,
                    following_tokens=following_tokens,
                )
            except DomainError as exc:
                repair = progress.get("reading_repairs", {}).get(key, {})
                failed = next(
                    (c for c in progress["calls"] if c.get("call_key") == repair.get("repair_call_key")), None
                )
                known_stop = (
                    self._report_budget_stop(exc)
                    or exc.code == "analysis_finishing_budget_reserved"
                    or _known_optional_read_failure(failed)
                    or repair.get("status") == "invalid"
                    and exc.code
                    in {
                        "analysis_reading_invalid",
                        "analysis_reference_invalid",
                        "analysis_passage_invalid",
                        "analysis_output_invalid",
                        "analysis_unbound_claim",
                        "analysis_query_invalid",
                    }
                )
                if optional and previous is not None and known_stop:
                    return retain_previous(exc.code, failed)
                raise
        else:
            reading = validate_reading(raw, progress["evidence"], fixed["search_scope"])
        state["reading_kind"] = "policy" if external else "baseline"
        state["reading"] = reading.model_dump(mode="json")
        state["reading_evidence_hash"] = fingerprint
        state.pop("refresh_stopped_reason", None)
        state.pop("refresh_not_semantically_reviewed", None)
        from bioradar.research.investigations.query_policy import normalize_policy_coverage

        normalized, issues = normalize_policy_coverage(
            reading.model_dump(mode="json"), declared.get("checks", []), progress["evidence"]
        )
        known_issues = state.setdefault("coverage_issues", [])
        for issue in issues:
            if issue not in known_issues:
                known_issues.append(issue)
        merged = {item["id"]: item for item in state["checks"]}
        for check in normalized.get("coverage_checks", []):
            merged[check["id"]].update(check)
        state["checks"] = list(merged.values())
        self._checkpoint(identifier, token, progress)
        return True

    def _authorize_query_sources(self, fixed, action):
        arguments = action["arguments"]
        provider = arguments.get("provider", "fabric")
        if provider == "weknora":
            sources = list(fixed["search_scope"])
        else:
            source = arguments.get("source_id")
            if source is None:
                for name in ("graph_record", "graph_binding", "binding"):
                    binding = arguments.get(name)
                    if isinstance(binding, dict) and binding.get("source_id"):
                        source = binding["source_id"]
                        break
            binding = arguments.get("binding")
            if isinstance(binding, dict) and source and binding.get("source_id") != source:
                raise DomainError("analysis_query_invalid", "图查询声明来源与固定绑定不一致，请求未发送", 422)
            sources = [source] if source else []
        if not sources:
            raise DomainError("analysis_query_invalid", "研究动作缺少明确可核验的来源", 422)
        allowed = (
            fixed.get("graph_scope", fixed["search_scope"])
            if action["kind"] in {"entity_relations", "graph_neighbors", "graph_path"}
            else fixed["search_scope"]
        )
        if action["kind"] == "document_read" or (
            action["kind"] in {"entity_relations", "graph_neighbors", "graph_path"}
            and isinstance(arguments.get("binding"), dict)
        ):
            allowed = sorted(
                set(allowed)
                | {item["source_id"] for item in fixed.get("dependency_sources", []) if item.get("history")}
            )
        if not set(sources).issubset(allowed):
            raise DomainError("analysis_query_invalid", "研究动作越过本轮已冻结来源范围，请求未发送", 422)
        instance = self._source_instance(fixed["is_demo"])
        if instance not in self.actor.source_allowlist or any(
            source not in self.settings.engine_source_id_allowlist.get(instance, []) for source in sources
        ):
            raise DomainError("history_source_revoked", "研究动作来源当前无读取许可，请求未发送", 403)
        self.engine._history_source_ids = None
        self.engine._history_permission_failure = None
        for source in sources:
            self.engine.authorize_history_source(source)

    def _catalog_discovery_plan(self, fixed, progress, action):
        """Rebuild a directory/page action from its actual committed source receipts."""
        from bioradar.research.investigations.query_policy import (
            catalog_discovery_candidates,
            pagination_candidates,
        )

        observed = copy.deepcopy(progress)
        state = observed["query_policy"]
        pending = state.get("pending_action")
        if pending is not None and pending.get("id") == action["id"]:
            state["pending_action"] = None
        observed["queries"] = [
            query for query in observed["queries"] if query.get("action_id") != action["id"]
        ]
        candidates = (
            pagination_candidates(fixed, observed, state.get("checks", []))
            if "cursor" in action["arguments"]
            else catalog_discovery_candidates(
                fixed, observed, state.get("initial_plans", []), state.get("checks", [])
            )
        )
        for candidate in candidates:
            if candidate["id"] == action["id"] and candidate["arguments"] == action["arguments"]:
                return copy.deepcopy(candidate["arguments"]["plan"])
        raise DomainError(
            "analysis_query_invalid", "目录或续页必须来自本轮真实回执并保持原查询范围，请求未发送", 422
        )

    def _execute_policy_action(self, identifier, token, fixed, progress, action, step):
        from bioradar.research.investigations.query_policy import (
            pagination_enabled,
            search_query_count,
            semantic_key,
        )

        kdd = fixed.get("query_policy", {}).get("schema_version") == "query-planner.external.v2"
        continuation = action["kind"] == "document_search" and bool(action["arguments"].get("cursor"))
        limits = fixed.get("query_policy", {}).get("action_limits", {})
        if not any(q.get("action_id") == action["id"] for q in progress.get("queries", [])):
            limit = limits.get(action["kind"])
            used = sum(
                q.get("kind", "document_search") == action["kind"] for q in progress.get("queries", [])
            )
            if kdd and action["kind"] == "document_search":
                used = search_query_count(fixed, progress)
            if limit is not None and used >= limit and not (kdd and continuation):
                raise DomainError("analysis_action_budget", "本类研究动作已达固定上限，未新增请求", 422)
        if action["kind"] == "source_fetch":
            from bioradar.research.investigations.fetch_execution import execute_source_fetch

            return execute_source_fetch(self, identifier, token, fixed, progress, action, step)
        if action["kind"] == "document_read":
            from bioradar.research.investigations.reading_execution import execute_document_read

            return execute_document_read(self, identifier, token, fixed, progress, action, step)
        if action["kind"] in {"dataset_prepare", "analysis_run", "professional_assess"}:
            from bioradar.research.investigations.kdd import execute_action

            return execute_action(self, identifier, token, fixed, progress, action, step)
        arguments, kind = action["arguments"], action["kind"]
        provider = arguments.get("provider", "graph" if kind != "document_search" else "fabric")
        phase = arguments.get("phase", progress["query_policy"]["phase"])
        receipt = next((q for q in progress["queries"] if q.get("action_id") == action["id"]), None)
        catalog_plan = None
        if ("catalog_discovery" in arguments or "cursor" in arguments) and (
            receipt is None or receipt["status"] == "prepared"
        ):
            if (
                kind != "document_search"
                or provider != "fabric"
                or "catalog_discovery" in arguments
                and arguments["catalog_discovery"] is not True
            ):
                raise DomainError("analysis_query_invalid", "目录与续页只接受明确的Fabric原件动作", 422)
            catalog_plan = self._catalog_discovery_plan(fixed, progress, action)
        if receipt is None:
            if (not kdd or action["kind"] == "document_search" and not continuation) and search_query_count(
                fixed, progress
            ) >= fixed["budget"]["max_queries"]:
                raise DomainError("analysis_query_budget", "本轮新查询预算已用尽", 422)
            if kind == "document_search":
                if provider not in {"fabric", "weknora"}:
                    raise DomainError("analysis_query_invalid", "查询候选须明确指定一个已知来源提供方", 422)
                plan_data = arguments.get("plan") or {
                    "source_id": arguments["source_id"],
                    "terms": arguments["terms"],
                    "reason": action.get("reason", "策略选择的有界查询"),
                }
                plan_type = (
                    PolicyPlannedQuery
                    if "coverage_check_ids" in plan_data
                    else PlannedQuery
                    if "gap_id" in plan_data
                    else InitialQuery
                )
                plan = catalog_plan if catalog_plan is not None else plan_type.model_validate(plan_data)
                source_id = plan["source_id"] if isinstance(plan, dict) else plan.source_id
                if source_id not in fixed["search_scope"]:
                    raise DomainError("analysis_query_invalid", "策略查询超出本轮授权来源", 422)
            elif kind in {"entity_relations", "graph_neighbors", "graph_path"}:
                plan = copy.deepcopy(arguments)
                provider = "fabric_graph"
            else:
                raise DomainError("analysis_query_invalid", "策略返回了不支持的研究动作", 422)
            receipt = AnalysisQueryReceipt(
                index=arguments.get("index", len(progress["queries"])),
                phase=phase,
                provider=provider,
                kind=kind,
                plan=plan,
                action_id=action["id"],
                semantic_key=semantic_key(kind, arguments),
                decision_step=step,
                status="prepared",
                scope="fabric_bounded_original_catalog_not_keyword_match"
                if arguments.get("catalog_discovery") is True
                else "fixed_graph_background_not_research_evidence"
                if provider == "fabric_graph"
                else "weknora_candidates_bound_to_authorized_fabric_versions"
                if provider == "weknora"
                else "fabric_current_adopted_versions_not_the_web",
                relaxed_from=arguments.get("relaxed_from"),
                relaxation_reason=arguments.get("relaxation_reason"),
            ).model_dump(mode="json")
            if continuation:
                receipt["usage"] = {"search_cursor": arguments["cursor"]}
            progress["queries"].append(receipt)
            self._checkpoint(identifier, token, progress)
        if receipt["status"] in {"sent", "unconfirmed"}:
            receipt.update(status="unconfirmed", error_code="analysis_query_outcome_unconfirmed")
            self._checkpoint(identifier, token, progress)
            return receipt
        if receipt["status"] == "prepared":
            mode, cutoff = self._evidence_time(fixed)
            if (
                provider == "fabric_graph"
                and (mode != "current" or fixed.get("evidence_time", {}).get("study_id"))
                and arguments.get("operation") in {"lookup", "capture"}
            ):
                receipt.update(
                    status="unavailable",
                    error_code="history_current_graph_query_unavailable",
                    records=[],
                    cost={"tool_calls": 0, "tokens": None},
                )
                self._checkpoint(identifier, token, progress)
            elif provider == "weknora" and mode != "current":
                receipt.update(
                    status="unavailable",
                    error_code="history_current_index_unavailable",
                    records=[],
                    cost={"tool_calls": 0, "tokens": None},
                )
                self._checkpoint(identifier, token, progress)
            elif self.reader is None and provider != "weknora":
                receipt.update(
                    status="unavailable",
                    error_code="history_not_connected",
                    records=[],
                    cost={"tool_calls": 0, "tokens": None},
                )
                self._checkpoint(identifier, token, progress)
            else:
                self._guard(identifier, token)
                try:
                    self._authorize_query_sources(fixed, action)
                except DomainError as exc:
                    receipt.update(
                        status="failed",
                        error_code=exc.code,
                        records=[],
                        cost={"tool_calls": 0, "tokens": None},
                        usage={"request_status": "not_sent"},
                    )
                    progress["query_policy"]["decisions"][step]["receipt"] = {
                        "action_id": action["id"],
                        "query_index": progress["queries"].index(receipt),
                        "status": "failed",
                        "error_code": exc.code,
                        "evidence_ids": [],
                        "cost": copy.deepcopy(receipt["cost"]),
                    }
                    self._checkpoint(identifier, token, progress)
                    raise
                self._guard(identifier, token, check_deadline=True)
                before = self._tool_start(fixed, progress)
                tool_allowance = self._policy_budget(fixed, progress)["remaining"]["tool_calls"]
                receipt["status"] = "sent"
                receipt["cost"] = {
                    "tool_calls": None,
                    "reserved_tool_calls": tool_allowance,
                    "tokens": None,
                }
                self._checkpoint(identifier, token, progress)
                seconds = analysis_budget(fixed, progress, datetime.now(UTC))["usage"]["remaining_seconds"]
                if seconds < 0.1:
                    receipt.update(
                        status="failed",
                        error_code="analysis_time_budget",
                        records=[],
                        cost={"tool_calls": 0, "tokens": None},
                        usage={"request_status": "not_sent", "embedding_rerank_tokens": None},
                    )
                    self._checkpoint(identifier, token, progress)
                    raise DomainError("analysis_time_budget", "查询发送意图保存后时限已到，请求未发送", 409)
                extra = relation_cost = 0
                try:
                    if provider == "fabric":
                        from bioradar.adapters.fabric_monitor import search_analysis_records

                        found = search_analysis_records(
                            self.reader,
                            source_id=receipt["plan"]["source_id"],
                            is_demo=fixed["is_demo"],
                            terms=receipt["plan"]["terms"],
                            cutoff=cutoff,
                            mode=mode,
                            limit=arguments.get("limit", reading_limits(fixed)["search_limit"]),
                            run_id=fixed.get("evidence_time", {}).get("run_id"),
                            catalog_discovery=arguments.get("catalog_discovery") is True,
                            cursor=arguments.get("cursor"),
                            max_seq=arguments.get("max_seq"),
                            pagination=pagination_enabled(fixed),
                        )
                        if arguments.get("catalog_discovery") is True:
                            receipt["usage"] = {
                                "catalog_discovery": True,
                                "coverage": copy.deepcopy(found["coverage"]),
                            }
                        receipt.update(
                            status="read" if found["items"] else "empty",
                            records=copy.deepcopy(found["items"]),
                            returned=len(found["items"]),
                            has_more=bool(found.get("next_cursor")),
                        )
                        if pagination_enabled(fixed):
                            receipt["pagination"] = {
                                "has_more": bool(found.get("next_cursor")),
                                "next_cursor": found.get("next_cursor"),
                                "max_seq": found["coverage"]["max_seq"],
                                "page_number": arguments.get("page_number", 1),
                            }
                    elif provider == "weknora":
                        from bioradar.adapters.weknora import search_candidates

                        kdd_arguments = {}
                        if fixed.get("query_policy", {}).get("schema_version") == "query-planner.external.v2":
                            from bioradar.adapters.weknora_kdd import search_candidates

                            kdd_arguments["direction"] = fixed["query_policy"]["direction"]
                        found = search_candidates(
                            self.settings,
                            " ".join(receipt["plan"]["terms"]),
                            reader=self.reader,
                            selected_evidence=progress["evidence"],
                            source_ids=fixed["search_scope"],
                            cutoff=cutoff,
                            is_demo=fixed["is_demo"],
                            limit=reading_limits(fixed)["search_limit"],
                            timeout_seconds=seconds,
                            max_tool_calls=tool_allowance,
                            **kdd_arguments,
                        )
                        extra = found.get("tool_requests", 0)
                        receipt.update(
                            status="read" if found["status"] == "completed" else found["status"],
                            records=copy.deepcopy(found.get("records", [])),
                            returned=len(found.get("records", [])),
                            candidates=copy.deepcopy(found.get("candidates", [])),
                            usage=found.get("usage"),
                            error_code=found.get("error_code"),
                        )
                    else:
                        try:
                            from bioradar.adapters.query_graph import execute_graph_action
                        except ImportError:
                            found = {"status": "unavailable", "error_code": "graph_tool_unavailable"}
                        else:
                            found = execute_graph_action(
                                self.settings,
                                self.reader,
                                arguments,
                                mode=mode,
                                cutoff=cutoff,
                                idempotency_key="analysis-graph:"
                                + canonical_hash([identifier, action["id"]]),
                                timeout_seconds=seconds,
                            )
                        receipt.update(
                            status="read"
                            if found.get("status") == "completed"
                            else found.get("status", "unavailable"),
                            error_code=found.get("error_code"),
                            records=copy.deepcopy(found.get("records", [])),
                            returned=len(found.get("records", [])),
                        )
                        if found.get("graph_context") is not None or found.get("graph_records"):
                            dependency = {
                                "instance": self._source_instance(fixed["is_demo"]),
                                "source_id": arguments.get("source_id")
                                or arguments.get("binding", {}).get("source_id"),
                                "history": True,
                            }
                            if dependency["source_id"] and dependency not in fixed["dependency_sources"]:
                                fixed["dependency_sources"].append(dependency)
                        if found.get("graph_context") is not None:
                            receipt["graph_context"] = copy.deepcopy(found["graph_context"])
                            progress.setdefault("graph_context", []).append(found["graph_context"])
                            progress["query_policy"]["graph_context"] = copy.deepcopy(
                                progress["graph_context"]
                            )
                        for graph_record in found.get("graph_records", []):
                            if graph_record not in progress.setdefault("graph_records", []):
                                progress["graph_records"].append(copy.deepcopy(graph_record))
                        actual_cost = found.get("actual_cost") or found.get("cost") or {}
                        if actual_cost.get("tool_calls") is not None:
                            extra = max(
                                0,
                                actual_cost["tool_calls"]
                                - (getattr(self.reader, "requests_made", 0) - before),
                            )
                        relation_cost = (found.get("actual_cost") or found.get("cost") or {}).get(
                            "relation_access", 0
                        )
                        progress.setdefault("tool_usage", {}).setdefault("relation_access", 0)
                        previous = progress["tool_usage"]["relation_access"]
                        progress["tool_usage"]["relation_access"] = (
                            previous + relation_cost
                            if previous is not None and relation_cost is not None
                            else None
                        )
                        receipt["usage"] = copy.deepcopy(found.get("usage"))
                except DomainError as exc:
                    receipt.update(status="failed", error_code=exc.code, records=[])
                finally:
                    count = self._tool_finish(progress, before, extra=extra)
                    receipt["cost"] = {
                        "tool_calls": count,
                        "tokens": None if provider == "weknora" else 0,
                        "relation_access": relation_cost,
                    }
                for original in receipt.get("records", []):
                    dependency = {
                        "instance": self._source_instance(fixed["is_demo"]),
                        "source_id": original["source_id"],
                        "history": True,
                    }
                    if dependency not in fixed["dependency_sources"]:
                        fixed["dependency_sources"].append(dependency)
                # Persist returned originals before adoption can have an unknown PG outcome.
                self._checkpoint(identifier, token, progress, fixed=fixed)
        for original in receipt.get("records", []):
            if original.get("record_kind") == "outcome" and fixed.get("evidence_time", {}).get("study_id"):
                continue
            ref = self.reader.ref(original).model_dump(mode="json")
            if any(e["record_ref"] == ref for e in progress["evidence"]):
                continue
            if (
                len({canonical_hash(e["record_ref"]) for e in progress["evidence"]})
                >= reading_limits(fixed)["max_versions"]
            ):
                message = (
                    f"查询命中超过本轮{reading_limits(fixed)['max_versions']}版本读取预算，保留为未读资料。"
                )
                if message not in progress["limitations"]:
                    progress["limitations"].append(message)
                break
            try:
                entry = self._adopt_original(
                    identifier,
                    token,
                    fixed,
                    progress,
                    original,
                    "policy:" + canonical_hash([action["id"], ref]),
                )
            except DomainError as exc:
                if exc.code != "analysis_evidence_budget":
                    raise
                # The search succeeded; keep its returned versions as unread hits.
                # A full excerpt budget must not discard the reading already saved.
                receipt["usage"] = dict(receipt.get("usage") or {})
                receipt["usage"]["adoption_stop_reason"] = exc.code
                message = "本轮原文片段容量已满；补查命中保留为未读资料，未作为已读证据。"
                if message not in progress["limitations"]:
                    progress["limitations"].append(message)
                self._checkpoint(identifier, token, progress, fixed=fixed)
                break
            progress["evidence"].append(entry)
            receipt["new_versions"] += 1
            receipt["evidence_keys"].append(entry["key"])
            self._checkpoint(identifier, token, progress, fixed=fixed)
        return receipt

    def _after_policy_mechanism(self, identifier, token, fixed, progress, reading):
        if fixed.get("query_policy", {}).get("schema_version") == "query-planner.external.v2":
            progress.setdefault(
                "mechanism_evaluation",
                {
                    "provider": "mech_evidence",
                    "status": "not_requested",
                    "error_code": "professional_capability_not_selected",
                },
            )
            self._checkpoint(identifier, token, progress)
            return progress
        return self._mechanism(identifier, token, fixed, progress, reading)

    def _policy_research(self, identifier, token, fixed, progress):
        from bioradar.research.investigations.analysis_models import ResearchQuestionPlan
        from bioradar.research.investigations.query_policy import (
            build_policy_context,
            decide,
            pagination_candidates,
            question_named_objects,
            question_plan_payload,
            validate_frozen_policy,
            validate_initial_plans,
        )

        state = progress["query_policy"]
        external = fixed["query_policy"].get("effective") == "query_planner"
        plan_schema = (
            ResearchQuestionPlan
            if fixed["query_policy"].get("research_contract_version") == "bioradar-research-input-v1"
            else QuestionPlan
        )
        decisions = state.get("decisions", [])
        terminal = (
            decisions[-1]
            if decisions
            and decisions[-1].get("decision") in {"complete", "insufficient"}
            and decisions[-1].get("state_summary", {}).get("terminal", True)
            else None
        )
        if terminal is not None and state.get("pending_action") is None and state.get("reading"):
            if any(q["status"] in {"prepared", "sent", "unconfirmed"} for q in progress["queries"]):
                raise DomainError(
                    "analysis_query_outcome_unconfirmed", "研究动作尚无确定回执，未继续规划", 409
                )
            self._guard(identifier, token)
            state.update(status=terminal["decision"], reason=terminal.get("reason"))
            self._checkpoint(identifier, token, progress)
            reading = self._saved_policy_reading(state)
            progress = self._after_policy_mechanism(identifier, token, fixed, progress, reading)
            return reading, progress
        if "initial_plans" not in state:
            plans = fixed.get("retrieval", {}).get("initial_queries", [])
            if (
                not plans
                and not progress["evidence"]
                and fixed["budget"]["max_queries"] > 0
                and fixed["search_scope"]
            ):
                raw, progress = self._call(
                    identifier,
                    token,
                    "question_plan",
                    plan_schema,
                    fixed,
                    progress,
                    question_plan_payload(fixed),
                )
                plans = validate_initial_plans(raw, fixed)
                state["named_objects"] = question_named_objects(fixed, raw.get("named_objects", []))
                for text in plan_schema.model_validate(raw).limitations:
                    limitation = "初始查找阶段：" + text
                    if limitation not in progress["limitations"]:
                        progress["limitations"].append(limitation)
            state["initial_plans"] = copy.deepcopy(plans)
            self._checkpoint(identifier, token, progress)
        while True:
            pending = state.get("pending_action")
            if pending is not None:
                receipt = self._execute_policy_action(
                    identifier, token, fixed, progress, pending, len(state["decisions"]) - 1
                )
                decision = state["decisions"][-1]
                decision["receipt"] = {
                    "action_id": pending["id"],
                    "query_index": progress["queries"].index(receipt),
                    "status": receipt["status"],
                    "error_code": receipt.get("error_code"),
                    "evidence_ids": list(receipt.get("evidence_keys", [])),
                    "cost": copy.deepcopy(receipt.get("cost")),
                    **(
                        {"new_artifact_refs": copy.deepcopy(receipt["artifact_refs"])}
                        if receipt.get("artifact_refs")
                        else {}
                    ),
                }
                if receipt["status"] not in {"sent", "unconfirmed"}:
                    state["pending_action"] = None
                self._checkpoint(identifier, token, progress)
                if receipt["status"] in {"sent", "unconfirmed"}:
                    raise DomainError(
                        "analysis_query_outcome_unconfirmed", "实际研究动作结果未知，未继续规划", 409
                    )
            reading = self._saved_policy_reading(state) if state.get("reading") else None
            if state.get("status") in {"complete", "insufficient"} and reading is not None:
                progress = self._after_policy_mechanism(identifier, token, fixed, progress, reading)
                return reading, progress
            if not external and reading is not None and state["phase"] == "initial":
                state["phase"] = "followup"
            if not external and reading is None and state["phase"] == "followup" and progress["evidence"]:
                self._policy_reading(identifier, token, fixed, progress)
                reading = self._saved_policy_reading(state)
            if any(q["status"] in {"prepared", "sent", "unconfirmed"} for q in progress["queries"]):
                raise DomainError(
                    "analysis_query_outcome_unconfirmed", "研究动作尚无确定回执，未继续规划", 409
                )
            from bioradar.research.investigations.kdd import ensure_artifacts, extend_context

            ensure_artifacts(self, identifier, token, fixed, progress)
            if external and progress["evidence"]:
                fingerprint = canonical_hash(
                    [
                        [[e["key"], e["selection_hash"], e["text_sha256"]] for e in progress["evidence"]],
                        progress.get("mechanism_evaluation"),
                        state.get("quality_feedback"),
                        progress.get("graph_context"),
                    ]
                )
                if fixed.get("query_context_version") == "semantic-v2":
                    from bioradar.research.investigations.analysis import semantic_reading_state

                    fingerprint = canonical_hash(
                        semantic_reading_state(progress["evidence"], self._reading_context(progress))
                    )
                if state.get("reading_evidence_hash") != fingerprint:
                    if not self._policy_reading(
                        identifier, token, fixed, progress, optional=reading is not None
                    ):
                        state.setdefault("refresh_stopped_reason", "analysis_model_budget")
                    reading = self._saved_policy_reading(state) if state.get("reading") else None
                if reading is not None:
                    state["phase"] = "followup"
                    if (
                        fixed["query_policy"].get("schema_version") != "query-planner.external.v2"
                        and reading.mechanism_candidate
                        and fixed.get("providers", {}).get("mech_evidence", {}).get("available")
                    ):
                        if "mechanism_evaluation" not in progress:
                            progress = self._after_policy_mechanism(
                                identifier, token, fixed, progress, reading
                            )
                            self._policy_reading(identifier, token, fixed, progress, optional=True)
                            reading = self._saved_policy_reading(state)
            phase = state["phase"]
            plans = (
                state["initial_plans"]
                if phase == "initial"
                else [q.model_dump(mode="json") for q in reading.next_queries]
                if reading is not None
                else []
            )
            state["budget"] = self._policy_budget(fixed, progress)
            declared_context = build_policy_context(
                fixed,
                progress,
                phase=phase,
                plans=plans,
                reading=reading.model_dump(mode="json") if reading is not None else None,
                provider_status=progress.get("providers", fixed.get("providers", {})),
            )
            state["checks"] = copy.deepcopy(declared_context["checks"])
            graph_candidates = []
            if external:
                try:
                    from bioradar.adapters.query_graph import build_graph_candidates
                except ImportError:
                    pass
                else:
                    graph_candidates = build_graph_candidates(
                        self.settings,
                        fixed,
                        progress,
                        reading.model_dump(mode="json") if reading is not None else None,
                        checks=state.get("checks", []),
                    )
            context = build_policy_context(
                fixed,
                progress,
                phase=phase,
                plans=plans,
                reading=reading.model_dump(mode="json") if reading is not None else None,
                provider_status=progress.get("providers", fixed.get("providers", {})),
                graph_candidates=graph_candidates,
            )
            context = extend_context(
                fixed, progress, context, reading.model_dump(mode="json") if reading else None
            )
            if fixed["query_policy"].get("research_contract_version") == "bioradar-research-input-v1":
                state["reading_manifest"] = copy.deepcopy(context.get("reading_manifest", []))
            state["checks"] = copy.deepcopy(context.get("checks", state.get("checks", [])))
            if state["budget"]["remaining"]["queries"] == 0 or state.get("refresh_stopped_reason"):
                if external and not state.get("refresh_stopped_reason"):
                    context["candidates"] = [
                        item
                        for item in context["candidates"]
                        if item["kind"] != "document_search" or item["arguments"].get("cursor")
                    ]
                else:
                    context["candidates"] = []
                if (
                    not external
                    and state["budget"]["remaining"]["queries"] == 0
                    and not progress.get("query_relaxation")
                    and not progress.get("initial_relaxation")
                    and any(
                        q.get("provider", "fabric") == "fabric"
                        and q["status"] == "empty"
                        and len(q.get("plan", {}).get("terms", [])) > 1
                        for q in progress["queries"]
                    )
                ):
                    limitation = "本轮查询预算已用尽，未追加减词补查。"
                    if limitation not in progress["limitations"]:
                        progress["limitations"].append(limitation)
            validate_frozen_policy(self.settings, fixed["query_policy"])
            self._guard(identifier, token, check_deadline=not bool(state.get("reading")))
            from bioradar.adapters.query_planner import decide_query

            choice = decide(
                context,
                policy=fixed["query_policy"]["effective"],
                planner=lambda request: decide_query(self.settings, request),
            )
            if choice.get("state_summary", {}).get("execution_state") == "awaiting_host_candidates":
                rebuilt = pagination_candidates(fixed, progress, context["checks"])
                existing = {item["id"] for item in context["candidates"]}
                context["candidates"].extend(item for item in rebuilt if item["id"] not in existing)
                choice = decide(
                    context,
                    policy=fixed["query_policy"]["effective"],
                    planner=lambda request: decide_query(self.settings, request),
                )
                if choice.get("state_summary", {}).get("execution_state") == "awaiting_host_candidates":
                    state["continuation_wait"] = copy.deepcopy(choice["state_summary"])
                    self._checkpoint(identifier, token, progress)
                    raise DomainError(
                        "analysis_continuation_unavailable",
                        "续页回执已保留，尚无可执行的授权候选；研究未标完成",
                        409,
                    )
            action = choice.get("action")
            if choice["decision"] == "continue":
                if action is None or not any(c == action for c in context.get("candidates", [])):
                    raise DomainError(
                        "analysis_policy_action_invalid", "策略未返回当前完整候选，未执行动作", 422
                    )
            elif action is not None:
                raise DomainError("analysis_policy_action_invalid", "结束决策不能同时要求执行动作", 422)
            observed = []
            for item in context.get("history", []):
                action_id = item.get("action_id", item.get("id"))
                actual = next(
                    ((i, q) for i, q in enumerate(progress["queries"]) if q.get("action_id") == action_id),
                    None,
                )
                observed.append(
                    {
                        "action_id": action_id,
                        "status": actual[1]["status"] if actual is not None else item.get("status"),
                        "policy_status": item.get("status"),
                        "query_index": actual[0] if actual is not None else item.get("query_index"),
                        "evidence_ids": list(actual[1].get("evidence_keys", []))
                        if actual is not None
                        else item.get("evidence_ids", item.get("new_evidence_ids", [])),
                        **(
                            {
                                "new_artifact_refs": copy.deepcopy(
                                    actual[1].get("artifact_refs", [])
                                    if actual
                                    else item.get("new_artifact_refs", [])
                                ),
                                "check_updates": copy.deepcopy(
                                    actual[1].get("check_updates", [])
                                    if actual
                                    else item.get("check_updates", [])
                                ),
                            }
                            if fixed.get("query_policy", {}).get("schema_version")
                            == "query-planner.external.v2"
                            else {}
                        ),
                    }
                )
            choice = copy.deepcopy(choice)
            if (
                choice["decision"] != "continue"
                and phase == "initial"
                and not external
                and progress["evidence"]
            ):
                state.setdefault("phase_transitions", []).append(
                    {
                        "from": "initial",
                        "to": "followup",
                        "after_step": len(state["decisions"]) - 1,
                        "phase_decision": choice,
                        "observed_history": observed,
                    }
                )
                state["phase"] = "followup"
                self._checkpoint(identifier, token, progress)
                self._policy_reading(identifier, token, fixed, progress)
                continue
            choice["step"] = len(state["decisions"])
            if context.get("schema_version") == "query-planner.external.v2":
                choice["policy_request"] = copy.deepcopy(context)
            choice.setdefault("state_summary", {})["observed_history"] = observed
            choice["receipt"] = None
            state["decisions"].append(choice)
            state["pending_action"] = copy.deepcopy(action)
            if action is not None and action.get("relaxation") is not None:
                progress.setdefault("query_relaxation", copy.deepcopy(action["relaxation"]))
            state["reason"] = choice.get("reason")
            if choice["decision"] != "continue":
                state["status"] = choice["decision"]
            self._checkpoint(identifier, token, progress, fixed=fixed)
            if action is None:
                break
        if not progress["evidence"]:
            raise DomainError(
                "analysis_no_material", "本轮实际查询未取得可解读材料，已保留真实回执与缺口", 422
            )
        if not state.get("reading"):
            self._policy_reading(identifier, token, fixed, progress)
        reading = self._saved_policy_reading(state)
        progress = self._after_policy_mechanism(identifier, token, fixed, progress, reading)
        return reading, progress

    def _initial_retrieval(self, identifier, token, fixed, progress):
        retrieval = fixed.get("retrieval", {})
        if not retrieval.get("auto_retrieve") or progress.get("initial_retrieval_done"):
            return progress
        plans = [InitialQuery.model_validate(q) for q in retrieval.get("initial_queries", [])]
        if not plans and progress["evidence"]:
            return progress
        if fixed["budget"]["max_queries"] == 0:
            raise DomainError("analysis_query_budget", "本轮查询预算为零，无法为问题取得资料", 422)
        if not fixed["search_scope"]:
            raise DomainError("analysis_no_retrieval_scope", "没有可用的授权补查来源，问题已保留", 422)
        if not plans:
            raw, progress = self._call(
                identifier,
                token,
                "question_plan",
                QuestionPlan,
                fixed,
                progress,
                {
                    "task": "question_plan",
                    "question": fixed["question"],
                    "search_scope": fixed["search_scope"],
                    "max_queries": min(2, fixed["budget"]["max_queries"]),
                    "instruction": (
                        "仅规划在允许来源内的最多两项字面查询，每项最多三个词。"
                        "terms中的每个词是必须同时满足的字面条件（AND），不是任选词或语义扩展。"
                        "先用辨识力强且问题中确实出现的研究对象词；对象词放第一项。"
                        "首查通常只需一个药物、靶点、疾病或登记编号，不叠加机制、试验类型等修饰词。"
                        "当前没有已读证据，不输出事实、命题或结论。"
                    ),
                },
            )
            try:
                plan = QuestionPlan.model_validate(raw)
            except ValueError:
                raise DomainError(
                    "analysis_question_plan_invalid", "初查规划格式无效，未补造查询", 422
                ) from None
            plans = plan.queries
            if len(plans) > min(2, fixed["budget"]["max_queries"]):
                raise DomainError("analysis_query_budget", "模型初查规划超过固定查询预算", 422)
            progress["limitations"].extend("初始查找阶段：" + text for text in plan.limitations)
        if any(p.source_id not in fixed["search_scope"] for p in plans):
            raise DomainError("analysis_query_invalid", "初查规划越过本次授权来源", 422)
        progress = self._retrieve(identifier, token, fixed, progress, plans, phase="initial")
        progress = self._relax_query(identifier, token, fixed, progress, plans, phase="initial")
        progress = self._weknora_retrieval(identifier, token, fixed, progress, plans, phase="initial")
        progress["initial_retrieval_done"] = True
        self._checkpoint(identifier, token, progress, fixed=fixed)
        return progress

    def _relax_query(self, identifier, token, fixed, progress, plans, *, phase):
        relaxation = progress.get("query_relaxation") or progress.get("initial_relaxation")
        if relaxation is not None and relaxation.get("phase", "initial") != phase:
            return progress
        if relaxation is None:
            for index, plan in enumerate(plans):
                previous = next(
                    (
                        q
                        for q in progress["queries"]
                        if q.get("provider", "fabric") == "fabric"
                        and q.get("phase") == phase
                        and q["index"] == index
                    ),
                    None,
                )
                terms = relaxed_query_terms(fixed["question"], plan.terms)
                if previous is None or previous["status"] != "empty" or terms is None:
                    continue
                if any(
                    q.get("provider", "fabric") == "fabric"
                    and q["plan"]["source_id"] == plan.source_id
                    and [t.strip().casefold() for t in q["plan"]["terms"]]
                    == [t.strip().casefold() for t in terms]
                    for q in progress["queries"]
                ):
                    continue
                if len(progress["queries"]) >= fixed["budget"]["max_queries"]:
                    message = "多词字面查询为空；本轮查询预算已满，未追加减词补查。"
                    if message not in progress["limitations"]:
                        progress["limitations"].append(message)
                        self._checkpoint(identifier, token, progress)
                    return progress
                reason = (
                    "原查询的字面条件须同时满足且结果为空；在同一授权来源仅减词补查一次，"
                    "优先保留问题中出现的原对象词。原terms：" + json.dumps(plan.terms, ensure_ascii=False)
                )
                relaxed = plan.model_copy(update={"terms": terms, "reason": reason})
                relaxation = {
                    "phase": phase,
                    "query_index": len(plans),
                    "original_plan": plan.model_dump(mode="json"),
                    "plan": relaxed.model_dump(mode="json"),
                    "reason": reason,
                }
                progress["query_relaxation"] = relaxation
                self._checkpoint(identifier, token, progress)
                break
        if relaxation is not None:
            plan_type = InitialQuery if phase == "initial" else PlannedQuery
            relaxed = plan_type.model_validate(relaxation["plan"])
            return self._retrieve(identifier, token, fixed, progress, [*plans, relaxed], phase=phase)
        return progress

    def _queries(self, identifier, token, fixed, progress, reading):
        progress = self._retrieve(identifier, token, fixed, progress, reading.next_queries, phase="followup")
        return self._relax_query(identifier, token, fixed, progress, reading.next_queries, phase="followup")

    def _retrieve(self, identifier, token, fixed, progress, plans, *, phase):
        from bioradar.adapters.fabric_monitor import search_analysis_records

        for index, plan in enumerate(plans):
            self._guard(identifier, token)
            receipt = next(
                (
                    q
                    for q in progress["queries"]
                    if q.get("provider", "fabric") == "fabric"
                    and q.get("phase", "followup") == phase
                    and q["index"] == index
                ),
                None,
            )
            if receipt is None:
                if len(progress["queries"]) >= fixed.get("budget", {}).get("max_queries", 2):
                    progress["limitations"].append("达到本轮查询预算，尚未执行的计划保留为缺口。")
                    self._checkpoint(identifier, token, progress)
                    break
                progress["stage"] = "initial_retrieval" if phase == "initial" else "digging"
                relaxation = progress.get("query_relaxation", progress.get("initial_relaxation", {}))
                is_relaxed = (
                    relaxation.get("phase", "initial") == phase and relaxation.get("query_index") == index
                )
                receipt = AnalysisQueryReceipt(
                    index=index,
                    phase=phase,
                    plan=plan,
                    status="prepared",
                    relaxed_from=relaxation.get("original_plan") if is_relaxed else None,
                    relaxation_reason=relaxation.get("reason") if is_relaxed else None,
                ).model_dump(mode="json")
                progress["queries"].append(receipt)
                self._checkpoint(identifier, token, progress)
            if "records" not in receipt and receipt["status"] == "prepared":
                self._guard(identifier, token, check_deadline=True)
                if self.reader is None:
                    receipt.update(status="failed", error_code="history_not_connected")
                    self._checkpoint(identifier, token, progress)
                    continue
                try:
                    found = search_analysis_records(
                        self.reader,
                        source_id=plan.source_id,
                        is_demo=fixed["is_demo"],
                        terms=plan.terms,
                        cutoff=datetime.fromisoformat(fixed["as_of"]),
                        limit=reading_limits(fixed)["search_limit"],
                    )
                except DomainError as exc:
                    receipt.update(status="failed", error_code=exc.code)
                    self._checkpoint(identifier, token, progress)
                    continue
                receipt.update(
                    status="read" if found["items"] else "empty",
                    returned=len(found["items"]),
                    has_more=found.get("next_cursor") is not None,
                    records=[copy.deepcopy(r) for r in found["items"]],
                )
                # The actual returned versions are fixed before any possibly ambiguous PG adoption.
                dependency = {
                    "instance": self._source_instance(fixed["is_demo"]),
                    "source_id": plan.source_id,
                    "history": True,
                }
                if dependency not in fixed["dependency_sources"]:
                    fixed["dependency_sources"].append(dependency)
                self._checkpoint(identifier, token, progress, fixed=fixed)
            for original in receipt.get("records", []):
                ref = self.reader.ref(original)
                if any(e["record_ref"] == ref.model_dump(mode="json") for e in progress["evidence"]):
                    continue
                if (
                    len({canonical_hash(e["record_ref"]) for e in progress["evidence"]})
                    >= reading_limits(fixed)["max_versions"]
                ):
                    progress["limitations"].append("查询返回超过本轮10版本证据预算，未将其冒充已读。")
                    break
                tag = "query:" + canonical_hash([phase, index, ref.model_dump(mode="json")])
                entry = self._adopt_original(identifier, token, fixed, progress, original, tag)
                receipt["new_versions"] += 1
                receipt["evidence_keys"].append(entry["key"])
                progress["evidence"].append(entry)
                self._checkpoint(identifier, token, progress, fixed=fixed)
        return progress

    def _weknora_retrieval(self, identifier, token, fixed, progress, plans, *, phase):
        if fixed.get("evidence_time", {}).get("mode", "current") != "current":
            return progress
        from bioradar.adapters.weknora import provider_status, search_candidates

        receipt = next((q for q in progress["queries"] if q.get("provider") == "weknora"), None)
        if receipt is None:
            if (
                not plans
                or not provider_status(self.settings).get("available")
                or len(progress["queries"]) >= fixed.get("budget", {}).get("max_queries", 2)
                or len({canonical_hash(e["record_ref"]) for e in progress["evidence"]})
                >= reading_limits(fixed)["max_versions"]
            ):
                return progress
            first = plans[0]
            plan = InitialQuery(source_id=first.source_id, terms=first.terms, reason=first.reason)
            receipt = AnalysisQueryReceipt(
                index=0,
                phase=phase,
                provider="weknora",
                plan=plan,
                status="prepared",
                scope="weknora_candidates_bound_to_authorized_fabric_versions",
            ).model_dump(mode="json")
            progress["queries"].append(receipt)
            self._checkpoint(identifier, token, progress)
        if receipt["status"] in {"sent", "unconfirmed"}:
            receipt.update(status="unconfirmed", error_code="weknora_outcome_unconfirmed")
            self._checkpoint(identifier, token, progress)
            return progress
        if (
            receipt.get("error_code") == "analysis_time_budget"
            and (receipt.get("usage") or {}).get("request_status") == "not_sent"
        ):
            raise DomainError("analysis_time_budget", "候选请求此前在发送前达到时间预算，未自动重发", 409)
        if receipt["status"] == "prepared":
            self._guard(identifier, token, check_deadline=True)
            receipt["status"] = "sent"
            self._checkpoint(identifier, token, progress)
            seconds_left = analysis_budget(fixed, progress, datetime.now(UTC))["usage"]["remaining_seconds"]
            if seconds_left < 0.1:
                receipt.update(
                    status="failed",
                    error_code="analysis_time_budget",
                    records=[],
                    usage={
                        "request_status": "not_sent",
                        "embedding_rerank_tokens": None,
                        "monetary_cost": None,
                    },
                )
                self._checkpoint(identifier, token, progress)
                raise DomainError("analysis_time_budget", "发送意图保存后时间预算已耗尽，候选请求未发送", 409)
            found = search_candidates(
                self.settings,
                " ".join(receipt["plan"]["terms"]),
                reader=self.reader,
                selected_evidence=progress["evidence"],
                source_ids=fixed["search_scope"],
                cutoff=datetime.fromisoformat(fixed["as_of"]),
                is_demo=fixed["is_demo"],
                limit=reading_limits(fixed)["search_limit"],
                timeout_seconds=seconds_left,
            )
            status = found["status"]
            receipt.update(
                status="read" if status == "completed" else status,
                returned=len(found.get("records", [])),
                records=copy.deepcopy(found.get("records", [])),
                candidates=copy.deepcopy(found.get("candidates", [])),
                usage=found.get("usage"),
                error_code=found.get("error_code"),
            )
            for original in receipt["records"]:
                dependency = {
                    "instance": self._source_instance(fixed["is_demo"]),
                    "source_id": original["source_id"],
                    "history": True,
                }
                if dependency not in fixed["dependency_sources"]:
                    fixed["dependency_sources"].append(dependency)
            self._checkpoint(identifier, token, progress, fixed=fixed)
        for original in receipt.get("records", []):
            ref = self.reader.ref(original).model_dump(mode="json")
            if any(e["record_ref"] == ref for e in progress["evidence"]):
                continue
            if (
                len({canonical_hash(e["record_ref"]) for e in progress["evidence"]})
                >= reading_limits(fixed)["max_versions"]
            ):
                break
            item = self._adopt_original(
                identifier, token, fixed, progress, original, "weknora:" + canonical_hash(ref)
            )
            progress["evidence"].append(item)
            receipt["new_versions"] += 1
            receipt["evidence_keys"].append(item["key"])
            self._checkpoint(identifier, token, progress, fixed=fixed)
        return progress

    def _saved(self, operation, key):
        with Session(self.engine.database) as session:
            row = session.scalar(
                select(EngineIdempotency).where(
                    EngineIdempotency.workspace_id == self.actor.workspace_id,
                    EngineIdempotency.actor_id == self.actor.actor_id,
                    EngineIdempotency.operation == operation,
                    EngineIdempotency.key == key,
                )
            )
            return copy.deepcopy(row.response_body) if row and row.response_code is not None else None

    def _adopt_original(self, identifier, token, fixed, progress, original, tag, *, start=None):
        from bioradar.adapters.fabric_history import time_upper

        self._guard(identifier, token)
        key = "analysis-snapshot:" + canonical_hash([identifier, tag])
        adopted = self._saved("snapshot.create", key)
        if adopted is None:
            snapshot = self.reader.snapshot(
                {
                    "items": [original],
                    "source_id": original["source_id"],
                    "is_demo": fixed["is_demo"],
                    "coverage": {"scope_description": "机器分析的固定有限查询版本"},
                }
            )
            if snapshot is None:
                raise DomainError("analysis_material_unavailable", "命中版本没有可采用的文本", 422)
            self._guard(identifier, token)
            _, adopted = self.engine.adopt_snapshot(self.actor, snapshot, key)
        if adopted["id"] not in fixed["snapshot_ids"]:
            fixed["snapshot_ids"].append(adopted["id"])
        current_versions = {
            (e["record_ref"]["provider"], e["record_ref"]["object_id"], e["record_ref"]["adopted_version"])
            for e in progress["evidence"]
        }
        from bioradar.adapters.query_graph import remember_graph_package

        remember_graph_package(fixed, progress, original)
        original_ref = self.reader.ref(original)
        version_key = (original_ref.provider, original_ref.object_id, original_ref.adopted_version)
        if (
            version_key not in current_versions
            and len(current_versions) >= reading_limits(fixed)["max_versions"]
        ):
            raise DomainError("analysis_evidence_budget", "本轮读取版本数已满，命中保留为未读资料", 422)
        remaining = reading_limits(fixed)["max_characters"] - sum(
            len(e["text"]) + len(json.dumps(e.get("facts", {}), ensure_ascii=False))
            for e in progress["evidence"]
        )
        selected = source_excerpt(original, remaining, start=start)
        if not selected["text"]:
            raise DomainError("analysis_evidence_budget", "本轮片段预算用尽，新增版本未送入模型", 422)
        ref = self.reader.ref(original)
        result = AnalysisEvidence(
            key="q"
            + canonical_hash(
                [original["source_id"], original["source_record_id"], original["version_id"]]
                + ([start] if start else [])
            )[:12],
            material_entry_id=None,
            snapshot_id=adopted["id"],
            record_ref=ref,
            source_id=original["source_id"],
            title=original["title"],
            **selected,
            history_binding={
                k: original[k]
                for k in ("source_id", "source_record_id", "version_id", "raw_sha256", "is_demo")
            },
            publication_time=json.dumps(original["source_published_at"], ensure_ascii=False)
            if original.get("source_published_at")
            else None,
            source_url=original.get("source_url"),
        ).model_dump(mode="json")
        # Immutable source clocks, never wall-clock now, determine a repeated adoption body.
        if time_upper(original.get("received_at")) is None:
            raise DomainError("analysis_source_mismatch", "来源缺可信读取时间", 422)
        return freeze_passages(result)

    def _append(self, identifier, token, fixed, progress, body, tag):
        self._guard(identifier, token)
        with Session(self.engine.database) as session:
            row = session.get(InvestigationAnalysisRecord, identifier)
            investigation_id = row.investigation_id
        key = "analysis-entry:" + canonical_hash([identifier, tag])
        operation = "investigation.append:" + investigation_id
        saved = self._saved(operation, key)
        if saved:
            progress["expected_revision"] = saved["revision"]
            self._checkpoint(identifier, token, progress)
            return InvestigationService(self.engine).get(self.actor, investigation_id)
        intents = progress.setdefault("append_intents", [])
        intent = next((item for item in intents if item["key"] == key), None)
        if intent is None:
            intent = {"key": key, "body": {"expected_revision": progress["expected_revision"], **body}}
            intents.append(intent)
            self._checkpoint(identifier, token, progress)
        self._guard(identifier, token)
        _, saved = InvestigationService(self.engine).append(
            self.actor,
            investigation_id,
            InvestigationEntryCreate.model_validate(intent["body"]),
            key,
            machine_analysis_id=identifier,
        )
        progress["expected_revision"] = saved["revision"]
        self._checkpoint(identifier, token, progress)
        return InvestigationService(self.engine).get(self.actor, saved["id"])

    def _shared_callback(self, identifier, token, fixed, progress):
        def call(stage, instruction, payload, response_schema=None, *, call_id=None):
            output, _ = self._call(
                identifier,
                token,
                stage,
                response_schema or {"type": "object"},
                fixed,
                progress,
                payload,
                system_prompt=instruction,
                call_key=call_id or stage,
            )
            return output

        return call

    def _use_provider(self, identifier, token, fixed, progress, name, version):
        """Lock the methods actually loaded for this invocation, not an earlier status observation."""
        if version is None:
            return
        prefix = "biocheck_" if name == "biocheck" else "mech_"
        existing_calls = any(c["stage"].startswith(prefix) for c in progress["calls"])
        versions = progress.setdefault("provider_versions", {})
        previous = versions.get(name)
        if previous is None and existing_calls:
            # Earlier runs recorded the same value only in their creation-time status.
            previous = fixed.get("providers", {}).get(name, {}).get("version")
        if existing_calls and previous is not None and previous != version:
            raise DomainError(
                "analysis_provider_changed", "已使用的共享方法版本已变化，旧回执保留，请明确另起尝试", 409
            )
        versions[name] = version
        providers = copy.deepcopy(progress.get("providers", fixed.get("providers", {})))
        providers.setdefault(name, {}).update(version=version, version_basis="actually_loaded_method")
        progress["providers"] = providers
        self._checkpoint(identifier, token, progress)

    def _load_professional(self, identifier, token, fixed, progress, name):
        from bioradar.research.investigations.professional import load_provider

        return load_provider(self, identifier, token, fixed, progress, name)

    def _mechanism(self, identifier, token, fixed, progress, reading):
        from bioradar.research.investigations.professional import mechanism

        return mechanism(self, identifier, token, fixed, progress, reading)

    @staticmethod
    def _report_budget_stop(exc):
        from bioradar.adapters.reasoning_model import ModelCallError

        if isinstance(exc, ModelCallError):
            if exc.receipt.status != "not_sent":
                return False
            if exc.code == "model_input_budget":
                return True
        return isinstance(exc, DomainError) and exc.code in {
            "analysis_model_budget",
            "analysis_token_budget",
            "analysis_time_budget",
            "analysis_query_budget",
            "analysis_tool_budget",
        }

    @staticmethod
    def _after_report_budget(fixed, progress, calls):
        budget = analysis_budget(fixed, progress, datetime.now(UTC))
        remaining_calls = budget["limits"]["max_calls"] - len(progress["calls"])
        remaining_tokens = budget["limits"]["max_tokens"] - budget["usage"]["budgeted_tokens"]
        input_bytes = min(
            fixed["model_parameters"]["max_input_bytes"],
            sum(len(e["text"].encode()) for e in progress["evidence"]) + 4096,
        )
        return (
            remaining_calls >= calls
            and remaining_tokens >= calls * (input_bytes + 128)
            and budget["usage"]["remaining_seconds"] > 0
        )

    @staticmethod
    def _report_source_errors(progress):
        return [
            {
                "action_id": q.get("action_id"),
                "source_id": q.get("plan", {}).get("source_id"),
                "error_code": q.get("error_code"),
                "status": q["status"],
            }
            for q in progress.get("queries", [])
            if q["status"] in {"failed", "unavailable", "unconfirmed"} and q.get("error_code")
        ]

    def _activate_report_round(self, identifier, token, fixed, progress, round_):
        progress["active_report_round"] = round_["index"]
        if round_.get("activated"):
            return
        for key in ("final_candidate", "final_report_plan", "final_report", "support_audit_plan"):
            progress.pop(key, None)
        progress.update(copy.deepcopy(round_.get("fields", {})))
        progress.setdefault(
            "final_report_plan",
            {
                "repair": False,
                "reserve_support_audit": True,
                "basis": "quality_feedback_generation_then_independent_audit",
                **(
                    {"retry_policy_pending": True}
                    if fixed.get("query_policy", {}).get("professional_retry_version")
                    == "biocheck-contract-repair-v1"
                    else {}
                ),
            },
        )
        round_["activated"] = True
        self._checkpoint(identifier, token, progress, fixed=fixed)

    @staticmethod
    def _snapshot_report_round(progress, round_, checked):
        round_["result"] = checked.model_dump(mode="json")
        round_["text_sha256"] = (checked.report or {}).get("text_sha256")
        round_["fields"] = {
            key: copy.deepcopy(progress[key])
            for key in ("final_candidate", "final_report_plan", "final_report", "support_audit_plan")
            if key in progress
        }

    @staticmethod
    def _reading_bindings(reading):
        return {
            claim["id"]: sorted(
                {
                    (link["evidence_key"], link.get("passage_id"), link["role"])
                    for link in claim.get("evidence_links", [])
                }
            )
            for claim in (reading or {}).get("claims", [])
        }

    @classmethod
    def _reading_bindings_changed(cls, before, after):
        old, new = cls._reading_bindings(before), cls._reading_bindings(after)
        old_refs = {tuple(link) for refs in old.values() for link in refs}
        new_refs = {tuple(link) for refs in new.values() for link in refs}
        return bool(new_refs) and (
            old_refs != new_refs or any(old[key] != new[key] for key in old.keys() & new.keys())
        )

    def _synthesis_payload(self, fixed, progress):
        context = None
        queries = [{k: v for k, v in q.items() if k != "records"} for q in progress["queries"]]
        if (
            fixed.get("query_context_version") == "semantic-v2"
            and fixed.get("query_policy", {}).get("effective") == "query_planner"
        ):
            from bioradar.research.investigations.analysis import compact_query_context

            context = compact_query_context(self._reading_context(progress))
            queries = context["query_history"]
        payload = freeze_payload(
            "synthesize", fixed, progress["evidence"], queries=queries, query_context=context
        )
        assessment = final_assessment_context(fixed)
        if assessment:
            payload["final_research_assessment"] = assessment
        return payload

    def _normalize_synthesis(self, identifier, token, fixed, progress, raw, stage):
        schema = synthesis_schema(fixed)
        if schema is not ModelSynthesis:
            risk_scope = (fixed.get("origin") or {}).get("topic_risk_scope")
            try:
                validate_synthesis(
                    raw,
                    progress["evidence"],
                    parent=None,
                    schema=schema,
                    topic_risk_scope=risk_scope,
                    strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                    == "bioradar-research-input-v1",
                )
            except DomainError as exc:
                round_ = progress.get("active_report_round", 0)
                prefix = "report-round:" + str(round_) + ":" if round_ else ""
                key = prefix + stage
                initial = next(c for c in progress["calls"] if c.get("call_key", c["stage"]) == key)
                saved = next(
                    (c for c in progress["calls"] if c.get("call_key") == key + ":assessment-repair"),
                    None,
                )
                # All reference errors share this existing one repair slot. A saved
                # v1 request keeps its original payload and validator after an upgrade.
                enhanced = (
                    saved["payload"].get("task") == "repair_final_references"
                    if saved is not None
                    else fixed.get("query_policy", {}).get("final_reference_repair_version")
                    == "bioradar-synthesis-reference-repair-v1"
                )
                if enhanced:
                    if exc.code not in {
                        "analysis_output_invalid",
                        "analysis_assessment_invalid",
                        "analysis_reference_invalid",
                        "analysis_passage_invalid",
                    }:
                        raise
                    payload = final_reference_repair_payload(
                        initial["payload"],
                        raw,
                        validation_error=exc.code,
                        topic_risk_scope=risk_scope,
                    )
                else:
                    errors = claim_assessment_errors(raw, topic_risk_scope=risk_scope)
                    if (
                        exc.code not in {"analysis_output_invalid", "analysis_assessment_invalid"}
                        or not errors
                        or any(item["reason"] != "unknown_claim" for item in errors)
                    ):
                        raise
                    payload = assessment_repair_payload(
                        initial["payload"],
                        raw,
                        validation_error=exc.code,
                        topic_risk_scope=risk_scope,
                    )
                if saved is not None:
                    payload = copy.deepcopy(saved["payload"])
                repaired, _ = self._call(
                    identifier,
                    token,
                    stage,
                    schema,
                    fixed,
                    progress,
                    payload,
                    call_key=stage + ":assessment-repair",
                )
                if enhanced:
                    validate_final_reference_repair(
                        raw, repaired, initial["payload"], topic_risk_scope=risk_scope
                    )
                else:
                    validate_assessment_repair(raw, repaired)
                validate_synthesis(
                    repaired,
                    progress["evidence"],
                    parent=None,
                    schema=schema,
                    topic_risk_scope=risk_scope,
                    strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                    == "bioradar-research-input-v1",
                )
                return repaired
            return raw
        if not (
            fixed.get("query_context_version") == "semantic-v2"
            and fixed.get("query_policy", {}).get("effective") == "query_planner"
        ):
            return raw
        from bioradar.research.investigations.analysis import normalize_policy_synthesis

        normalized, adjustments = normalize_policy_synthesis(raw)
        if adjustments:
            call = next(c for c in reversed(progress["calls"]) if c["stage"] == stage)
            key = call.get("call_key", stage)
            progress.setdefault("model_output_adjustments", {})[key] = adjustments
            self._checkpoint(identifier, token, progress)
        return normalized

    @staticmethod
    def _final_assessment_fields(parsed):
        from bioradar.research.investigations.analysis_models import FinalResearchSynthesis

        return (
            {
                name: getattr(parsed, name)
                for name in (
                    "warning_assessment",
                    "competing_hypotheses",
                    "current_judgment",
                    "topic_risk_reassessment",
                )
            }
            if isinstance(parsed, FinalResearchSynthesis)
            else {}
        )

    def _quality_synthesis(self, identifier, token, fixed, progress):
        payload = self._synthesis_payload(fixed, progress)
        payload["quality_evidence_selection"] = {
            "qualification": "已通过本轮固定原文位置校验的机器引用选择；仍须综合与正文核验。",
            "claim_bindings": self._reading_bindings(progress["query_policy"].get("reading")),
        }
        raw, progress = self._call(
            identifier,
            token,
            "synthesize",
            synthesis_schema(fixed),
            fixed,
            progress,
            payload,
        )
        raw = self._normalize_synthesis(identifier, token, fixed, progress, raw, "synthesize")
        parsed, verified = validate_synthesis(
            raw,
            progress["evidence"],
            parent=None,
            schema=synthesis_schema(fixed),
            topic_risk_scope=(fixed.get("origin") or {}).get("topic_risk_scope"),
            strict_references=fixed.get("query_policy", {}).get("research_contract_version")
            == "bioradar-research-input-v1",
        )
        checked = AnalysisResult(
            claims=verified,
            explanations=parsed.explanations,
            gaps=parsed.gaps,
            conditional_answer=parsed.conditional_answer,
            update=program_update(fixed.get("parent_result") is not None, review_passed=False),
            review=GenericReview(
                status="needs_review",
                claims=[],
                overall_limitations=["补查后的结构化分析供最终成稿使用；正文须取得本轮独立核验及片段复核。"],
            ),
            **self._final_assessment_fields(parsed),
            limitations=progress["limitations"],
        )
        return checked, raw

    def _reopen_quality_queries(self, identifier, token, fixed, progress, feedback):
        state = progress["query_policy"]
        if state["decisions"] and state["decisions"][-1]["decision"] in {"complete", "insufficient"}:
            stopped = state["decisions"].pop()
            state.setdefault("phase_transitions", []).append(
                {
                    "from": state.get("status"),
                    "to": "quality_feedback",
                    "after_step": len(state["decisions"]) - 1,
                    "phase_decision": stopped,
                    "scope": "initial_retrieval_stopped_before_quality_feedback",
                }
            )
        state.update(
            status="running",
            phase="followup",
            quality_round=1,
            quality_feedback=copy.deepcopy(feedback),
            reading=None,
            reading_evidence_hash=None,
        )
        state.pop("refresh_stopped_reason", None)
        fixed["quality_feedback"] = copy.deepcopy(feedback)
        self._checkpoint(identifier, token, progress, fixed=fixed)

    def _final_report(self, identifier, token, fixed, progress, checked, raw):

        if fixed.get("execution_version") != "query-step-v1":
            return self._saved_final_report(identifier, token, fixed, progress, checked, raw)
        loaded = self._load_professional(identifier, token, fixed, progress, "biocheck")
        if not callable(getattr(loaded[0], "quality_feedback", None)):
            # An older professional module retains its saved bounded repair behavior.
            return self._saved_final_report(identifier, token, fixed, progress, checked, raw)
        return self._quality_reports(identifier, token, fixed, progress, checked, raw, loaded)

    def _quality_reports(self, identifier, token, fixed, progress, checked, raw, loaded):
        from bioradar.adapters.biocheck import (
            audit_biocheck_support,
            build_packet,
            check_biocheck,
            quality_feedback_biocheck,
            revise_biocheck,
            support_audit_targets,
        )

        workflow = progress.get("report_workflow")
        if workflow is None:
            workflow = {
                "version": "biocheck-quality-feedback-v1",
                "phase": "generation",
                "base_checked": checked.model_dump(mode="json"),
                "base_synthesis": copy.deepcopy(raw),
                "evidence_feedback_used": False,
                "repair_used": False,
            }
            progress["report_workflow"] = workflow
            progress["report_rounds"] = [
                {
                    "index": 0,
                    "kind": "generation",
                    "status": "running",
                    "analysis": checked.model_dump(mode="json"),
                    "synthesis": copy.deepcopy(raw),
                    "evidence": copy.deepcopy(progress["evidence"]),
                }
            ]
            progress["active_report_round"] = 0
            self._checkpoint(identifier, token, progress, fixed=fixed)
        rounds = progress["report_rounds"]
        while workflow["phase"] != "complete":
            current = rounds[progress["active_report_round"]]
            phase = workflow["phase"]
            if phase == "generation":
                try:
                    self._activate_report_round(identifier, token, fixed, progress, current)
                    if current.get("analysis") is None:
                        candidate, candidate_raw = self._quality_synthesis(identifier, token, fixed, progress)
                        current.update(
                            analysis=candidate.model_dump(mode="json"),
                            synthesis=copy.deepcopy(candidate_raw),
                            evidence=copy.deepcopy(progress["evidence"]),
                        )
                        self._checkpoint(identifier, token, progress, fixed=fixed)
                    candidate = AnalysisResult.model_validate(current["analysis"])
                    if current.get("result") is None:
                        candidate, progress = self._saved_final_report(
                            identifier, token, fixed, progress, candidate, current["synthesis"]
                        )
                        current["status"] = "checked"
                        self._snapshot_report_round(progress, current, candidate)
                        self._checkpoint(
                            identifier, token, progress, result=candidate.model_dump(mode="json")
                        )
                except DomainError as exc:
                    prior = next(
                        (item for item in reversed(rounds[: current["index"]]) if item.get("result")), None
                    )
                    if prior is None:
                        raise
                    current.update(error_code=exc.code)
                    self._checkpoint(identifier, token, progress, result=copy.deepcopy(prior["result"]))
                    if not self._report_budget_stop(exc):
                        raise
                    current.update(status="stopped_budget", result=copy.deepcopy(prior["result"]))
                    workflow["phase"] = "complete"
                    self._checkpoint(identifier, token, progress, result=copy.deepcopy(prior["result"]))
                    continue
                checked = AnalysisResult.model_validate(current["result"])
                packet = build_packet(
                    fixed["question"],
                    current["evidence"],
                    self._report_context(fixed, progress, checked.model_dump(mode="json"), freeze=True),
                    input_version=fixed.get("query_policy", {}).get("biocheck_input_version"),
                )
                if "quality_feedback" not in current:
                    report = checked.report or {}
                    current["quality_feedback"] = quality_feedback_biocheck(
                        self.settings,
                        packet,
                        report.get("verification") or {},
                        audit=report.get("support_audit"),
                        source_errors=self._report_source_errors(progress),
                        loaded=loaded,
                    )
                    checked.report["quality_feedback"] = copy.deepcopy(current["quality_feedback"])
                    self._snapshot_report_round(progress, current, checked)
                    self._checkpoint(identifier, token, progress, result=checked.model_dump(mode="json"))
                feedback = current["quality_feedback"]
                can_query = (
                    fixed["query_policy"].get("effective") == "query_planner"
                    and not workflow["evidence_feedback_used"]
                    and bool(feedback.get("evidence_gaps"))
                    and not feedback.get("review_required")
                    and self._policy_budget(fixed, progress)["remaining"]["queries"] > 0
                    and self._policy_budget(fixed, progress)["remaining"]["tool_calls"] > 0
                    and self._optional_budget(fixed, progress, 2)
                )
                if can_query:
                    workflow.update(
                        evidence_feedback_used=True,
                        phase="feedback_query",
                        feedback_evidence_count=len(progress["evidence"]),
                        feedback_reading=copy.deepcopy(progress["query_policy"].get("reading")),
                    )
                    self._reopen_quality_queries(identifier, token, fixed, progress, feedback)
                else:
                    if feedback.get("evidence_gaps"):
                        current["unsearched_gaps"] = copy.deepcopy(feedback["evidence_gaps"])
                        current["feedback_stop_reason"] = (
                            "quality_feedback_round_limit"
                            if workflow["evidence_feedback_used"]
                            else "baseline_fixed_sequence"
                            if fixed["query_policy"].get("effective") == "baseline"
                            else "budget_or_review_required"
                        )
                    workflow["phase"] = "repair"
                    self._checkpoint(identifier, token, progress, result=checked.model_dump(mode="json"))
            elif phase == "feedback_query":
                preserved = AnalysisResult.model_validate(current["result"])
                try:
                    _, progress = self._policy_research(identifier, token, fixed, progress)
                except DomainError as exc:
                    workflow["feedback_error_code"] = exc.code
                    self._checkpoint(identifier, token, progress, result=preserved.model_dump(mode="json"))
                    if not self._report_budget_stop(exc):
                        raise
                    workflow["phase"] = "repair"
                else:
                    reselected = self._reading_bindings_changed(
                        workflow.get("feedback_reading"), progress["query_policy"].get("reading")
                    )
                    if len(progress["evidence"]) > workflow["feedback_evidence_count"] or reselected:
                        workflow["feedback_reselected_existing_passages"] = reselected
                        rounds.append(
                            {
                                "index": len(rounds),
                                "kind": "generation",
                                "status": "running",
                                "analysis": None,
                                "synthesis": None,
                                "evidence": copy.deepcopy(progress["evidence"]),
                            }
                        )
                        progress["active_report_round"] = len(rounds) - 1
                        workflow["phase"] = "generation"
                    else:
                        current["feedback_stop_reason"] = "no_new_material_after_actual_queries"
                        workflow["phase"] = "repair"
                self._checkpoint(
                    identifier, token, progress, fixed=fixed, result=preserved.model_dump(mode="json")
                )
            elif phase == "repair":
                feedback = current.get("quality_feedback", {})
                if (
                    not workflow["repair_used"]
                    and feedback.get("repair_units")
                    and not feedback.get("review_required")
                    and self._after_report_budget(fixed, progress, 3)
                ):
                    workflow.update(repair_used=True, phase="repair_active")
                    rounds.append(
                        {
                            "index": len(rounds),
                            "kind": "repair",
                            "status": "running",
                            "parent_index": current["index"],
                            "evidence": copy.deepcopy(current["evidence"]),
                        }
                    )
                    progress["active_report_round"] = len(rounds) - 1
                else:
                    if feedback.get("repair_units"):
                        current["repair_stop_reason"] = "budget_or_review_required"
                    workflow["phase"] = "complete"
                self._checkpoint(identifier, token, progress, fixed=fixed)
            elif phase == "repair_active":
                parent = rounds[current["parent_index"]]
                preserved = AnalysisResult.model_validate(parent["result"])
                self._activate_report_round(identifier, token, fixed, progress, current)
                self._freeze_report_retries(identifier, token, progress, loaded)
                packet = build_packet(
                    fixed["question"],
                    current["evidence"],
                    self._report_context(fixed, progress, preserved.model_dump(mode="json"), freeze=True),
                    input_version=fixed.get("query_policy", {}).get("biocheck_input_version"),
                )
                self._save_risk_report_packet(identifier, token, fixed, progress, packet)
                try:
                    if "revision" not in current:
                        current["revision"] = revise_biocheck(
                            self.settings,
                            packet,
                            preserved.report["verification"],
                            self._shared_callback(identifier, token, fixed, progress),
                            audit=preserved.report.get("support_audit"),
                            source_errors=self._report_source_errors(progress),
                            loaded=loaded,
                        )
                        self._checkpoint(
                            identifier, token, progress, result=preserved.model_dump(mode="json")
                        )
                    revision = current["revision"]
                    if not revision.get("needs_verification") or revision.get("status") != "changed":
                        current.update(status=revision.get("status", "not_changed"), result=parent["result"])
                        workflow["phase"] = "complete"
                        continue
                    if "candidate" not in current:
                        report = check_biocheck(
                            self.settings,
                            packet,
                            revision["draft"],
                            self._shared_callback(identifier, token, fixed, progress),
                            repair=False,
                            loaded=loaded,
                            max_verify_retries=progress["final_report_plan"].get("max_verify_retries", 0),
                            verification_mode=progress["final_report_plan"].get("verification_mode", "whole"),
                            verify_target="biocheck_recheck",
                        )
                        report.update(
                            revision=copy.deepcopy(revision),
                            professional_approval="not_reviewed",
                            structured_analysis_relation="source_draft",
                            **self._risk_report_binding(fixed, progress),
                        )
                        candidate = preserved.model_copy(deep=True)
                        candidate.report = report
                        current["candidate"] = candidate.model_dump(mode="json")
                        self._checkpoint(
                            identifier, token, progress, result=preserved.model_dump(mode="json")
                        )
                    candidate = AnalysisResult.model_validate(current["candidate"])
                    if candidate.report["status"] != "PASS":
                        current.update(status="verification_needs_review", result=parent["result"])
                        workflow["phase"] = "complete"
                        continue
                    if "audit_plan" not in current:
                        current["audit_plan"] = {
                            "targets": support_audit_targets(candidate.report["verification"]),
                            "text_sha256": candidate.report["text_sha256"],
                        }
                        self._checkpoint(identifier, token, progress)
                    if "support_audit" not in candidate.report:
                        audit = audit_biocheck_support(
                            self.settings,
                            packet,
                            candidate.report["verification"],
                            self._shared_callback(identifier, token, fixed, progress),
                            targets=current["audit_plan"]["targets"],
                            loaded=loaded,
                            input_version=fixed.get("query_policy", {}).get("biocheck_input_version"),
                        )
                        candidate.report["support_audit"] = audit
                    candidate.report["quality_feedback"] = quality_feedback_biocheck(
                        self.settings,
                        packet,
                        candidate.report["verification"],
                        audit=candidate.report.get("support_audit"),
                        source_errors=self._report_source_errors(progress),
                        loaded=loaded,
                    )
                    current.update(status="checked", result=candidate.model_dump(mode="json"))
                    workflow["phase"] = "complete"
                    self._checkpoint(identifier, token, progress, result=candidate.model_dump(mode="json"))
                except DomainError as exc:
                    current["error_code"] = exc.code
                    self._checkpoint(identifier, token, progress, result=preserved.model_dump(mode="json"))
                    if not self._report_budget_stop(exc):
                        raise
                    current.update(status="stopped_budget", result=parent["result"])
                    workflow["phase"] = "complete"
            else:
                raise DomainError("analysis_report_state_invalid", "报告恢复阶段不可识别，保留已核稿", 409)
        current = rounds[progress["active_report_round"]]
        checked = AnalysisResult.model_validate(current["result"])
        checked.report["rounds"] = [
            {
                "index": item["index"],
                "kind": item["kind"],
                "status": item["status"],
                "report": copy.deepcopy((item.get("candidate") or item.get("result") or {}).get("report")),
                "quality_feedback": copy.deepcopy(item.get("quality_feedback")),
                "feedback_stop_reason": item.get("feedback_stop_reason"),
                "unsearched_gaps": copy.deepcopy(item.get("unsearched_gaps", [])),
                "error_code": item.get("error_code"),
                "revision": copy.deepcopy(item.get("revision")),
            }
            for item in rounds
        ]
        checked.query_policy = self._policy_view(fixed, progress)
        workflow["completed_result"] = checked.model_dump(mode="json")
        self._checkpoint(identifier, token, progress, fixed=fixed, result=checked.model_dump(mode="json"))
        return checked, progress

    def _save_risk_report_packet(self, identifier, token, fixed, progress, packet):
        """Keep the actual checked input once, outside the public report/export text."""
        scope = (fixed.get("origin") or {}).get("topic_risk_scope")
        if scope is None or synthesis_schema(fixed) is ModelSynthesis:
            return
        key = str(progress.get("active_report_round", 0))
        packets = progress.setdefault("report_risk_packets", {})
        previous = packets.get(key)
        if previous is not None and canonical_hash(previous) != canonical_hash(packet):
            raise DomainError("analysis_report_input_changed", "风险报告本轮固定材料已变化，未重用核验", 409)
        if previous is None:
            packets[key] = copy.deepcopy(packet)
            self._checkpoint(identifier, token, progress)

    @staticmethod
    def _risk_report_binding(fixed, progress):
        key = str(progress.get("active_report_round", 0))
        packet = progress.get("report_risk_packets", {}).get(key)
        scope = (fixed.get("origin") or {}).get("topic_risk_scope")
        if packet is None or scope is None:
            return {}
        reassessment = packet.get("assessment", {}).get("radar_analysis", {}).get("topic_risk_reassessment")
        return {
            "topic_risk_binding": {
                "scope": copy.deepcopy(scope),
                "reassessment": copy.deepcopy(reassessment),
                "report_round": int(key),
                "packet_sha256": canonical_hash(packet),
            }
        }

    def _freeze_report_retries(self, identifier, token, progress, loaded):
        """Pin opt-in arguments before a professional operation is first prepared."""
        from bioradar.adapters.biocheck import supports_bounded_retry, supports_paragraph_verification

        plan = progress["final_report_plan"]
        if not plan.pop("retry_policy_pending", False):
            return
        plan["max_plan_retries"] = int(supports_bounded_retry(loaded[0], "plan"))
        plan["verification_mode"] = "paragraph" if supports_paragraph_verification(loaded[0]) else "whole"
        plan["max_verify_retries"] = (
            0
            if plan["verification_mode"] == "paragraph"
            else int(supports_bounded_retry(loaded[0], "verify"))
        )
        self._checkpoint(identifier, token, progress)

    def _saved_final_report(self, identifier, token, fixed, progress, checked, raw):
        from bioradar.adapters.biocheck import (
            audit_biocheck_support,
            build_packet,
            run_biocheck,
            support_audit_targets,
        )

        self._guard(identifier, token)
        candidate = render_report(checked.model_dump(mode="json"), fixed["question"])
        frozen_candidate = progress.get("final_candidate")
        if frozen_candidate is not None and frozen_candidate != candidate:
            raise DomainError(
                "analysis_final_candidate_changed", "恢复时最终待核稿与原稿不同，未重用旧核验", 409
            )
        progress["final_candidate"] = candidate
        progress["stage"] = "final_verification"
        if "final_report_plan" not in progress:
            used = [c for c in progress["calls"] if c["stage"].startswith("biocheck_")]
            other_calls = len(progress["calls"]) - len(used)
            legacy = bool(used or progress.get("final_report"))
            progress["final_report_plan"] = {
                "repair": any(c["stage"] in {"biocheck_revise", "biocheck_recheck"} for c in used)
                or other_calls + (5 if legacy else 6) <= fixed["model_parameters"]["max_calls"],
                "reserve_support_audit": not legacy,
                "basis": "existing_provider_stages" if legacy else "new_report_with_support_audit",
                **(
                    {"retry_policy_pending": True}
                    if not legacy
                    and fixed.get("query_policy", {}).get("professional_retry_version")
                    == "biocheck-contract-repair-v1"
                    else {}
                ),
            }
        saved_report = progress.get("final_report") or any(
            item.get("result") for item in progress.get("report_rounds", [])
        )
        self._checkpoint(
            identifier,
            token,
            progress,
            fixed=fixed,
            **({"result": checked.model_dump(mode="json")} if not saved_report else {}),
        )
        report = progress.get("final_report")
        loaded = None
        if report is None:
            context = self._report_context(fixed, progress, checked.model_dump(mode="json"), freeze=True)
            packet = build_packet(
                fixed["question"],
                progress["evidence"],
                context,
                input_version=fixed.get("query_policy", {}).get("biocheck_input_version"),
            )
            self._save_risk_report_packet(identifier, token, fixed, progress, packet)
            loaded = self._load_professional(identifier, token, fixed, progress, "biocheck")
            self._use_provider(identifier, token, fixed, progress, "biocheck", loaded[-1])
            self._freeze_report_retries(identifier, token, progress, loaded)
            report = run_biocheck(
                self.settings,
                packet,
                self._shared_callback(identifier, token, fixed, progress),
                loaded=loaded,
                repair=progress["final_report_plan"]["repair"],
                max_plan_retries=progress["final_report_plan"].get("max_plan_retries", 0),
                max_verify_retries=progress["final_report_plan"].get("max_verify_retries", 0),
                verification_mode=progress["final_report_plan"].get("verification_mode", "whole"),
            )
            progress["final_report"] = report
            self._checkpoint(identifier, token, progress)
        if report["status"] == "unavailable":
            report = {**report, "final_text": candidate}
        if (
            report["status"] == "unavailable"
            and fixed.get("parent_result") is not None
            and checked.update.stance in {"maintained", "revised"}
        ):
            # Current-draft verification cannot cover a comparison written afterward.
            final_raw = {**raw, "update": checked.update.model_dump(mode="json")}
            reviewed, progress = self._call(
                identifier,
                token,
                "final_check",
                ModelReview,
                fixed,
                progress,
                freeze_payload(
                    "final_check",
                    fixed,
                    progress["evidence"],
                    synthesis=final_raw,
                    queries=[{k: v for k, v in q.items() if k != "records"} for q in progress["queries"]],
                    verified_claims=checked.claims,
                ),
            )
            checked = validated_result(
                final_raw,
                reviewed,
                progress["evidence"],
                parent=fixed.get("parent_result"),
                limitations=progress["limitations"],
                schema=synthesis_schema(fixed),
                topic_risk_scope=(fixed.get("origin") or {}).get("topic_risk_scope"),
                strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                == "bioradar-research-input-v1",
            )
            checked.review.checked_sections = [
                "claims",
                "explanations",
                "gaps",
                "conditional_answer",
                "update",
            ]
            report = {**report, "generic_verification": reviewed}
        if not isinstance(report.get("final_text"), str) or not report["final_text"].strip():
            report = {**report, "final_text": candidate, "status": "failed"}
            checked.review.status = "needs_review"
        report = {
            **report,
            "text_sha256": hashlib.sha256(report["final_text"].encode()).hexdigest(),
            "structured_analysis_relation": "source_draft",
            "professional_approval": "not_reviewed",
            **self._risk_report_binding(fixed, progress),
        }
        checked.report = report
        checked.mechanism_evaluation = progress.get("mechanism_evaluation")
        if fixed.get("providers", {}).get("biocheck", {}).get("available") and "support_audit" not in report:
            # Save the checked text before a separately fallible support audit.
            progress["final_report"] = report
            self._checkpoint(identifier, token, progress, result=checked.model_dump(mode="json"))
            if loaded is None:
                loaded = self._load_professional(identifier, token, fixed, progress, "biocheck")
                self._use_provider(identifier, token, fixed, progress, "biocheck", loaded[-1])
                context = self._report_context(fixed, progress, checked.model_dump(mode="json"), freeze=True)
                packet = copy.deepcopy(
                    progress.get("report_risk_packets", {}).get(str(progress.get("active_report_round", 0)))
                ) or build_packet(
                    fixed["question"],
                    progress["evidence"],
                    context,
                    input_version=fixed.get("query_policy", {}).get("biocheck_input_version"),
                )
            review = report.get("verification") or {}
            if "support_audit_plan" not in progress:
                try:
                    targets = support_audit_targets(review)
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    targets = []
                progress["support_audit_plan"] = {"targets": targets, "text_sha256": report["text_sha256"]}
                self._checkpoint(identifier, token, progress)
            progress["stage"] = "biocheck_support_audit"
            audit = audit_biocheck_support(
                self.settings,
                packet,
                review,
                self._shared_callback(identifier, token, fixed, progress),
                targets=progress["support_audit_plan"]["targets"],
                loaded=loaded,
                input_version=fixed.get("query_policy", {}).get("biocheck_input_version"),
            )
            report = {**report, "support_audit": audit}
            checked.report = progress["final_report"] = report
            self._checkpoint(identifier, token, progress, result=checked.model_dump(mode="json"))
        providers = copy.deepcopy(progress.get("providers", fixed.get("providers", {})))
        providers.setdefault("biocheck", {})["result_status"] = report["status"]
        providers.setdefault("mech_evidence", {})["result_status"] = (
            progress.get("mechanism_evaluation") or {}
        ).get("status", "unavailable")
        progress["providers"] = providers
        self._checkpoint(identifier, token, progress)
        return checked, progress

    def _retained_reading_draft(self, identifier, token, fixed, progress, failure):
        """Deliver a prior, source-bound reading when final synthesis is known incomplete."""
        from bioradar.adapters.reasoning_model import ModelCallError
        from bioradar.research.investigations.analysis import validate_claims
        from bioradar.research.investigations.reading_coverage import retained_reading_coverage
        from bioradar.research.investigations.reading_execution import retained_policy_reading

        known_invalid_response = (
            isinstance(failure, ModelCallError)
            and failure.receipt.status == "failed"
            and failure.receipt.http_status == 200
            and failure.code in {"model_output_invalid", "model_output_truncated", "model_protocol_error"}
        )
        if fixed.get("query_policy", {}).get(
            "final_reference_repair_version"
        ) != "bioradar-synthesis-reference-repair-v1" or not (
            self._report_budget_stop(failure)
            or known_invalid_response
            or not isinstance(failure, ModelCallError)
            and failure.code
            in {
                "analysis_output_invalid",
                "analysis_assessment_invalid",
                "analysis_reference_invalid",
                "analysis_passage_invalid",
                "analysis_unbound_claim",
                "analysis_reference_repair_invalid",
                "analysis_reference_repair_context_invalid",
            }
        ):
            raise failure
        retained = retained_policy_reading(fixed, progress)
        if retained is None:
            raise failure
        reading, original = retained
        note = (
            "最终综合未完成，以下保留此前阅读草稿；只确认引用对应原先读过的固定材料，"
            "未完成通用或专业核验，新增材料及后续缺口未因此视为已解决。"
            "该草稿不能作为当前结论；原阅读输入及未纳入的新证据、计算和专业产物分别列出。"
        )
        if note not in progress["limitations"]:
            progress["limitations"].append(note)
        progress["synthesis_stop_reason"] = failure.code
        progress["synthesis_reading_basis"] = {
            "call_key": original.get("call_key", original["stage"]),
            "call_id": original["call_id"],
            "reading_evidence_hash": progress["query_policy"]["reading_evidence_hash"],
            "status": "unfinished_reading_draft",
            "qualification": note,
            **retained_reading_coverage(progress, original),
        }
        result = AnalysisResult(
            claims=validate_claims(reading.claims, progress["evidence"]),
            explanations=reading.explanations,
            gaps=reading.gaps,
            conditional_answer=reading.conditional_answer,
            update=program_update(fixed.get("parent_result") is not None),
            review=GenericReview(status="needs_review", claims=[], overall_limitations=[note]),
            limitations=progress["limitations"],
            query_policy=self._policy_view(fixed, progress),
        ).model_dump(mode="json")
        # Reuse the existing final-delivery checkpoint so a restart cannot resume
        # a stopped synthesis or replace this reading with an unverified draft.
        progress["final_delivery_result"] = copy.deepcopy(result)
        self._checkpoint(identifier, token, progress, fixed=fixed, result=result)
        return result

    def _research_result(self, identifier, token, fixed, progress):
        workflow = progress.get("report_workflow")
        if workflow:
            checked = AnalysisResult.model_validate(workflow["base_checked"])
            checked, progress = self._final_report(
                identifier, token, fixed, progress, checked, workflow["base_synthesis"]
            )
            checked.query_policy = self._policy_view(fixed, progress)
            result = checked.model_dump(mode="json")
            self._checkpoint(identifier, token, progress, fixed=fixed, result=result)
            return result, progress
        progress = self._prepare(identifier, token, fixed, progress)
        if fixed.get("execution_version") == "query-step-v1":
            reading, progress = self._policy_research(identifier, token, fixed, progress)
        else:
            # Persisted pre-step runs retain their original stage and receipt identities.
            progress = self._initial_retrieval(identifier, token, fixed, progress)
            if not progress["evidence"]:
                raise DomainError(
                    "analysis_no_material",
                    "本轮初查未取得可解读的授权片段；实际空结果或失败回执已保留",
                    422,
                )
            raw, progress = self._call(
                identifier,
                token,
                "read_plan",
                ModelReading,
                fixed,
                progress,
                freeze_payload("read_plan", fixed, progress["evidence"]),
            )
            reading = validate_reading(raw, progress["evidence"], fixed["search_scope"])
            progress = self._queries(identifier, token, fixed, progress, reading)
            progress = self._weknora_retrieval(
                identifier, token, fixed, progress, reading.next_queries, phase="followup"
            )
            progress = self._mechanism(identifier, token, fixed, progress, reading)
        try:
            raw, progress = self._call(
                identifier,
                token,
                "synthesize",
                synthesis_schema(fixed),
                fixed,
                progress,
                self._synthesis_payload(fixed, progress),
            )
            raw = self._normalize_synthesis(identifier, token, fixed, progress, raw, "synthesize")
            _, verified = validate_synthesis(
                raw,
                progress["evidence"],
                parent=None,
                schema=synthesis_schema(fixed),
                topic_risk_scope=(fixed.get("origin") or {}).get("topic_risk_scope"),
                strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                == "bioradar-research-input-v1",
            )
        except DomainError as exc:
            return self._retained_reading_draft(identifier, token, fixed, progress, exc), progress
        query_receipts = [{k: v for k, v in q.items() if k != "records"} for q in progress["queries"]]
        shared_final_check = (
            bool(fixed.get("providers", {}).get("biocheck", {}).get("available"))
            and not any(c["stage"] == "check" for c in progress["calls"])
            and (fixed.get("parent_result") is None or not self._optional_budget(fixed, progress, 1))
        )
        if shared_final_check:
            parsed, verified = validate_synthesis(
                raw,
                progress["evidence"],
                parent=None,
                schema=synthesis_schema(fixed),
                topic_risk_scope=(fixed.get("origin") or {}).get("topic_risk_scope"),
                strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                == "bioradar-research-input-v1",
            )
            checked = AnalysisResult(
                claims=verified,
                explanations=parsed.explanations,
                gaps=parsed.gaps,
                conditional_answer=parsed.conditional_answer,
                update=parsed.update,
                review=GenericReview(
                    status="needs_review",
                    claims=[],
                    overall_limitations=["前期结构化分析未单独核验；最终正文的BioCheck结果另列report。"],
                ),
                **self._final_assessment_fields(parsed),
                limitations=progress["limitations"],
            )
        else:
            reviewed, progress = self._call(
                identifier,
                token,
                "check",
                ModelReview,
                fixed,
                progress,
                freeze_payload(
                    "check",
                    fixed,
                    progress["evidence"],
                    synthesis=raw,
                    queries=query_receipts,
                    verified_claims=verified,
                ),
            )
            checked = validated_result(
                raw,
                reviewed,
                progress["evidence"],
                parent=None,
                limitations=progress["limitations"],
                schema=synthesis_schema(fixed),
                topic_risk_scope=(fixed.get("origin") or {}).get("topic_risk_scope"),
                strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                == "bioradar-research-input-v1",
            )
        has_parent = fixed.get("parent_result") is not None
        checked.update = program_update(has_parent, review_passed=checked.review.status == "passed")
        can_revise = any(c["stage"] == "revise" for c in progress["calls"]) or self._optional_budget(
            fixed, progress, 2
        )
        if needs_revision(checked.review) and not can_revise:
            progress["limitations"].append("剩余预算保留给最终生成与核验，本轮未修订前期草稿。")
        if needs_revision(checked.review) and can_revise:
            progress.setdefault("initial_draft", checked.model_dump(mode="json"))
            self._checkpoint(
                identifier,
                token,
                progress,
                fixed=fixed,
                result=progress["initial_draft"],
            )
            try:
                revision_payload = freeze_payload(
                    "revise",
                    fixed,
                    progress["evidence"],
                    synthesis=raw,
                    review=reviewed,
                    queries=query_receipts,
                    verified_claims=checked.claims,
                )
                if final_assessment_context(fixed):
                    revision_payload["final_research_assessment"] = final_assessment_context(fixed)
                revised, progress = self._call(
                    identifier,
                    token,
                    "revise",
                    synthesis_schema(fixed),
                    fixed,
                    progress,
                    revision_payload,
                )
                revised = self._normalize_synthesis(identifier, token, fixed, progress, revised, "revise")
                _, revised_verified = validate_synthesis(
                    revised,
                    progress["evidence"],
                    parent=None,
                    schema=synthesis_schema(fixed),
                    topic_risk_scope=(fixed.get("origin") or {}).get("topic_risk_scope"),
                    strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                    == "bioradar-research-input-v1",
                )
                rechecked, progress = self._call(
                    identifier,
                    token,
                    "recheck",
                    ModelReview,
                    fixed,
                    progress,
                    freeze_payload(
                        "recheck",
                        fixed,
                        progress["evidence"],
                        synthesis=revised,
                        queries=query_receipts,
                        verified_claims=revised_verified,
                    ),
                )
                checked = validated_result(
                    revised,
                    rechecked,
                    progress["evidence"],
                    parent=None,
                    limitations=progress["limitations"],
                    schema=synthesis_schema(fixed),
                    topic_risk_scope=(fixed.get("origin") or {}).get("topic_risk_scope"),
                    strict_references=fixed.get("query_policy", {}).get("research_contract_version")
                    == "bioradar-research-input-v1",
                )
                raw = revised
                checked.update = program_update(has_parent, review_passed=checked.review.status == "passed")
            except DomainError as exc:
                if not self._report_budget_stop(exc):
                    raise
                checked = AnalysisResult.model_validate(progress["initial_draft"])
                progress["structured_revision_stop_reason"] = exc.code
                self._checkpoint(identifier, token, progress, result=checked.model_dump(mode="json"))
        can_compare = any(c["stage"] == "compare" for c in progress["calls"]) or self._optional_budget(
            fixed, progress, 1
        )
        if has_parent and checked.review.status == "passed" and not can_compare:
            progress["limitations"].append("剩余预算保留给最终生成与核验，本轮未执行旧版比较。")
        if has_parent and checked.review.status == "passed" and can_compare:
            from bioradar.adapters.reasoning_model import ModelCallError

            # Preserve the verified current analysis before a separately fallible comparison.
            self._checkpoint(identifier, token, progress, fixed=fixed, result=checked.model_dump(mode="json"))
            try:
                compared, progress = self._call(
                    identifier,
                    token,
                    "compare",
                    AnalysisUpdate,
                    fixed,
                    progress,
                    freeze_payload(
                        "compare", fixed, progress["evidence"], synthesis=raw, queries=query_receipts
                    ),
                )
                checked.update = validate_update(
                    compared, checked.claims, progress["evidence"], parent=fixed["parent_result"]
                )
            except DomainError as exc:
                if not (
                    isinstance(exc, ModelCallError)
                    or exc.code
                    in {
                        "model_call_failed",
                        "model_call_outcome_unconfirmed",
                        "analysis_update_invalid",
                        "analysis_reference_invalid",
                        "analysis_model_budget",
                        "analysis_token_budget",
                    }
                ):
                    raise
                progress["comparison_error_code"] = exc.code
                checked.update.reasons = [
                    "当前材料解读已保留；旧版比较未完成或输出无效，未据此生成维持或修订判断。"
                ]
        checked.query_policy = self._policy_view(fixed, progress)
        checked, progress = self._final_report(identifier, token, fixed, progress, checked, raw)
        checked.query_policy = self._policy_view(fixed, progress)
        result = checked.model_dump(mode="json")
        self._checkpoint(identifier, token, progress, fixed=fixed, result=result)
        return result, progress

    def execute(self, identifier, expected_hash):
        token = secrets.token_hex(24)
        with Session(self.engine.database) as checked:
            self._row(checked, self.actor, identifier)
        with Session(self.engine.database) as session, session.begin():
            row = self._row(session, self.actor, identifier, lock=True)
            if (
                row.input_hash != expected_hash
                or row.state != "queued"
                or row.progress.get("manual_budget_pending")
                or row.progress.get("budget_wait")
                or row.progress.get("waiting_fetch")
            ):
                return identifier
            row.state, row.execution_token = "running", token
            row.lease_until = datetime.now(UTC) + timedelta(seconds=execution_seconds(row.fixed_input) + 10)
            fixed, progress = copy.deepcopy(row.fixed_input), copy.deepcopy(row.progress)
            fixed["is_demo"] = row.is_demo
            progress.setdefault("expected_revision", row.based_on_revision)
            progress.setdefault("started_at", datetime.now(UTC).isoformat())
            row.progress = copy.deepcopy(progress)
        try:
            self._guard(identifier, token)
            has_parent = fixed.get("parent_result") is not None
            if progress.get("final_delivery_result") is not None:
                result = copy.deepcopy(progress["final_delivery_result"])
            else:
                result, progress = self._research_result(identifier, token, fixed, progress)
                if (
                    fixed.get("query_policy", {}).get("research_contract_version")
                    == "bioradar-research-input-v1"
                    and synthesis_schema(fixed) is ModelSynthesis
                ):
                    reading_context = progress.get("query_policy", {}).get("reading") or {}
                    for name in ("warning_assessment", "competing_hypotheses", "current_judgment"):
                        if reading_context.get(name):
                            result[name] = copy.deepcopy(reading_context[name])
                if fixed.get("execution_version") == "query-step-v1":
                    progress["final_delivery_result"] = copy.deepcopy(result)
                    self._checkpoint(identifier, token, progress, fixed=fixed, result=result)
            for item in progress["evidence"]:
                if item["material_entry_id"] is not None:
                    continue
                view = self._append(
                    identifier,
                    token,
                    fixed,
                    progress,
                    {
                        "kind": "material",
                        "snapshot_id": item["snapshot_id"],
                        "record_ref": item["record_ref"],
                        "stance": "context",
                        "text": "机器按明确证据缺口补查取得，支持或反驳关系见机器分析草稿。",
                    },
                    "material:" + item["key"],
                )
                item["material_entry_id"] = next(
                    e.id
                    for e in view.entries
                    if e.kind == "material"
                    and e.payload.machine_analysis_id == identifier
                    and e.payload.support.record_ref.model_dump(mode="json") == item["record_ref"]
                )
                self._checkpoint(identifier, token, progress)
            view = self._append(
                identifier,
                token,
                fixed,
                progress,
                {"kind": "note", "text": readable_result(result)},
                "result",
            )
            self._guard(identifier, token)
            with Session(self.engine.database) as session, session.begin():
                row = session.get(InvestigationAnalysisRecord, identifier, with_for_update=True)
                if row.execution_token != token:
                    return identifier
                report_status = (result.get("report") or {}).get("status")
                audit_status = ((result.get("report") or {}).get("support_audit") or {}).get("status")
                if progress.get("query_policy", {}).get(
                    "refresh_not_semantically_reviewed"
                ) or audit_status in {"REVIEW", "NOT_RUN"}:
                    row.state = "needs_review"
                elif report_status in {"PASS", "REVIEW", "FAIL", "failed"}:
                    row.state = "completed" if report_status == "PASS" else "needs_review"
                else:
                    row.state = (
                        "needs_review"
                        if result["review"]["status"] == "needs_review"
                        or has_parent
                        and result["update"]["stance"] == "unable_to_compare"
                        else "completed"
                    )
                row.progress = progress
                row.note_entry_id = next(
                    (
                        e.id
                        for e in reversed(view.entries)
                        if e.kind == "note" and e.payload.machine_analysis_id == identifier
                    ),
                    None,
                )
                progress["stage"] = "completed" if row.state == "completed" else "needs_review"
                progress["finished_at"] = datetime.now(UTC).isoformat()
                row.progress = copy.deepcopy(progress)
                row.completed_at, row.lease_until, row.execution_token = datetime.now(UTC), None, None
                row.error_code = progress.get("comparison_error_code") or progress.get(
                    "synthesis_stop_reason"
                )
            return identifier
        except SQLAlchemyError:
            # The preceding commit may already exist. Keep its durable state and let
            # recovery inspect that exact receipt before another local write.
            raise RuntimeError(
                "Machine analysis database outcome unconfirmed; reconcile the saved record"
            ) from None
        except Exception as exc:
            code = exc.code if isinstance(exc, DomainError) else "analysis_execution_failed"
            if not isinstance(exc, DomainError):
                progress["execution_diagnostic"] = {
                    "exception_type": type(exc).__name__,
                    "frames": [
                        {
                            "file": frame.f_code.co_filename.rsplit("/", 1)[-1],
                            "function": frame.f_code.co_name,
                            "line": line,
                        }
                        for frame, line in traceback.walk_tb(exc.__traceback__)
                    ][-16:],
                }
            self._sync_query_receipts(progress)
            if fixed.get("execution_version") == "query-step-v1":
                policy_state = progress.setdefault("query_policy", {})
                if code not in {WAIT_CODE, "analysis_fetch_pending"} and policy_state.get("status") not in {
                    "complete",
                    "insufficient",
                }:
                    policy_state.update(status="blocked", reason=code)
            paused = code == "analysis_paused"
            budget_wait = code == WAIT_CODE
            fetch_wait = code == "analysis_fetch_pending"
            if fetch_wait:
                pending = progress.get("query_policy", {}).get("pending_action") or {}
                progress["waiting_fetch"] = {
                    "reason": code,
                    "action_id": pending.get("id"),
                    "next_poll_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
                }
            with Session(self.engine.database) as session, session.begin():
                row = session.get(InvestigationAnalysisRecord, identifier, with_for_update=True)
                if row and row.execution_token == token:
                    comparison_only = bool(
                        row.result
                        and row.result["review"]["status"] == "passed"
                        and any(
                            c["stage"] == "compare" and c["status"] in {"sent", "unconfirmed"}
                            for c in row.progress["calls"]
                        )
                    )
                    row.state = (
                        "queued"
                        if paused or budget_wait or fetch_wait
                        else "needs_review"
                        if comparison_only
                        else "blocked"
                        if isinstance(exc, DomainError)
                        else "failed"
                    )
                    row.error_code = progress.get("comparison_error_code", code)
                    row.dispatch_pending = paused
                    if not paused and not budget_wait and not fetch_wait:
                        progress["finished_at"] = datetime.now(UTC).isoformat()
                        row.completed_at = datetime.now(UTC)
                    row.progress = copy.deepcopy(progress)
                    if row.result is not None and fixed.get("execution_version") == "query-step-v1":
                        saved_result = copy.deepcopy(row.result)
                        saved_result["query_policy"] = self._policy_view(fixed, progress)
                        row.result = saved_result
                    row.execution_token, row.lease_until = None, None
            if not isinstance(exc, DomainError):
                raise RuntimeError("Machine analysis failed; see persisted safe status") from None
            return identifier

    def evidence(self, actor, investigation_id, identifier, key):
        with Session(self.engine.database) as session:
            row = self._row(session, actor, identifier)
            if row.investigation_id != investigation_id:
                raise DomainError("not_found", "机器分析不属于本调查", 404)
            data = row.progress.get("evidence", row.fixed_input["evidence"])
            item = next((e for e in data if e["key"] == key), None)
            if item is None:
                raise DomainError("not_found", "未找到本版固定证据", 404)
            record = self.engine.get_snapshot(actor, item["snapshot_id"]).snapshot
            source = next(
                r for r in record.records if r.record_ref.model_dump(mode="json") == item["record_ref"]
            )
            content = source.normalized_text or ""
            if item.get("text_basis") in {"source_clean_text", "source_raw_content"}:
                if self.reader is None:
                    raise DomainError("history_not_connected", "尚未接通固定原件读取", 503)
                mode, cutoff = self._evidence_time(row.fixed_input)
                original = self.reader.read_version(item["history_binding"], mode=mode, cutoff=cutoff)
                content = (
                    original["raw_content"]
                    if item["text_basis"] == "source_raw_content"
                    else original["clean_text"]
                )
            if hashlib.sha256(content.encode()).hexdigest() != item["text_sha256"]:
                raise DomainError("analysis_source_mismatch", "固定证据文本哈希不一致", 502)
            return AnalysisEvidenceRead(
                evidence=item, normalized_text=content, source_url=item.get("source_url")
            )


def execute_analysis(identifier, input_hash):
    from types import SimpleNamespace

    from bioradar.api.config import Settings
    from bioradar.data.database import create_database
    from bioradar.research.history.service import history_reader

    settings = Settings()
    database = create_database(settings)
    if database is None:
        raise RuntimeError("Research database is not configured")
    try:
        engine = EngineService(database, settings)
        if settings.data_fabric_url and settings.data_fabric_history_read_token:
            with Session(database) as session:
                row = session.get(InvestigationAnalysisRecord, identifier)
                if row is None or row.workspace_id != settings.engine_workspace_id:
                    return identifier
                seconds = execution_seconds(row.fixed_input)
            read_budget = SimpleNamespace(
                max_seconds=seconds + 10,
                max_records=reading_limits(row.fixed_input)["max_versions"]
                + reading_limits(row.fixed_input)["search_limit"]
                * row.fixed_input.get("budget", {}).get("max_queries", 2),
                max_bytes=settings.engine_max_snapshot_bytes,
            )
            with history_reader(settings, read_budget) as reader:
                return AnalysisService(engine, reader).execute(identifier, input_hash)
        return AnalysisService(engine).execute(identifier, input_hash)
    finally:
        database.dispose()
