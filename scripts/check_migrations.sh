#!/bin/sh
# Migrations on a real PostgreSQL: a throwaway postgres:17-alpine container, then
#   alembic upgrade head -> the schema compared with the models -> downgrade base -> upgrade head,
# then the container is removed (by its exact name), also when a step fails. The tests run on SQLite with
# create_all, so this is what checks the migrations themselves, JSONB and the PostgreSQL types.
# Downgrades refuse while audit tables hold rows (by design); the database here starts empty.
# Usage: sh scripts/check_migrations.sh
#   PYTHON         the command that runs a python with the dependencies (default: python3; may be
#                  "uv run --frozen python"); PYTHONPATH gets services
#   PG_CONTAINER   container name (default: radar-migcheck-<random>)
#   PG_PORT        host port on 127.0.0.1 (default: a free one in 19801-19999)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
RANDOM_ID=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
NAME=${PG_CONTAINER:-radar-migcheck-$RANDOM_ID}
PORT=${PG_PORT:-$($PYTHON -c '
import random, socket
for port in random.sample(range(19801, 20000), 199):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
')}
PASSWORD=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
docker run -d --name "$NAME" -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB=bioradar \
  -p "127.0.0.1:$PORT:5432" postgres:17-alpine >/dev/null
echo "postgres:17-alpine as $NAME on 127.0.0.1:$PORT"
for _ in $(seq 60); do
  docker exec "$NAME" pg_isready -U postgres -d bioradar -h 127.0.0.1 >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" pg_isready -U postgres -d bioradar -h 127.0.0.1 >/dev/null

export BIORADAR_DATABASE_URL="postgresql+psycopg://postgres:$PASSWORD@127.0.0.1:$PORT/bioradar"
export PYTHONPATH="$ROOT/services${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT/db"
alembic() { $PYTHON -m alembic -c alembic.ini "$@"; }
step() { echo "== $*"; }

step "upgrade head"
alembic upgrade head
step "schema of the migrations compared with the models"
$PYTHON - <<'EOF'
import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from bioradar import db

with db.engine().connect() as connection:
    diff = compare_metadata(MigrationContext.configure(connection), db.Base.metadata)
for d in diff:
    print("  drift:", d)
if diff:
    sys.exit(f"{len(diff)} differences between the migrated schema and the models")
print("  migrated schema matches the models")
EOF
step "downgrade base"
alembic downgrade base
step "upgrade head again"
alembic upgrade head
echo "migrations ok: upgrade head -> downgrade base -> upgrade head on PostgreSQL 17"
