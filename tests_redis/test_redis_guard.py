"""The guard refuses anything that is not this run's own Redis, before a destructive command.

Negative cases use a stand-in for the marker read: no Redis is touched."""

import json

import pytest
from redis_guard import Refused, verify

RUN = "0123abcd"
CONTAINER = "c" * 64
PORT = 19850
URL = f"redis://:x@127.0.0.1:{PORT}/0"
MARKER = f"{RUN}:{CONTAINER}"


def env(tmp_path, records=None, **changes):
    records = records or [
        {"event": "run", "run": RUN},
        {"event": "created", "id": CONTAINER, "role": "redis", "port": PORT},
    ]
    path = tmp_path / "manifest.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    base = {
        "BIORADAR_TEST_REDIS_URL": URL,
        "BIORADAR_TEST_REDIS_RUN": RUN,
        "BIORADAR_TEST_REDIS_CONTAINER": CONTAINER,
        "BIORADAR_TEST_REDIS_MANIFEST": str(path),
    }
    return {**base, **changes}


class Reader:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def __call__(self, url):
        self.calls += 1
        return self.answer


def test_this_runs_own_redis_is_accepted(tmp_path):
    reader = Reader(MARKER)
    assert verify(env(tmp_path), reader) == URL and reader.calls == 1


@pytest.mark.parametrize("name", ["URL", "RUN", "CONTAINER", "MANIFEST"])
def test_a_missing_setting_is_refused_without_connecting(tmp_path, name):
    reader = Reader(MARKER)
    with pytest.raises(Refused, match="make test-redis"):
        verify(env(tmp_path, **{f"BIORADAR_TEST_REDIS_{name}": ""}), reader)
    assert reader.calls == 0


@pytest.mark.parametrize(
    "url", ["redis://:x@redis.example.com:19850/0", "redis://:x@10.0.0.5:19850/0", "redis://:x@127.0.0.1/0"]
)
def test_a_redis_that_is_not_on_this_machine_with_a_port_is_refused(tmp_path, url):
    reader = Reader(MARKER)
    with pytest.raises(Refused, match="port on this machine"):
        verify(env(tmp_path, BIORADAR_TEST_REDIS_URL=url), reader)
    assert reader.calls == 0


def test_a_live_looking_redis_on_loopback_without_this_runs_marker_is_refused(tmp_path):
    for answer in (None, f"{RUN}:{'d' * 64}", f"ffffffff:{CONTAINER}"):
        with pytest.raises(Refused, match="marker"):
            verify(env(tmp_path), Reader(answer))


def test_a_container_this_run_did_not_create_or_with_another_port_is_refused(tmp_path):
    other = [
        {"event": "run", "run": RUN},
        {"event": "created", "id": "e" * 64, "role": "redis", "port": PORT},
    ]
    with pytest.raises(Refused, match="Redis container"):
        verify(env(tmp_path, other), Reader(MARKER))
    moved = [{"event": "run", "run": RUN}, {"event": "created", "id": CONTAINER, "role": "redis", "port": 1}]
    with pytest.raises(Refused, match="port of the URL"):
        verify(env(tmp_path, moved), Reader(MARKER))
    postgres = [
        {"event": "run", "run": RUN},
        {"event": "created", "id": CONTAINER, "role": "pg", "port": PORT},
    ]
    with pytest.raises(Refused, match="Redis container"):
        verify(env(tmp_path, postgres), Reader(MARKER))


def test_a_run_whose_container_is_already_torn_down_is_refused(tmp_path):
    records = [
        {"event": "run", "run": RUN},
        {"event": "created", "id": CONTAINER, "role": "redis", "port": PORT},
        {"event": "teardown", "exit": 0},
    ]
    with pytest.raises(Refused, match="torn down"):
        verify(env(tmp_path, records), Reader(MARKER))


def test_a_manifest_without_this_run_is_refused(tmp_path):
    records = [
        {"event": "run", "run": "ffffffff"},
        {"event": "created", "id": CONTAINER, "role": "redis", "port": PORT},
    ]
    with pytest.raises(Refused, match="no record of this run"):
        verify(env(tmp_path, records), Reader(MARKER))
