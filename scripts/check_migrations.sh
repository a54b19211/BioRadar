#!/bin/sh
# Migrations on a real PostgreSQL: a throwaway postgres:17-alpine container (scripts/pg_container.sh), then
#   alembic upgrade head -> the schema compared with the models -> downgrade base -> upgrade head,
# then the container is removed, also when a step fails or the run is interrupted. The tests run on SQLite
# with create_all, so this is what checks the migrations themselves, JSONB and the PostgreSQL types.
# Downgrades refuse while audit tables hold rows (by design); the database here starts empty.
# The container lifecycle (label, manifest, teardown by the registered ID, `docker rm -v`) is described in
# scripts/pg_container.sh.
#
# Usage: sh scripts/check_migrations.sh
#   PYTHON             the command that runs a python with the dependencies (default: python3; may be
#                      "uv run --frozen python"); PYTHONPATH gets services
#   PG_CONTAINER       container name (default: radar-migcheck-<random>)
#   PG_PORT            host port on 127.0.0.1 (default: a free one in 19801-19999)
#   MIGCHECK_MANIFEST  the container manifest (default: /tmp/radar-migcheck-<random>.containers.jsonl)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PG_TAG=migcheck
PG_MANIFEST=${MIGCHECK_MANIFEST:-}
. "$ROOT/scripts/pg_container.sh"
pg_start

export BIORADAR_DATABASE_URL="postgresql+psycopg://postgres:$PASSWORD@127.0.0.1:$PORT/$DB"
export PYTHONPATH="$ROOT/services${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT/db"
alembic() { $PYTHON -m alembic -c alembic.ini "$@"; }
step() { echo "== $*"; }

step "upgrade head"
alembic upgrade head
alembic current
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
echo "  current revision: [$(alembic current 2>/dev/null)] (empty = base)"
step "upgrade head again"
alembic upgrade head
alembic current
echo "migrations ok: upgrade head -> downgrade base -> upgrade head on PostgreSQL 17"
