"""Two PostgreSQL tables and direct operations for a manual research record."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from bioradar.api.errors import DomainError
from bioradar.data.database import Base
from bioradar.research.engine.auth import DevelopmentAccess, EngineActor
from bioradar.research.engine.service import SOURCE_LIST_NOTE, SOURCE_READ_UNAVAILABLE, EngineService
from bioradar.research.investigations.models import (
    InvestigationAutomation,
    InvestigationCreate,
    InvestigationEntry,
    InvestigationEntryCreate,
    InvestigationJudgment,
    InvestigationList,
    InvestigationMaterial,
    InvestigationOrigin,
    InvestigationQuestion,
    InvestigationSummary,
    InvestigationView,
)
from bioradar.research.mining.models import EvidenceSupport, MiningResult, NormalizedSnapshot

MAX_ENTRIES = 128
MAX_VIEW_BYTES = 2_000_000


class InvestigationRecord(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'paused', 'closed')", name="ck_investigation_status"),
        CheckConstraint("revision >= 1", name="ck_investigation_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    question: Mapped[str] = mapped_column(String(4000), nullable=False)
    origin: Mapped[dict | None] = mapped_column(JSONB)
    questions: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationEntryRecord(Base):
    __tablename__ = "investigation_entries"
    __table_args__ = (
        UniqueConstraint("investigation_id", "revision"),
        CheckConstraint(
            "kind IN ('material', 'note', 'judgment', 'status')", name="ck_investigation_entry_kind"
        ),
        CheckConstraint("revision >= 2", name="ck_investigation_entry_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvestigationService:
    def __init__(self, engine: EngineService):
        self.engine = engine

    def _actor(self, actor: EngineActor) -> None:
        if actor != DevelopmentAccess(self.engine.settings).actor():
            raise DomainError("permission_denied", "开发授权上下文不匹配", 403)

    @staticmethod
    def _entries(session: Session, identifier: str) -> list[InvestigationEntryRecord]:
        return list(
            session.scalars(
                select(InvestigationEntryRecord)
                .where(
                    InvestigationEntryRecord.investigation_id == identifier,
                )
                .order_by(InvestigationEntryRecord.revision)
            )
        )

    def _row(self, session: Session, actor: EngineActor, identifier: str, *, lock=False, sources=None):
        self._actor(actor)
        statement = select(InvestigationRecord).where(
            InvestigationRecord.id == identifier,
            InvestigationRecord.workspace_id == actor.workspace_id,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = session.scalar(statement)
        if row is None:
            raise DomainError("not_found", "未找到可访问的调查记录", 404)
        entries = self._entries(session, row.id)
        topic_bases = {
            (automation.get("topic_id"), automation.get("evaluation_id"))
            for entry in entries
            if (automation := entry.payload.get("automation") or {}).get("source") == "topic"
        }
        if topic_bases:
            # A program note can be saved before its material preview. Its full frozen
            # evaluation, including evidence outside that preview, retains source access.
            from bioradar.topics.service import TopicService

            topics = TopicService(self.engine)
            for topic_id, evaluation_id in topic_bases:
                topics._evaluation_row(session, actor, topic_id, evaluation_id)
        bindings = []
        if row.origin is not None:
            bindings.append(row.origin)
        bindings.extend(entry.payload for entry in entries if entry.kind == "material")
        checked = {} if sources is None else sources
        # Saved copies retain the current access conditions of every source.
        for binding in bindings:
            identifier = binding["snapshot_id"]
            if identifier not in checked:
                checked[identifier] = self.engine._snapshot(session, actor, identifier)
            if checked[identifier].snapshot_hash != binding["snapshot_hash"]:
                raise DomainError("snapshot_integrity_error", "调查来源与固定采用包不一致", 502)
        return row, entries

    @staticmethod
    def _view(row: InvestigationRecord, entries: list[InvestigationEntryRecord]) -> InvestigationView:
        saved = [
            InvestigationEntry(
                id=entry.id,
                investigation_id=entry.investigation_id,
                revision=entry.revision,
                kind=entry.kind,
                created_by=entry.created_by,
                created_at=entry.created_at,
                payload=entry.payload,
            )
            for entry in entries
        ]
        judgments = [entry for entry in saved if entry.kind == "judgment"]
        latest = judgments[-1] if judgments else None
        materials = [entry for entry in saved if entry.kind == "material"]
        return InvestigationView(
            id=row.id,
            title=row.title,
            question=row.question,
            status=row.status,
            revision=row.revision,
            is_demo=row.is_demo,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            origin=row.origin,
            questions=row.questions,
            entries=saved,
            needs_review=latest is not None
            and any(entry.id not in latest.payload.basis_entry_ids for entry in materials),
            latest_judgment_entry_id=latest.id if latest else None,
            material_count=len(materials),
            judgment_count=len(judgments),
        )

    @staticmethod
    def _response(row, entries) -> tuple[int, dict]:
        view = InvestigationService._view(row, entries)
        if len(view.model_dump_json().encode()) > MAX_VIEW_BYTES:
            raise DomainError("investigation_budget_exceeded", "调查记录超过保存预算，请拆分研究问题", 422)
        return 201, view.model_dump(mode="json")

    def _origin(self, session: Session, actor: EngineActor, request):
        if request is None:
            return None, False
        run = self.engine._run(session, actor, request.run_id)
        if run.result is None or run.state not in {"completed", "partial"}:
            raise DomainError("candidate_unavailable", "计算尚未取得可采用候选", 409)
        if run.result_hash != request.result_hash:
            raise DomainError("result_conflict", "候选结果版本已变化，请重新选择", 409)
        result = MiningResult.model_validate(run.result)
        candidate = next(
            (item for item in result.candidates if item.candidate_key == request.candidate_key), None
        )
        if candidate is None:
            raise DomainError("not_found", "未找到可采用的候选", 404)
        snapshot = self.engine._snapshot(session, actor, run.snapshot_id)
        origin = InvestigationOrigin(
            run_id=run.id,
            result_hash=run.result_hash,
            candidate_key=candidate.candidate_key,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            candidate=candidate,
        )
        return origin, snapshot.payload["is_demo"]

    def create(self, actor: EngineActor, body: InvestigationCreate, key: str) -> tuple[int, dict]:
        self._actor(actor)
        with Session(self.engine.database) as session, session.begin():
            origin, source_demo = self._origin(session, actor, body.origin)

            def mutate():
                questions = body.questions
                if not questions and origin is not None:
                    questions = list(origin.candidate.next_questions[:32])
                row = InvestigationRecord(
                    id=str(uuid4()),
                    workspace_id=actor.workspace_id,
                    title=body.title,
                    question=body.question,
                    origin=origin.model_dump(mode="json") if origin else None,
                    questions=[
                        InvestigationQuestion(question_id=str(uuid4()), text=text).model_dump(mode="json")
                        for text in questions
                    ],
                    status="open",
                    revision=1,
                    is_demo=body.is_demo or source_demo,
                    created_by=actor.actor_id,
                    updated_at=datetime.now(UTC),
                )
                session.add(row)
                session.flush()
                return self._response(row, [])

            code, payload = self.engine._idempotent(session, actor, "investigation.create", key, body, mutate)
            # A replay may contain material copies added after the original request.
            self._row(session, actor, payload["id"])
            return code, payload

    def get(self, actor: EngineActor, identifier: str) -> InvestigationView:
        with Session(self.engine.database) as session:
            return self._view(*self._row(session, actor, identifier))

    def list(self, actor: EngineActor, cursor=None, limit=30) -> InvestigationList:
        self._actor(actor)
        with Session(self.engine.database) as session:
            statement = select(InvestigationRecord).where(
                InvestigationRecord.workspace_id == actor.workspace_id
            )
            rows = list(session.scalars(self.engine._page(statement, InvestigationRecord, cursor, limit)))
            items, unavailable_count = [], 0
            for row in rows[:limit]:
                try:
                    view = self._view(*self._row(session, actor, row.id))
                    items.append(
                        InvestigationSummary.model_validate(
                            {name: getattr(view, name) for name in InvestigationSummary.model_fields}
                        )
                    )
                except DomainError as exc:
                    if exc.code in SOURCE_READ_UNAVAILABLE:
                        unavailable_count += 1
                    elif exc.status_code != 404:
                        raise
            return InvestigationList(
                items=items,
                next_cursor=self.engine._next_cursor(rows, limit),
                unavailable_count=unavailable_count,
                availability_note=SOURCE_LIST_NOTE if unavailable_count else None,
            )

    def _material(
        self, session: Session, actor: EngineActor, request, sources
    ) -> tuple[InvestigationMaterial, bool]:
        adopted = sources.get(request.snapshot_id)
        if adopted is None:
            adopted = self.engine._snapshot(session, actor, request.snapshot_id)
        snapshot = NormalizedSnapshot.model_validate(adopted.payload)
        record = next(
            (record for record in snapshot.records if record.record_ref == request.record_ref), None
        )
        if record is None:
            raise DomainError("material_source_mismatch", "材料引用不属于所选固定采用包", 422)
        start = None
        if request.quote is not None:
            if record.text_status != "inline_authorized" or record.normalized_text is None:
                raise DomainError("material_quote_unavailable", "所选材料没有可核对的采用文本", 422)
            start = record.normalized_text.find(request.quote)
            if start < 0:
                raise DomainError("material_quote_mismatch", "引用片段不在所选固定文本中", 422)
        support = EvidenceSupport(
            record_ref=record.record_ref,
            document_hash=record.document_hash,
            locator=record.locator,
            evidence_refs=record.evidence_refs,
            quote=request.quote,
            normalized_start=start,
            normalized_end=start + len(request.quote) if start is not None else None,
            normalized_text_hash=record.normalized_text_hash,
            quote_status="normalized_only" if request.quote is not None else "unavailable",
            source_url=record.source_url,
        )
        return InvestigationMaterial(
            text=request.text,
            stance=request.stance,
            snapshot_id=adopted.id,
            snapshot_hash=adopted.snapshot_hash,
            support=support,
        ), snapshot.is_demo

    def append(
        self,
        actor: EngineActor,
        identifier: str,
        body: InvestigationEntryCreate,
        key: str,
        *,
        automation: InvestigationAutomation | None = None,
    ):
        request = body.root
        self._actor(actor)
        if automation is not None and request.kind not in {"material", "note"}:
            raise DomainError("automation_judgment_forbidden", "后台仅可整理材料和笔记，不能签研究判断", 422)
        with Session(self.engine.database) as session:
            self._row(session, actor, identifier)
            if automation is not None and automation.source == "topic":
                from bioradar.topics.service import TopicService

                TopicService(self.engine)._evaluation_row(
                    session, actor, automation.topic_id, automation.evaluation_id
                )
        with Session(self.engine.database) as session, session.begin():

            def mutate():
                sources = {}
                row, entries = self._row(session, actor, identifier, lock=True, sources=sources)
                if row.revision != request.expected_revision:
                    raise DomainError("revision_conflict", "调查已更新，请重新读取后保存", 409)
                if len(entries) >= MAX_ENTRIES:
                    raise DomainError(
                        "investigation_budget_exceeded", "调查记录已达保存上限，请拆分研究问题", 422
                    )
                if row.status == "closed" and (request.kind != "status" or request.status == "closed"):
                    raise DomainError("investigation_closed", "调查已关闭，请先重新开启", 409)
                payload = request.model_dump(mode="json", exclude={"expected_revision"})
                if request.kind == "material":
                    saved, source_demo = self._material(session, actor, request, sources)
                    if any(source.payload["is_demo"] != source_demo for source in sources.values()):
                        raise DomainError("fixture_scope_mismatch", "教学与真实来源不能混入同一调查", 422)
                    payload = saved.model_dump(mode="json")
                    row.is_demo = row.is_demo or source_demo
                elif request.kind == "judgment":
                    allowed = {entry.id for entry in entries if entry.kind == "material"}
                    basis = request.basis_entry_ids
                    if len(set(basis)) != len(basis) or not set(basis).issubset(allowed):
                        raise DomainError("judgment_basis_invalid", "判断依据必须是本调查已有的材料记录", 422)
                    if request.assessment == "provisional" and not basis:
                        raise DomainError("judgment_basis_required", "没有采用材料时请记录无法判断", 422)
                    latest = next((entry for entry in reversed(entries) if entry.kind == "judgment"), None)
                    payload = InvestigationJudgment(
                        text=request.text,
                        conditions=request.conditions,
                        assessment=request.assessment,
                        basis_entry_ids=basis,
                        based_on_revision=row.revision,
                        parent_judgment_entry_id=latest.id if latest else None,
                    ).model_dump(mode="json")
                elif request.kind == "status":
                    updates = {update.question_id: update.status for update in request.question_updates}
                    if len(updates) != len(request.question_updates) or not set(updates).issubset(
                        {question["question_id"] for question in row.questions}
                    ):
                        raise DomainError("question_update_invalid", "核实问题不存在或重复更新", 422)
                    row.questions = [
                        {**question, "status": updates.get(question["question_id"], question["status"])}
                        for question in row.questions
                    ]
                    row.status = request.status
                if automation is not None:
                    payload["automation"] = automation.model_dump(mode="json", exclude_none=True)
                row.revision += 1
                row.updated_at = datetime.now(UTC)
                entry = InvestigationEntryRecord(
                    id=str(uuid4()),
                    investigation_id=row.id,
                    revision=row.revision,
                    kind=request.kind,
                    payload=payload,
                    created_by=actor.actor_id,
                )
                session.add(entry)
                session.flush()
                return self._response(row, [*entries, entry])

            code, payload = self.engine._idempotent(
                session,
                actor,
                "investigation.append:" + identifier,
                key,
                {
                    "request": body.model_dump(mode="json"),
                    "automation": automation.model_dump(mode="json", exclude_none=True),
                }
                if automation
                else body,
                mutate,
            )
            self._row(session, actor, identifier)
            return code, payload
