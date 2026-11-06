"""Tests that need a real PostgreSQL (run by scripts/test_pg.sh, `make test-pg`, part of `make check`).

The database is a throwaway container started by the script, with the real migrations applied; the URL comes
in BIORADAR_TEST_PG_URL. Nothing here skips: run outside the script, the session stops with an error instead
of passing without having tested anything. The tables the tests write are emptied before each test, and only a
database on this machine is ever emptied."""

import os
from urllib.parse import urlsplit

import pytest

URL = os.environ.get("BIORADAR_TEST_PG_URL", "")
if not URL:
    raise pytest.UsageError("BIORADAR_TEST_PG_URL is not set: run these tests with `make test-pg`")
if urlsplit(URL).hostname not in ("127.0.0.1", "localhost", "::1"):
    raise pytest.UsageError("BIORADAR_TEST_PG_URL must name a database on this machine")

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


@pytest.fixture(autouse=True)
def empty_leased_tables():
    with db.engine().begin() as connection:
        connection.execute(text(f"TRUNCATE {LEASED_TABLES} RESTART IDENTITY CASCADE"))
    yield


def pytest_sessionfinish(session, exitstatus):
    db.engine().dispose()
