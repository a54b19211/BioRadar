"""Process entry points:
python -m bioradar.ops worker    RQ worker that runs analyses
python -m bioradar.ops monitor   live monitoring loop (BIORADAR_MONITOR_ENABLED=true) plus recovery
python -m bioradar.ops select-display --cutoff T1
                                 prints, as JSON, the group the first judgment is about and every group
                                 visible at T1 (research/groups.select_display); reads, writes nothing"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime

from bioradar import db, guard, jobs
from bioradar.adapters.fabric import OPERATIONAL, Fabric
from bioradar.config import settings
from bioradar.research import analyses, groups, monitor
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


def moment(text: str) -> datetime:
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        found = None
    if found is None or found.tzinfo is None:
        raise argparse.ArgumentTypeError("an ISO 8601 time with a time zone, such as 2026-11-10T06:00:00Z")
    return found


def select_display(argv) -> None:
    """Print the selection of the display group as of --cutoff. The versions of the configured Fabric
    backfill runs are background; a run whose receipt cannot be read is listed in the coverage, and the
    outcome is then unverified. The coverage also holds the cutoff day's leads with the gaps Fabric's reads of
    its windows left, read in the mode the first judgment is submitted in (operational replay)."""
    parser = argparse.ArgumentParser(prog="bioradar.ops select-display")
    parser.add_argument("--cutoff", type=moment, required=True)
    cutoff = parser.parse_args(argv).cutoff
    s = settings()
    fabric = Fabric(s)
    background, unread = fabric.background_version_ids(s.backfill_run_ids)
    with db.session() as session:
        found = groups.for_selection(
            session, s, is_demo=False, cutoff=cutoff, background=background, unread=unread
        )
    groups.cover_leads(
        found["selection"]["coverage"]["leads"], fabric, is_demo=False, mode=OPERATIONAL, cutoff=cutoff
    )
    sys.stdout.write(json.dumps(found, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    command, *arguments = sys.argv[1:]
    if command == "select-display":
        select_display(arguments)
    else:
        {"worker": worker, "monitor": monitor_loop}[command]()
