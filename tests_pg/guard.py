"""The guard in front of every destructive statement of tests_pg/.

Loopback is not proof that a database was created by this run: the live database is on loopback too. The
entry script (scripts/test_pg.sh) therefore creates, with the container, a database of its own
(radar_pgtest_<run>) and writes a marker row into it (table radar_run_marker: run id and container id), and
records the container id, port and database in its manifest. These tests run only if all of this agrees:

  BIORADAR_TEST_PG_URL        the URL of that database, on this machine
  BIORADAR_TEST_PG_RUN        the run id of the script
  BIORADAR_TEST_PG_CONTAINER  the container id the script's `docker run` returned
  BIORADAR_TEST_PG_MANIFEST   the script's manifest: a `run` record for the run id, a `created` record for the
                              container id with this port and database, and no `removed` record for it
  the server                  answers with exactly that database name, run id and container id in the marker

Anything else raises Refused, before the first statement that changes data."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = ("127.0.0.1", "localhost", "::1")
DATABASE_PREFIX = "radar_pgtest_"
NAMES = ("URL", "RUN", "CONTAINER", "MANIFEST")


class Refused(Exception):
    pass


def read_marker(url):
    """[(database, run id, container id)] as the server at `url` reports them (a read-only SELECT)."""
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT current_database(), run_id, container_id FROM radar_run_marker")
            ).all()
        return [tuple(r) for r in rows]
    finally:
        engine.dispose()


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
    """Return (url, database) when this database is the one the entry script created for this run."""
    values = {n: env.get(f"BIORADAR_TEST_PG_{n}", "") for n in NAMES}
    missing = [f"BIORADAR_TEST_PG_{n}" for n, v in values.items() if not v]
    if missing:
        raise Refused(f"{', '.join(missing)} not set: these tests run only through `make test-pg`")
    url, run, container, manifest = (values[n] for n in NAMES)
    parts = urlsplit(url)
    database = parts.path.lstrip("/")
    if parts.hostname not in LOOPBACK or not parts.port:
        raise Refused("the URL must name a port on this machine")
    if not re.fullmatch(re.escape(DATABASE_PREFIX) + r"[0-9a-f]{8}", database) or not database.endswith(run):
        raise Refused("the database is not the dedicated one of this run")
    records = _records(manifest)
    if not any(r.get("event") == "run" and r.get("run") == run for r in records):
        raise Refused("the manifest has no record of this run")
    created = [r for r in records if r.get("event") == "created" and r.get("id") == container]
    if len(created) != 1:
        raise Refused("the manifest has no record of this container")
    if created[0].get("port") != parts.port or created[0].get("database") != database:
        raise Refused("the port or database of the URL is not the one the container was created with")
    if any(r.get("event") in ("removed", "teardown") for r in records):
        raise Refused("the manifest says this run's container is already torn down")
    marker = read(url)
    if marker != [(database, run, container)]:
        raise Refused("the database does not carry this run's marker")
    return url, database
