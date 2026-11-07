"""The radar's own corner of Redis: which queue, which jobs, which wire format.

Redis is shared infrastructure; the radar must not run what another application (or another radar
environment) puts on a queue its worker reads. Three guards, each enough on its own for one mistake:

- the queue name is the radar's (BIORADAR_QUEUE_NAME must start with "bioradar", e.g. bioradar-staging for a
  second environment on the same Redis), so a worker is never pointed at another application's queue;
- jobs are JSON, not pickle: a payload that is not the radar's JSON is refused when it is read, and nothing a
  stranger wrote is ever unpickled by the worker;
- a job runs only if its function is on the list below, and a refused job's callbacks do not run either; it
  fails with ForeignJob and shows up in the failed registry."""

import re

from redis import Redis
from rq import Queue, Worker
from rq.job import Job
from rq.serializers import JSONSerializer

from bioradar.config import Settings

JOB_FUNCTIONS = frozenset({"bioradar.research.analyses.execute"})
QUEUE_NAME = re.compile(r"bioradar([._:-][a-z0-9][a-z0-9._:-]*)?")


class ForeignJob(Exception):
    """A job on the radar's queue that is not one of the radar's."""


def valid_queue_name(name: str) -> bool:
    return QUEUE_NAME.fullmatch(name) is not None


class RadarJob(Job):
    def ours(self) -> bool:
        try:
            return self.func_name in JOB_FUNCTIONS
        except Exception:  # noqa: BLE001 - a payload that is not the radar's JSON is not the radar's job
            return False

    def perform(self):
        if not self.ours():
            raise ForeignJob(self.id)
        return super().perform()

    # a refused job's callbacks are the stranger's code too
    def execute_success_callback(self, death_penalty_class, result):
        if self.ours():
            super().execute_success_callback(death_penalty_class, result)

    def execute_failure_callback(self, death_penalty_class, *exc_info):
        if self.ours():
            super().execute_failure_callback(death_penalty_class, *exc_info)

    def execute_stopped_callback(self, death_penalty_class):
        if self.ours():
            super().execute_stopped_callback(death_penalty_class)


def connection(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url)


def queue(settings: Settings) -> Queue:
    return Queue(
        settings.queue_name, connection=connection(settings), job_class=RadarJob, serializer=JSONSerializer
    )


def worker(settings: Settings) -> Worker:
    return Worker(
        [settings.queue_name],
        connection=connection(settings),
        job_class=RadarJob,
        serializer=JSONSerializer,
    )
