"""Acceptance runs A/B/C (simulated) and D (real 2019 public-history replay) against an isolated stack.

  python scripts/scenarios.py abc --data DIR --out OUT     four waves, directed analyses, topic lifecycle
  python scripts/scenarios.py d --data DIR/D --out OUT     import the replay archive, run the cutoff cases
  python scripts/scenarios.py d ... --daily 2019-12-01 2020-01-21   also replay monitoring day by day
  python scripts/scenarios.py cases --data DIR --out OUT     a fixed-material case packet (no date shift)

The radar, Fabric and the services must already run with an isolated database (see README).
A/B/C dates are shifted two months earlier so every simulated time is in the past for Fabric;
order and spacing stay the same. Every analysis gets a cutoff so it cannot see later waves.
Results: OUT/runs.jsonl (one line per run) and OUT/reports/<name>.md."""

import argparse
import copy
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from bioradar.adapters.fabric import Fabric
from bioradar.config import settings
from bioradar.research import analyses, monitor

DIRECTION = {"A": "public_health", "B": "pipeline", "C": "business"}
TIME_FIELDS = ("source_published_at", "published_at", "observed_at", "effective_time", "available_at")


def months_earlier(text, months=2):
    """'2026-10-27T08:00:00+08:00' -> '2026-08-27T08:00:00+08:00'; '10月26日' -> '8月26日'."""

    def iso(m):
        year, month = int(m.group(1)), int(m.group(2)) - months
        if month < 1:
            year, month = year - 1, month + 12
        return f"{year:04d}-{month:02d}"

    text = re.sub(r"(20\d\d)-(\d\d)(?=-\d\d)", iso, text)
    text = re.sub(r"(\d{1,2})月(\d{1,2})日", lambda m: f"{int(m.group(1)) - months}月{m.group(2)}日", text)
    return re.sub(r"(20\d\d)年(\d{1,2})月", lambda m: f"{m.group(1)}年{int(m.group(2)) - months}月", text)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_hash(records):
    """Same rule as Fabric's archive_manifest_hash: the proof is bound to every raw record."""
    body = [
        {"source_record_id": r["source_record_id"], "raw_content": r["raw_content"]}
        for r in sorted(records, key=lambda r: r["source_record_id"])
    ]
    return hashlib.sha256(canonical(body).encode()).hexdigest()


class Runner:
    def __init__(self, out: Path):
        self.out = out
        (out / "reports").mkdir(parents=True, exist_ok=True)
        self.fabric = Fabric(settings())
        self.versions = {}  # source_record_id -> version_id read back from Fabric
        repo = Path(__file__).resolve().parents[1]
        self.commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def log(self, **row):
        row = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "commit": self.commit,
            **row,
        }
        with open(self.out / "runs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False)[:300], flush=True)

    def publish(self, entry, *, shift):
        request = copy.deepcopy(entry["archive_request"])
        for record in request["records"]:
            raw = json.loads(record["raw_content"])
            if shift:
                for key in TIME_FIELDS:
                    if isinstance(raw.get(key), str):
                        raw[key] = months_earlier(raw[key])
                raw["full_text"] = months_earlier(raw.get("full_text", ""))
            for relation in raw.get("document_relations", []):
                target = relation.get("target_source_record_id")
                if target in self.versions:
                    relation["target_version_id"] = self.versions[target]
            record["raw_content"] = canonical(raw)
        if request.get("proof"):
            if shift:
                request["proof"]["available_at"]["value"] = months_earlier(
                    request["proof"]["available_at"]["value"]
                )
            request["proof"]["sha256"] = manifest_hash(request["records"])
        run = self.fabric.archive(request)
        ids = [r["source_record_id"] for r in request["records"]]
        found, _ = self.fabric.search(
            request["source_id"], (), is_demo=request["is_demo"], record_ids=ids, limit=len(ids)
        )
        self.versions.update({d["record_id"]: d["version_id"] for d in found})
        return run

    def analyse(self, name, **kwargs):
        row = analyses.create(**kwargs)
        row = analyses.wait_until_done(row.id, timeout=7200)
        self.save(name, row)
        return row

    def save(self, name, row):
        value = analyses.view(row, full=True)
        result = row.result or {}
        (self.out / "reports" / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (self.out / "reports" / f"{name}.md").write_text(result.get("draft") or "", encoding="utf-8")
        usage = result.get("usage", {})
        self.log(
            scenario=name.split("-")[0],
            name=name,
            method=row.method,
            analysis_id=row.id,
            state=row.state,
            report_status=result.get("report_status"),
            calls=usage.get("llm_calls"),
            tokens=usage.get("tokens"),
            error=row.error_code,
            reason=(result.get("stop") or {}).get("reason") or row.error_code,
        )

    def wait_queue(self, ids):
        for analysis_id in ids:
            self.save(f"topic-{analysis_id[:8]}", analyses.wait_until_done(analysis_id, timeout=7200))

    def tick(self, label, **kwargs):
        summary = monitor.tick(**kwargs)
        topics = [monitor.topic_view(t) for t in _topics(kwargs.get("is_demo", False))]
        self.log(
            scenario="monitor",
            name=label,
            records=summary["records"],
            clock=summary["clock"],
            topics=[{k: t[k] for k in ("title", "state", "alert", "awakenings")} for t in topics],
            analyses=summary["analyses"],
        )
        return summary


def _topics(is_demo):
    from sqlalchemy import select

    from bioradar import db

    with db.session() as s:
        return list(s.scalars(select(db.Topic).where(db.Topic.is_demo == is_demo)))


def run_abc(args):
    data, runner = Path(args.data), Runner(Path(args.out))
    entries = json.loads((data / "archive-templates.json").read_text(encoding="utf-8"))["entries"]
    packets = {
        k: json.loads((data / f"{k}-packet.json").read_text(encoding="utf-8"))["cases"][0] for k in "ABC"
    }
    lifecycle = json.loads((data / "lifecycle.json").read_text(encoding="utf-8"))
    wave_time = {}
    for entry in entries:
        proof = entry["archive_request"]["proof"]["available_at"]["value"]
        wave_time[entry["wave"]] = max(wave_time.get(entry["wave"], ""), months_earlier(proof))
    for wave in ("W1", "W2", "W3", "W4"):
        for entry in [e for e in entries if e["wave"] == wave]:
            runner.publish(entry, shift=True)
        cutoff = datetime.fromisoformat(wave_time[wave]) + timedelta(hours=1)
        summary = runner.tick(f"wave-{wave}", is_demo=True, cutoff=cutoff)
        runner.wait_queue(summary["analyses"])
    after_w4 = (datetime.fromisoformat(wave_time["W4"]) + timedelta(hours=2)).isoformat()
    for key in [k for k in "ABC" if k in args.only]:
        for method in ("planner", "baseline"):
            runner.analyse(
                f"{key}-{method}",
                question=packets[key]["question"],
                direction=DIRECTION[key],
                method=method,
                is_demo=True,
                cutoff=after_w4,
            )
    if "A" not in args.only:
        return
    w4 = datetime.fromisoformat(wave_time["W4"])
    life = {e["wave"]: e for e in entries if e["wave"] in ("L1", "L4")}
    clocks = {"U1": w4 + timedelta(days=7), "U2": w4 + timedelta(days=9)}
    for step in lifecycle["steps"]:
        name = step["id"]
        if step["publish"]:
            runner.publish(life[name], shift=True)
            at = datetime.fromisoformat(
                months_earlier(life[name]["archive_request"]["proof"]["available_at"]["value"])
            )
            clocks[name] = at
            summary = runner.tick(f"life-{name}", is_demo=True, cutoff=at + timedelta(hours=1))
        else:
            # R1 is a restart: state is re-read from the database, the clock does not move.
            clock = clocks.get(name) or max(clocks.values()) + timedelta(days=0 if name == "R1" else 2)
            clocks[name] = clock
            summary = runner.tick(f"life-{name}", is_demo=True, clock=clock)  # blank round, no new record
        runner.wait_queue(summary["analyses"])


def run_d(args):
    data, runner = Path(args.data), Runner(Path(args.out))
    if not args.skip_import:
        entries = json.loads((data / "archive-templates.json").read_text(encoding="utf-8"))["entries"]
        started = time.monotonic()
        for i, entry in enumerate(entries, 1):
            runner.publish(entry, shift=False)
            if i % 100 == 0:
                print(f"imported {i}/{len(entries)} in {time.monotonic() - started:.0f}s", flush=True)
        runner.log(scenario="D", name="import", records=len(entries))
    if args.daily:
        day, end = (
            datetime.fromisoformat(args.daily[0] + "T23:59:59+08:00"),
            datetime.fromisoformat(args.daily[1] + "T23:59:59+08:00"),
        )
        while day <= end:
            summary = runner.tick(f"D-day-{day.date()}", is_demo=False, cutoff=day)
            runner.wait_queue(summary["analyses"])
            day += timedelta(days=1)
    cases = json.loads((data / "D-packet.json").read_text(encoding="utf-8"))["cases"]
    for case in cases:
        if args.cases and case["case_id"] not in args.cases:
            continue
        methods = ["planner", "baseline"] if case["case_id"].startswith("history-window-01") else ["planner"]
        for method in methods:
            runner.analyse(
                f"D-{case['case_id']}-{method}",
                question=case["question"],
                direction="public_health",
                method=method,
                is_demo=False,
                sources=case["source_ids"],
                cutoff=case["historical_scope"]["cutoff"],
                scope_record_ids=case["scope_record_ids"],
            )


def run_cases(args):
    """A fixed-material case packet (e.g. the signal cases): import the archive in wave order
    without shifting dates, then run every case with the methods it lists, each with its own cutoff
    and fixed material set."""
    data, runner = Path(args.data), Runner(Path(args.out))
    entries = json.loads((data / "archive-templates.json").read_text(encoding="utf-8"))["entries"]
    if not args.skip_import:
        for entry in sorted(entries, key=lambda e: e["wave"]):  # stable: file order within a wave
            runner.publish(entry, shift=False)
        runner.log(scenario="cases", name="import", records=len(entries))
    for case in json.loads((data / args.packet).read_text(encoding="utf-8"))["cases"]:
        if args.cases and case["case_id"] not in args.cases:
            continue
        for method in case.get("methods", ["planner"]):
            runner.analyse(
                f"{case['case_id']}-{method}",
                question=case["question"],
                direction=case["direction"],
                method=method,
                is_demo=case["is_demo"],
                sources=case["source_ids"],
                cutoff=case["historical_scope"]["cutoff"],
                scope_record_ids=case["scope_record_ids"],
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    abc = sub.add_parser("abc")
    abc.add_argument("--data", required=True)
    abc.add_argument("--out", required=True)
    abc.add_argument("--only", default="ABC")
    d = sub.add_parser("d")
    d.add_argument("--data", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--cases", nargs="*")
    d.add_argument("--daily", nargs=2, metavar=("START", "END"))
    d.add_argument("--skip-import", action="store_true")
    cases = sub.add_parser("cases")
    cases.add_argument("--data", required=True)
    cases.add_argument("--out", required=True)
    cases.add_argument("--packet", default="R10-packet.json")
    cases.add_argument("--cases", nargs="*")
    cases.add_argument("--skip-import", action="store_true")
    parsed = parser.parse_args()
    {"abc": run_abc, "d": run_d, "cases": run_cases}[parsed.command](parsed)
