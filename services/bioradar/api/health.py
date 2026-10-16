import asyncio

from alembic.config import Config
from alembic.script import ScriptDirectory
from redis import Redis
from rq import Worker
from sqlalchemy import text
from sqlalchemy.engine import Engine

from bioradar.api.config import Settings


def database_status(engine: Engine | None) -> str:
    if engine is None:
        return "not_configured"
    try:
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        expected = ScriptDirectory.from_config(Config("db/alembic.ini")).get_current_head()
        return "ready" if revision == expected else "migration_required"
    except Exception:
        # Connection strings and database exceptions never reach the HTTP response.
        return "unavailable"


def queue_status(settings: Settings) -> dict[str, str]:
    if settings.redis_url is None:
        return {"redis": "not_configured", "worker": "not_configured"}
    connection = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
    )
    try:
        connection.ping()
        workers = Worker.all(connection=connection)
        expected = f"{settings.queue_prefix}:routine"
        active = any(expected in worker.queue_names() for worker in workers)
        return {"redis": "ready", "worker": "ready" if active else "unavailable"}
    except Exception:
        return {"redis": "unavailable", "worker": "unavailable"}
    finally:
        connection.close()


async def dependencies(settings: Settings, engine: Engine | None) -> dict[str, str]:
    database, queues = await asyncio.gather(
        asyncio.to_thread(database_status, engine), asyncio.to_thread(queue_status, settings)
    )
    return {"postgres": database, **queues}
