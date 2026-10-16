from datetime import UTC, datetime

from redis import Redis
from rq import Worker
from sqlalchemy.orm import Session

from bioradar.api.config import Settings
from bioradar.data.database import create_database
from bioradar.ops.models import InfrastructureProbe


def finish_probe(probe_id: str) -> str:
    """An idempotent infrastructure check; it never calls upstream services."""
    engine = create_database(Settings())
    if engine is None:
        raise RuntimeError("Database is not configured")
    try:
        with Session(engine) as session, session.begin():
            probe = session.get(InfrastructureProbe, probe_id, with_for_update=True)
            if probe is None:
                raise RuntimeError("Probe does not exist")
            if probe.state != "completed":
                probe.state = "completed"
                probe.completed_at = datetime.now(UTC)
        return probe_id
    finally:
        engine.dispose()


def main() -> None:
    settings = Settings()
    if settings.redis_url is None or settings.database_url is None:
        raise SystemExit("Configure BIORADAR_DATABASE_URL and BIORADAR_REDIS_URL")
    connection = Redis.from_url(settings.redis_url.get_secret_value())
    Worker([f"{settings.queue_prefix}:routine"], connection=connection).work()


if __name__ == "__main__":
    main()
