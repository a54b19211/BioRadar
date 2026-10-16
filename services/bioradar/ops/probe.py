"""Exercise actual PostgreSQL → RQ → worker → PostgreSQL, without model calls."""

import time
from uuid import uuid4

from redis import Redis
from rq import Queue
from sqlalchemy.orm import Session

from bioradar.api.config import Settings
from bioradar.data.database import create_database
from bioradar.ops.models import InfrastructureProbe
from bioradar.ops.worker import finish_probe


def main() -> None:
    settings = Settings()
    engine = create_database(settings)
    if engine is None or settings.redis_url is None:
        raise SystemExit("Database and Redis configuration are required")
    probe_id = str(uuid4())
    try:
        with Session(engine) as session, session.begin():
            session.add(InfrastructureProbe(id=probe_id, state="queued"))
        redis = Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=3)
        queue = Queue(f"{settings.queue_prefix}:routine", connection=redis)
        queue.enqueue(finish_probe, probe_id, job_id=f"probe-{probe_id}", job_timeout=20, result_ttl=300)
        for _ in range(30):
            with Session(engine) as session:
                probe = session.get(InfrastructureProbe, probe_id)
                if probe and probe.state == "completed":
                    print(f"PASS infrastructure probe {probe_id}; no intelligence capability evaluated")
                    return
            time.sleep(0.5)
        raise SystemExit("Probe incomplete: inspect worker/queue; queued is not completed")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
