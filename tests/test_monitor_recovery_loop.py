"""One actual maintenance-loop turn, with service side effects isolated."""

import logging
from types import SimpleNamespace

import pytest
from bioradar.api.config import Settings
from bioradar.ops import monitor
from bioradar.research.investigations import analysis_service

pytestmark = pytest.mark.ENGINE


@pytest.fixture
def loop(monkeypatch):
    settings = Settings(
        _env_file=None,
        monitor_enabled=False,
        data_fabric_url="http://127.0.0.1:1",
        data_fabric_history_read_token="synthetic-test-token",
    )
    events = []
    failing = []

    class StopAfterOneTurn:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, _seconds):
            self.set()

    def action(name, result):
        events.append(name)
        if name in failing:
            raise RuntimeError("synthetic service failure")
        return result

    database = SimpleNamespace(dispose=lambda: events.append("dispose"))
    client = SimpleNamespace()
    access = SimpleNamespace(actor=lambda: SimpleNamespace(grant_ref="synthetic-grant"))

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *_args):
            pass

    class Monitor:
        def __init__(self, _engine, _reader):
            pass

        def tick(self):
            return action("monitor", {"state": "idle"})

        def retry_batch(self, _actor, identifier):
            assert identifier == "synthetic-batch"
            return action("retry_batch", {"state": "queued"})

    class Topics:
        def __init__(self, _engine, _reader):
            pass

        def tick(self):
            return action("topic", {"state": "idle"})

    class Analyses:
        def __init__(self, _engine, _reader):
            pass

        def recover(self, limit):
            assert limit == 4
            return action("recover", 1)

    monkeypatch.setattr("sys.argv", ["monitor"])
    monkeypatch.setattr(monitor, "Settings", lambda: settings)
    monkeypatch.setattr(monitor, "create_database", lambda _settings: database)
    monkeypatch.setattr(monitor.threading, "Event", StopAfterOneTurn)
    monkeypatch.setattr(monitor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(monitor.httpx, "Client", Client)
    monkeypatch.setattr(monitor, "FabricHistoryReader", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(monitor, "DevelopmentAccess", lambda _settings: access)
    monkeypatch.setattr(monitor, "EngineService", lambda *_args: object())
    monkeypatch.setattr(monitor, "MonitorService", Monitor)
    monkeypatch.setattr(monitor, "TopicService", Topics)
    monkeypatch.setattr(analysis_service, "AnalysisService", Analyses)
    return settings, events, failing


def test_disabled_discovery_still_recovers_accepted_investigations(loop):
    _settings, events, _failing = loop
    monitor.main()
    assert events == ["recover", "dispose"]


@pytest.mark.parametrize("failed_service", ["monitor", "topic"])
def test_discovery_failure_cannot_starve_recovery(loop, failed_service):
    settings, events, failing = loop
    settings.monitor_enabled = True
    failing.append(failed_service)
    monitor.main()
    expected = ["monitor"] + (["topic"] if failed_service == "topic" else [])
    assert events == [*expected, "recover", "dispose"]


def test_recovery_failure_is_reported_without_success_or_loop_crash(loop, caplog):
    _settings, events, failing = loop
    failing.append("recover")
    with caplog.at_level(logging.INFO, logger=monitor.__name__):
        monitor.main()
    assert events == ["recover", "dispose"]
    assert any(
        entry.levelno == logging.WARNING and "Research recovery incomplete" in entry.message
        for entry in caplog.records
    )
    assert not any("Research recovery dispatched" in entry.message for entry in caplog.records)


def test_enabled_discovery_keeps_both_ticks_and_one_recovery(loop):
    settings, events, _failing = loop
    settings.monitor_enabled = True
    monitor.main()
    assert events == ["monitor", "topic", "recover", "dispose"]


@pytest.mark.parametrize("missing", ["data_fabric_url", "data_fabric_history_read_token"])
def test_missing_source_configuration_does_not_schedule_work(loop, missing):
    settings, events, _failing = loop
    setattr(settings, missing, None)
    monitor.main()
    assert events == ["dispose"]


def test_retry_batch_does_not_run_discovery_or_periodic_recovery(loop, monkeypatch):
    _settings, events, _failing = loop
    monkeypatch.setattr("sys.argv", ["monitor", "--retry-batch", "synthetic-batch"])
    monitor.main()
    assert events == ["retry_batch", "dispose"]
