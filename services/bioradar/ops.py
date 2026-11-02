"""Process entry points:
python -m bioradar.ops worker    RQ worker that runs analyses
python -m bioradar.ops monitor   live monitoring loop (BIORADAR_MONITOR_ENABLED=true) plus recovery"""

import logging
import sys
import time

from redis import Redis
from rq import Worker

from bioradar.config import settings
from bioradar.research import analyses, monitor

log = logging.getLogger("bioradar")


def worker():
    s = settings()
    Worker([s.queue_name], connection=Redis.from_url(s.redis_url)).work(with_scheduler=False)


def monitor_loop():
    s = settings()
    while True:
        try:
            if s.monitor_enabled:
                log.info("monitor %s", monitor.tick(is_demo=False))
            recovered = analyses.recover()
            if recovered:
                log.info("re-queued %s analyses", recovered)
        except Exception:  # keep the loop alive; the next round retries
            log.exception("monitor round failed")
        time.sleep(s.monitor_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    {"worker": worker, "monitor": monitor_loop}[sys.argv[1]]()
