"""Tests that need a real Redis (run by scripts/test_redis.sh, `make test-redis`, part of `make check`).

The Redis is the throwaway one the script created for this run. Before anything else the guard
(redis_guard.py) checks that the URL names exactly that Redis: the run marker the script wrote into it and the
container id, port and role of its manifest. A URL set by hand, a remote or loopback Redis that is not this
run's, or a missing marker stops the session before any command that changes data. The database of the code
under test is a SQLite file the script created. Nothing here skips: run outside the script, the session
stops with an error."""

import os

import pytest
from redis_guard import Refused, verify

try:
    URL = verify(os.environ)
except Refused as refusal:
    raise pytest.UsageError(f"tests_redis refused to run: {refusal}") from None

os.environ["BIORADAR_REDIS_URL"] = URL
os.environ["BIORADAR_LLM_BASE_URL"] = "http://model.invalid"
os.environ["BIORADAR_LLM_MODEL"] = "fake"
os.environ["BIORADAR_DEV_CREDENTIAL"] = "test-login"
os.environ["BIORADAR_SESSION_SECRET"] = "test-session-key"
os.environ["BIORADAR_PLANNER_URL"] = "http://planner.invalid"
os.environ["BIORADAR_BIOCHECK_URL"] = "http://biocheck.invalid"

from redis import Redis  # noqa: E402

from bioradar import db  # noqa: E402
from bioradar.config import settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def verified_redis():
    """The settings the code under test uses point at the verified Redis (checked again, through the settings'
    own URL), and the database has its tables."""
    assert verify({**os.environ, "BIORADAR_TEST_REDIS_URL": settings().redis_url}) == URL
    db.Base.metadata.create_all(db.engine())


@pytest.fixture(autouse=True)
def empty_redis_and_database(verified_redis):
    connection = Redis.from_url(URL)
    marker = connection.get("radar:run-marker")
    connection.flushdb()
    connection.set("radar:run-marker", marker)  # the marker stays: the guard is checked again by each session
    connection.close()
    with db.engine().begin() as c:
        for table in reversed(db.Base.metadata.sorted_tables):
            c.execute(table.delete())
    yield


def pytest_sessionfinish(session, exitstatus):
    db.engine().dispose()
