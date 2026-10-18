from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bioradar import __version__
from bioradar.adapters.registry import PROVIDERS
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.api.health import dependencies
from bioradar.data.database import create_database
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.routes import create_engine_router
from bioradar.research.history.routes import create_history_router
from bioradar.research.pipeline import STAGES
from bioradar.topics.routes import create_topic_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database = create_database(settings)
        yield
        if app.state.database is not None:
            app.state.database.dispose()

    app = FastAPI(title="BioRadar research engine", version=__version__, lifespan=lifespan)
    app.include_router(create_engine_router(settings))
    app.include_router(create_history_router(settings))
    app.include_router(create_topic_router(settings))

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
                "request_id": request.state.request_id,
            },
        )

    @app.get("/health/live")
    def live():
        return {"status": "alive", "version": __version__, "stage": "research_engine"}

    @app.get("/health/ready")
    async def ready(request: Request):
        checks = await dependencies(settings, request.app.state.database)
        is_ready = all(value == "ready" for value in checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "degraded",
                "scope": "infrastructure_only",
                "dependencies": checks,
                "intelligence_ready": False,
            },
        )

    @app.get("/api/system/status")
    async def system_status(request: Request):
        configured = bool(
            settings.monitor_enabled
            and DevelopmentAccess(settings).enabled
            and settings.data_fabric_url
            and settings.data_fabric_history_read_token
        )
        return {
            "version": __version__,
            "stage": "research_engine",
            "environment": settings.environment,
            "intelligence_ready": False,
            "discovery_mode": "bounded_incremental_monitor" if configured else "frozen_snapshot",
            "autonomous_investigation": configured,
            "monitor_configuration": "configured_not_observed" if configured else "not_configured",
            "monitor_status_url": "/api/engine/monitor/status",
            "bounded_investigation": "data_fabric_cli",
            "dependencies": await dependencies(settings, request.app.state.database),
            "providers": PROVIDERS,
            "research_stages": [{"key": s.key, "label": s.label} for s in STAGES],
        }

    # The local research preview above has its own explicit access and contract.
    # The complete V1 investigation/delivery application is not implemented here.
    @app.api_route(
        "/api/v1/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=False
    )
    def pending_business_api(path: str):
        raise DomainError("capability_not_implemented", "业务接口尚未实现，请按对应任务卡接续开发", 503)

    return app


app = create_app()
