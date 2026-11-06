"""The guard refuses anything that is not this run's own database, before a destructive statement.

Negative cases use a stand-in for the marker read: no database is touched, and the stand-in records whether
the guard would have connected."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from guard import Refused, verify

RUN = "0123abcd"
CONTAINER = "c" * 64
PORT = 19850
DATABASE = f"radar_pgtest_{RUN}"
URL = f"postgresql+psycopg://postgres:x@127.0.0.1:{PORT}/{DATABASE}"


def manifest(tmp_path, **changes):
    records = [
        {"event": "run", "run": RUN},
        {"event": "created", "id": CONTAINER, "port": PORT, "database": DATABASE},
    ]
    path = tmp_path / "manifest.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records + changes.get("extra", [])))
    return str(path)


def good_env(tmp_path):
    return {
        "BIORADAR_TEST_PG_URL": URL,
        "BIORADAR_TEST_PG_RUN": RUN,
        "BIORADAR_TEST_PG_CONTAINER": CONTAINER,
        "BIORADAR_TEST_PG_MANIFEST": manifest(tmp_path),
    }


class Reader:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def __call__(self, url):
        self.calls += 1
        return self.answer


GOOD_MARKER = [(DATABASE, RUN, CONTAINER)]


def test_this_runs_own_database_is_accepted(tmp_path):
    reader = Reader(GOOD_MARKER)
    assert verify(good_env(tmp_path), reader) == (URL, DATABASE) and reader.calls == 1


@pytest.mark.parametrize(
    "change",
    [
        {"BIORADAR_TEST_PG_URL": ""},  # no URL
        {"BIORADAR_TEST_PG_RUN": ""},  # no run marker given to pytest
        {"BIORADAR_TEST_PG_CONTAINER": ""},
        {"BIORADAR_TEST_PG_MANIFEST": ""},
        {"BIORADAR_TEST_PG_URL": f"postgresql+psycopg://u:p@db.example.com:{PORT}/{DATABASE}"},  # remote
        {"BIORADAR_TEST_PG_URL": "postgresql+psycopg://u:p@localhost:5432/anything"},  # loopback by hand
        {"BIORADAR_TEST_PG_URL": f"postgresql+psycopg://u:p@127.0.0.1:{PORT}/bioradar_live"},  # the live name
        {"BIORADAR_TEST_PG_URL": f"postgresql+psycopg://u:p@127.0.0.1:15436/{DATABASE}"},  # other port
        {"BIORADAR_TEST_PG_CONTAINER": "d" * 64},  # not the container of the manifest
        {"BIORADAR_TEST_PG_RUN": "ffffffff"},  # not the run of the manifest
        {"BIORADAR_TEST_PG_MANIFEST": "/nonexistent/manifest.jsonl"},
    ],
)
def test_a_url_that_is_not_this_runs_resource_is_refused_without_connecting(tmp_path, change):
    reader = Reader(GOOD_MARKER)
    with pytest.raises(Refused):
        verify({**good_env(tmp_path), **change}, reader)
    assert reader.calls == 0


def test_loopback_without_the_marker_in_the_database_is_refused(tmp_path):
    for answer in ([], None, [("bioradar_live", RUN, CONTAINER)], [(DATABASE, "other", CONTAINER)]):
        with pytest.raises(Refused):
            verify(good_env(tmp_path), Reader(answer))
    with pytest.raises(Refused):  # the marker of another container, or two markers
        verify(good_env(tmp_path), Reader([(DATABASE, RUN, "d" * 64)]))
    with pytest.raises(Refused):
        verify(good_env(tmp_path), Reader(GOOD_MARKER * 2))


def test_a_torn_down_run_is_refused(tmp_path):
    env = good_env(tmp_path)
    env["BIORADAR_TEST_PG_MANIFEST"] = manifest(tmp_path, extra=[{"event": "removed", "id": CONTAINER}])
    reader = Reader(GOOD_MARKER)
    with pytest.raises(Refused):
        verify(env, reader)
    assert reader.calls == 0


def test_the_session_refuses_a_hand_set_url_before_connecting(tmp_path):
    """Whole pytest session with only a URL: it must stop in conftest, without trying to reach the server
    (port 1 would give a connection error, not a refusal) and so without any statement."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BIORADAR_")}
    env["BIORADAR_TEST_PG_URL"] = "postgresql+psycopg://u:p@127.0.0.1:1/bioradar_live"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root / "tests_pg")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0 and "tests_pg refused to run" in output
    assert "onnection" not in output and "OperationalError" not in output
