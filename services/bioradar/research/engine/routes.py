"""Actual development engine endpoints; the frozen /api/v1 remains separate."""

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.comparison import SnapshotComparison, compare_snapshots
from bioradar.research.engine.auth import COOKIE_NAME, DevelopmentAccess, EngineActor
from bioradar.research.engine.schemas import (
    DecisionRunCreate,
    DecisionRunList,
    DecisionRunView,
    FeedbackCreate,
    FeedbackView,
    MethodInfo,
    RunAction,
    RunCreate,
    RunList,
    RunView,
    SessionLogin,
    SessionView,
    SnapshotDetail,
    SnapshotList,
    SnapshotSummary,
)
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.graph import (
    GraphInvestigationCreate,
    GraphInvestigationResult,
    GraphPackageList,
)
from bioradar.research.investigations.models import (
    InvestigationCreate,
    InvestigationEntryCreate,
    InvestigationList,
    InvestigationView,
)
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.models import NormalizedSnapshot, canonical_hash
from bioradar.research.mining.registry import methods_metadata
from bioradar.research.monitor.models import MonitorBatchList, MonitorControl, MonitorLeadList, MonitorStatus
from bioradar.research.monitor.service import MonitorService

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=160)]


class EngineRoute(APIRoute):
    decision_max_bytes = 2_000_000

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            over_budget = False
            max_bytes = None
            if request.method in {"POST", "PUT"}:
                if self.path.startswith("/api/engine/decision-runs"):
                    max_bytes = self.decision_max_bytes
                elif self.path.startswith(
                    ("/api/engine/investigations", "/api/engine/history", "/api/engine/topics")
                ):
                    max_bytes = 64_000
            if max_bytes is not None:
                try:
                    declared_size = int(request.headers.get("content-length", "0"))
                except ValueError:
                    declared_size = 0
                if declared_size > max_bytes:
                    raise DomainError("engine_budget_exceeded", "研究输入超过服务器保存预算", 422)
                original_receive = request.receive
                received = 0

                async def bounded_receive():
                    nonlocal received, over_budget
                    message = await original_receive()
                    if message["type"] == "http.request":
                        received += len(message.get("body", b""))
                        if received > max_bytes:
                            over_budget = True
                            raise DomainError("engine_budget_exceeded", "研究输入超过服务器保存预算", 422)
                    return message

                request = Request(request.scope, receive=bounded_receive)
            try:
                return await handler(request)
            except (RequestValidationError, HTTPException) as exc:
                if over_budget:
                    # FastAPI wraps receive errors during JSON parsing; retain
                    # our static diagnosis and never include the raw payload.
                    raise DomainError("engine_budget_exceeded", "研究输入超过服务器保存预算", 422) from None
                if isinstance(exc, HTTPException):
                    raise
                # Pydantic validation errors may interpolate raw credentials/text.
                raise DomainError("engine_input_invalid", "参数不符合研究引擎契约", 422) from None
            except SQLAlchemyError:
                raise DomainError(
                    "engine_database_unavailable", "研究状态暂时无法持久读取或受理", 503, True
                ) from None

        return safe_handler


def create_engine_router(settings: Settings) -> APIRouter:
    class ConfiguredEngineRoute(EngineRoute):
        decision_max_bytes = settings.engine_max_decision_bytes

    router = APIRouter(
        prefix="/api/engine", tags=["Development research engine"], route_class=ConfiguredEngineRoute
    )
    access = DevelopmentAccess(settings)

    def service(request: Request) -> EngineService:
        database = request.app.state.database
        if database is None:
            raise DomainError("engine_database_unavailable", "研究数据库尚未配置", 503, True)
        return EngineService(database, settings)

    @router.get("/session", response_model=SessionView)
    def current_session(request: Request):
        actor = access.optional_actor(request)
        return SessionView(
            enabled=access.enabled,
            authenticated=actor is not None,
            actor_id=actor.actor_id if actor else None,
            workspace_id=actor.workspace_id if actor else None,
        )

    @router.post("/session", response_model=SessionView)
    def login(body: SessionLogin, request: Request, response: Response):
        access.check_local(request)
        access.check_origin(request)
        access.check_credential(body.credential.get_secret_value())
        response.set_cookie(
            COOKIE_NAME,
            access.create_session(),
            max_age=settings.engine_session_seconds,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/api/engine",
        )
        actor = access.actor()
        return SessionView(
            enabled=True, authenticated=True, actor_id=actor.actor_id, workspace_id=actor.workspace_id
        )

    @router.delete("/session", response_model=SessionView)
    def logout(request: Request, response: Response):
        access.check_local(request)
        if request.headers.get("authorization") is None:
            access.check_origin(request)
        else:
            access.require_actor(request)
        response.delete_cookie(COOKIE_NAME, path="/api/engine", httponly=True, samesite="strict")
        return SessionView(enabled=access.enabled, authenticated=False)

    @router.get("/methods", response_model=list[MethodInfo])
    def methods(actor: EngineActor = Depends(access.require_actor)):
        return [MethodInfo.model_validate(item) for item in methods_metadata()]

    @router.post("/snapshots", response_model=SnapshotSummary, status_code=201)
    def adopt_snapshot(
        body: NormalizedSnapshot,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        code, payload = service(request).adopt_snapshot(actor, body, idempotency_key)
        return JSONResponse(status_code=code, content=payload)

    @router.get("/snapshots", response_model=SnapshotList)
    def snapshots(
        request: Request,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        actor: EngineActor = Depends(access.require_actor),
    ):
        return service(request).list_snapshots(actor, cursor, limit)

    @router.get("/snapshots/{snapshot_id}", response_model=SnapshotDetail)
    def snapshot(snapshot_id: str, request: Request, actor: EngineActor = Depends(access.require_actor)):
        return service(request).get_snapshot(actor, snapshot_id)

    @router.get("/snapshot-comparisons", response_model=SnapshotComparison)
    def comparison(
        request: Request,
        before_id: Annotated[str, Query(min_length=1, max_length=36)],
        after_id: Annotated[str, Query(min_length=1, max_length=36)],
        actor: EngineActor = Depends(access.require_actor),
    ):
        engine = service(request)
        before = engine.get_snapshot(actor, before_id)
        after = engine.get_snapshot(actor, after_id)
        try:
            return compare_snapshots(before.snapshot, after.snapshot)
        except ValueError:
            raise DomainError(
                "snapshot_comparison_invalid", "采用包的来源、版本或比较范围不兼容", 422
            ) from None

    @router.post("/investigations", response_model=InvestigationView, status_code=201)
    def create_investigation(
        body: InvestigationCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        code, payload = InvestigationService(service(request)).create(actor, body, idempotency_key)
        return JSONResponse(status_code=code, content=payload)

    @router.get("/graph/packages", response_model=GraphPackageList)
    def graph_packages(
        request: Request,
        source_id: Annotated[Literal["reviewed_graph", "primekg_graph"], Query()],
        is_demo: bool = False,
        cursor: Annotated[str | None, Query(max_length=6000)] = None,
        cutoff: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
        actor: EngineActor = Depends(access.require_actor),
    ):
        from bioradar.adapters.fabric_graph import graph_reader, list_graph_packages

        if cutoff is None:
            if cursor is not None:
                raise DomainError("graph_catalog_cutoff_required", "继续读取须保留原列表截止时间", 422)
            cutoff = datetime.now(UTC)
        if cutoff.tzinfo is None or cutoff.utcoffset() is None or cutoff > datetime.now(UTC):
            raise DomainError("graph_cutoff_invalid", "列表截止时间须为已有的含时区时间", 422)
        instance = "fixture" if is_demo else "datafabric-test"
        if instance not in actor.source_allowlist or source_id not in settings.engine_source_id_allowlist.get(
            instance, []
        ):
            raise DomainError("source_not_authorized", "图来源不在当前本地许可范围", 403)
        engine = service(request)
        engine.authorize_history_source(source_id)
        with graph_reader(settings) as reader:
            return list_graph_packages(
                reader, source_id=source_id, is_demo=is_demo, cutoff=cutoff, limit=limit, cursor=cursor
            )

    @router.post("/investigations/graph", response_model=GraphInvestigationResult, status_code=201)
    def graph_investigation(
        body: GraphInvestigationCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        from bioradar.adapters.fabric_graph import graph_clinical_reader, graph_reader
        from bioradar.research.investigations.graph import run_graph_investigation

        engine = service(request)
        with graph_reader(settings) as reader, graph_clinical_reader(settings, actor) as clinical:
            return run_graph_investigation(
                engine,
                actor,
                reader,
                body,
                execution_id=idempotency_key,
                clinical_reader=clinical,
            )

    @router.get("/investigations", response_model=InvestigationList)
    def investigations(
        request: Request,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        actor: EngineActor = Depends(access.require_actor),
    ):
        return InvestigationService(service(request)).list(actor, cursor, limit)

    @router.get("/investigations/research-options")
    def research_options(
        request: Request,
        is_demo: bool = False,
        actor: EngineActor = Depends(access.require_actor),
    ):
        with analysis_runtime(request) as analyses:
            return analyses.options(actor, is_demo=is_demo)

    @router.get("/investigations/{investigation_id}", response_model=InvestigationView)
    def investigation(
        investigation_id: str,
        request: Request,
        actor: EngineActor = Depends(access.require_actor),
    ):
        return InvestigationService(service(request)).get(actor, investigation_id)

    @router.post(
        "/investigations/{investigation_id}/entries", response_model=InvestigationView, status_code=201
    )
    def append_investigation(
        investigation_id: str,
        body: InvestigationEntryCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        code, payload = InvestigationService(service(request)).append(
            actor, investigation_id, body, idempotency_key
        )
        return JSONResponse(status_code=code, content=payload)

    # Ordinary semantic investigations use the same local access boundary and RQ queue.
    from bioradar.research.history.service import history_reader
    from bioradar.research.investigations.analysis_models import (
        AnalysisCreate,
        AnalysisEvidenceRead,
        AnalysisList,
        AnalysisView,
        QuestionAsk,
    )
    from bioradar.research.investigations.analysis_service import AnalysisService

    @contextmanager
    def analysis_runtime(request):
        engine = service(request)
        if settings.data_fabric_url and settings.data_fabric_history_read_token:
            with history_reader(settings) as reader:
                yield AnalysisService(engine, reader)
        else:
            yield AnalysisService(engine)

    @router.post("/investigations/ask", status_code=202)
    def research_ask(
        body: QuestionAsk,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        identifier = None
        try:
            with analysis_runtime(request) as analyses:
                _, investigation = InvestigationService(analyses.engine).create(
                    actor,
                    InvestigationCreate(
                        title=body.title or body.question[:200], question=body.question, is_demo=body.is_demo
                    ),
                    "ask-investigation:" + canonical_hash(idempotency_key),
                )
                identifier = investigation["id"]
                code, analysis = analyses.start(
                    actor,
                    identifier,
                    AnalysisCreate(
                        expected_revision=investigation["revision"],
                        question=body.question,
                        auto_retrieve=True,
                        source_ids=body.source_ids,
                        initial_queries=body.initial_queries,
                        budget=body.budget,
                        query_policy=body.query_policy,
                    ),
                    "ask-analysis:" + canonical_hash(idempotency_key),
                )
                current = analyses.get(actor, identifier, analysis["id"])
        except DomainError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": exc.retryable,
                        "details": {"investigation_id": identifier, "analysis_id": None},
                    },
                    "request_id": request.state.request_id,
                },
            )
        return JSONResponse(
            status_code=code,
            content={
                "investigation_id": identifier,
                "analysis_id": current.id,
                "analysis": current.model_dump(mode="json"),
            },
        )

    @router.get("/investigations/{investigation_id}/analyses", response_model=AnalysisList)
    def analysis_list(
        investigation_id: str,
        request: Request,
        cursor: Annotated[str | None, Query(max_length=36)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        actor: EngineActor = Depends(access.require_actor),
    ):
        with analysis_runtime(request) as analyses:
            return analyses.list(actor, investigation_id, cursor=cursor, limit=limit)

    @router.post("/investigations/{investigation_id}/analyses", response_model=AnalysisView, status_code=202)
    def analysis_create(
        investigation_id: str,
        body: AnalysisCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        with analysis_runtime(request) as analyses:
            code, payload = analyses.start(actor, investigation_id, body, idempotency_key)
            return JSONResponse(status_code=code, content=payload)

    @router.get("/investigations/{investigation_id}/analyses/{analysis_id}", response_model=AnalysisView)
    def analysis_get(
        investigation_id: str,
        analysis_id: str,
        request: Request,
        actor: EngineActor = Depends(access.require_actor),
    ):
        with analysis_runtime(request) as analyses:
            return analyses.get(actor, investigation_id, analysis_id)

    @router.get(
        "/investigations/{investigation_id}/analyses/{analysis_id}/evidence/{evidence_key}",
        response_model=AnalysisEvidenceRead,
    )
    def analysis_evidence(
        investigation_id: str,
        analysis_id: str,
        evidence_key: str,
        request: Request,
        actor: EngineActor = Depends(access.require_actor),
    ):
        with analysis_runtime(request) as analyses:
            return analyses.evidence(actor, investigation_id, analysis_id, evidence_key)

    @router.get("/investigations/{investigation_id}/analyses/{analysis_id}/export")
    def analysis_export(
        investigation_id: str,
        analysis_id: str,
        request: Request,
        format: Literal["json", "markdown"] = "json",
        actor: EngineActor = Depends(access.require_actor),
    ):
        with analysis_runtime(request) as analyses:
            analysis = analyses.get(actor, investigation_id, analysis_id)
            if format == "markdown":
                if analysis.result is None:
                    raise DomainError("analysis_report_unavailable", "本轮尚无可导出的分析稿", 409)
                from bioradar.research.investigations.analysis import render_report

                content = (analysis.result.report or {}).get("final_text") or render_report(
                    analysis.result.model_dump(mode="json"), analysis.question
                )
                return Response(
                    content=content,
                    media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="analysis-{analysis_id}.md"'},
                )
            return {
                "analysis": analysis.model_dump(mode="json"),
                "warning": "有出处的机器分析，专业方法评价或模型核验不等于人工批准。",
            }

    @router.post("/runs", response_model=RunView, status_code=202)
    def accept_run(
        body: RunCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        engine = service(request)
        code, payload = engine.create_run(actor, body, idempotency_key)
        engine.dispatch(payload["id"])
        return JSONResponse(status_code=code, content=payload)

    @router.get("/runs", response_model=RunList)
    def runs(
        request: Request,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        actor: EngineActor = Depends(access.require_actor),
    ):
        return service(request).list_runs(actor, cursor, limit)

    @router.get("/runs/{run_id}", response_model=RunView)
    def run(run_id: str, request: Request, actor: EngineActor = Depends(access.require_actor)):
        return service(request).get_run(actor, run_id)

    @router.post("/runs/{run_id}/actions", response_model=RunView, status_code=202)
    def action(
        run_id: str,
        body: RunAction,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        engine = service(request)
        code, payload = engine.retry_run(actor, run_id, body, idempotency_key)
        engine.dispatch(payload["id"])
        return JSONResponse(status_code=code, content=payload)

    @router.post("/runs/{run_id}/feedback", response_model=FeedbackView, status_code=201)
    def feedback(
        run_id: str,
        body: FeedbackCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        code, payload = service(request).add_feedback(actor, run_id, body, idempotency_key)
        return JSONResponse(status_code=code, content=payload)

    @router.post("/decision-runs", response_model=DecisionRunView, status_code=202)
    def accept_decision(
        body: DecisionRunCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        engine = service(request)
        code, payload = engine.decisions().create(actor, body, idempotency_key)
        engine.dispatch(payload["id"])
        return JSONResponse(status_code=code, content=payload)

    @router.get("/decision-runs", response_model=DecisionRunList)
    def decisions(
        request: Request,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        actor: EngineActor = Depends(access.require_actor),
    ):
        return service(request).decisions().list(actor, cursor, limit)

    @router.get("/decision-runs/{run_id}", response_model=DecisionRunView)
    def decision(run_id: str, request: Request, actor: EngineActor = Depends(access.require_actor)):
        return service(request).decisions().get(actor, run_id)

    @router.post("/decision-runs/{run_id}/actions", response_model=DecisionRunView, status_code=202)
    def decision_action(
        run_id: str,
        body: RunAction,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        engine = service(request)
        code, payload = engine.decisions().retry(actor, run_id, body, idempotency_key)
        engine.dispatch(payload["id"])
        return JSONResponse(status_code=code, content=payload)

    @contextmanager
    def monitor_runtime(request):
        engine = service(request)
        if settings.data_fabric_url and settings.data_fabric_history_read_token:
            from bioradar.research.history.service import history_reader

            with history_reader(settings) as reader:
                yield MonitorService(engine, reader)
        else:
            yield MonitorService(engine)

    @router.get("/monitor/status", response_model=MonitorStatus)
    def monitor_status(request: Request, actor: EngineActor = Depends(access.require_actor)):
        with monitor_runtime(request) as runtime:
            return runtime.status(actor)

    @router.get("/monitor/batches", response_model=MonitorBatchList)
    def monitor_batches(
        request: Request,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        actor: EngineActor = Depends(access.require_actor),
    ):
        with monitor_runtime(request) as runtime:
            return runtime.batches(actor, cursor, limit)

    @router.get("/monitor/leads", response_model=MonitorLeadList)
    def monitor_leads(
        request: Request,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        actor: EngineActor = Depends(access.require_actor),
    ):
        with monitor_runtime(request) as runtime:
            return runtime.leads(actor, cursor, limit)

    @router.post("/monitor/control")
    def monitor_control(
        body: MonitorControl,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        code, payload = MonitorService(service(request)).control(actor, body, idempotency_key)
        return JSONResponse(status_code=code, content=payload)

    return router
