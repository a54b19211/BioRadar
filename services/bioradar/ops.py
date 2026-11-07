"""Process entry points:
python -m bioradar.ops worker    RQ worker that runs analyses
python -m bioradar.ops monitor   live monitoring loop (BIORADAR_MONITOR_ENABLED=true) plus recovery"""

import logging
import sys
import time

from bioradar import guard, jobs
from bioradar.config import settings
from bioradar.research import analyses, monitor
from bioradar.research.leader import Leader

log = logging.getLogger("bioradar")


def worker():
    jobs.worker(settings()).work(with_scheduler=False)


def monitor_round(leader, s=None) -> bool:
    """One round of the monitor, only while this process is the leader (research.leader); False when another
    monitor process is."""
    s = s or settings()
    if not leader.acquire():
        log.info("monitor: another process holds the monitor lock, this round is skipped")
        return False
    with guard.holding(
        leader.assert_held
    ):  # a dropped lock connection stops the round at its next send or write
        if s.monitor_enabled:
            log.info("monitor %s", monitor.tick(is_demo=False))
        recovered = analyses.recover()
        if recovered:
            log.info("re-queued %s analyses", recovered)
    return True


def monitor_loop():
    s = settings()
    leader = Leader()
    try:
        while True:
            try:
                monitor_round(leader, s)
            except Exception:  # keep the loop alive; the next round retries
                log.exception("monitor round failed")
            time.sleep(s.monitor_seconds)
    finally:
        leader.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    {"worker": worker, "monitor": monitor_loop}[sys.argv[1]]()
