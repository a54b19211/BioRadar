"""Persistent monitor loop. It does no mining in the API process and uses no new queue."""

import argparse
import json
import logging
import signal
import threading

import httpx

from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.data.database import create_database
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.service import EngineService
from bioradar.research.monitor.service import MonitorService

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-batch", help="核对并恢复同一固定阻塞批次，不重新取输入或跳过游标")
    arguments = parser.parse_args()
    settings = Settings()
    database = create_database(settings)
    if database is None:
        raise SystemExit("Monitor database is not configured")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        if arguments.retry_batch:
            if not settings.data_fabric_url or not settings.data_fabric_history_read_token:
                raise SystemExit("History read permission is not configured")
            try:
                with httpx.Client(
                    base_url=settings.data_fabric_url,
                    trust_env=False,
                    headers={
                        "Authorization": "Bearer "
                        + settings.data_fabric_history_read_token.get_secret_value()
                    },
                ) as client:
                    reader = FabricHistoryReader(
                        client, authorization_ref=DevelopmentAccess(settings).actor().grant_ref
                    )
                    result = MonitorService(EngineService(database, settings), reader).retry_batch(
                        DevelopmentAccess(settings).actor(), arguments.retry_batch
                    )
                    print(json.dumps(result, ensure_ascii=False))
            except DomainError as exc:
                print(
                    json.dumps(
                        {"state": "not_retried", "error_code": exc.code, "message": exc.message},
                        ensure_ascii=False,
                    )
                )
                raise SystemExit(1) from None
            except Exception:
                print(
                    '{"state":"write_outcome_unconfirmed","message":"请先核对原批次及固定回执，未自动重发"}'
                )
                raise SystemExit(1) from None
            return
        while not stop.is_set():
            if (
                settings.monitor_enabled
                and settings.data_fabric_url
                and settings.data_fabric_history_read_token
            ):
                try:
                    with httpx.Client(
                        base_url=settings.data_fabric_url,
                        trust_env=False,
                        headers={
                            "Authorization": "Bearer "
                            + settings.data_fabric_history_read_token.get_secret_value()
                        },
                    ) as client:
                        reader = FabricHistoryReader(
                            client,
                            authorization_ref=DevelopmentAccess(settings).actor().grant_ref,
                            max_records=settings.engine_max_records,
                            max_bytes=settings.engine_max_snapshot_bytes,
                            max_seconds=90,
                        )
                        result = MonitorService(EngineService(database, settings), reader).tick()
                        logger.info(
                            "Monitor tick state=%s code=%s", result["state"], result.get("error_code", "none")
                        )
                except Exception as exc:
                    # PG outcome may be unknown: the next tick reconciles durable ids, never new write keys.
                    logger.warning("Monitor tick incomplete type=%s", type(exc).__name__)
            stop.wait(settings.monitor_poll_seconds)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
