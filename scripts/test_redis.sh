#!/bin/sh
# The tests that need a real Redis (tests_redis/): the radar's JSON queue, the job allow-list, an old pickled
# job left over from before the switch, and recover's questions to RQ. A throwaway redis:7.4-alpine container
# (scripts/pg_container.sh: the same label, manifest and teardown-by-registered-ID discipline as test-pg) is
# started on 127.0.0.1 with a generated password and a run marker (the key radar:run-marker holding the run id
# and the container id), pytest runs tests_redis/ against it with a SQLite file of its own for the database,
# and the container is removed again, also when a step fails or the run is interrupted. The tests flush the
# Redis they run on: they run only against the container this script created, which they verify (marker,
# container id, port) before the first statement; see tests_redis/redis_guard.py. No test is skipped: without
# Docker this fails.
#
# Usage: sh scripts/test_redis.sh [pytest arguments]
#   PYTHON          the command that runs a python with the dependencies (default: python3; may be
#                   "uv run --frozen python"); PYTHONPATH gets services
#   PG_CONTAINER    container name (default: radar-redistest-<random>)
#   PG_PORT         host port on 127.0.0.1 (default: a free one in 19801-19999)
#   REDISTEST_MANIFEST  the container manifest (default: .test-artifacts/radar-redistest-<random>.containers.jsonl)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PG_TAG=redistest
ROLE=redis
PG_MANIFEST=${REDISTEST_MANIFEST:-}
. "$ROOT/scripts/pg_container.sh"
WORK=$(mktemp -d /tmp/radar-redistest-work.XXXXXX)
on_exit() { rm -f "$WORK"/test.db*; rmdir "$WORK" 2>/dev/null || true; }
redis_start

export BIORADAR_REDIS_URL="redis://:$PASSWORD@127.0.0.1:$PORT/0"
export BIORADAR_QUEUE_NAME=bioradar-redistest
export BIORADAR_DATABASE_URL="sqlite:///$WORK/test.db"
# what tests_redis/redis_guard.py checks before it flushes anything
export BIORADAR_TEST_REDIS_URL="$BIORADAR_REDIS_URL" BIORADAR_TEST_REDIS_RUN="$RANDOM_ID"
export BIORADAR_TEST_REDIS_CONTAINER="$ID" BIORADAR_TEST_REDIS_MANIFEST="$MANIFEST"
export PYTHONPATH="$ROOT/services${PYTHONPATH:+:$PYTHONPATH}"
echo "== pytest tests_redis"
cd "$ROOT"
$PYTHON -m pytest -q tests_redis "$@"
