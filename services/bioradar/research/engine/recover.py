"""Bounded dispatch/reconcile CLI; cron can call it without a second scheduler."""

import argparse
import json

from bioradar.api.config import Settings
from bioradar.data.database import create_database
from bioradar.research.engine.service import EngineService


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover persisted development mining and decision runs")
    parser.add_argument("--limit", type=int, default=100)
    arguments = parser.parse_args()
    settings = Settings()
    database = create_database(settings)
    if database is None:
        raise SystemExit("Research database is required")
    try:
        result = EngineService(database, settings).recover(arguments.limit)
        print(json.dumps(result))
        if result["dependency_unavailable"]:
            raise SystemExit(1)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
