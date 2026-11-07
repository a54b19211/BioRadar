"""The guard in front of every destructive statement of tests_redis/ (the Redis twin of tests_pg/guard.py).

Loopback is not proof that a Redis was created by this run: the live Redis is on loopback too. The entry
script (scripts/test_redis.sh) therefore creates the container, records its id and port in its manifest and
writes a marker key into it (radar:run-marker = "<run id>:<container id>"). These tests run only if all of
this agrees:

  BIORADAR_TEST_REDIS_URL        the URL of that Redis, on this machine
  BIORADAR_TEST_REDIS_RUN        the run id of the script
  BIORADAR_TEST_REDIS_CONTAINER  the container id the script's `docker run` returned
  BIORADAR_TEST_REDIS_MANIFEST   the script's manifest: a `run` record for the run id, a `created` record for
                                 the container id with this port, and no `removed` / `teardown` record
  the server                     answers with exactly that run id and container id in the marker key

Anything else raises Refused, before the first command that changes data."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = ("127.0.0.1", "localhost", "::1")
MARKER_KEY = "radar:run-marker"
NAMES = ("URL", "RUN", "CONTAINER", "MANIFEST")


class Refused(Exception):
    pass


def read_marker(url):
    """The marker the server at `url` holds (a read-only GET)."""
    from redis import Redis

    connection = Redis.from_url(url)
    try:
        value = connection.get(MARKER_KEY)
        return value.decode() if value is not None else None
    finally:
        connection.close()


def _records(manifest):
    path = Path(manifest)
    if not path.is_file():
        raise Refused("the manifest of the entry script does not exist")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def verify(env, read=read_marker):
    """Return the URL when this Redis is the one the entry script created for this run."""
    values = {n: env.get(f"BIORADAR_TEST_REDIS_{n}", "") for n in NAMES}
    missing = [f"BIORADAR_TEST_REDIS_{n}" for n, v in values.items() if not v]
    if missing:
        raise Refused(f"{', '.join(missing)} not set: these tests run only through `make test-redis`")
    url, run, container, manifest = (values[n] for n in NAMES)
    parts = urlsplit(url)
    if parts.hostname not in LOOPBACK or not parts.port:
        raise Refused("the URL must name a port on this machine")
    if not re.fullmatch(r"[0-9a-f]{8}", run) or not re.fullmatch(r"[0-9a-f]{64}", container):
        raise Refused("the run id or container id is not one the entry script makes")
    records = _records(manifest)
    if not any(r.get("event") == "run" and r.get("run") == run for r in records):
        raise Refused("the manifest has no record of this run")
    created = [r for r in records if r.get("event") == "created" and r.get("id") == container]
    if len(created) != 1 or created[0].get("role") != "redis":
        raise Refused("the manifest has no record of this Redis container")
    if created[0].get("port") != parts.port:
        raise Refused("the port of the URL is not the one the container was created with")
    if any(r.get("event") in ("removed", "teardown") for r in records):
        raise Refused("the manifest says this run's container is already torn down")
    if read(url) != f"{run}:{container}":
        raise Refused("the Redis does not carry this run's marker")
    return url
