"""The radar's corner of a shared Redis: its queue name, its job list and its wire format (bioradar/jobs.py).
No Redis here: the guards are in the job class and the settings, which is where a stranger's job would meet
them."""

import pickle

import pytest
from pydantic import ValidationError
from rq.job import Job
from rq.serializers import JSONSerializer

from bioradar import jobs
from bioradar.config import Settings

TRIPPED = []


def trip():
    TRIPPED.append("unpickled")


class Booby:
    def __reduce__(self):  # what a pickled job of another application could carry
        return (trip, ())


class Connection:
    """Just enough of a Redis connection for a job that is created and performed in memory."""

    def persist(self, key):
        pass


def job(function, **kwargs):
    return jobs.RadarJob.create(function, connection=Connection(), serializer=JSONSerializer, **kwargs)


@pytest.mark.parametrize(
    "name",
    [
        "bioradar",
        "bioradar-staging",
        "bioradar.test",
        "bioradar:replay-2",
        "bioradar-offline-20261107T020539Z-6cb094",
    ],
)
def test_a_radar_queue_name_is_accepted(name):
    assert jobs.valid_queue_name(name) and Settings(queue_name=name).queue_name == name


@pytest.mark.parametrize("name", ["default", "high", "other-app", "bioradarx", "bioradar-", "Bioradar", ""])
def test_another_applications_queue_name_cannot_be_configured(name):
    assert not jobs.valid_queue_name(name)
    with pytest.raises(ValidationError):
        Settings(queue_name=name)


def test_a_job_of_another_application_is_refused_and_not_run():
    foreign = job("os.getcwd")
    assert not foreign.ours()
    with pytest.raises(jobs.ForeignJob):
        foreign.perform()


def test_the_callbacks_of_a_refused_job_do_not_run(monkeypatch):
    foreign = job("os.getcwd")

    def explode(self):
        raise AssertionError("a stranger's callback was looked at")

    for name in ("success_callback", "failure_callback", "stopped_callback"):
        monkeypatch.setattr(jobs.RadarJob, name, property(explode))
    foreign.execute_success_callback(None, 1)
    foreign.execute_failure_callback(None, None, None, None)
    foreign.execute_stopped_callback(None)


def test_the_radars_own_job_runs():
    mine = job("bioradar.research.analyses.execute", args=("no-such-analysis",))
    assert mine.ours()
    assert mine.perform() is None  # execute returns at once for an analysis that does not exist


def test_a_pickled_payload_is_never_unpickled():
    stranger = jobs.RadarJob(id="x", connection=Connection(), serializer=JSONSerializer)
    stranger.data = pickle.dumps((Job.__module__, None, (Booby(),), {}))
    assert not stranger.ours()
    with pytest.raises(jobs.ForeignJob):
        stranger.perform()
    assert TRIPPED == []
