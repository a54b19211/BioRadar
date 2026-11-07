#!/bin/sh
# Sourced by scripts/check_migrations.sh, scripts/test_pg.sh and scripts/test_redis.sh: one throwaway container
# per run (postgres:17-alpine, or redis:7.4-alpine with redis_start), removed again also when a step fails or
# the run is interrupted.
#
# The container carries the label radar-<PG_TAG>.run=<this run's random id>. Only the ID `docker run`
# returned (or, when it created the container but could not start it, the ID of the container with this
# run's label and exact name) is registered, with its image and anonymous volume, as JSON lines in the
# manifest; the teardown checks the ID still carries this run's label, stops it and removes it with its
# anonymous volume (`docker rm -v`; nothing is scanned or pruned), and records whether anything is left.
# The manifest is kept. A container this run did not create is never stopped or removed.
#
# The caller sets before sourcing:
#   PG_TAG       short name of the check ("migcheck", "pgtest"): label, container name and manifest name
#   PG_MANIFEST  the container manifest (default: .test-artifacts/radar-<PG_TAG>-<random>.containers.jsonl
#                in the checkout, git-ignored)
#   PYTHON       the command that runs a python (default: python3)
# and may set PG_CONTAINER (container name), PG_PORT (host port on 127.0.0.1; default: a free one in
# 19801-19999), PG_DB_PREFIX (the database is then <prefix>_<random id>, else "bioradar") and PG_MARKER=1
# (write the run marker into the database: table radar_run_marker with the run id and the container id,
# which a test run checks before it changes any data). After `pg_start` the caller has $ID, $PORT,
# $PASSWORD, $DB and $RANDOM_ID.
# The caller's own cleanup goes in a function named on_exit, run before the container is torn down.
set -eu
: "${PG_TAG:?PG_TAG is required}"
PYTHON=${PYTHON:-python3}
RANDOM_ID=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
NAME=${PG_CONTAINER:-radar-$PG_TAG-$RANDOM_ID}
LABEL=radar-$PG_TAG.run
DB=${PG_DB_PREFIX:+${PG_DB_PREFIX}_$RANDOM_ID}
DB=${DB:-bioradar}
MANIFEST=${PG_MANIFEST:-$(pwd)/.test-artifacts/radar-$PG_TAG-$RANDOM_ID.containers.jsonl}
mkdir -p "$(dirname "$MANIFEST")"
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
  record "\"event\":\"created\",\"role\":\"${ROLE:-pg}\",\"name\":\"$NAME\",\"id\":\"$ID\",\"port\":$PORT,\"database\":\"$DB\",\"image\":\"${image% *}\",\"image_id\":\"${image#* }\",\"anonymous_volumes\":[$(echo $VOLUMES | sed 's/\([^ ]*\)/"\1"/g; s/ /,/g')]"
}
teardown() {
  rc=$1
  left=""
  if command -v on_exit >/dev/null 2>&1; then on_exit || true; fi
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

pg_start() {
  record "\"event\":\"run\",\"run\":\"$RANDOM_ID\",\"label\":\"$LABEL=$RANDOM_ID\""
  if out=$(docker run -d --name "$NAME" --label "$LABEL=$RANDOM_ID" -e POSTGRES_PASSWORD="$PASSWORD" \
    -e POSTGRES_DB=$DB -p "127.0.0.1:$PORT:5432" postgres:17-alpine); then
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
    docker exec "$ID" pg_isready -U postgres -d "$DB" -h 127.0.0.1 >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec "$ID" pg_isready -U postgres -d "$DB" -h 127.0.0.1 >/dev/null
  if [ "${PG_MARKER:-}" = 1 ]; then
    docker exec "$ID" psql -U postgres -d "$DB" -h 127.0.0.1 -v ON_ERROR_STOP=1 -q -c \
      "CREATE TABLE radar_run_marker (run_id text NOT NULL, container_id text NOT NULL); \
       INSERT INTO radar_run_marker VALUES ('$RANDOM_ID', '$ID')" >/dev/null
  fi
}

redis_start() {
  record "\"event\":\"run\",\"run\":\"$RANDOM_ID\",\"label\":\"$LABEL=$RANDOM_ID\""
  if out=$(docker run -d --name "$NAME" --label "$LABEL=$RANDOM_ID" -p "127.0.0.1:$PORT:6379" \
    redis:7.4-alpine redis-server --requirepass "$PASSWORD" --save ""); then
    register "$out"
  else
    rc=$?
    created=$(docker ps -aq --no-trunc --filter "label=$LABEL=$RANDOM_ID" --filter "name=^/$NAME\$" 2>/dev/null || true)
    record "\"event\":\"start_failed\",\"name\":\"$NAME\",\"exit\":$rc,\"created_id\":\"$created\""
    [ -z "$created" ] || register "$created"
    exit 2
  fi
  echo "redis:7.4-alpine as $NAME on 127.0.0.1:$PORT (manifest $MANIFEST)"
  for _ in $(seq 30); do
    docker exec -e REDISCLI_AUTH="$PASSWORD" "$ID" redis-cli ping >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec -e REDISCLI_AUTH="$PASSWORD" "$ID" redis-cli ping >/dev/null
  # the run marker a test run checks before it flushes anything
  docker exec -e REDISCLI_AUTH="$PASSWORD" "$ID" redis-cli SET radar:run-marker "$RANDOM_ID:$ID" >/dev/null
}
