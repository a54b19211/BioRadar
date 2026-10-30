"""PostgreSQL is authoritative; RQ only executes accepted, frozen computations."""

import base64
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.decision.models import DecisionResult
from bioradar.research.engine.auth import DevelopmentAccess, EngineActor
from bioradar.research.engine.models import EngineFeedback, EngineIdempotency, EngineRun, EngineSnapshot
from bioradar.research.engine.schemas import (
    FeedbackCreate,
    FeedbackView,
    RunAction,
    RunCreate,
    RunError,
    RunList,
    RunView,
    SnapshotDetail,
    SnapshotList,
    SnapshotSummary,
)
from bioradar.research.mining.models import (
    DiscoveryPolicy,
    KddPolicy,
    MiningPolicy,
    MiningResult,
    NormalizedSnapshot,
    ResourceLimits,
    canonical_hash,
)
from bioradar.research.mining.prepare import snapshot_hash
from bioradar.research.mining.registry import ENGINE_COMPUTE_VERSION, method_versions

SOURCE_READ_UNAVAILABLE = frozenset(
    {"history_source_revoked", "history_source_not_accessible", "history_permission_unavailable"}
)
SOURCE_LIST_NOTE = "本页部分来源当前不可读取，相关项目未返回；当前列表不是全部已采用资料。"

RESULT_BUDGET_MESSAGE = "结果超过运行预算，请缩小采用范围或候选上限后新建分析"


def computation_versions(method_ids: tuple[str, ...], *, policy=None) -> dict[str, str]:
    if isinstance(policy, dict) and "discovery_directions" in policy:
        policy = DiscoveryPolicy.model_validate(policy)
    elif isinstance(policy, dict) and "kdd_direction" in policy:
        policy = KddPolicy.model_validate(policy)
    if isinstance(policy, (KddPolicy, DiscoveryPolicy)):
        from bioradar.research.mining.kdd import kdd_versions

        return kdd_versions(policy)
    return {**method_versions(method_ids), "__engine_compute__": ENGINE_COMPUTE_VERSION}


class EngineService:
    def __init__(self, database: Engine, settings: Settings):
        self.database = database
        self.settings = settings
        # Service instances belong to one HTTP request or worker execution.
        # Permission results must never be promoted to a process-global cache.
        self._history_source_ids = None
        self.history_source_reader = None
        self._history_permission_failure = None

    def authorize_history_source(self, source_id):
        if self._history_permission_failure is not None:
            raise DomainError(*self._history_permission_failure) from None
        if self._history_source_ids is None:
            from bioradar.adapters.fabric_history import current_history_sources

            try:
                catalog = (
                    self.history_source_reader()
                    if self.history_source_reader is not None
                    else current_history_sources(self.settings)
                )
            except DomainError as exc:
                self._history_permission_failure = (
                    (exc.code, exc.message, exc.status_code, exc.retryable)
                    if exc.status_code == 403
                    else ("history_permission_unavailable", "来源当前许可暂不能核对，正文未返回", 503, True)
                )
                raise DomainError(*self._history_permission_failure) from None
            self._history_source_ids = {
                row["source_id"]
                for row in catalog
                if isinstance(row, dict)
                and isinstance(row.get("source_id"), str)
                and row.get("read_enabled") is True
            }
        if source_id not in self._history_source_ids:
            raise DomainError("history_source_revoked", "来源当前未获准读取，旧正文未返回", 403)

    def decisions(self):
        from bioradar.research.engine.decision_service import DecisionRuntime

        return DecisionRuntime(self)

    def _authorize_snapshot(self, actor: EngineActor, snapshot: NormalizedSnapshot) -> None:
        if actor != DevelopmentAccess(self.settings).actor():
            raise DomainError("permission_denied", "开发授权上下文不匹配", 403)
        if snapshot.source_instance not in actor.source_allowlist:
            raise DomainError("source_not_authorized", "来源不在当前开发采用许可范围", 403)
        demo_instance = self.settings.data_fabric_demo_source_instance or "fixture"
        if (snapshot.is_demo and snapshot.source_instance != demo_instance) or (
            not snapshot.is_demo and snapshot.source_instance in {"fixture", demo_instance}
        ):
            raise DomainError("fixture_scope_mismatch", "测试材料与真实来源采用必须分开", 422)
        if not snapshot.records:
            raise DomainError("snapshot_empty", "未取得可采用记录，不能创建空成功快照", 422)
        if len(snapshot.records) > self.settings.engine_max_records:
            raise DomainError("snapshot_limit_exceeded", "快照超过开发许可记录上限", 422)
        if len(snapshot.model_dump_json().encode()) > self.settings.engine_max_snapshot_bytes:
            raise DomainError("snapshot_limit_exceeded", "快照超过开发许可大小上限", 422)
        if any(r.source_instance != snapshot.source_instance for r in snapshot.records):
            raise DomainError("source_scope_mismatch", "记录来源实例与采用包不一致", 422)
        if any(c.source_instance != snapshot.source_instance for c in snapshot.coverage):
            raise DomainError("source_scope_mismatch", "覆盖来源实例与采用包不一致", 422)
        source_ids = set(self.settings.engine_source_id_allowlist.get(snapshot.source_instance, []))
        if any(r.source_id not in source_ids for r in snapshot.records) or any(
            c.source_id not in source_ids for c in snapshot.coverage
        ):
            raise DomainError("source_not_authorized", "来源站点不在当前开发采用许可范围", 403)

        def check_refs(value: Any) -> None:
            if isinstance(value, dict):
                if {"provider", "object_type", "object_id", "authorization_ref"} <= value.keys():
                    if value["authorization_ref"] != actor.grant_ref:
                        raise DomainError("reference_not_authorized", "引用未绑定当前采用授权", 403)
                for child in value.values():
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)

        check_refs(snapshot.model_dump(mode="json"))
        for source_id in {
            record.source_id
            for record in snapshot.records
            if record.record_ref.provider in {"data_fabric_history", "data_fabric_graph_research"}
        }:
            self.authorize_history_source(source_id)

    def _snapshot(self, session: Session, actor: EngineActor, identifier: str) -> EngineSnapshot:
        row = session.scalar(
            select(EngineSnapshot).where(
                EngineSnapshot.id == identifier,
                EngineSnapshot.workspace_id == actor.workspace_id,
            )
        )
        if row is None:
            raise DomainError("not_found", "未找到可访问的固定快照", 404)
        if row.grant_ref != actor.grant_ref or row.source_instance not in actor.source_allowlist:
            raise DomainError("not_found", "未找到可访问的固定快照", 404)
        try:
            snapshot = NormalizedSnapshot.model_validate(row.payload)
            self._authorize_snapshot(actor, snapshot)
        except DomainError as exc:
            if exc.code in {"history_source_revoked", "history_source_not_accessible"}:
                raise
            if exc.status_code == 403:
                raise DomainError("not_found", "未找到可访问的固定快照", 404) from None
            raise
        if snapshot_hash(snapshot) != row.snapshot_hash:
            raise DomainError("snapshot_integrity_error", "固定快照的内容哈希不一致", 502)
        return row

    def _run(self, session: Session, actor: EngineActor, identifier: str, *, lock=False) -> EngineRun:
        statement = select(EngineRun).where(
            EngineRun.id == identifier,
            EngineRun.workspace_id == actor.workspace_id,
            EngineRun.run_kind == "mining",
        )
        if lock:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise DomainError("not_found", "未找到可访问的分析运行", 404)
        self._snapshot(session, actor, row.snapshot_id)
        return row

    @staticmethod
    def _summary(row: EngineSnapshot) -> SnapshotSummary:
        payload = row.payload
        return SnapshotSummary(
            id=row.id,
            snapshot_hash=row.snapshot_hash,
            is_demo=payload["is_demo"],
            source_instance=row.source_instance,
            coverage_mode=payload["coverage_mode"],
            record_count=len(payload["records"]),
            observed_at=payload["observed_at"],
            created_at=row.created_at,
            metadata_gaps=payload["metadata_gaps"],
        )

    @staticmethod
    def _feedback(row: EngineFeedback) -> FeedbackView:
        return FeedbackView(
            id=row.id,
            run_id=row.run_id,
            actor_id=row.actor_id,
            candidate_key=row.candidate_key,
            result_hash=row.result_hash,
            decision=row.decision,
            reason=row.reason,
            created_at=row.created_at,
        )

    def _view(self, session: Session, row: EngineRun, *, detail: bool = True) -> RunView:
        if row.run_kind != "mining":
            raise DomainError("not_found", "未找到可访问的挖掘运行", 404)
        feedback = []
        if detail:
            feedback = [
                self._feedback(item)
                for item in session.scalars(
                    select(EngineFeedback)
                    .where(EngineFeedback.run_id == row.id)
                    .order_by(
                        EngineFeedback.created_at,
                        EngineFeedback.id,
                    )
                )
            ]
        policy = self._compatible_policy(row.policy)
        return RunView(
            id=row.id,
            snapshot_id=row.snapshot_id,
            input_hash=row.input_hash,
            original_policy=row.policy,
            policy=policy,
            policy_view_status="current" if policy is not None else "historical_requires_new_run",
            state=row.state,
            revision=row.revision,
            attempt=row.attempt,
            dispatch_pending=row.dispatch_pending,
            dependency_note="计算已持久受理，RQ尚未确认投递" if row.dispatch_pending else None,
            created_at=row.created_at,
            completed_at=row.completed_at,
            error=RunError(code=row.error_code, message=row.error_message or "分析执行失败")
            if row.error_code
            else None,
            result=MiningResult.model_validate(row.result) if detail and row.result else None,
            feedback=feedback,
            candidate_count=len(row.result["candidates"]) if row.result is not None else None,
            attempt_diagnostics=row.attempt_diagnostics if detail else [],
        )

    @staticmethod
    def _compatible_policy(original: dict) -> MiningPolicy | None:
        try:
            policy_type = (
                DiscoveryPolicy
                if "discovery_directions" in original
                else KddPolicy
                if "kdd_direction" in original
                else MiningPolicy
            )
            policy = policy_type.model_validate(original)
        except ValidationError:
            return None
        return policy if canonical_hash(policy) == canonical_hash(original) else None

    def _idempotent(
        self,
        session: Session,
        actor: EngineActor,
        operation: str,
        key: str,
        body: Any,
        mutate: Callable[[], tuple[int, dict]],
    ) -> tuple[int, dict]:
        if not key or len(key) > 160:
            raise DomainError("idempotency_key_invalid", "需要有界的Idempotency-Key", 422)
        body_hash = canonical_hash(body)
        session.execute(
            insert(EngineIdempotency)
            .values(
                id=str(uuid4()),
                workspace_id=actor.workspace_id,
                actor_id=actor.actor_id,
                operation=operation,
                key=key,
                body_hash=body_hash,
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "actor_id", "operation", "key"])
        )
        row = session.scalar(
            select(EngineIdempotency)
            .where(
                EngineIdempotency.workspace_id == actor.workspace_id,
                EngineIdempotency.actor_id == actor.actor_id,
                EngineIdempotency.operation == operation,
                EngineIdempotency.key == key,
            )
            .with_for_update()
        )
        assert row is not None
        if row.body_hash != body_hash:
            raise DomainError("idempotency_conflict", "同一幂等键的请求内容不同", 409)
        if row.response_code is not None:
            return row.response_code, row.response_body or {}
        code, payload = mutate()
        row.response_code = code
        row.response_body = payload
        return code, payload

    def adopt_snapshot(self, actor: EngineActor, snapshot: NormalizedSnapshot, key: str) -> tuple[int, dict]:
        self._authorize_snapshot(actor, snapshot)
        try:
            content_hash = snapshot_hash(snapshot)
        except ValueError as exc:
            raise DomainError("snapshot_version_conflict", "同版本记录的固定内容不一致", 422) from exc
        with Session(self.database) as session, session.begin():

            def mutate():
                session.execute(
                    insert(EngineSnapshot)
                    .values(
                        id=str(uuid4()),
                        workspace_id=actor.workspace_id,
                        snapshot_hash=content_hash,
                        source_instance=snapshot.source_instance,
                        grant_ref=actor.grant_ref,
                        actor_id=actor.actor_id,
                        payload=snapshot.model_dump(mode="json"),
                    )
                    .on_conflict_do_nothing(index_elements=["workspace_id", "snapshot_hash"])
                )
                row = session.scalar(
                    select(EngineSnapshot).where(
                        EngineSnapshot.workspace_id == actor.workspace_id,
                        EngineSnapshot.snapshot_hash == content_hash,
                    )
                )
                assert row is not None
                return 201, self._summary(row).model_dump(mode="json")

            return self._idempotent(session, actor, "snapshot.create", key, snapshot, mutate)

    def get_snapshot(self, actor: EngineActor, identifier: str) -> SnapshotDetail:
        with Session(self.database) as session:
            row = self._snapshot(session, actor, identifier)
            return SnapshotDetail(id=row.id, snapshot_hash=row.snapshot_hash, snapshot=row.payload)

    @staticmethod
    def _page(statement, model, cursor: str | None, limit: int):
        if cursor is not None:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor))
                created_at = datetime.fromisoformat(value["created_at"])
                if created_at.tzinfo is None or not isinstance(value["id"], str):
                    raise ValueError("invalid cursor")
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise DomainError("cursor_invalid", "列表游标无效", 422) from exc
            statement = statement.where(
                or_(
                    model.created_at < created_at,
                    and_(model.created_at == created_at, model.id < value["id"]),
                )
            )
        return statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)

    @staticmethod
    def _next_cursor(rows: list, limit: int) -> str | None:
        if len(rows) <= limit:
            return None
        row = rows[limit - 1]
        value = {"created_at": row.created_at.isoformat(), "id": row.id}
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()

    def list_snapshots(self, actor: EngineActor, cursor=None, limit=30) -> SnapshotList:
        with Session(self.database) as session:
            statement = select(EngineSnapshot).where(
                EngineSnapshot.workspace_id == actor.workspace_id,
                EngineSnapshot.source_instance.in_(actor.source_allowlist),
                EngineSnapshot.grant_ref == actor.grant_ref,
            )
            rows = list(session.scalars(self._page(statement, EngineSnapshot, cursor, limit)))
            visible, unavailable_count = [], 0
            for row in rows[:limit]:
                try:
                    self._snapshot(session, actor, row.id)
                    visible.append(row)
                except DomainError as exc:
                    if exc.code in SOURCE_READ_UNAVAILABLE:
                        unavailable_count += 1
                    elif exc.status_code != 404:
                        raise
            return SnapshotList(
                items=[self._summary(row) for row in visible],
                next_cursor=self._next_cursor(rows, limit),
                unavailable_count=unavailable_count,
                availability_note=SOURCE_LIST_NOTE if unavailable_count else None,
            )

    def create_run(self, actor: EngineActor, body: RunCreate, key: str) -> tuple[int, dict]:
        limits = body.policy.limits
        permitted = ResourceLimits()
        if not body.policy.method_ids:
            raise DomainError("methods_required", "至少选择一个实际方法", 422)
        if (
            limits.max_records > self.settings.engine_max_records
            or limits.max_text_characters > permitted.max_text_characters
            or limits.max_features > permitted.max_features
            or limits.max_candidates_per_method > permitted.max_candidates_per_method
            or limits.review_capacity > permitted.review_capacity
            or limits.max_graph_edges > permitted.max_graph_edges
            or limits.max_link_pairs > permitted.max_link_pairs
            or limits.max_graph_hops > permitted.max_graph_hops
            or limits.max_paths > permitted.max_paths
            or limits.max_temporal_bins > permitted.max_temporal_bins
            or body.policy.topic_components > 20
        ):
            raise DomainError("engine_budget_exceeded", "计算参数超过开发授权资源上限", 422)
        versions = computation_versions(body.policy.method_ids, policy=body.policy)
        with Session(self.database) as session, session.begin():
            snapshot = self._snapshot(session, actor, body.snapshot_id)
            input_hash = canonical_hash(
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "policy": body.policy.model_dump(mode="json"),
                    "method_versions": versions,
                }
            )

            def mutate():
                run_id = str(uuid4())
                session.execute(
                    insert(EngineRun)
                    .values(
                        id=run_id,
                        workspace_id=actor.workspace_id,
                        snapshot_id=snapshot.id,
                        input_hash=input_hash,
                        policy=body.policy.model_dump(mode="json"),
                        method_versions=versions,
                        state="queued",
                        revision=1,
                        attempt=1,
                        accept_token=secrets.token_hex(32),
                        dispatch_pending=True,
                        rq_job_id=f"engine-{run_id}-a1",
                    )
                    .on_conflict_do_nothing(index_elements=["workspace_id", "input_hash"])
                )
                row = session.scalar(
                    select(EngineRun).where(
                        EngineRun.workspace_id == actor.workspace_id,
                        EngineRun.input_hash == input_hash,
                    )
                )
                assert row is not None
                return 202, self._view(session, row, detail=False).model_dump(mode="json")

            return self._idempotent(session, actor, "run.create", key, body, mutate)

    def get_run(self, actor: EngineActor, identifier: str) -> RunView:
        with Session(self.database) as session:
            return self._view(session, self._run(session, actor, identifier))

    def list_runs(self, actor: EngineActor, cursor=None, limit=30) -> RunList:
        with Session(self.database) as session:
            statement = (
                select(EngineRun)
                .join(EngineSnapshot)
                .where(
                    EngineRun.workspace_id == actor.workspace_id,
                    EngineRun.run_kind == "mining",
                    EngineSnapshot.source_instance.in_(actor.source_allowlist),
                    EngineSnapshot.grant_ref == actor.grant_ref,
                )
            )
            rows = list(session.scalars(self._page(statement, EngineRun, cursor, limit)))
            visible, unavailable_count = [], 0
            for row in rows[:limit]:
                try:
                    self._snapshot(session, actor, row.snapshot_id)
                    visible.append(row)
                except DomainError as exc:
                    if exc.code in SOURCE_READ_UNAVAILABLE:
                        unavailable_count += 1
                    elif exc.status_code != 404:
                        raise
            return RunList(
                items=[self._view(session, row, detail=False) for row in visible],
                next_cursor=self._next_cursor(rows, limit),
                unavailable_count=unavailable_count,
                availability_note=SOURCE_LIST_NOTE if unavailable_count else None,
            )

    def retry_run(self, actor: EngineActor, identifier: str, action: RunAction, key: str) -> tuple[int, dict]:
        with Session(self.database) as session, session.begin():
            # Access is checked even when replaying an idempotent response after revocation.
            self._run(session, actor, identifier)

            def mutate():
                row = self._run(session, actor, identifier, lock=True)
                partial_failure = (
                    row.run_kind == "mining"
                    and row.state == "partial"
                    and row.result
                    and any(
                        item.get("execution_status") == "failed"
                        for item in row.result.get("method_results", [])
                    )
                )
                if row.revision != action.expected_revision or not (row.state == "failed" or partial_failure):
                    raise DomainError("run_state_conflict", "运行版本或状态不允许重试", 409)
                if row.attempt >= self.settings.engine_retry_limit:
                    raise DomainError("retry_limit_exceeded", "已达到本次固定输入重试上限", 409)
                if row.error_code == "result_budget_exceeded":
                    raise DomainError("result_budget_exceeded", RESULT_BUDGET_MESSAGE, 409)
                if self._compatible_policy(row.policy) is None:
                    raise DomainError("policy_schema_changed", "原始策略采用旧Schema，请创建新分析运行", 409)
                if (
                    computation_versions(tuple(row.policy["method_ids"]), policy=row.policy)
                    != row.method_versions
                ):
                    raise DomainError("method_version_changed", "方法已升级，请创建新分析运行", 409)
                self._requeue(row)
                return 202, self._view(session, row, detail=False).model_dump(mode="json")

            return self._idempotent(session, actor, f"run.retry:{identifier}", key, action, mutate)

    @staticmethod
    def _requeue(row: EngineRun) -> None:
        diagnosis = {
            "attempt": row.attempt,
            "state": row.state,
            "error_code": row.error_code,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "result_hash": row.result_hash,
        }
        if row.run_kind == "mining":
            diagnosis["method_results"] = row.result["method_results"] if row.result else []
        else:
            diagnosis["decision_result"] = row.result
        row.attempt_diagnostics = [*row.attempt_diagnostics, diagnosis]
        row.attempt += 1
        row.revision += 1
        row.accept_token = secrets.token_hex(32)
        row.rq_job_id = f"engine-{row.id}-a{row.attempt}"
        row.state, row.dispatch_pending = "queued", True
        row.error_code = row.error_message = None
        row.started_at = row.completed_at = None
        row.result = row.result_hash = None

    def add_feedback(
        self, actor: EngineActor, identifier: str, body: FeedbackCreate, key: str
    ) -> tuple[int, dict]:
        with Session(self.database) as session, session.begin():
            row = self._run(session, actor, identifier)
            if row.result is None or row.result_hash != body.result_hash:
                raise DomainError("result_hash_conflict", "反馈目标不是已接受的固定结果", 409)
            candidate_keys = {c["candidate_key"] for c in row.result["candidates"]}
            candidate_keys.update(
                candidate["candidate_key"]
                for method in row.result.get("method_results", [])
                for candidate in method["candidates"]
            )
            if body.candidate_key not in candidate_keys:
                raise DomainError("candidate_not_found", "未找到该固定结果中的候选", 404)

            def mutate():
                feedback = EngineFeedback(
                    id=str(uuid4()),
                    workspace_id=actor.workspace_id,
                    run_id=row.id,
                    actor_id=actor.actor_id,
                    **body.model_dump(),
                )
                session.add(feedback)
                session.flush()
                return 201, self._feedback(feedback).model_dump(mode="json")

            return self._idempotent(session, actor, f"feedback.create:{identifier}", key, body, mutate)

    def dispatch(self, identifier: str) -> bool:
        with Session(self.database) as session:
            row = session.get(EngineRun, identifier)
            if row is None or row.workspace_id != self.settings.engine_workspace_id or row.state != "queued":
                return False
            attempt, token, input_hash, rq_id = row.attempt, row.accept_token, row.input_hash, row.rq_job_id
        if self.settings.redis_url is None:
            return False
        redis = Redis.from_url(
            self.settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
        )
        try:
            try:
                existing = Job.fetch(rq_id, connection=redis)
                status = existing.get_status(refresh=True)
                if str(getattr(status, "value", status)) not in {
                    "queued",
                    "started",
                    "deferred",
                    "scheduled",
                }:
                    return False
            except NoSuchJobError:
                from bioradar.research.engine.worker import execute_run

                Queue(f"{self.settings.queue_prefix}:routine", connection=redis).enqueue(
                    execute_run,
                    identifier,
                    attempt,
                    token,
                    input_hash,
                    job_id=rq_id,
                    job_timeout=self.settings.engine_job_timeout,
                    result_ttl=86400,
                    failure_ttl=604800,
                )
            with Session(self.database) as session, session.begin():
                row = session.get(EngineRun, identifier, with_for_update=True)
                if row and (row.attempt, row.accept_token, row.input_hash) == (attempt, token, input_hash):
                    row.dispatch_pending = False
            return True
        except Exception:
            # Queue errors may contain credentials/addresses; acceptance remains queued.
            return False
        finally:
            redis.close()

    def start_run(self, identifier: str, attempt: int, token: str, input_hash: str):
        with Session(self.database) as session, session.begin():
            row = session.get(EngineRun, identifier, with_for_update=True)
            if (
                row is None
                or row.workspace_id != self.settings.engine_workspace_id
                or row.state != "queued"
                or (row.attempt, row.accept_token, row.input_hash) != (attempt, token, input_hash)
            ):
                return None
            if row.run_kind == "decision":
                return self.decisions().start(session, row)
            actor = DevelopmentAccess(self.settings).actor()
            stored = self._snapshot(session, actor, row.snapshot_id)
            snapshot = NormalizedSnapshot.model_validate(stored.payload)
            policy = self._compatible_policy(row.policy)
            if policy is None:
                row.state, row.error_code = "failed", "policy_schema_changed"
                row.error_message = "原始策略采用旧Schema；原输入保留，请按当前Schema创建新分析"
                row.completed_at, row.dispatch_pending = datetime.now(UTC), False
                row.revision += 1
                return None
            if computation_versions(policy.method_ids, policy=policy) != row.method_versions:
                row.state, row.error_code = "failed", "method_version_changed"
                row.error_message = "方法版本已变化，请创建新分析"
                row.completed_at, row.dispatch_pending = datetime.now(UTC), False
                row.revision += 1
                return None
            row.state, row.started_at, row.dispatch_pending = "running", datetime.now(UTC), False
            row.revision += 1
            return snapshot, policy

    def finish_run(
        self,
        identifier: str,
        attempt: int,
        token: str,
        input_hash: str,
        result: MiningResult | DecisionResult,
    ) -> bool:
        with Session(self.database) as session, session.begin():
            row = session.get(EngineRun, identifier, with_for_update=True)
            if (
                row is None
                or row.state != "running"
                or (row.attempt, row.accept_token, row.input_hash) != (attempt, token, input_hash)
            ):
                return False
            if row.run_kind == "decision":
                return self.decisions().finish(session, row, result)
            if not isinstance(result, MiningResult):
                raise DomainError("result_kind_mismatch", "计算结果种类与冻结运行不一致", 502)
            actor = DevelopmentAccess(self.settings).actor()
            stored = self._snapshot(session, actor, row.snapshot_id)
            if result.snapshot_hash != stored.snapshot_hash or result.policy_hash != canonical_hash(
                row.policy
            ):
                raise DomainError("result_input_mismatch", "计算结果与固定输入不匹配", 502)
            expected_versions = {m.method_id: m.method_version for m in result.method_results}
            if len(expected_versions) != len(result.method_results):
                raise DomainError("result_method_mismatch", "结果包含重复的方法执行项", 502)
            expected_versions["__engine_compute__"] = result.compute_version
            if (
                expected_versions != row.method_versions
                or computation_versions(tuple(row.policy["method_ids"]), policy=row.policy)
                != row.method_versions
            ):
                raise DomainError("result_method_mismatch", "计算结果的方法集合与固定调用不匹配", 502)
            payload = result.model_dump(mode="json")
            if len(result.model_dump_json().encode("utf-8")) > self.settings.engine_max_result_bytes:
                raise DomainError("result_budget_exceeded", RESULT_BUDGET_MESSAGE, 502)
            if canonical_hash({k: v for k, v in payload.items() if k != "result_hash"}) != result.result_hash:
                raise DomainError("result_hash_mismatch", "计算结果内容哈希不匹配", 502)
            self._validate_supports(NormalizedSnapshot.model_validate(stored.payload), result)
            row.result, row.result_hash = payload, result.result_hash
            outcomes = {m.execution_status for m in result.method_results}
            if "failed" in outcomes and not outcomes & {"success", "partial"}:
                row.state, row.error_code = "failed", "all_methods_failed"
                row.error_message = "所选方法未成功完成；方法诊断已保存，可按同一固定输入重试"
            else:
                row.state = "partial" if outcomes & {"partial", "failed"} else "completed"
                row.error_code = row.error_message = None
            row.completed_at, row.dispatch_pending = datetime.now(UTC), False
            row.revision += 1
            return True

    @staticmethod
    def _validate_supports(snapshot: NormalizedSnapshot, result: MiningResult) -> None:
        records = {canonical_hash(r.record_ref): r for r in snapshot.records}
        method_candidates = [candidate for method in result.method_results for candidate in method.candidates]
        versions = {method.method_id: method.method_version for method in result.method_results}
        candidates_by_key = {}
        for method in result.method_results:
            if any(
                candidate.method_id != method.method_id or candidate.method_version != method.method_version
                for candidate in method.candidates
            ):
                raise DomainError("candidate_method_mismatch", "方法返回了其他方法的候选", 502)
        for candidate in method_candidates:
            if not candidate.supports:
                raise DomainError("candidate_source_missing", "候选缺少固定输入依据", 502)
            if versions.get(candidate.method_id) != candidate.method_version:
                raise DomainError("candidate_method_mismatch", "候选方法与固定执行结果不匹配", 502)
            prior = candidates_by_key.get(candidate.candidate_key)
            if prior is not None and canonical_hash(prior) != canonical_hash(candidate):
                raise DomainError("candidate_key_conflict", "同一候选键的内容不一致", 502)
            candidates_by_key[candidate.candidate_key] = candidate
            for support in candidate.supports:
                record = records.get(canonical_hash(support.record_ref))
                if (
                    record is None
                    or support.document_hash != record.document_hash
                    or support.locator != record.locator
                    or support.source_url != record.source_url
                    or not {canonical_hash(ref) for ref in support.evidence_refs}.issubset(
                        {canonical_hash(ref) for ref in record.evidence_refs}
                    )
                ):
                    raise DomainError("candidate_source_mismatch", "候选来源不是冻结快照的固定引用", 502)
                if support.quote is not None:
                    start, end, text = (
                        support.normalized_start,
                        support.normalized_end,
                        record.normalized_text,
                    )
                    if (
                        text is None
                        or start is None
                        or end is None
                        or end > len(text)
                        or support.quote_status != "normalized_only"
                        or text[start:end] != support.quote
                        or support.normalized_text_hash != record.normalized_text_hash
                    ):
                        raise DomainError("candidate_quote_mismatch", "候选引用片段无法在冻结文本中核对", 502)
                elif (
                    support.quote_status != "unavailable"
                    or support.normalized_start is not None
                    or support.normalized_end is not None
                ):
                    raise DomainError("candidate_quote_mismatch", "候选定位与片段检查状态不一致", 502)
        if len({candidate.candidate_key for candidate in result.candidates}) != len(result.candidates):
            raise DomainError("candidate_key_conflict", "最终候选清单包含重复键", 502)
        for candidate in result.candidates:
            original = candidates_by_key.get(candidate.candidate_key)
            if original is None or canonical_hash(original) != canonical_hash(candidate):
                raise DomainError("candidate_selection_mismatch", "最终候选不是方法实际产物的固定子集", 502)

    def fail_run(
        self, identifier: str, attempt: int, token: str, input_hash: str, *, code="computation_failed"
    ) -> bool:
        with Session(self.database) as session, session.begin():
            row = session.get(EngineRun, identifier, with_for_update=True)
            if (
                row is None
                or row.state not in {"queued", "running"}
                or (row.attempt, row.accept_token, row.input_hash) != (attempt, token, input_hash)
            ):
                return False
            row.state, row.error_code = "failed", code
            row.error_message = (
                RESULT_BUDGET_MESSAGE
                if code == "result_budget_exceeded"
                else "计算未完成，固定输入已保留；核对原因后可重试"
            )
            row.completed_at, row.dispatch_pending = datetime.now(UTC), False
            row.revision += 1
            return True

    def recover(self, limit: int = 100) -> dict[str, int]:
        if not 1 <= limit <= 1000:
            raise ValueError("recover limit must be between 1 and 1000")
        with Session(self.database) as session:
            rows = list(
                session.scalars(
                    select(EngineRun)
                    .where(
                        EngineRun.workspace_id == self.settings.engine_workspace_id,
                        EngineRun.state.in_(["queued", "running"]),
                    )
                    .order_by(EngineRun.created_at)
                    .limit(limit)
                )
            )
            pending = [(r.id, r.attempt, r.accept_token, r.input_hash, r.rq_job_id, r.state) for r in rows]
        counts = {"examined": len(pending), "dispatched": 0, "failed": 0, "dependency_unavailable": 0}
        if self.settings.redis_url is None:
            counts["dependency_unavailable"] = len(pending)
            return counts
        redis = Redis.from_url(
            self.settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
        )
        try:
            for identifier, attempt, token, input_hash, rq_id, state in pending:
                try:
                    try:
                        status = Job.fetch(rq_id, connection=redis).get_status(refresh=True)
                        status = str(getattr(status, "value", status))
                    except NoSuchJobError:
                        status = "missing"
                    if status in {"failed", "stopped", "canceled", "finished"} or (
                        status == "missing" and state == "running"
                    ):
                        counts["failed"] += int(
                            self.fail_run(
                                identifier,
                                attempt,
                                token,
                                input_hash,
                                code="rq_execution_incomplete",
                            )
                        )
                    elif state == "queued":
                        dispatched = self.dispatch(identifier)
                        counts["dispatched"] += int(dispatched)
                        if not dispatched:
                            # A fetch can succeed just before enqueue loses Redis.
                            # Report still-pending work without misclassifying a worker race.
                            with Session(self.database) as session:
                                row = session.get(EngineRun, identifier)
                                if (
                                    row
                                    and row.state == "queued"
                                    and row.dispatch_pending
                                    and (row.attempt, row.accept_token, row.input_hash)
                                    == (attempt, token, input_hash)
                                ):
                                    counts["dependency_unavailable"] += 1
                except Exception:
                    counts["dependency_unavailable"] += 1
        finally:
            redis.close()
        return counts
