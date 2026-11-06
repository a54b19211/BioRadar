#!/bin/sh
# Migrations on a real PostgreSQL: a throwaway postgres:17-alpine container, then
#   alembic upgrade head -> the schema compared with the models -> downgrade base -> upgrade head,
# then the container is removed, also when a step fails or the run is interrupted. The tests run on SQLite
# with create_all, so this is what checks the migrations themselves, JSONB and the PostgreSQL types.
# Downgrades refuse while audit tables hold rows (by design); the database here starts empty.
#
# The container carries the label radar-migcheck.run=<this run's random id>. Only the ID `docker run`
# returned (or, when it created the container but could not start it, the ID of the container with this
# run's label and exact name) is registered, with its image and anonymous volume, as JSON lines in the
# manifest; the teardown checks the ID still carries this run's label, stops it and removes it with its
# anonymous volume (`docker rm -v`; nothing is scanned or pruned), and records whether anything is left.
# The manifest is kept. A container this run did not create is never stopped or removed.
#
# Usage: sh scripts/check_migrations.sh
#   PYTHON             the command that runs a python with the dependencies (default: python3; may be
#                      "uv run --frozen python"); PYTHONPATH gets services
#   PG_CONTAINER       container name (default: radar-migcheck-<random>)
#   PG_PORT            host port on 127.0.0.1 (default: a free one in 19801-19999)
#   MIGCHECK_MANIFEST  the container manifest (default: /tmp/radar-migcheck-<random>.containers.jsonl)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
RANDOM_ID=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
NAME=${PG_CONTAINER:-radar-migcheck-$RANDOM_ID}
LABEL=radar-migcheck.run
MANIFEST=${MIGCHECK_MANIFEST:-/tmp/radar-migcheck-$RANDOM_ID.containers.jsonl}
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

record() { printf '{"at":"%s",%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$MANIFEST"; }
ID="" VOLUMES=""
register() {
  ID=$1
  VOLUMES=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$ID" 2>/dev/null || true)
  image=$(docker inspect -f '{{.Config.Image}} {{.Image}}' "$ID" 2>/dev/null || true)
  record "\"event\":\"created\",\"role\":\"pg\",\"name\":\"$NAME\",\"id\":\"$ID\",\"image\":\"${image% *}\",\"image_id\":\"${image#* }\",\"anonymous_volumes\":[$(echo $VOLUMES | sed 's/\([^ ]*\)/"\1"/g; s/ /,/g')]"
}
teardown() {
  rc=$1
  left=""
  if [ -n "$ID" ]; then
    label=$(docker inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$ID" 2>/dev/null || true)
    if [ "$label" = "$RANDOM_ID" ]; then
      docker stop -t 10 "$ID" >/dev/null 2>&1 || true
      docker rm -v "$ID" >/dev/null 2>&1 || true
    elif docker inspect "$ID" >/dev/null 2>&1; then
      record "\"event\":\"not_touched\",\"id\":\"$ID\",\"reason\":\"label is not this run's\""
    fi
    if docker inspect "$ID" >/dev/null 2>&1 && [ "$label" = "$RANDOM_ID" ]; then left="$ID"; fi
    for v in $VOLUMES; do
      if docker volume inspect "$v" >/dev/null 2>&1; then docker volume rm "$v" >/dev/null 2>&1 || true; fi
      if docker volume inspect "$v" >/dev/null 2>&1; then left="$left $v"; fi
    done
    record "\"event\":\"removed\",\"id\":\"$ID\",\"left\":\"$left\""
  fi
  record "\"event\":\"teardown\",\"exit\":$rc,\"left\":\"$left\""
  echo "container of this run torn down (manifest $MANIFEST)" >&2
  if [ -n "$left" ]; then
    echo "left behind: $left" >&2
    [ "$rc" -ne 0 ] || rc=3
  fi
  exit "$rc"
}
trap 'teardown $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
record "\"event\":\"run\",\"run\":\"$RANDOM_ID\",\"label\":\"$LABEL=$RANDOM_ID\""

if out=$(docker run -d --name "$NAME" --label "$LABEL=$RANDOM_ID" -e POSTGRES_PASSWORD="$PASSWORD" \
  -e POSTGRES_DB=bioradar -p "127.0.0.1:$PORT:5432" postgres:17-alpine); then
  register "$out"
else
  rc=$?
  # `docker run` can create the container and then fail to start it (e.g. the port was taken meanwhile)
  created=$(docker ps -aq --no-trunc --filter "label=$LABEL=$RANDOM_ID" --filter "name=^/$NAME\$" 2>/dev/null || true)
  record "\"event\":\"start_failed\",\"name\":\"$NAME\",\"exit\":$rc,\"created_id\":\"$created\""
  [ -z "$created" ] || register "$created"
  exit 2
fi
echo "postgres:17-alpine as $NAME on 127.0.0.1:$PORT (manifest $MANIFEST)"
for _ in $(seq 60); do
  docker exec "$ID" pg_isready -U postgres -d bioradar -h 127.0.0.1 >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$ID" pg_isready -U postgres -d bioradar -h 127.0.0.1 >/dev/null

export BIORADAR_DATABASE_URL="postgresql+psycopg://postgres:$PASSWORD@127.0.0.1:$PORT/bioradar"
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
