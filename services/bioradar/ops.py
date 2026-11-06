"""Process entry points:
python -m bioradar.ops worker    RQ worker that runs analyses
python -m bioradar.ops monitor   live monitoring loop (BIORADAR_MONITOR_ENABLED=true) plus recovery
python -m bioradar.ops upgrade-reads --source S --processor P [--limit N] [--run ID] [--demo]
python -m bioradar.ops backfill-corrections [--limit N] [--run ID] [--demo]
    T-26 operator-run, bounded upkeep of existing state; prints the run's summary (continue it with --run)"""

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


def upkeep_command(argv):
    import argparse
    import json

    from bioradar.adapters.fabric import Fabric
    from bioradar.research import upkeep

    p = argparse.ArgumentParser(prog="python -m bioradar.ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("upgrade-reads")
    a.add_argument("--source")
    a.add_argument("--processor")
    b = sub.add_parser("backfill-corrections")
    for x in (a, b):
        x.add_argument("--limit", type=int, default=20)
        x.add_argument("--run", type=int)
        x.add_argument("--demo", action="store_true")
    args = p.parse_args(argv)
    s = settings()
    if args.cmd == "upgrade-reads":
        if args.run is None and not (args.source and args.processor):
            p.error("upgrade-reads needs --source and --processor, or --run")
        out = upkeep.upgrade_reads(
            s,
            Fabric(s),
            is_demo=args.demo,
            source=args.source,
            processor=args.processor,
            limit=args.limit,
            run_id=args.run,
        )
    else:
        out = upkeep.backfill_corrections(s, Fabric(s), is_demo=args.demo, limit=args.limit, run_id=args.run)
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if sys.argv[1] in ("upgrade-reads", "backfill-corrections"):
        upkeep_command(sys.argv[1:])
    else:
        {"worker": worker, "monitor": monitor_loop}[sys.argv[1]]()
