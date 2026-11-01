"""Persistent topic observations. Reads do not start computation."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.routes import EngineRoute, IdempotencyKey
from bioradar.research.engine.schemas import RuntimeModel
from bioradar.research.engine.service import EngineService
from bioradar.research.history.service import history_reader
from bioradar.topics.models import (
    TopicControl,
    TopicCreate,
    TopicEvaluation,
    TopicEvaluationList,
    TopicList,
    TopicRiskReviewView,
    TopicTemplates,
    TopicUpdate,
    TopicView,
)
from bioradar.topics.risk import TopicRiskReviewRequest
from bioradar.topics.service import TopicService


class TopicSimulationTick(RuntimeModel):
    as_of: AwareDatetime


def create_topic_router(settings):
    router = APIRouter(prefix="/api/engine/topics", tags=["Long-term topics"], route_class=EngineRoute)
    access = DevelopmentAccess(settings)

    def service(request):
        if request.app.state.database is None:
            raise DomainError("engine_database_unavailable", "研究数据库尚未配置", 503)
        return TopicService(EngineService(request.app.state.database, settings))

    @router.post("/simulation/tick")
    def simulation_tick(
        body: TopicSimulationTick,
        request: Request,
        actor=Depends(access.require_actor),
    ):
        if settings.environment != "test":
            raise DomainError("simulation_tick_unavailable", "模拟时钟仅在隔离测试环境使用", 404)
        runtime = service(request)
        with history_reader(settings) as reader:
            result = TopicService(runtime.engine, reader).tick(now=body.as_of, only_demo=True)
        return {
            "clock_mode": "accelerated_demo_clock",
            "evaluation_as_of": body.as_of.isoformat(),
            "executed_at": datetime.now(UTC).isoformat(),
            "qualification": "仅推进模拟专题的同一调度规则；原文和实际采集、模型调用时间不改写。",
            "result": result,
        }

    @router.get("/templates", response_model=TopicTemplates)
    def templates(request: Request, actor=Depends(access.require_actor)):
        return service(request).templates(actor)

    @router.get("", response_model=TopicList)
    def listing(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        cursor: Annotated[str | None, Query(max_length=36)] = None,
        actor=Depends(access.require_actor),
    ):
        return service(request).list(actor, limit, cursor)

    @router.post("", response_model=TopicView, status_code=201)
    def create(
        body: TopicCreate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor=Depends(access.require_actor),
    ):
        code, result = service(request).create(actor, body, idempotency_key)
        return JSONResponse(status_code=code, content=result)

    @router.get("/{identifier}", response_model=TopicView)
    def detail(identifier: str, request: Request, actor=Depends(access.require_actor)):
        return service(request).get(actor, identifier)

    @router.put("/{identifier}", response_model=TopicView)
    def update(
        identifier: str,
        body: TopicUpdate,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor=Depends(access.require_actor),
    ):
        code, result = service(request).update(actor, identifier, body, idempotency_key)
        return JSONResponse(status_code=code, content=result)

    @router.post("/{identifier}/control", response_model=TopicView)
    def control(
        identifier: str,
        body: TopicControl,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor=Depends(access.require_actor),
    ):
        code, result = service(request).control(actor, identifier, body, idempotency_key)
        return JSONResponse(status_code=code, content=result)

    @router.post("/{identifier}/risk-reassessments", response_model=TopicRiskReviewView)
    def risk_reassessment(
        identifier: str,
        body: TopicRiskReviewRequest,
        request: Request,
        idempotency_key: IdempotencyKey,
        actor=Depends(access.require_actor),
    ):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            runtime.engine.history_source_reader = reader.sources
            code, result = runtime.review_risk(actor, identifier, body, idempotency_key)
        return JSONResponse(status_code=code, content=result)

    @router.get("/{identifier}/evaluations", response_model=TopicEvaluationList)
    def evaluations(
        identifier: str,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=30)] = 20,
        cursor: Annotated[str | None, Query(max_length=36)] = None,
        actor=Depends(access.require_actor),
    ):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            runtime.engine.history_source_reader = reader.sources
            return runtime.evaluations(actor, identifier, limit, cursor)

    @router.get("/{identifier}/evaluations/{evaluation_id}", response_model=TopicEvaluation)
    def evaluation(
        identifier: str, evaluation_id: str, request: Request, actor=Depends(access.require_actor)
    ):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            runtime.engine.history_source_reader = reader.sources
            return runtime.evaluation(actor, identifier, evaluation_id)

    @router.get("/{identifier}/evaluations/{evaluation_id}/evidence/{evidence_key}")
    def evidence(
        identifier: str,
        evaluation_id: str,
        evidence_key: str,
        request: Request,
        actor=Depends(access.require_actor),
    ):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            runtime.engine.history_source_reader = reader.sources
            return runtime.evidence(actor, identifier, evaluation_id, evidence_key)

    @router.get("/{identifier}/evaluations/{evaluation_id}/export", response_model=TopicEvaluation)
    def export(identifier: str, evaluation_id: str, request: Request, actor=Depends(access.require_actor)):
        runtime = service(request)
        with history_reader(settings) as reader:
            runtime.reader = reader
            runtime.engine.history_source_reader = reader.sources
            result = runtime.evaluation(actor, identifier, evaluation_id)
        return JSONResponse(
            content=result.model_dump(mode="json"),
            headers={
                "Content-Disposition": f'attachment; filename="topic-{identifier}-{evaluation_id}.json"'
            },
        )

    return router
