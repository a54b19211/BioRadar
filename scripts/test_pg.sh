#!/bin/sh
# The tests that need a real PostgreSQL (tests_pg/): row locks, SKIP LOCKED and lease fencing cannot be shown on
# SQLite. A throwaway postgres:17-alpine container (scripts/pg_container.sh) is started, the real migrations
# are applied (alembic upgrade head), pytest runs tests_pg/ against it, and the container is removed again,
# also when a step fails or the run is interrupted. No test is skipped: without Docker this fails.
#
# Usage: sh scripts/test_pg.sh [pytest arguments]
#   PYTHON          the command that runs a python with the dependencies (default: python3; may be
#                   "uv run --frozen python"); PYTHONPATH gets services
#   PG_CONTAINER    container name (default: radar-pgtest-<random>)
#   PG_PORT         host port on 127.0.0.1 (default: a free one in 19801-19999)
#   PGTEST_MANIFEST the container manifest (default: /tmp/radar-pgtest-<random>.containers.jsonl)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PG_TAG=pgtest
PG_MANIFEST=${PGTEST_MANIFEST:-}
. "$ROOT/scripts/pg_container.sh"
pg_start

export BIORADAR_DATABASE_URL="postgresql+psycopg://postgres:$PASSWORD@127.0.0.1:$PORT/bioradar"
export BIORADAR_TEST_PG_URL="$BIORADAR_DATABASE_URL"
export PYTHONPATH="$ROOT/services${PYTHONPATH:+:$PYTHONPATH}"
echo "== alembic upgrade head"
(cd "$ROOT/db" && $PYTHON -m alembic -c alembic.ini upgrade head)
echo "== pytest tests_pg"
cd "$ROOT"
$PYTHON -m pytest -q tests_pg "$@"
