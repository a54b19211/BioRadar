"""Tests that need a real PostgreSQL (run by scripts/test_pg.sh, `make test-pg`, part of `make check`).

The database is the throwaway one the script created for this run, with the real migrations applied. Before
anything else the guard (guard.py) checks that the URL names exactly that database: the run marker the script
wrote into it, the container id, port and database of its manifest. A URL set by hand, a remote or loopback
database that is not this run's, or a missing marker stops the session before any statement that changes data.
Nothing here skips: run outside the script, the session stops with an error."""

import os

import pytest
from guard import Refused, verify

try:
    URL, DATABASE = verify(os.environ)
except Refused as refusal:
    raise pytest.UsageError(f"tests_pg refused to run: {refusal}") from None

os.environ["BIORADAR_DATABASE_URL"] = URL
os.environ["BIORADAR_LLM_BASE_URL"] = "http://model.invalid"
os.environ["BIORADAR_LLM_MODEL"] = "fake"
os.environ["BIORADAR_DEV_CREDENTIAL"] = "test-login"
os.environ["BIORADAR_SESSION_SECRET"] = "test-session-key"
os.environ["BIORADAR_PLANNER_URL"] = "http://planner.invalid"
os.environ["BIORADAR_BIOCHECK_URL"] = "http://biocheck.invalid"

from sqlalchemy import text  # noqa: E402

from bioradar import db  # noqa: E402

# the tables the lease code works on, with everything that refers to them
LEASED_TABLES = (
    "measurement_check_tasks, measurement_checks, measurement_candidates, record_read_attempts, "
    "record_read_windows, record_reads, analyses, investigations"
)


@pytest.fixture(scope="session", autouse=True)
def verified_database():
    """The engine the code under test uses points at the verified database (checked again, with the marker
    read through that engine's own URL) before the first TRUNCATE."""
    engine_url = db.engine().url.render_as_string(hide_password=False)
    assert verify({**os.environ, "BIORADAR_TEST_PG_URL": engine_url}) == (URL, DATABASE)


@pytest.fixture(autouse=True)
def empty_leased_tables(verified_database):
    with db.engine().begin() as connection:
        connection.execute(text(f"TRUNCATE {LEASED_TABLES} RESTART IDENTITY CASCADE"))
    yield


def pytest_sessionfinish(session, exitstatus):
    db.engine().dispose()
