"""Local history research API; saved studies are ordinary records, not analysis runs."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess, EngineActor
from bioradar.research.engine.routes import EngineRoute, IdempotencyKey
from bioradar.research.engine.service import EngineService
from bioradar.research.history.models import (
    HistoryEvaluationCreate,
    HistoryStudyCreate,
    HistoryStudyView,
)
from bioradar.research.history.service import HistoryService, history_reader


def create_history_router(settings: Settings):
    router = APIRouter(prefix="/api/engine/history", tags=["Historical research"], route_class=EngineRoute)
    access = DevelopmentAccess(settings)

    def service(request):
        if request.app.state.database is None:
            raise DomainError("engine_database_unavailable", "研究数据库尚未配置", 503)
        return HistoryService(EngineService(request.app.state.database, settings))

    @router.get("/sources")
    def sources(actor: EngineActor = Depends(access.require_actor)):
        with history_reader(settings) as reader:
            values = reader.sources()
        allowed = set().union(*settings.engine_source_id_allowlist.values())
        return {
            "items": [v for v in values if v.get("source_id") in allowed],
            "limitation": "来源目录不是已取得历史覆盖或可预测能力声明。",
        }

    @router.post("/studies", response_model=HistoryStudyView, status_code=201)
    def create(
        body: HistoryStudyCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        runtime = service(request)
        runtime._authorize(actor, body.source_id, body.is_demo)
        with history_reader(settings, body) as reader:
            runtime.reader = reader
            return runtime.create(actor, body, idempotency_key)

    @router.get("/studies")
    def listing(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        actor: EngineActor = Depends(access.require_actor),
    ):
        return service(request).list(actor, limit)

    @router.get("/studies/{identifier}", response_model=HistoryStudyView)
    def detail(identifier: str, request: Request, actor: EngineActor = Depends(access.require_actor)):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            return runtime.get(actor, identifier)

    @router.post("/studies/{identifier}/evaluations", status_code=201)
    def evaluate(
        identifier: str,
        body: HistoryEvaluationCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor: EngineActor = Depends(access.require_actor),
    ):
        runtime = service(request)
        with history_reader(settings) as permission_reader:
            runtime.reader = permission_reader
            saved = runtime.get(actor, identifier)
        with history_reader(settings, saved.request) as reader:
            runtime.reader = reader
            return runtime.evaluate(actor, identifier, body, idempotency_key)

    @router.get("/studies/{identifier}/evaluations/{evaluation_id}")
    def evaluation(
        identifier: str,
        evaluation_id: str,
        request: Request,
        actor: EngineActor = Depends(access.require_actor),
    ):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            return runtime.evaluation(actor, identifier, evaluation_id)

    @router.get("/studies/{identifier}/evidence/{version_id}")
    def evidence(
        identifier: str,
        version_id: str,
        request: Request,
        evaluation_id: Annotated[str | None, Query(max_length=36)] = None,
        actor: EngineActor = Depends(access.require_actor),
    ):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            return runtime.evidence(actor, identifier, version_id, evaluation_id)

    @router.get("/studies/{identifier}/export", response_class=PlainTextResponse)
    def export(identifier: str, request: Request, actor: EngineActor = Depends(access.require_actor)):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            content = runtime.export(actor, identifier)
        return PlainTextResponse(
            content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="historical-research.md"'},
        )

    return router
