"""One bounded semantic research record; PostgreSQL owns input, progress and output."""

import copy
import hashlib
import json
import secrets
import time
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
    freeze_passages,
    freeze_payload,
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
    verification_input_allowance,
)
from bioradar.research.investigations.analysis_models import (
    ANALYSIS_VERSION,
    PROMPT_VERSION,
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
    QuestionPlan,
)
from bioradar.research.investigations.models import InvestigationEntryCreate
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
            self.engine.history_source_reader = reader.sources

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
        instance = (
            "fixture"
            if is_demo
            else (self.reader.source_instance if self.reader is not None else "datafabric-test")
        )
        if instance not in actor.source_allowlist:
            return []
        allowed = set(self.settings.engine_source_id_allowlist.get(instance, []))
        result = []
        for source in self.settings.analysis_search_source_ids:
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
            "retrieval_sources": self._search_scope(actor, is_demo),
            "limits": {
                **self._budget_limits(),
                "manual_analyses_per_hour": self.settings.analysis_manual_per_hour,
                "evidence_versions": MAX_EVIDENCE,
                "evidence_characters": MAX_CHARACTERS,
                "records_per_query": 3,
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
        }
        if requested:
            for name, value in requested.model_dump(exclude_none=True).items():
                if value > limits[name]:
                    raise DomainError("analysis_budget_invalid", "请求预算不能超过服务上限", 422)
                limits[name] = value
        return limits

    def _provider_status(self):
        from bioradar.adapters.biocheck import provider_status as biocheck_status
        from bioradar.adapters.mech_evidence import provider_status as mechanism_status
        from bioradar.adapters.weknora import provider_status as retrieval_status

        return {
            "biocheck": biocheck_status(self.settings),
            "mech_evidence": mechanism_status(self.settings),
            "weknora": retrieval_status(self.settings),
        }

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
    def _view(cls, row):
        progress = row.progress
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
            result=AnalysisResult.model_validate(row.result) if row.result else None,
            initial_draft=progress.get("initial_draft"),
            limitations=progress.get("limitations", row.fixed_input["limitations"]),
            note_entry_id=row.note_entry_id,
            progress={
                "stage": progress.get("stage", row.state),
                "started_at": progress.get("started_at"),
                "updated_at": progress.get("updated_at"),
                "waiting_reason": row.error_code,
            },
            budget=analysis_budget(row.fixed_input, progress, datetime.now(UTC)),
            providers=progress.get("providers", row.fixed_input.get("providers", {})),
        )

    def get(self, actor, investigation_id, identifier):
        with Session(self.engine.database) as session:
            row = self._row(session, actor, identifier)
            if row.investigation_id != investigation_id:
                raise DomainError("not_found", "机器分析不属于本调查", 404)
            return self._view(row)

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
                "normal_model_calls": 3,
                "conditional_revision_calls": 2,
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

    def _initial_evidence(self, session, actor, view, selected_ids, *, allow_empty=False):
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
            if len(evidence) >= MAX_EVIDENCE:
                limitations.append("材料超过10个版本，本轮只选择最近的10个外来版本。")
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

    def _manual_count(self, session):
        return session.scalar(
            select(func.count())
            .select_from(InvestigationAnalysisRecord)
            .where(
                InvestigationAnalysisRecord.workspace_id == self.actor.workspace_id,
                InvestigationAnalysisRecord.trigger.in_(("manual", "material_update")),
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
            if latest is None:
                return None
            question = latest.fixed_input["question"]
            parent_id = parent.id if parent else None
        view = InvestigationService(self.engine).get(self.actor, investigation_id)
        if view.status != "open":
            return None
        body = AnalysisCreate(
            expected_revision=view.revision, parent_analysis_id=parent_id, question=question
        )
        key = "analysis-material-update:" + canonical_hash(
            [investigation_id, parent_id, view.revision, question]
        )
        return self.start(self.actor, investigation_id, body, key, defer_on_budget=True)[1]["id"]

    def start(self, actor, investigation_id, body, key, *, origin=None, defer_on_budget=False):
        from bioradar.adapters.reasoning_model import model_status

        status = model_status(self.settings)
        if not status.enabled or not status.configured:
            raise DomainError("analysis_not_configured", "机器分析尚未启用或模型未配置，未调用模型", 503)
        runtime = InvestigationService(self.engine)
        view = runtime.get(actor, investigation_id)
        if view.status != "open":
            raise DomainError("investigation_closed", "调查已暂停或关闭，未启动机器分析", 409)
        search_scope = self._search_scope(actor, view.is_demo)
        if body.source_ids:
            if not set(body.source_ids).issubset(search_scope):
                raise DomainError("analysis_query_invalid", "初查来源不在当前可补查许可范围", 422)
            search_scope = sorted(set(body.source_ids))
        if any(query.source_id not in search_scope for query in body.initial_queries):
            raise DomainError("analysis_query_invalid", "初始查询超出本次来源范围", 422)
        if body.initial_queries and not body.auto_retrieve:
            raise DomainError("analysis_query_invalid", "初始查询须启用自动取资料", 422)
        budget = self._budget_limits(body.budget)
        providers = self._provider_status()
        if len(body.initial_queries) > min(2, budget["max_queries"]):
            raise DomainError("analysis_query_budget", "初始查询最多两项，且不得超过本次查询预算", 422)
        question = body.question or view.question
        retrieval = {
            "auto_retrieve": body.auto_retrieve,
            "initial_queries": [q.model_dump(mode="json") for q in body.initial_queries],
            "budget": budget,
        }
        with Session(self.engine.database) as session:
            evidence, snapshots, dependencies, limitations = self._initial_evidence(
                session, actor, view, body.material_entry_ids, allow_empty=body.auto_retrieve
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
                    "blocked", "failed", "needs_review"
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
            parameters = model_parameters(self.settings)
            transport_parameters = {
                key: value for key, value in parameters.items() if key not in {"model", "model_revision"}
            }
            signature = canonical_hash(
                [
                    question,
                    retrieval,
                    providers,
                    [[e["record_ref"], e["text_sha256"]] for e in evidence],
                    parameters,
                    search_scope,
                ]
            )
            completed_signature = None
            if parent is not None and {
                canonical_hash([item["record_ref"], item["snapshot_id"]]) for item in evidence
            } == {
                canonical_hash([item["record_ref"], item["snapshot_id"]])
                for item in parent.progress.get("evidence", [])
            }:
                # Automatic originals are part of the parent's completed basis, even
                # when they were not present at its initial admission.
                previous = parent.fixed_input
                completed_signature = canonical_hash(
                    [
                        previous["question"],
                        previous.get("retrieval", {}),
                        previous.get("providers", {}),
                        [[item["record_ref"], item["text_sha256"]] for item in evidence],
                        normalize_model_parameters(
                            {
                                "model": previous["model"],
                                "model_revision": previous["model_revision"],
                                **previous["model_parameters"],
                            }
                        ),
                        previous["search_scope"],
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
                ]
            )
            max_calls = budget["max_calls"]
            fixed = {
                "question": question,
                "retrieval": retrieval,
                "budget": budget,
                "providers": providers,
                "basis_signature": signature,
                "as_of": datetime.now(UTC).isoformat(),
                "version": ANALYSIS_VERSION,
                "prompt_version": PROMPT_VERSION,
                "evidence": evidence,
                "snapshot_ids": snapshots,
                "dependency_sources": dependencies,
                "search_scope": search_scope,
                "parent_result": copy.deepcopy(parent.result) if parent else None,
                "origin": copy.deepcopy(origin),
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
                    if paused or self._manual_count(session) >= self.settings.analysis_manual_per_hour:
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
                    else ("material_update" if parent or defer_on_budget else "manual"),
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
                        .where(material.c.revision > latest.c.revision)
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
            from bioradar.research.monitor.models import MonitorState

            control = session.get(MonitorState, self.actor.workspace_id)
            for pending in waiting:
                if not self.settings.semantic_llm_enabled or control is not None and control.paused:
                    break
                inquiry = session.get(InvestigationRecord, pending.investigation_id)
                if inquiry is None or inquiry.status != "open":
                    pending.state, pending.error_code = "blocked", "investigation_closed"
                    continue
                if self._manual_count(session) >= self.settings.analysis_manual_per_hour:
                    break
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
                comparison_only = bool(
                    uncertain
                    and all(c["stage"] == "compare" for c in uncertain)
                    and row.result
                    and row.result["review"]["status"] == "passed"
                )
                row.state = "needs_review" if comparison_only else ("blocked" if uncertain else "queued")
                row.error_code = "model_call_outcome_unconfirmed" if uncertain else None
                if comparison_only:
                    state = copy.deepcopy(row.progress)
                    state["comparison_error_code"] = row.error_code
                    row.progress, row.completed_at = state, now
                row.execution_token, row.lease_until, row.dispatch_pending = None, None, not uncertain
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

    def _guard(self, identifier, token):
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
            remaining_seconds = budget["limits"]["max_seconds"] - budget["usage"]["elapsed_seconds"]
            if remaining_seconds <= 0:
                raise DomainError("analysis_time_budget", "本轮研究达到固定时间预算，已保留进度", 409)
            if self.reader is not None:
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

    def _checkpoint(self, identifier, token, progress, *, fixed=None, result=None):
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
        remaining = MAX_CHARACTERS
        evidence = []
        for item in fixed["evidence"]:
            self._guard(identifier, token)
            value = copy.deepcopy(item)
            if value.get("history_binding") and self.reader is not None:
                original = self.reader.read_version(
                    value["history_binding"], mode="current", cutoff=datetime.now(UTC)
                )
                if original["raw_sha256"] != value["history_binding"]["raw_sha256"]:
                    raise DomainError("analysis_source_mismatch", "固定原件哈希不一致", 502)
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
    def _final_token_allowance(fixed, progress):
        final_calls = (
            3
            if fixed.get("providers", {}).get("biocheck", {}).get("available")
            else int(fixed.get("parent_result") is not None)
        )
        if not final_calls:
            return 0
        source_bytes = sum(len(e["text"].encode()) for e in progress["evidence"])
        return final_calls * (min(fixed["model_parameters"]["max_input_bytes"], source_bytes + 4096) + 128)

    def _optional_budget(self, fixed, progress, calls, *, following=0):
        budget = analysis_budget(fixed, progress, datetime.now(UTC))
        final_calls = (
            3
            if fixed.get("providers", {}).get("biocheck", {}).get("available")
            else int(fixed.get("parent_result") is not None)
        )
        return len(progress["calls"]) + calls + following + final_calls <= budget["limits"]["max_calls"] and (
            budget["limits"]["max_tokens"] or 10_000_000
        ) - budget["usage"]["budgeted_tokens"] > self._final_token_allowance(fixed, progress)

    def _call(
        self, identifier, token, stage, schema, fixed, progress, payload, *, system_prompt=None, call_key=None
    ):
        from bioradar.adapters.reasoning_model import ModelCallError, complete_json, encode_request

        self._guard(identifier, token)
        prompt = system_prompt or SYSTEM_PROMPT
        key = call_key or stage
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
                raise DomainError("model_call_failed", "本阶段模型调用已失败，未自动重发", 409)
            if call["status"] in {"sent", "unconfirmed"}:
                raise DomainError("model_call_outcome_unconfirmed", "上次模型调用结果未知，未自动重发", 409)
        if call is None:
            parameters = dict(fixed["model_parameters"])
            budget = analysis_budget(fixed, progress, datetime.now(UTC))
            if len(progress["calls"]) >= parameters["max_calls"]:
                raise DomainError("analysis_model_budget", "达到本轮实际模型调用上限", 422)
            # This is a conservative reservation, never reported as actual provider token usage.
            try:
                input_upper = (
                    len(
                        encode_request(
                            self.settings,
                            stage=stage,
                            system_prompt=prompt,
                            payload=payload,
                            response_schema=response_schema,
                        )
                    )
                    + 1024
                )
            except (ValueError, TypeError, RecursionError):
                raise DomainError("model_input_invalid", "模型输入不是受支持的有限JSON对象", 422) from None
            remaining = (budget["limits"]["max_tokens"] or 10_000_000) - budget["usage"]["budgeted_tokens"]
            reserved = 0
            if stage.startswith("mech_") or stage in {"revise", "recheck", "compare"}:
                reserved = self._final_token_allowance(fixed, progress)
            if stage in {"biocheck_write", "biocheck_revise"}:
                # Both requests need output room. Split what remains after this input
                # and the verifier's bounded input, rather than reserving its full
                # configured output while starving generation. Received calls return above.
                verification_input = verification_input_allowance(
                    payload, progress.get("final_candidate", ""), parameters["max_input_bytes"]
                )
                shared_output = (remaining - input_upper - verification_input) // 2
                following_output = min(parameters["max_output_tokens"], shared_output)
                reserved = verification_input + max(128, following_output)
                if len(progress["calls"]) + 2 > parameters["max_calls"]:
                    raise DomainError(
                        "analysis_model_budget", "剩余调用不足以生成后核验，未发送生成请求", 422
                    )
            if stage == "biocheck_plan" and len(progress["calls"]) + 3 > parameters["max_calls"]:
                raise DomainError("analysis_model_budget", "剩余调用不足以完成最终规划、生成和核验", 422)
            output_cap = min(parameters["max_output_tokens"], remaining - input_upper - reserved)
            if output_cap < 128:
                raise DomainError("analysis_token_budget", "剩余token预算不足以发送下一次有界请求", 422)
            parameters["max_output_tokens"] = output_cap
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
            }
            progress["calls"].append(call)
            progress["stage"] = stage
            self._checkpoint(identifier, token, progress)
        self._guard(identifier, token)
        invoke = self.model_call or complete_json
        budget = analysis_budget(fixed, progress, datetime.now(UTC))
        seconds_left = budget["limits"]["max_seconds"] - budget["usage"]["elapsed_seconds"]
        if seconds_left < 1:
            raise DomainError("analysis_time_budget", "剩余总时间不足以发送请求，未调用模型", 409)
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
        call["status"] = "sent"
        self._checkpoint(identifier, token, progress)
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
                        limit=3,
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
                    "instance": "fixture" if fixed["is_demo"] else self.reader.source_instance,
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
                if len(progress["evidence"]) >= MAX_EVIDENCE:
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
        from bioradar.adapters.weknora import provider_status, search_candidates

        receipt = next((q for q in progress["queries"] if q.get("provider") == "weknora"), None)
        if receipt is None:
            if (
                not plans
                or not provider_status(self.settings).get("available")
                or len(progress["queries"]) >= fixed.get("budget", {}).get("max_queries", 2)
                or len(progress["evidence"]) >= MAX_EVIDENCE
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
        if receipt["status"] == "prepared":
            self._guard(identifier, token)
            receipt["status"] = "sent"
            self._checkpoint(identifier, token, progress)
            found = search_candidates(
                self.settings,
                " ".join(receipt["plan"]["terms"]),
                reader=self.reader,
                source_ids=fixed["search_scope"],
                cutoff=datetime.fromisoformat(fixed["as_of"]),
                is_demo=fixed["is_demo"],
                limit=3,
                timeout_seconds=max(
                    0.1,
                    fixed["budget"]["max_seconds"]
                    - analysis_budget(fixed, progress, datetime.now(UTC))["usage"]["elapsed_seconds"],
                ),
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
                    "instance": "fixture" if fixed["is_demo"] else self.reader.source_instance,
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
            if len(progress["evidence"]) >= MAX_EVIDENCE:
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

    def _adopt_original(self, identifier, token, fixed, progress, original, tag):
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
        remaining = MAX_CHARACTERS - sum(
            len(e["text"]) + len(json.dumps(e.get("facts", {}), ensure_ascii=False))
            for e in progress["evidence"]
        )
        selected = source_excerpt(original, remaining)
        if not selected["text"]:
            raise DomainError("analysis_evidence_budget", "本轮片段预算用尽，新增版本未送入模型", 422)
        ref = self.reader.ref(original)
        result = AnalysisEvidence(
            key="q"
            + canonical_hash([original["source_id"], original["source_record_id"], original["version_id"]])[
                :12
            ],
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
        def call(stage, instruction, payload, response_schema=None):
            output, _ = self._call(
                identifier,
                token,
                stage,
                response_schema or {"type": "object"},
                fixed,
                progress,
                payload,
                system_prompt=instruction,
                call_key=stage,
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

    def _mechanism(self, identifier, token, fixed, progress, reading):
        from bioradar.adapters.biocheck import build_packet
        from bioradar.adapters.mech_evidence import evaluate_mechanism, load_mech_evidence

        if "mechanism_evaluation" not in progress:
            self._guard(identifier, token)
            progress["stage"] = "mechanism_evaluation"
            self._checkpoint(identifier, token, progress)
            packet = build_packet(
                fixed["question"],
                progress["evidence"],
                {
                    "mechanism": reading.mechanism_candidate,
                    "qualification": "machine_proposed_candidate_not_source_fact",
                },
            )
            if (
                reading.mechanism_candidate
                and fixed.get("providers", {}).get("mech_evidence", {}).get("available")
                and not self._optional_budget(fixed, progress, 3, following=1)
            ):
                assessment = {
                    "provider": "mech_evidence",
                    "status": "skipped_budget",
                    "error_code": "analysis_model_budget",
                }
            else:
                try:
                    loaded = load_mech_evidence(self.settings)
                    self._use_provider(identifier, token, fixed, progress, "mech_evidence", loaded[-1])
                    assessment = evaluate_mechanism(
                        self.settings,
                        packet,
                        self._shared_callback(identifier, token, fixed, progress),
                        loaded=loaded,
                    )
                except DomainError as exc:
                    if exc.code not in {"analysis_model_budget", "analysis_token_budget"}:
                        raise
                    assessment = {
                        "provider": "mech_evidence",
                        "status": "skipped_budget",
                        "error_code": exc.code,
                    }

            progress["mechanism_evaluation"] = assessment
            self._checkpoint(identifier, token, progress)
        fixed["mechanism_evaluation"] = progress["mechanism_evaluation"]
        self._checkpoint(identifier, token, progress, fixed=fixed)
        return progress

    def _final_report(self, identifier, token, fixed, progress, checked, raw):
        from bioradar.adapters.biocheck import build_packet, load_biocheck, run_biocheck

        self._guard(identifier, token)
        candidate = render_report(checked.model_dump(mode="json"), fixed["question"])
        frozen_candidate = progress.get("final_candidate")
        if frozen_candidate is not None and frozen_candidate != candidate:
            raise DomainError(
                "analysis_final_candidate_changed", "恢复时最终待核稿与原稿不同，未重用旧核验", 409
            )
        progress["final_candidate"] = candidate
        progress["stage"] = "final_verification"
        self._checkpoint(identifier, token, progress, fixed=fixed)
        report = progress.get("final_report")
        if report is None:
            context = report_context(checked.model_dump(mode="json"), progress.get("mechanism_evaluation"))
            packet = build_packet(fixed["question"], progress["evidence"], context)
            loaded = load_biocheck(self.settings)
            self._use_provider(identifier, token, fixed, progress, "biocheck", loaded[-1])
            report = run_biocheck(
                self.settings,
                packet,
                self._shared_callback(identifier, token, fixed, progress),
                loaded=loaded,
                repair=(
                    sum(not c["stage"].startswith("biocheck_") for c in progress["calls"]) + 5
                    <= fixed["model_parameters"]["max_calls"]
                ),
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
        }
        checked.report = report
        checked.mechanism_evaluation = progress.get("mechanism_evaluation")
        providers = copy.deepcopy(progress.get("providers", fixed.get("providers", {})))
        providers.setdefault("biocheck", {})["result_status"] = report["status"]
        providers.setdefault("mech_evidence", {})["result_status"] = (
            progress.get("mechanism_evaluation") or {}
        ).get("status", "unavailable")
        progress["providers"] = providers
        self._checkpoint(identifier, token, progress)
        return checked, progress

    def execute(self, identifier, expected_hash):
        token = secrets.token_hex(24)
        with Session(self.engine.database) as checked:
            self._row(checked, self.actor, identifier)
        with Session(self.engine.database) as session, session.begin():
            row = self._row(session, self.actor, identifier, lock=True)
            if row.input_hash != expected_hash or row.state != "queued":
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
            progress = self._prepare(identifier, token, fixed, progress)
            progress = self._initial_retrieval(identifier, token, fixed, progress)
            if not progress["evidence"]:
                raise DomainError(
                    "analysis_no_material", "本轮初查未取得可解读的授权片段；实际空结果或失败回执已保留", 422
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
            raw, progress = self._call(
                identifier,
                token,
                "synthesize",
                ModelSynthesis,
                fixed,
                progress,
                freeze_payload(
                    "synthesize",
                    fixed,
                    progress["evidence"],
                    queries=[{k: v for k, v in q.items() if k != "records"} for q in progress["queries"]],
                ),
            )
            _, verified = validate_synthesis(raw, progress["evidence"], parent=None)
            query_receipts = [{k: v for k, v in q.items() if k != "records"} for q in progress["queries"]]
            shared_final_check = bool(fixed.get("providers", {}).get("biocheck", {}).get("available")) and (
                fixed.get("parent_result") is None or not self._optional_budget(fixed, progress, 1)
            )
            if shared_final_check:
                parsed, verified = validate_synthesis(raw, progress["evidence"], parent=None)
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
                )
            has_parent = fixed.get("parent_result") is not None
            checked.update = program_update(has_parent, review_passed=checked.review.status == "passed")
            if needs_revision(checked.review) and not self._optional_budget(fixed, progress, 2):
                progress["limitations"].append("剩余预算保留给最终生成与核验，本轮未修订前期草稿。")
            if needs_revision(checked.review) and self._optional_budget(fixed, progress, 2):
                progress.setdefault("initial_draft", checked.model_dump(mode="json"))
                self._checkpoint(
                    identifier,
                    token,
                    progress,
                    fixed=fixed,
                    result=progress["initial_draft"],
                )
                revised, progress = self._call(
                    identifier,
                    token,
                    "revise",
                    ModelSynthesis,
                    fixed,
                    progress,
                    freeze_payload(
                        "revise",
                        fixed,
                        progress["evidence"],
                        synthesis=raw,
                        review=reviewed,
                        queries=query_receipts,
                        verified_claims=checked.claims,
                    ),
                )
                _, revised_verified = validate_synthesis(revised, progress["evidence"], parent=None)
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
                )
                raw = revised
                checked.update = program_update(has_parent, review_passed=checked.review.status == "passed")
            if (
                has_parent
                and checked.review.status == "passed"
                and not self._optional_budget(fixed, progress, 1)
            ):
                progress["limitations"].append("剩余预算保留给最终生成与核验，本轮未执行旧版比较。")
            if has_parent and checked.review.status == "passed" and self._optional_budget(fixed, progress, 1):
                from bioradar.adapters.reasoning_model import ModelCallError

                # Preserve the verified current analysis before a separately fallible comparison.
                self._checkpoint(
                    identifier, token, progress, fixed=fixed, result=checked.model_dump(mode="json")
                )
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
            checked, progress = self._final_report(identifier, token, fixed, progress, checked, raw)
            result = checked.model_dump(mode="json")
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
                if report_status in {"PASS", "REVIEW", "FAIL", "failed"}:
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
                row.error_code = progress.get("comparison_error_code")
            return identifier
        except SQLAlchemyError:
            # The preceding commit may already exist. Keep its durable state and let
            # recovery inspect that exact receipt before another local write.
            raise RuntimeError(
                "Machine analysis database outcome unconfirmed; reconcile the saved record"
            ) from None
        except Exception as exc:
            code = exc.code if isinstance(exc, DomainError) else "analysis_execution_failed"
            paused = code == "analysis_paused"
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
                        if paused
                        else "needs_review"
                        if comparison_only
                        else "blocked"
                        if isinstance(exc, DomainError)
                        else "failed"
                    )
                    row.error_code = progress.get("comparison_error_code", code)
                    row.dispatch_pending = paused
                    if not paused:
                        progress["finished_at"] = datetime.now(UTC).isoformat()
                        row.completed_at = datetime.now(UTC)
                    row.progress = copy.deepcopy(progress)
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
                original = self.reader.read_version(
                    item["history_binding"], mode="current", cutoff=datetime.now(UTC)
                )
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
                max_records=MAX_EVIDENCE + 3 * row.fixed_input.get("budget", {}).get("max_queries", 2),
                max_bytes=settings.engine_max_snapshot_bytes,
            )
            with history_reader(settings, read_budget) as reader:
                return AnalysisService(engine, reader).execute(identifier, input_hash)
        return AnalysisService(engine).execute(identifier, input_hash)
    finally:
        database.dispose()
