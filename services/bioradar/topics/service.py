"""Independent topic seed/catch-up and ordinary investigation follow-up, on existing PG/RQ."""

import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from bioradar.adapters.fabric_history import fact_time
from bioradar.adapters.fabric_monitor import build_snapshot, change_page, project_record, search_records
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.models import EngineIdempotency
from bioradar.research.engine.service import SOURCE_READ_UNAVAILABLE
from bioradar.research.investigations.models import (
    InvestigationAutomation,
    InvestigationCreate,
    InvestigationEntryCreate,
)
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.models import canonical_hash
from bioradar.research.mining.topic_series import (
    TOPIC_COMPUTE_VERSION,
    evaluate_series,
    followup_term,
    project_contribution,
)
from bioradar.topics.models import (
    TopicControl,
    TopicCreate,
    TopicDefinition,
    TopicEvaluation,
    TopicEvaluationList,
    TopicEvaluationRecord,
    TopicList,
    TopicObservationRecord,
    TopicScopeRecord,
    TopicTemplate,
    TopicTemplates,
    TopicUpdate,
    TopicView,
)
from bioradar.topics.risk import (
    TopicRiskReviewRequest,
    assess_risk_reassessment,
    risk_scope_stamp,
)
from bioradar.topics.tracking import hypothesis_comparison, tracking_view, update_tracking

EVALUATION_LIMIT = 50000
SOURCE_GAPS = {"history_source_unavailable", "history_read_budget", "history_not_connected"} | set(
    SOURCE_READ_UNAVAILABLE
)


def _selected(revision):
    return (
        TopicObservationRecord.selected_from <= revision,
        or_(TopicObservationRecord.selected_to.is_(None), TopicObservationRecord.selected_to > revision),
    )


def observation_row(scope_id, item, value):
    observation = value.get("observation") or {}
    basis_ref = (observation.get("basis") or {}).get("ref") or {}
    return TopicObservationRecord(
        id=str(uuid4()),
        scope_id=scope_id,
        source_id=item["source_id"],
        source_record_id=item["source_record_id"],
        version_id=item["version_id"],
        evidence_key=canonical_hash([item["source_id"], item["source_record_id"], item["version_id"]]),
        contribution=copy.deepcopy(value),
        matched=value["matched"],
        withdrawn=value["withdrawn"],
        source_site=value["source_site"],
        publication_start=datetime.fromisoformat(value["publication_start"])
        if value["publication_start"]
        else None,
        publication_end=datetime.fromisoformat(value["publication_end"])
        if value["publication_end"]
        else None,
        observed_at=datetime.fromisoformat(item["received_at"].replace("Z", "+00:00")),
        trial_id=value["facts"].get("trial_id"),
        registration_status=value["facts"].get("status") if value["facts"].get("trial_id") else None,
        metric_period_end=fact_time(observation["period_end"], lower=True)
        if observation.get("kind") == "metric"
        else None,
        dependency_source_id=basis_ref.get("source_id"),
        supersedes_ref=observation.get("supersedes"),
        attention_pending=False,
    )


def analysis_budget_wait(analysis):
    progress = analysis.get("progress") or {}
    wait = progress.get("budget_wait") or {}
    waiting = analysis.get("state") == "waiting_budget" or bool(progress.get("budget_pending")) or bool(wait)
    return waiting, wait.get("next_budget_at") or progress.get("next_budget_at")


class TopicService:
    def __init__(self, engine, reader=None, *, fetch_hook=None):
        self.engine, self.reader, self.fetch_hook = engine, reader, fetch_hook
        self.actor = DevelopmentAccess(engine.settings).actor()
        if reader is not None:
            self.engine.history_source_reader = reader.sources
            if fetch_hook is None and getattr(engine.settings, "data_fabric_history_fetch_token", None):
                from bioradar.research.investigations.fetch_execution import make_topic_fetch_hook

                self.fetch_hook = make_topic_fetch_hook(
                    engine.settings, reader, guard=self._guard_fetch_context
                )

    def _guard_fetch_context(self, context):
        # A persisted topic does not retain a revoked source permission.
        self.engine._history_source_ids = None
        self.engine._history_permission_failure = None
        view = self.get(self.actor, context["topic_id"])
        if view.scope_revision != context["scope_revision"] or view.paused:
            raise DomainError("topic_followup_paused", "专题已暂停或范围已更新", 409)
        self.engine.authorize_history_source(context["source_id"])
        if getattr(self, "_active_monitor", None):
            with Session(self.engine.database) as session:
                self._active_monitor._guard(self._active_monitor._state(session))

    def _authorize(self, actor, definition, is_demo, *, online=False):
        if actor != self.actor:
            raise DomainError("permission_denied", "研究授权上下文不匹配", 403)
        instance = definition["source_instance"]
        demo_instance = getattr(self.engine.settings, "data_fabric_demo_source_instance", None) or "fixture"
        if (is_demo and instance != demo_instance) or (
            not is_demo and instance in {"fixture", demo_instance}
        ):
            raise DomainError("fixture_scope_mismatch", "教学与真实专题来源须分开", 422)
        allowed = self.engine.settings.engine_source_id_allowlist.get(instance, [])
        if instance not in actor.source_allowlist or not set(definition["source_ids"]) <= set(allowed):
            raise DomainError("source_not_authorized", "专题来源不在当前采用许可范围", 403)
        if online:
            for source in definition["source_ids"]:
                self.engine.authorize_history_source(source)

    def _scope(self, session, actor, identifier, *, lock=False):
        statement = select(TopicScopeRecord).where(
            TopicScopeRecord.topic_id == identifier,
            TopicScopeRecord.workspace_id == actor.workspace_id,
            TopicScopeRecord.is_current.is_(True),
        )
        row = session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise DomainError("not_found", "未找到可访问的专题", 404)
        self._authorize(actor, row.definition, row.is_demo)
        return row

    def _view(self, session, row):
        progress = row.progress
        sources = [
            {
                "source_id": s,
                **{
                    k: v
                    for k, v in progress.get(s, {}).items()
                    if k
                    in {
                        "after",
                        "scan_max_seq",
                        "seed_complete",
                        "last_success_at",
                        "error_code",
                        "upstream_health",
                    }
                },
            }
            for s in row.definition["source_ids"]
        ]
        errors = [v.get("error_code") for v in sources if v.get("error_code")]
        latest = session.scalar(
            select(TopicEvaluationRecord.id)
            .where(TopicEvaluationRecord.scope_id == row.id)
            .order_by(TopicEvaluationRecord.created_at.desc())
            .limit(1)
        )
        count = session.scalar(
            select(func.count())
            .select_from(TopicObservationRecord)
            .where(TopicObservationRecord.scope_id == row.id, *_selected(row.observation_revision))
        )
        state = (
            "paused"
            if row.paused
            else "source_unavailable"
            if errors
            else "seeding"
            if not all(v.get("seed_complete") for v in sources)
            else "catching_up"
            if any(progress.get(s["source_id"], {}).get("cursor") for s in sources)
            else "current"
        )
        return TopicView(
            id=row.topic_id,
            is_demo=row.is_demo,
            definition=row.definition,
            scope_revision=row.scope_revision,
            control_revision=row.control_revision,
            observation_revision=row.observation_revision,
            paused=row.paused,
            state=state,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_checked_at=row.last_checked_at,
            last_evaluated_at=row.last_evaluated_at,
            next_evaluation_at=row.next_evaluation_at,
            sources=sources,
            observation_count=count,
            latest_evaluation_id=latest,
            tracking=tracking_view(progress.get("_tracking")) or None,
            data_gaps=[
                "source_collection_freshness_unknown",
                "adopted_records_not_complete_domain_denominator",
            ]
            + (["source_unavailable_not_no_change"] if errors else []),
        )

    def _authorize_content(self, session, actor, row):
        self._authorize(actor, row.definition, row.is_demo, online=True)
        sources = session.scalars(
            select(TopicObservationRecord.dependency_source_id)
            .where(
                TopicObservationRecord.scope_id == row.id,
                *_selected(row.observation_revision),
                TopicObservationRecord.dependency_source_id.is_not(None),
            )
            .distinct()
        )
        allowed = self.engine.settings.engine_source_id_allowlist.get(row.definition["source_instance"], [])
        risk = (row.progress.get("_tracking") or {}).get("risk") or {}
        risk_sources = {
            source
            for assessment in (risk.get("assessment"), risk.get("prior_resolution"))
            if isinstance(assessment, dict)
            for source in assessment.get("basis_source_ids", [])
        }
        for source in set(sources) | risk_sources:
            if source not in allowed:
                raise DomainError("source_not_authorized", "专题依据当前未获来源许可", 403)
            self.engine.authorize_history_source(source)

    def templates(self, actor):
        if actor != self.actor:
            raise DomainError("permission_denied", "研究授权上下文不匹配", 403)
        allowed = self.engine.settings.engine_source_id_allowlist.get("datafabric-test", [])

        def sources(*choices):
            result = [s for s in choices if s in allowed]
            return result or list(choices[:1])

        templates = [
            (
                "pipeline",
                "管线登记与里程碑",
                "按原始登记ID累计状态和预计日期；不把登记猜成药物项目。",
                sources("legacy_documents", "clinicaltrials"),
                ["clinicaltrials"],
                ["text_exposure", "registered_state"],
                "登记字段可用时比较版本；无确认药物谱系时只报告试验登记。",
                [],
            ),
            (
                "research",
                "科研方向与证据修订",
                "累计文献文本线索，复查更正和撤回；同一研究的多篇报告不视为独立。",
                sources("legacy_documents", "pubmed", "europe_pmc"),
                ["pubmed", "europe_pmc"],
                ["text_exposure", "source_event"],
                "文本出现率仅代表已采用材料；研究独立性不足时不确认趋势。",
                [],
            ),
            (
                "business",
                "商业与资源变化",
                "同单位、同期间、同范围的原始指标到达后才计算，不以新闻量推收入。",
                sources("external_archive"),
                [],
                ["typed_metric"],
                "等待有出处的商业指标；当前未提供不代表经营没有变化。",
                ["sec_reported_metrics"],
            ),
            (
                "public_health",
                "公共卫生观察",
                "公告是事件材料；病例率和检测阳性率须另有同口径指标与分母。",
                sources("who_don", "external_archive"),
                [],
                ["source_event", "typed_metric"],
                "公告数量不是病例数；未接入合格分母时不计算发病风险。",
                ["comparable_population_or_testing_denominator"],
            ),
        ]
        return TopicTemplates(
            items=[
                TopicTemplate(
                    id=key,
                    title=title,
                    description=description,
                    definition=TopicDefinition(
                        title=title,
                        question=description,
                        domain=key,
                        source_ids=ids,
                        source_sites=sites,
                        profiles=profiles,
                    ),
                    qualification=qualification,
                    missing_sources=missing,
                )
                for key, title, description, ids, sites, profiles, qualification, missing in templates
            ],
            limitations=[
                "模板不扩大来源许可，不保证各领域已取得数据。",
                "当前指标字面绑定不等于专业真实性、独立性或可比性评价。",
            ],
        )

    def create(self, actor, body: TopicCreate, key, *, automation=None):
        definition = body.definition.model_dump(mode="json")
        self._authorize(actor, definition, body.is_demo)
        with Session(self.engine.database) as session, session.begin():

            def mutate():
                if automation is not None:
                    from bioradar.research.monitor.models import MonitorState

                    session.get(MonitorState, actor.workspace_id, with_for_update=True)
                    active = session.scalar(
                        select(func.count())
                        .select_from(TopicScopeRecord)
                        .where(
                            TopicScopeRecord.workspace_id == actor.workspace_id,
                            TopicScopeRecord.is_current.is_(True),
                            TopicScopeRecord.progress["_tracking"]["automatic"].as_boolean().is_(True),
                            TopicScopeRecord.progress["_tracking"]["lifecycle"].astext != "closed",
                        )
                    )
                    if active >= self.engine.settings.topic_max_auto_active:
                        raise DomainError(
                            "automatic_topic_capacity", "自动专题上限已满，线索保留等待名额", 409
                        )
                now, identifier = datetime.now(UTC), str(uuid4())
                row = TopicScopeRecord(
                    id=str(uuid4()),
                    topic_id=identifier,
                    workspace_id=actor.workspace_id,
                    scope_revision=1,
                    control_revision=1,
                    is_current=True,
                    is_demo=body.is_demo,
                    paused=False,
                    definition=definition,
                    progress={
                        "_tracking": {
                            "version": "source-evidence-heat-lifecycle-v1",
                            "automatic": True,
                            "origin": copy.deepcopy(automation),
                            "lifecycle": "incipient",
                        }
                    }
                    if automation is not None
                    else {},
                    observation_revision=0,
                    next_source=0,
                    updated_at=now,
                    next_evaluation_at=now,
                )
                session.add(row)
                session.flush()
                return 201, self._view(session, row).model_dump(mode="json")

            request = (
                {"request": body.model_dump(mode="json"), "automation": automation}
                if automation is not None
                else body
            )
            return self.engine._idempotent(session, actor, "topic.create", key, request, mutate)

    def update(self, actor, identifier, body: TopicUpdate, key):
        definition = body.definition.model_dump(mode="json")
        self._authorize(actor, definition, body.is_demo)
        with Session(self.engine.database) as session, session.begin():

            def mutate():
                old = self._scope(session, actor, identifier, lock=True)
                if old.control_revision != body.expected_revision:
                    raise DomainError("revision_conflict", "专题范围已更新，请刷新后修改", 409)
                if old.is_demo != body.is_demo:
                    raise DomainError("fixture_scope_mismatch", "同一专题不能切换真实与教学资料", 422)
                old.is_current = False
                session.flush()
                now = datetime.now(UTC)
                row = TopicScopeRecord(
                    id=str(uuid4()),
                    topic_id=identifier,
                    workspace_id=actor.workspace_id,
                    scope_revision=old.scope_revision + 1,
                    control_revision=old.control_revision + 1,
                    is_current=True,
                    is_demo=old.is_demo,
                    paused=old.paused,
                    definition=definition,
                    progress={"_tracking": copy.deepcopy(old.progress["_tracking"])}
                    if old.progress.get("_tracking")
                    else {},
                    observation_revision=0,
                    next_source=0,
                    updated_at=now,
                    next_evaluation_at=now,
                )
                if row.progress.get("_tracking"):
                    tracking = copy.deepcopy(row.progress["_tracking"])
                    risk = tracking.get("risk") or {}
                    if risk.get("assessment") or risk.get("status") == "resolved":
                        tracking["risk"] = {
                            "status": "unresolved" if definition["domain"] == "public_health" else "unknown",
                            "critical_gaps": ["topic_scope_changed_requires_reassessment"],
                            "basis_refs": [],
                            "prior_resolution": copy.deepcopy(risk.get("assessment")),
                        }
                        row.progress = {"_tracking": tracking}
                session.add(row)
                session.flush()
                return 200, self._view(session, row).model_dump(mode="json")

            return self.engine._idempotent(session, actor, "topic.update:" + identifier, key, body, mutate)

    def control(self, actor, identifier, body: TopicControl, key):
        with Session(self.engine.database) as session:
            self._authorize_content(session, actor, self._scope(session, actor, identifier))
        with Session(self.engine.database) as session, session.begin():

            def mutate():
                row = self._scope(session, actor, identifier, lock=True)
                if row.control_revision != body.expected_revision:
                    raise DomainError("revision_conflict", "专题开关已更新，请刷新后操作", 409)
                if body.lifecycle is not None:
                    progress = copy.deepcopy(row.progress)
                    tracking = copy.deepcopy(progress.get("_tracking") or {})
                    tracking.update(
                        lifecycle=body.lifecycle,
                        _manual_closed=body.lifecycle == "closed",
                        manual_control={
                            "actor_id": actor.actor_id,
                            "at": datetime.now(UTC).isoformat(),
                            "reason": body.reason,
                        },
                    )
                    progress["_tracking"] = tracking
                    row.progress = progress
                    if body.lifecycle == "warming":
                        row.next_evaluation_at = datetime.now(UTC)
                row.paused, row.control_revision, row.updated_at = (
                    True if body.lifecycle == "closed" else body.paused,
                    row.control_revision + 1,
                    datetime.now(UTC),
                )
                return 200, self._view(session, row).model_dump(mode="json")

            code, payload = self.engine._idempotent(
                session, actor, "topic.control:" + identifier, key, body, mutate
            )
        with Session(self.engine.database) as session:
            frozen = session.scalar(
                select(TopicScopeRecord).where(
                    TopicScopeRecord.topic_id == identifier,
                    TopicScopeRecord.workspace_id == actor.workspace_id,
                    TopicScopeRecord.scope_revision == payload["scope_revision"],
                )
            )
            self._authorize_content(session, actor, frozen)
        return code, payload

    def get(self, actor, identifier):
        with Session(self.engine.database) as session:
            row = self._scope(session, actor, identifier)
            self._authorize_content(session, actor, row)
            return self._view(session, row)

    def list(self, actor, limit=30, cursor=None):
        with Session(self.engine.database) as session:
            query = select(TopicScopeRecord).where(
                TopicScopeRecord.workspace_id == actor.workspace_id, TopicScopeRecord.is_current.is_(True)
            )
            if cursor:
                query = query.where(TopicScopeRecord.topic_id > cursor)
            rows = list(session.scalars(query.order_by(TopicScopeRecord.topic_id).limit(limit + 1)))
            values, unavailable = [], 0
            for row in rows[:limit]:
                try:
                    self._authorize_content(session, actor, row)
                except DomainError as exc:
                    if exc.code in SOURCE_READ_UNAVAILABLE | {"source_not_authorized"}:
                        unavailable += 1
                        continue
                    raise
                values.append(self._view(session, row))
            return TopicList(
                items=values,
                unavailable_count=unavailable,
                next_cursor=rows[limit - 1].topic_id if len(rows) > limit else None,
            )

    def _evaluation_row(self, session, actor, identifier, evaluation_id):
        row = session.get(TopicEvaluationRecord, evaluation_id)
        scope = session.get(TopicScopeRecord, row.scope_id) if row else None
        if scope is None or scope.topic_id != identifier or scope.workspace_id != actor.workspace_id:
            raise DomainError("not_found", "未找到可访问的专题评价", 404)
        # Use the old definition and all its source permissions, not today's edited scope.
        self._authorize(actor, scope.definition, scope.is_demo, online=True)
        for source in row.result.get("dependency_source_ids", []):
            if source not in self.engine.settings.engine_source_id_allowlist.get(
                scope.definition["source_instance"], []
            ):
                raise DomainError("source_not_authorized", "该评价的原始依据当前未获授权", 403)
            self.engine.authorize_history_source(source)
        return row, scope

    @staticmethod
    def _evaluation_view(row):
        result = {
            k: v
            for k, v in row.result.items()
            if k
            not in {
                "dependency_source_ids",
                "coverage_fingerprint",
                "attention_fingerprint",
                "content_revision",
                "closed_bucket_end",
            }
        }
        if row.followup.get("hypothesis_comparison"):
            result["tracking"] = copy.deepcopy(result.get("tracking") or {})
            result["tracking"]["hypothesis_comparison"] = copy.deepcopy(row.followup["hypothesis_comparison"])
        if result.get("tracking") and row.followup.get("status") == "waiting_budget":
            result["tracking"] = copy.deepcopy(result["tracking"])
            result["tracking"]["model_update"].update(
                status="waiting_budget", retry_at=row.followup.get("next_budget_at")
            )
        result.update(
            investigation_id=row.followup.get("investigation_id"),
            automation_status=row.followup.get("status", "not_needed"),
        )
        return TopicEvaluation.model_validate(result)

    def evaluation(self, actor, identifier, evaluation_id):
        with Session(self.engine.database) as session:
            row, _ = self._evaluation_row(session, actor, identifier, evaluation_id)
            return self._evaluation_view(row)

    def evaluations(self, actor, identifier, limit=20, cursor=None):
        with Session(self.engine.database) as session:
            self._scope(session, actor, identifier)
            query = (
                select(TopicEvaluationRecord)
                .join(TopicScopeRecord)
                .where(
                    TopicScopeRecord.topic_id == identifier,
                    TopicScopeRecord.workspace_id == actor.workspace_id,
                )
            )
            if cursor:
                boundary = session.get(TopicEvaluationRecord, cursor)
                if boundary is None:
                    raise DomainError("cursor_invalid", "评价列表位置无效", 422)
                query = query.where(
                    or_(
                        TopicEvaluationRecord.created_at < boundary.created_at,
                        (TopicEvaluationRecord.created_at == boundary.created_at)
                        & (TopicEvaluationRecord.id < cursor),
                    )
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        TopicEvaluationRecord.created_at.desc(), TopicEvaluationRecord.id.desc()
                    ).limit(limit + 1)
                )
            )
            values, unavailable = [], 0
            for row in rows[:limit]:
                try:
                    row, _ = self._evaluation_row(session, actor, identifier, row.id)
                except DomainError as exc:
                    if exc.code in SOURCE_READ_UNAVAILABLE | {"source_not_authorized"}:
                        unavailable += 1
                        continue
                    raise
                values.append(self._evaluation_view(row))
            return TopicEvaluationList(
                items=values,
                unavailable_count=unavailable,
                next_cursor=rows[limit - 1].id if len(rows) > limit else None,
            )

    def evidence(self, actor, identifier, evaluation_id, evidence_key):
        with Session(self.engine.database) as session:
            row, scope = self._evaluation_row(session, actor, identifier, evaluation_id)
            # Full old-revision ledger lookup, even when the readable report preview was bounded.
            item = session.scalar(
                select(TopicObservationRecord).where(
                    TopicObservationRecord.scope_id == scope.id,
                    TopicObservationRecord.evidence_key == evidence_key,
                    *_selected(row.observation_revision),
                )
            )
            binding = item.contribution if item else None
            if binding is None:
                raise DomainError("not_found", "该版本不是本次冻结评价的依据", 404)
            demo = scope.is_demo
        return self.reader.read_version(
            {**binding, "is_demo": demo}, mode="current", cutoff=datetime.now(UTC)
        )

    def apply_page(self, scope_id, source, expected, page, *, now):
        """All contribution replacements and this topic's page ACK share one transaction."""
        with Session(self.engine.database) as session:
            initial = session.get(TopicScopeRecord, scope_id)
            definition = copy.deepcopy(initial.definition)
        projected = []
        for item in page["items"]:
            engine_record = project_record(
                self.reader, item, as_of=now, bootstrap=not expected.get("seed_complete")
            )
            if engine_record is None:
                continue
            projected.append((item, project_contribution(engine_record, item, definition, as_of=now)))
        with Session(self.engine.database) as session, session.begin():
            row = session.get(TopicScopeRecord, scope_id, with_for_update=True)
            if getattr(self, "_active_monitor", None) is not None:
                self._active_monitor._guard(self._active_monitor._state(session))
            if not row.is_current or row.paused or row.progress.get(source, {}) != expected:
                return False
            progress = copy.deepcopy(row.progress)
            revision = row.observation_revision + 1
            changed = False
            for item, contribution in projected:
                native = item["source_record_id"]

                def version_query(native_id, source_id=source):
                    return select(TopicObservationRecord).where(
                        TopicObservationRecord.scope_id == row.id,
                        TopicObservationRecord.source_id == source_id,
                        TopicObservationRecord.source_record_id == native_id,
                    )

                old = session.scalar(
                    version_query(native).where(
                        TopicObservationRecord.selected_from.is_not(None),
                        TopicObservationRecord.selected_to.is_(None),
                    )
                )
                existing = session.scalar(
                    version_query(native).where(TopicObservationRecord.version_id == item["version_id"])
                )
                supersedes = (contribution.get("observation") or {}).get("supersedes")
                replaced = None
                if supersedes:
                    replaced = session.scalar(
                        version_query(supersedes["source_record_id"], supersedes["source_id"]).where(
                            TopicObservationRecord.version_id == supersedes["version_id"],
                            TopicObservationRecord.selected_from.is_not(None),
                            TopicObservationRecord.selected_to.is_(None),
                        )
                    )
                    if replaced and replaced.contribution["raw_sha256"] != supersedes["raw_sha256"]:
                        raise DomainError("topic_reference_integrity", "修订引用与已采用原件不一致", 502)
                head = page["current_version_ids"][native] == item["version_id"]
                if existing is None and (contribution["matched"] or old is not None or supersedes):
                    existing = observation_row(row.id, item, contribution)
                    session.add(existing)
                    changed = True
                if not head or existing is None or existing.selected_from is not None:
                    continue
                exact_ref = {
                    k: item[k] for k in ("source_id", "source_record_id", "version_id", "raw_sha256")
                }
                superseder = session.scalar(
                    select(TopicObservationRecord.id)
                    .where(
                        TopicObservationRecord.scope_id == row.id,
                        TopicObservationRecord.supersedes_ref.contains(exact_ref),
                        TopicObservationRecord.selected_from.is_not(None),
                    )
                    .limit(1)
                )
                if superseder:
                    continue  # A revision may arrive before the older original during seed.
                prior_row = old or replaced
                for previous_row in {
                    v.id: v for v in (old, replaced) if v is not None and v.id != existing.id
                }.values():
                    previous_row.selected_to = revision
                session.flush()
                if prior_row is not None and prior_row.id != existing.id:
                    prior = prior_row.contribution
                    contribution["change"] = {
                        "state_changed": any(
                            prior["facts"].get(k) != contribution["facts"].get(k) for k in ("status", "phase")
                        )
                        and bool(contribution["facts"].get("trial_id")),
                        "evidence_changed": prior["raw_sha256"] != contribution["raw_sha256"]
                        and prior["content_fingerprint"] != contribution["content_fingerprint"],
                        "projection_changed": prior["raw_sha256"] == contribution["raw_sha256"]
                        and prior["content_fingerprint"] != contribution["content_fingerprint"],
                        "previous_version_id": prior_row.version_id,
                        "previous_matched": prior["matched"],
                        "previous_status": prior["facts"].get("status"),
                        "previous_phase": prior["facts"].get("phase"),
                    }
                    if not contribution["change"]["state_changed"]:
                        contribution["first_seen_at"] = prior["first_seen_at"]
                    existing.contribution = contribution
                existing.selected_from = revision
                if prior_row is None or (
                    prior_row.contribution["raw_sha256"] != contribution["raw_sha256"]
                    and prior_row.contribution["content_fingerprint"] != contribution["content_fingerprint"]
                ):
                    progress["_content_revision"] = progress.get("_content_revision", 0) + 1
                existing.attention_pending = (
                    contribution["withdrawn"]
                    or bool(contribution.get("observation"))
                    or contribution["source_id"] in {"who_don", "fda_crl"}
                    or any(
                        contribution.get("change", {}).get(k) for k in ("state_changed", "evidence_changed")
                    )
                )
                changed = True
            progress[source] = {
                "after": page["next_after"] if not page["next_cursor"] else expected.get("after", 0),
                "cursor": page["next_cursor"],
                "last_page_after": page["next_after"],
                "scan_max_seq": page["max_seq"] if page["next_cursor"] else None,
                "seed_complete": expected.get("seed_complete", False) or page["next_cursor"] is None,
                "last_success_at": now.isoformat(),
                "error_code": None,
                "upstream_health": page.get("_upstream_health"),
            }
            row.progress, row.observation_revision = (
                progress,
                revision if changed else row.observation_revision,
            )
            row.last_checked_at, row.updated_at = now, now
            row.next_source = (row.next_source + 1) % len(definition["source_ids"])
            return True

    def evaluate(self, scope_id, *, now):
        with Session(self.engine.database) as session:
            row = session.get(TopicScopeRecord, scope_id)
            if not row.is_current or row.paused:
                return None
            definition, progress = copy.deepcopy(row.definition), copy.deepcopy(row.progress)
            revision, topic_id, scope_revision = row.observation_revision, row.topic_id, row.scope_revision
            previous = session.scalar(
                select(TopicEvaluationRecord)
                .where(TopicEvaluationRecord.scope_id == row.id)
                .order_by(TopicEvaluationRecord.created_at.desc())
                .limit(1)
            )
            coverage_key = canonical_hash(
                {
                    s: {
                        "seed_complete": progress.get(s, {}).get("seed_complete", False),
                        "catching_up": bool(progress.get(s, {}).get("cursor")),
                        "error_code": progress.get(s, {}).get("error_code"),
                    }
                    for s in definition["source_ids"]
                }
            )
            pending_attention_ids = list(
                session.scalars(
                    select(TopicObservationRecord.id)
                    .where(
                        TopicObservationRecord.scope_id == row.id,
                        *_selected(revision),
                        TopicObservationRecord.attention_pending.is_(True),
                    )
                    .order_by(
                        case(
                            (
                                TopicObservationRecord.contribution["observation"]["kind"].astext == "metric",
                                1,
                            ),
                            else_=0,
                        ),
                        TopicObservationRecord.selected_from,
                        TopicObservationRecord.id,
                    )
                    .limit(50)
                )
            )
            attention_key = canonical_hash(pending_attention_ids)
            # Source changes can freeze an updated report; a quiet topic only re-evaluates when due.
            if (
                previous
                and previous.result.get("compute_version") == TOPIC_COMPUTE_VERSION
                and (
                    previous.observation_revision == revision
                    or not all(
                        progress.get(s, {}).get("seed_complete") and not progress.get(s, {}).get("cursor")
                        for s in definition["source_ids"]
                    )
                )
                and previous.result.get("coverage_fingerprint") == coverage_key
                and (
                    not pending_attention_ids or previous.result.get("attention_fingerprint") == attention_key
                )
                and row.next_evaluation_at
                and now < row.next_evaluation_at
            ):
                return previous.id
            due_slot = int(now.timestamp()) // (definition["cadence_hours"] * 3600)
            key = canonical_hash(
                [scope_id, revision, due_slot, coverage_key, attention_key, TOPIC_COMPUTE_VERSION]
            )
            existing = session.scalar(
                select(TopicEvaluationRecord).where(
                    TopicEvaluationRecord.scope_id == scope_id, TopicEvaluationRecord.evaluation_key == key
                )
            )
            if existing:
                return existing.id
            selected = select(TopicObservationRecord).where(
                TopicObservationRecord.scope_id == row.id, *_selected(revision)
            )
            matched = TopicObservationRecord.matched.is_(True)
            withdrawn = TopicObservationRecord.withdrawn.is_(True)
            full_counts = session.execute(
                select(
                    func.count(),
                    func.sum(case((matched, 1), else_=0)),
                    func.sum(case((matched & ~withdrawn, 1), else_=0)),
                    func.sum(case((matched & withdrawn, 1), else_=0)),
                    func.sum(
                        case(
                            (
                                matched & TopicObservationRecord.publication_start.is_(None),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                ).where(TopicObservationRecord.scope_id == row.id, *_selected(revision))
            ).one()
            dependency_sources = sorted(
                session.scalars(
                    select(TopicObservationRecord.dependency_source_id)
                    .where(
                        TopicObservationRecord.scope_id == row.id,
                        *_selected(revision),
                        TopicObservationRecord.dependency_source_id.is_not(None),
                    )
                    .distinct()
                )
            )
            observed_sites = sorted(
                session.scalars(
                    select(TopicObservationRecord.source_site)
                    .where(TopicObservationRecord.scope_id == row.id, *_selected(revision), matched)
                    .distinct()
                )
            )
            registrations = (
                select(
                    TopicObservationRecord.trial_id,
                    func.count(func.distinct(TopicObservationRecord.registration_status)).label("statuses"),
                    func.min(TopicObservationRecord.registration_status).label("status"),
                )
                .where(
                    TopicObservationRecord.scope_id == row.id,
                    *_selected(revision),
                    matched,
                    ~withdrawn,
                    TopicObservationRecord.trial_id.is_not(None),
                )
                .group_by(TopicObservationRecord.trial_id)
                .subquery()
            )
            registration_status = case(
                (registrations.c.statuses == 0, "unknown"),
                (registrations.c.statuses == 1, registrations.c.status),
                else_="conflicting_registration_status",
            )
            status_counts = dict(
                session.execute(
                    select(registration_status, func.count())
                    .select_from(registrations)
                    .group_by(registration_status)
                ).all()
            )
            # An explicit calendar window for text, not a random sample of the entire stock.
            from bioradar.research.mining.topic_series import _bucket_start, _previous_bucket

            window_start = _bucket_start(now, definition["bucket"])
            for _ in range(definition["lookback_buckets"] - 1):
                window_start = _previous_bucket(window_start, definition["bucket"])
            dated = or_(
                TopicObservationRecord.publication_end >= window_start,
                TopicObservationRecord.metric_period_end >= window_start,
            )
            recent_query = selected.where(matched, dated)
            recent_count = session.scalar(select(func.count()).select_from(recent_query.subquery()))
            observations = list(
                session.scalars(
                    recent_query.order_by(
                        TopicObservationRecord.publication_start.desc().nullslast(),
                        TopicObservationRecord.source_id,
                        TopicObservationRecord.source_record_id,
                    ).limit(EVALUATION_LIMIT)
                )
            )
            # Pending changes bypass text-volume budgets. At most one source page enters per tick.
            attention = list(
                session.scalars(
                    selected.where(TopicObservationRecord.attention_pending.is_(True))
                    .order_by(
                        case(
                            (
                                TopicObservationRecord.contribution["observation"]["kind"].astext == "metric",
                                1,
                            ),
                            else_=0,
                        ),
                        TopicObservationRecord.selected_from,
                        TopicObservationRecord.id,
                    )
                    .limit(50)
                )
            )
            preview = list(
                session.scalars(
                    selected.where(
                        matched,
                        ~withdrawn,
                        TopicObservationRecord.trial_id.is_not(None),
                    )
                    .order_by(
                        TopicObservationRecord.observed_at.desc(),
                        TopicObservationRecord.source_record_id,
                    )
                    .limit(100)
                )
            )
            unique = {v.id: v for v in [*observations, *attention, *preview]}
            values = [
                {**copy.deepcopy(v.contribution), "include_attention": v.id in {a.id for a in attention}}
                for v in unique.values()
            ]
            omitted = max(0, recent_count - EVALUATION_LIMIT)
            seed = all(
                progress.get(s, {}).get("seed_complete") and not progress.get(s, {}).get("cursor")
                for s in definition["source_ids"]
            )
            watermarks = {s: progress.get(s, {}).get("last_page_after", 0) for s in definition["source_ids"]}
            errors = {
                s: progress[s]["error_code"]
                for s in definition["source_ids"]
                if progress.get(s, {}).get("error_code")
            }
            old_keys = set(progress.get("_notified_signals", []))
            expected_progress = copy.deepcopy(progress)
        computed = evaluate_series(
            values,
            definition,
            as_of=now,
            seed_complete=seed,
            source_watermarks=watermarks,
            source_errors=errors,
            omitted_records=omitted,
        )
        computed["coverage"].update(
            matched_records=int(full_counts[1] or 0),
            active_records=int(full_counts[2] or 0),
            withdrawn_records=int(full_counts[3] or 0),
            undated_records=int(full_counts[4] or 0),
            registered_status_counts=status_counts,
            observed_source_sites=observed_sites,
            missing_source_sites=sorted(set(definition["source_sites"]) - set(observed_sites)),
        )
        old_summary = computed["summary"]
        active_count = int(full_counts[2] or 0)
        undated_count = int(full_counts[4] or 0)
        computed["summary"] = (
            f"已采用范围共有{active_count}条当前有效记录；本次有限计算{len(values)}条，"
            f"形成{len(computed['signals'])}项待核变化。"
        )
        if undated_count:
            computed["summary"] += (
                f"累计采用记录中有{undated_count}条缺少可用于分期的日期，不据此填成零增长。"
            )
            if "undated_or_unverified_publication_time" not in computed["data_gaps"]:
                computed["data_gaps"].append("undated_or_unverified_publication_time")
        if omitted:
            computed["summary"] += f"文字窗口另有{omitted}条未参与，不能确认整体趋势。"
        if active_count and not values:
            computed["data_gaps"] = [
                gap
                for gap in computed["data_gaps"]
                if gap != "no_eligible_observations_not_evidence_of_absence"
            ]
            computed["data_gaps"].append("adopted_records_outside_current_computation")
            computed["next_questions"] = [
                "已累计资料，但当前计算窗口或日期、字段资格不足；哪些原始字段需要补齐？"
                if question == "当前来源尚未提供合格资料；请核对采集与发布覆盖。"
                else question
                for question in computed["next_questions"]
            ]
            computed["state"] = "insufficient_data"
            computed["draft"] = computed["draft"].replace(
                "当前来源尚未提供合格资料；请核对采集与发布覆盖。",
                "已累计资料，但当前计算窗口或日期、字段资格不足；请核对原始日期与覆盖。",
            )
        computed["draft"] = computed["draft"].replace(old_summary, computed["summary"], 1)
        if sum(status_counts.values()) > 100:
            computed["data_gaps"].append("registered_state_preview_limited")
        tracking = update_tracking(
            values, definition, progress.get("_tracking"), as_of=now, source_errors=errors
        )
        progress["_tracking"] = tracking
        if tracking["model_update"]["needed"]:
            claim_values = [v for v in values if (v.get("attention") or {}).get("checkable_claims")]
            if claim_values:
                computed["signals"].append(
                    {
                        "key": canonical_hash(["source_claim", tracking["_evidence_fingerprint"]]),
                        "kind": "source_claim",
                        "title": "有出处的待核说法发生变化",
                        "reason": "；".join(tracking["assessment_reason"]),
                        "support_units": len(claim_values),
                        "qualification": "attributed_claim_not_confirmation_of_truth",
                        "details": {"risk": tracking["risk"]},
                        "evidence_keys": [
                            canonical_hash([v["source_id"], v["source_record_id"], v["version_id"]])
                            for v in claim_values
                        ],
                        "next_questions": ["哪些原始来源、反证或检验可以区分竞争解释？"],
                    }
                )
        closed_bucket_end = max(
            (b["end"] for b in computed["buckets"] if b["complete_time_bucket"]), default=None
        )
        content_revision = progress.get("_content_revision", 0)
        slow_changed = bool(
            progress.get("_baseline_complete")
            and (
                content_revision != progress.get("_slow_content_revision", 0)
                or closed_bucket_end != progress.get("_slow_closed_bucket_end")
            )
        )
        baseline_keys = set(progress.get("_baseline_signals", []))
        baseline_complete = bool(progress.get("_baseline_complete"))
        meaningful = []
        for signal in computed["signals"]:
            if signal["key"] in old_keys:
                continue
            if signal["kind"] in {"text_pattern", "milestone_check"}:
                if not (
                    baseline_complete
                    and seed
                    and not errors
                    and slow_changed
                    and signal["key"] not in baseline_keys
                ):
                    continue
            elif previous is None and signal["kind"] not in {"source_event", "source_claim"}:
                continue
            meaningful.append(signal)
        # The first complete stock is a baseline. Subsequent qualified changes become
        # one bounded topic inquiry, never a medical conclusion or one inquiry per phrase.
        if seed and not errors:
            progress["_slow_content_revision"] = content_revision
            progress["_slow_closed_bucket_end"] = closed_bucket_end
        if seed and not errors and not baseline_complete:
            progress["_baseline_complete"] = True
            progress["_baseline_signals"] = [
                s["key"] for s in computed["signals"] if s["kind"] in {"text_pattern", "milestone_check"}
            ]
        meaningful = [
            s
            for s in meaningful
            if s["kind"] != "source_event"
            or (
                (event := s["details"].get("event_time")) is not None
                and fact_time(event.get("value") if isinstance(event, dict) else event)
                and fact_time(event.get("value") if isinstance(event, dict) else event)
                >= now - timedelta(days=30)
            )
        ]
        grounded_claim_keys = {
            canonical_hash([v["source_id"], v["source_record_id"], v["version_id"]])
            for v in values
            if (v.get("attention") or {}).get("automatic_investigation_allowed")
        }
        # Exposure statistics stay visible. Generic words cannot borrow a native
        # registration's unrelated metadata to become an automatic inquiry.
        meaningful = [
            s
            for s in meaningful
            if s["kind"] != "text_pattern" or grounded_claim_keys.intersection(s["evidence_keys"])
        ]
        actionable = [
            s
            for s in meaningful
            if s["kind"] in {"state_change", "evidence_revision", "source_event", "source_claim"}
            or seed
            and not errors
        ]
        eligible = bool(actionable) and tracking["lifecycle"] not in {"dormant", "closed"}
        if eligible and not tracking["model_update"]["needed"]:
            tracking["model_update"] = {
                "needed": True,
                "status": "ready",
                "reasons": ["new_fixed_source_signal_requires_review"],
            }
        meaningful = actionable
        identifier = str(uuid4())
        result = {
            "id": identifier,
            "topic_id": topic_id,
            "scope_revision": scope_revision,
            "definition": definition,
            "coverage_fingerprint": coverage_key,
            "attention_fingerprint": attention_key,
            "content_revision": content_revision,
            "closed_bucket_end": closed_bucket_end,
            "compute_version": TOPIC_COMPUTE_VERSION,
            "observation_revision": revision,
            "as_of": now.isoformat(),
            "created_at": now.isoformat(),
            **computed,
            "dependency_source_ids": dependency_sources,
            "tracking": tracking_view(tracking),
        }
        followup = {
            "status": "deferred" if eligible else "not_needed",
            "signal_keys": [s["key"] for s in meaningful],
            "steps": [],
            "query_intents": [],
            "investigation_id": None,
        }
        with Session(self.engine.database) as session, session.begin():
            row = session.get(TopicScopeRecord, scope_id, with_for_update=True)
            if (
                not row.is_current
                or row.paused
                or row.observation_revision != revision
                or row.progress != expected_progress
            ):
                return None
            exists = session.scalar(
                select(TopicEvaluationRecord).where(
                    TopicEvaluationRecord.scope_id == scope_id, TopicEvaluationRecord.evaluation_key == key
                )
            )
            if exists:
                return exists.id
            session.add(
                TopicEvaluationRecord(
                    id=identifier,
                    scope_id=scope_id,
                    evaluation_key=key,
                    observation_revision=revision,
                    result=result,
                    followup=followup,
                )
            )
            for item in attention:
                is_metric = (item.contribution.get("observation") or {}).get("kind") == "metric"
                if (seed and not errors) or not is_metric:
                    stored = session.get(TopicObservationRecord, item.id)
                    stored.attention_pending = False
            progress["_notified_signals"] = sorted(
                old_keys | ({s["key"] for s in meaningful} if eligible else set())
            )
            row.progress, row.last_evaluated_at = progress, now
            row.next_evaluation_at = datetime.fromisoformat(tracking["next_due_at"])
        return identifier

    def _save_followup(self, identifier, followup):
        with Session(self.engine.database) as session, session.begin():
            row = session.get(TopicEvaluationRecord, identifier, with_for_update=True)
            row.followup = copy.deepcopy(followup)

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

    def _followup_guard(self, evaluation_id):
        with Session(self.engine.database) as session:
            if getattr(self, "_active_monitor", None) is not None:
                self._active_monitor._guard(self._active_monitor._state(session))
            record = session.get(TopicEvaluationRecord, evaluation_id)
            scope = session.get(TopicScopeRecord, record.scope_id)
            if not scope.is_current or scope.paused:
                raise DomainError("topic_followup_paused", "专题已暂停或范围已更新，未继续自动整理", 409)

    def _append(self, evaluation, followup, view, body, tag):
        self._followup_guard(evaluation.id)
        key = "topic-entry:" + canonical_hash([evaluation.id, tag])
        saved = self._saved("investigation.append:" + view.id, key)
        if saved:
            return InvestigationService(self.engine).get(self.actor, view.id)
        intent = next((v for v in followup["steps"] if v["key"] == key), None)
        if intent is None:
            intent = {"key": key, "body": {"expected_revision": view.revision, **body}}
            followup["steps"].append(intent)
            self._save_followup(evaluation.id, followup)
        _, saved = InvestigationService(self.engine).append(
            self.actor,
            view.id,
            InvestigationEntryCreate.model_validate(intent["body"]),
            key,
            automation=InvestigationAutomation(
                source="topic", topic_id=evaluation.topic_id, evaluation_id=evaluation.id
            ),
        )
        return InvestigationService(self.engine).get(self.actor, saved["id"])

    def _adopt_risk_review(self, session, actor, scope, evaluation, analysis_id):
        from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord

        state = copy.deepcopy(evaluation.followup)
        if (
            evaluation.scope_id != scope.id
            or state.get("analysis_id") != analysis_id
            or not state.get("investigation_id")
        ):
            raise DomainError("topic_risk_analysis_mismatch", "仅可采用该专题评价实际关联的分析", 409)
        stored = session.get(InvestigationAnalysisRecord, analysis_id, with_for_update=True)
        if (
            stored is None
            or stored.workspace_id != actor.workspace_id
            or stored.is_demo != scope.is_demo
            or stored.investigation_id != state["investigation_id"]
        ):
            raise DomainError("not_found", "未找到可访问的专题分析", 404)
        progress = copy.deepcopy(scope.progress)
        tracking = copy.deepcopy(progress.get("_tracking") or {})
        expected = risk_scope_stamp(
            scope.topic_id, scope.scope_revision, scope.observation_revision, scope.definition, tracking
        )
        if (stored.result or {}).get("current_judgment") is not None:
            comparison = hypothesis_comparison(
                stored.result.get("competing_hypotheses") or [],
                stored.result.get("claims") or [],
                model_judgment=stored.result["current_judgment"],
            )
            comparison.update(
                analysis_id=stored.id,
                evidence_fingerprint=tracking.get("_evidence_fingerprint"),
                current_basis_unchanged=(stored.fixed_input.get("origin") or {}).get("topic_risk_scope")
                == expected,
            )
            tracking["hypothesis_comparison"] = comparison
            progress["_tracking"] = tracking
            scope.progress = progress
            state["hypothesis_comparison"] = comparison
            evaluation.followup = state
        analysis = {
            "id": stored.id,
            "state": stored.state,
            "fixed_input": stored.fixed_input,
            "result": stored.result,
            "progress": stored.progress,
        }
        signature = canonical_hash(
            [
                expected,
                stored.state,
                stored.result,
                stored.progress.get("report_risk_packets"),
                stored.progress.get("professional_operations"),
                (stored.progress.get("query_policy") or {}).get("refresh_not_semantically_reviewed"),
            ]
        )
        prior = state.get("risk_review") or {}
        if prior.get("input_sha256") == signature:
            if prior.get("status") == "pending":
                prior["last_checked_at"] = datetime.now(UTC).isoformat()
                state["risk_review"] = prior
                evaluation.followup = state
            return prior
        receipt = assess_risk_reassessment(analysis, expected_scope=expected)
        receipt.update(
            input_sha256=signature,
            evaluated_at=datetime.now(UTC).isoformat(),
            last_checked_at=datetime.now(UTC).isoformat(),
        )
        state["risk_review"] = receipt
        evaluation.followup = state
        if receipt["status"] == "accepted":
            finding = receipt["reassessment"]
            risk = copy.deepcopy(tracking.get("risk") or {})
            risk.update(
                status="resolved" if finding["decision"] == "resolved_within_scope" else "unresolved",
                critical_gaps=finding["remaining_critical_gaps"],
                conditions=finding["conditions"],
                reason=finding["reason"],
                scope=copy.deepcopy(expected),
                basis_refs=receipt["basis_refs"],
                assessment=copy.deepcopy(receipt),
            )
            tracking["risk"] = risk
            # A resolved scope may cool normally; an unresolved finding keeps the minimum cadence.
            if risk["status"] == "unresolved":
                tracking["cadence_hours"] = 1
            else:
                tracking["cadence_hours"] = scope.definition["cadence_hours"]
            due = datetime.now(UTC) + timedelta(hours=tracking["cadence_hours"])
            tracking["next_due_at"] = due.isoformat()
            progress["_tracking"] = tracking
            scope.progress, scope.next_evaluation_at = progress, due
            scope.control_revision += 1
            scope.updated_at = datetime.now(UTC)
        return receipt

    def review_risk(self, actor, identifier, body: TopicRiskReviewRequest, key):
        """The caller names an existing result; it cannot supply a risk decision."""
        from bioradar.research.investigations.analysis_service import AnalysisService

        with Session(self.engine.database) as session:
            evaluation, scope = self._evaluation_row(session, actor, identifier, body.evaluation_id)
            self._authorize_content(session, actor, scope)
            state = copy.deepcopy(evaluation.followup)
            if state.get("analysis_id") != body.analysis_id or not state.get("investigation_id"):
                raise DomainError("topic_risk_analysis_mismatch", "分析不属于本次专题评价", 409)
        # This normal read checks every analysis source permission, including supplementary evidence.
        AnalysisService(self.engine, self.reader).get(actor, state["investigation_id"], body.analysis_id)

        def mutate():
            scope = self._scope(session, actor, identifier, lock=True)
            self._authorize_content(session, actor, scope)
            if scope.control_revision != body.expected_revision:
                raise DomainError("revision_conflict", "专题控制状态已改变，请重新读取", 409)
            evaluation = session.get(TopicEvaluationRecord, body.evaluation_id, with_for_update=True)
            receipt = self._adopt_risk_review(session, actor, scope, evaluation, body.analysis_id)
            return 200, {
                "topic": self._view(session, scope).model_dump(mode="json"),
                "assessment": receipt,
            }

        with Session(self.engine.database) as session, session.begin():
            return self.engine._idempotent(
                session,
                actor,
                "topic.risk-review:" + identifier,
                key,
                body.model_dump(mode="json"),
                mutate,
            )

    def reconcile_risk(self, evaluation_id):
        """Read the existing analysis only; never append, start, or resend a model call."""
        from bioradar.research.investigations.analysis_service import AnalysisService

        with Session(self.engine.database) as session:
            evaluation = session.get(TopicEvaluationRecord, evaluation_id)
            scope = session.get(TopicScopeRecord, evaluation.scope_id)
            if not scope.is_current or scope.paused:
                return
            self._authorize_content(session, self.actor, scope)
            state = copy.deepcopy(evaluation.followup)
            if not state.get("analysis_id") or not state.get("topic_risk_scope"):
                return
            topic_id = scope.topic_id
        AnalysisService(self.engine, self.reader).get(
            self.actor, state["investigation_id"], state["analysis_id"]
        )
        with Session(self.engine.database) as session, session.begin():
            scope = self._scope(session, self.actor, topic_id, lock=True)
            if scope.paused:
                return
            self._authorize_content(session, self.actor, scope)
            evaluation = session.get(TopicEvaluationRecord, evaluation_id, with_for_update=True)
            return self._adopt_risk_review(session, self.actor, scope, evaluation, state["analysis_id"])

    def followup(self, evaluation_id, monitor):
        self._active_monitor = monitor
        self._followup_guard(evaluation_id)
        with Session(self.engine.database) as session:
            record = session.get(TopicEvaluationRecord, evaluation_id)
            scope = session.get(TopicScopeRecord, record.scope_id)
            row, scope = self._evaluation_row(session, self.actor, scope.topic_id, evaluation_id)
            evaluation, state = self._evaluation_view(row), copy.deepcopy(row.followup)
            scope_id, demo = scope.id, scope.is_demo
        if state.get("analysis_id"):
            # A model-budget wait is owned by the existing analysis resumer. Never
            # reappend materials or create a second analysis to escape that wait.
            from bioradar.research.investigations.analysis_service import AnalysisService

            saved_analysis = AnalysisService(self.engine, self.reader).get(
                self.actor, state["investigation_id"], state["analysis_id"]
            )
            analysis = saved_analysis.model_dump(mode="json")
            waiting, next_budget_at = analysis_budget_wait(analysis)
            state.update(status="waiting_budget" if waiting else "saved")
            state["next_budget_at"] = next_budget_at
            self._save_followup(evaluation_id, state)
            return
        retry = state.get("next_budget_at")
        if (
            state.get("status") == "waiting_budget"
            and retry
            and datetime.fromisoformat(retry) > datetime.now(UTC)
        ):
            return
        runtime = InvestigationService(self.engine)
        key = "topic-investigation:" + scope_id
        saved = self._saved("investigation.create", key)
        if saved:
            view = runtime.get(self.actor, saved["id"])
            if view.status != "open":
                state.update(
                    status="needs_review", investigation_id=view.id, error_code="investigation_closed"
                )
                self._save_followup(evaluation_id, state)
                return
        else:
            view = None
        lead_key = canonical_hash(["topic", scope_id])
        self._followup_guard(evaluation_id)
        if not monitor.reserve_investigation(lead_key, evaluation_id, "routine", origin="topic"):
            state.update(
                status="waiting_budget",
                next_budget_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                error_code="topic_hourly_budget",
            )
            self._save_followup(evaluation_id, state)
            return
        if view is None:
            self._followup_guard(evaluation_id)
            _, saved = runtime.create(
                self.actor,
                InvestigationCreate(
                    title=evaluation.definition.title,
                    question=evaluation.definition.question,
                    questions=evaluation.next_questions[:32],
                    is_demo=demo,
                ),
                key,
            )
            view = runtime.get(self.actor, saved["id"])
        state.update(investigation_id=view.id, status="deferred")
        self._save_followup(evaluation_id, state)
        view = self._append(
            evaluation, state, view, {"kind": "note", "text": evaluation.draft[:7900]}, "draft"
        )
        # Read fixed immutable originals; never substitute a latest record after an uncertain save.
        evidence = [
            e
            for e in evaluation.evidence
            if any(e.key in s.evidence_keys for s in evaluation.signals if s.key in state["signal_keys"])
        ]
        for item in evidence:
            tag = "evidence:" + item.key
            snapshot_key = "topic-snapshot:" + canonical_hash([evaluation_id, tag])
            adopted = self._saved("snapshot.create", snapshot_key)
            if adopted is None:
                original = self.reader.read_version(
                    {**item.model_dump(mode="json"), "is_demo": demo},
                    mode="current",
                    cutoff=datetime.now(UTC),
                )
                projected = project_record(self.reader, original, as_of=evaluation.as_of)
                snapshot = build_snapshot([projected], is_demo=demo, as_of=evaluation.as_of)
                # The version ref and stable key, not a body copy, are the durable intent.
                self._followup_guard(evaluation_id)
                _, adopted = self.engine.adopt_snapshot(self.actor, snapshot, snapshot_key)
            view = self._append(
                evaluation,
                state,
                view,
                {
                    "kind": "material",
                    "snapshot_id": adopted["id"],
                    "record_ref": item.record_ref.model_dump(mode="json"),
                    "stance": "context",
                    "text": "专题累计评价的固定来源版本，程序整理，未作专业判断。",
                },
                tag,
            )
        from bioradar.research.investigations.analysis_models import AnalysisCreate
        from bioradar.research.investigations.analysis_service import AnalysisService, semantic_enabled

        if semantic_enabled(self.engine.settings):
            intent = state.get("analysis_intent")
            if intent is None:
                intent = {
                    "key": "topic-analysis:" + evaluation.id,
                    "body": AnalysisCreate(expected_revision=view.revision).model_dump(mode="json"),
                }
                state["analysis_intent"] = intent
                if evaluation.definition.domain == "public_health":
                    stamp = risk_scope_stamp(
                        evaluation.topic_id,
                        evaluation.scope_revision,
                        evaluation.observation_revision,
                        evaluation.definition.model_dump(mode="json"),
                        evaluation.tracking,
                    )
                    if stamp:
                        state["topic_risk_scope"] = stamp
                        state["topic_risk_definition"] = evaluation.definition.model_dump(mode="json")
                self._save_followup(evaluation_id, state)
            _, analysis = AnalysisService(self.engine, self.reader).start(
                self.actor,
                view.id,
                AnalysisCreate.model_validate(intent["body"]),
                intent["key"],
                origin={
                    "source": "topic",
                    "topic_id": evaluation.topic_id,
                    "evaluation_id": evaluation_id,
                    **(
                        {
                            "topic_risk_scope": state["topic_risk_scope"],
                            "topic_risk_definition": state["topic_risk_definition"],
                        }
                        if state.get("topic_risk_scope") and state.get("topic_risk_definition")
                        else {}
                    ),
                    "reservation_key": "monitor-dig:" + canonical_hash([lead_key, evaluation_id]),
                    "tracking": {
                        key: (evaluation.tracking or {}).get(key)
                        for key in ("lifecycle", "risk", "assessment_reason")
                    },
                },
            )
            waiting, next_budget_at = analysis_budget_wait(analysis)
            state.update(status="waiting_budget" if waiting else "saved", analysis_id=analysis["id"])
            if waiting:
                state["next_budget_at"] = next_budget_at
            self._save_followup(evaluation_id, state)
            return
        # Scope terms are declared by the researcher; otherwise derive an exact literal from a fixed original.
        terms = evaluation.definition.terms_all + evaluation.definition.terms_any
        plan = state.get("query_plan")
        if plan is None and terms:
            plan = {"term": terms[0], "field": "declared_scope", "reason": "研究人员声明的范围词"}
        if plan is None and evidence:
            item = evidence[0]
            original = self.reader.read_version(
                {**item.model_dump(mode="json"), "is_demo": demo}, mode="current", cutoff=datetime.now(UTC)
            )
            plan = followup_term(original)
            if plan:
                plan["source_ref"] = item.model_dump(mode="json")
        if plan is not None and "query_plan" not in state:
            state["query_plan"] = plan
            self._save_followup(evaluation_id, state)
        for index, term in enumerate([plan["term"]] if plan else []):
            intent = next((q for q in state["query_intents"] if q["index"] == index), None)
            if intent is None:
                self._followup_guard(evaluation_id)
                source = evidence[0].source_id if evidence else evaluation.definition.source_ids[0]
                found = search_records(self.reader, source_id=source, is_demo=demo, term=term)
                intent = {
                    "index": index,
                    "source_id": source,
                    "term": term,
                    "basis": plan,
                    "scope": "fabric_current_adopted_versions_not_the_web",
                    "records": [
                        {
                            k: v[k]
                            for k in ("source_id", "source_record_id", "version_id", "raw_sha256", "is_demo")
                        }
                        for v in found["items"]
                    ],
                    "has_more": found.get("next_cursor") is not None,
                }
                state["query_intents"].append(intent)
                self._save_followup(evaluation_id, state)
            for binding in intent["records"][:3]:
                if any(
                    binding["version_id"] == e.version_id
                    and binding["source_id"] == e.source_id
                    and binding["source_record_id"] == e.source_record_id
                    for e in evidence
                ):
                    continue  # A verification hit on the same immutable original is not new evidence.
                tag = "query:" + canonical_hash(binding)
                snapshot_key = "topic-snapshot:" + canonical_hash([evaluation_id, tag])
                adopted = self._saved("snapshot.create", snapshot_key)
                if adopted is None:
                    original = self.reader.read_version(binding, mode="current", cutoff=datetime.now(UTC))
                    # Fixed source timestamps keep the same idempotent body after an
                    # uncertain commit; the current read/permission cutoff remains now.
                    snapshot_as_of = max(
                        evaluation.as_of,
                        fact_time(original["received_at"]),
                        fact_time(original["first_seen_at"]),
                    )
                    snapshot = build_snapshot(
                        [project_record(self.reader, original, as_of=snapshot_as_of)],
                        is_demo=demo,
                        as_of=snapshot_as_of,
                    )
                    self._followup_guard(evaluation_id)
                    _, adopted = self.engine.adopt_snapshot(self.actor, snapshot, snapshot_key)
                ref = self.reader.ref(original) if adopted is None else None
                if ref is None:
                    saved_snapshot = self.engine.get_snapshot(self.actor, adopted["id"])
                    ref = saved_snapshot.snapshot.records[0].record_ref
                view = self._append(
                    evaluation,
                    state,
                    view,
                    {
                        "kind": "material",
                        "snapshot_id": adopted["id"],
                        "record_ref": ref.model_dump(mode="json"),
                        "stance": "context",
                        "text": "按已采用资料中的明确词项追加查找；命中不等于支持，未命中不等于不存在。",
                    },
                    tag,
                )
            duplicate_count = sum(
                any(
                    binding["version_id"] == e.version_id
                    and binding["source_id"] == e.source_id
                    and binding["source_record_id"] == e.source_record_id
                    for e in evidence
                )
                for binding in intent["records"]
            )
            view = self._append(
                evaluation,
                state,
                view,
                {
                    "kind": "note",
                    "text": f"追加查找：{intent['term']}；命中{len(intent['records'])}条，"
                    f"其中{duplicate_count}条为原已采用版本，"
                    f"新增{len(intent['records']) - duplicate_count}个版本；"
                    + ("还有更多，受本轮预算限制。" if intent["has_more"] else "已读尽本次有限查询返回。"),
                },
                "query-receipt:" + str(index),
            )
        state["status"] = "saved"
        self._save_followup(evaluation_id, state)

    def _fetch_source(self, scope_id, source, *, now):
        """Use the host fetch receipt once per due topic cycle, outside model execution."""
        if self.fetch_hook is None:
            return
        with Session(self.engine.database) as session:
            row = session.get(TopicScopeRecord, scope_id)
            saved = copy.deepcopy(row.progress.get("_fetch", {}).get(source))
            due = row.next_evaluation_at is None or row.next_evaluation_at <= now
            pending = bool(
                saved
                and (
                    saved.get("receipt", {}).get("status") in {"sent", "unknown"}
                    or (saved.get("receipt", {}).get("result") or {}).get("status") == "pending"
                )
            )
            if not due and not pending:
                return
            slot = (row.next_evaluation_at or row.created_at).isoformat()
            key = "topic-fetch:" + canonical_hash([scope_id, source, slot])
            if pending:
                key = saved["call_key"]
            prior = saved.get("receipt") if saved and saved["call_key"] == key else None
            definition = copy.deepcopy(row.definition)
            context = {
                "topic_id": row.topic_id,
                "scope_revision": row.scope_revision,
                "source_id": source,
                "is_demo": row.is_demo,
                "terms_all": definition.get("terms_all", []),
                "terms_any": definition.get("terms_any", []),
                "since": (row.last_checked_at or row.created_at).isoformat(),
                "until": now.isoformat(),
                "may_send": not row.is_demo and due,
            }
            if saved and saved["call_key"] == key:
                context = saved["context"]

        def checkpoint(receipt):
            with Session(self.engine.database) as session, session.begin():
                row = session.get(TopicScopeRecord, scope_id, with_for_update=True)
                self._authorize(self.actor, row.definition, row.is_demo, online=True)
                if not row.is_current or row.paused:
                    raise DomainError("topic_followup_paused", "专题已暂停或范围更新", 409)
                if getattr(self, "_active_monitor", None):
                    self._active_monitor._guard(self._active_monitor._state(session))
                progress = copy.deepcopy(row.progress)
                progress.setdefault("_fetch", {})[source] = {
                    "call_key": key,
                    "context": context,
                    "receipt": copy.deepcopy(receipt),
                }
                row.progress = progress

        receipt = self.fetch_hook(context, call_key=key, prior_receipt=prior, checkpoint=checkpoint)
        checkpoint(receipt)

    def tick(self, *, now=None, only_demo=False):
        """One topic/source page per existing monitor loop; a failed topic does not starve others."""
        from bioradar.research.monitor.service import MonitorService

        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise DomainError("simulation_clock_invalid", "专题时钟必须含时区", 422)
        if only_demo and self.engine.settings.environment != "test":
            raise DomainError("simulation_not_allowed", "加速专题检查仅限test教学范围", 403)
        monitor = MonitorService(self.engine, self.reader)
        if not self.engine.settings.monitor_enabled or not DevelopmentAccess(self.engine.settings).enabled:
            return {"state": "disabled"}
        if not monitor.claim():
            return {"state": "paused_or_busy"}
        scope_id, source, source_offset = None, None, 0
        self._active_monitor = monitor
        try:
            with Session(self.engine.database) as session:
                row = session.scalar(
                    select(TopicScopeRecord)
                    .where(
                        TopicScopeRecord.workspace_id == self.actor.workspace_id,
                        TopicScopeRecord.is_current.is_(True),
                        TopicScopeRecord.paused.is_(False),
                        *([TopicScopeRecord.is_demo.is_(True)] if only_demo else []),
                    )
                    .order_by(TopicScopeRecord.last_checked_at.asc().nullsfirst(), TopicScopeRecord.topic_id)
                    .limit(1)
                )
                if row is None:
                    return {"state": "no_topics"}
                scope_id, definition, demo = row.id, copy.deepcopy(row.definition), row.is_demo
                source_offset = row.next_source % len(definition["source_ids"])
                source = definition["source_ids"][source_offset]
                expected = copy.deepcopy(row.progress.get(source, {}))
            self._authorize(self.actor, definition, demo)
            self.engine.authorize_history_source(source)
            self._fetch_source(scope_id, source, now=now)
            page = change_page(
                self.reader,
                source_id=source,
                is_demo=demo,
                after=expected.get("after", 0),
                cursor=expected.get("cursor"),
                minimum_after=expected.get("last_page_after", 0),
                limit=min(50, self.engine.settings.monitor_batch_records),
            )
            try:
                health = self.reader._get(
                    "/api/history/source-health", {"source_id": source}, request_seconds=3
                )
                if (
                    not isinstance(health, dict)
                    or health.get("schema_version") != "datafabric.source.health.v1"
                ):
                    health = {"state": "unknown", "reason": "source_health_contract_unavailable"}
            except DomainError:
                health = {"state": "unknown", "reason": "source_health_unavailable"}
            page["_upstream_health"] = health
            self.apply_page(scope_id, source, expected, page, now=now)
            self.evaluate(scope_id, now=now)
            with Session(self.engine.database) as session:
                risk_pending = session.scalar(
                    select(TopicEvaluationRecord.id)
                    .where(
                        TopicEvaluationRecord.scope_id == scope_id,
                        TopicEvaluationRecord.followup["analysis_id"].astext.is_not(None),
                        TopicEvaluationRecord.followup["topic_risk_scope"].is_not(None),
                        or_(
                            TopicEvaluationRecord.followup["risk_review"]["status"].astext.is_(None),
                            TopicEvaluationRecord.followup["risk_review"]["status"].astext == "pending",
                        ),
                    )
                    .order_by(
                        TopicEvaluationRecord.followup["risk_review"]["last_checked_at"]
                        .astext.asc()
                        .nullsfirst(),
                        TopicEvaluationRecord.created_at,
                    )
                    .limit(1)
                )
            if risk_pending:
                self.reconcile_risk(risk_pending)
            with Session(self.engine.database) as session:
                pending = session.scalar(
                    select(TopicEvaluationRecord.id)
                    .where(
                        TopicEvaluationRecord.scope_id == scope_id,
                        TopicEvaluationRecord.followup["status"].astext.in_(("deferred", "waiting_budget")),
                    )
                    .order_by(TopicEvaluationRecord.created_at)
                    .limit(1)
                )
            if pending:
                try:
                    self.followup(pending, monitor)
                except DomainError as exc:
                    if exc.status_code == 422 or exc.code in {
                        "revision_conflict",
                        "investigation_closed",
                        "topic_followup_paused",
                        "monitor_lease_lost",
                    }:
                        with Session(self.engine.database) as session:
                            state = copy.deepcopy(session.get(TopicEvaluationRecord, pending).followup)
                        state.update(
                            status="deferred"
                            if exc.code in {"topic_followup_paused", "monitor_lease_lost"}
                            else "blocked",
                            error_code=exc.code,
                        )
                        self._save_followup(pending, state)
                    else:
                        raise
            return {"state": "checked", "topic_id": row.topic_id}
        except DomainError as exc:
            if scope_id is not None:
                with Session(self.engine.database) as session, session.begin():
                    row = session.get(TopicScopeRecord, scope_id, with_for_update=True)
                    if row.is_current:
                        progress = copy.deepcopy(row.progress)
                        progress[source] = {**progress.get(source, {}), "error_code": exc.code}
                        row.progress, row.last_checked_at = progress, now
                        row.next_source = (source_offset + 1) % len(row.definition["source_ids"])
            return {
                "state": "source_unavailable" if exc.status_code in {403, 502, 503} else "blocked",
                "error_code": exc.code,
            }
        finally:
            monitor.release()
