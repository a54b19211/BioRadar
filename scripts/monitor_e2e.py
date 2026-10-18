"""Exercise the unattended pipeline by writing only to an isolated Fabric source API.

The publisher, monitor and RQ worker must already be running. This script never
calls a Radar run/investigate endpoint or an internal publisher/monitor tick.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from history_e2e import Exercise


class MonitorExercise(Exercise):
    def status(self):
        return self.call(self.radar, "GET", "/api/engine/monitor/status", auth=self.user).json()

    def batches(self):
        return self.call(
            self.radar,
            "GET",
            "/api/engine/monitor/batches",
            auth=self.user,
            params={"limit": 100},
        ).json()["items"]

    def wait(self, predicate, message, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(1)
        raise AssertionError(message)

    def control(self, paused):
        current = self.status()
        return self.call(
            self.radar,
            "POST",
            "/api/engine/monitor/control",
            auth=self.user,
            key=self.prefix + "-" + uuid4().hex,
            body={"expected_revision": current["control_revision"], "paused": paused},
        ).json()

    def upload(self, name, content):
        response = self.fabric.post(
            "/api/documents/upload",
            files={"file": (name + ".md", content.encode(), "text/markdown")},
            data={"source_site": "nmpa"},
        )
        assert response.status_code == 200, "test source upload failed"
        result = response.json()
        assert result["success"], "test source did not accept document"
        return result["uuid"]

    def current_records(self):
        return self.call(
            self.fabric,
            "GET",
            "/api/history/records",
            auth=self.read,
            params={
                "source_id": "legacy_documents",
                "is_demo": "true",
                "mode": "current",
                "limit": 100,
                "cutoff": datetime.now(UTC).isoformat(),
            },
        ).json()["items"]

    def run(self, expected_database, expected_namespace):
        publication = self.call(self.fabric, "GET", "/api/history/publication/status", auth=self.read).json()
        self.check(
            publication["is_demo"] is True
            and publication["source_database"] == expected_database
            and publication["namespace"] == expected_namespace
            and expected_namespace.startswith("synthetic."),
            "server_confirms_disposable_synthetic_source_before_any_upload",
        )
        status = self.status()
        self.check(status["enabled"], "monitor_enabled")
        self.check(
            all(source["is_demo"] for source in status["sources"]),
            "monitor_reads_only_demo_versions",
        )
        self.control(True)
        self.wait(
            lambda: (
                not any(row["state"] in {"accepted", "queued", "investigating"} for row in self.batches())
            ),
            "an earlier batch is still running",
        )
        baseline_ids = {row["id"] for row in self.batches()}
        content = (
            f"# Explicit teaching record {self.prefix}\n\n"
            "photon hydrogel macrophage targeting. Material stability remains unknown.\n"
        )
        uuid = self.upload(self.prefix + "-one", content)
        companion_uuid = self.upload(
            self.prefix + "-companion",
            f"# Synthetic fixture {self.prefix} companion\n\n"
            "photon hydrogel macrophage targeting. Independent replication was not assessed.\n",
        )
        self.wait(
            lambda: next(
                (
                    row
                    for row in self.current_records()
                    if row["source_record_id"] == "legacy:nmpa:" + companion_uuid
                ),
                None,
            ),
            "companion source record was not automatically published",
        )
        native = "legacy:nmpa:" + uuid
        record = self.wait(
            lambda: next((row for row in self.current_records() if row["source_record_id"] == native), None),
            "source commit was not published automatically",
        )
        self.check(record["is_demo"], "automatically_published_version_is_demo")
        time.sleep(3)
        self.check(
            {row["id"] for row in self.batches()} == baseline_ids,
            "paused_monitor_does_not_schedule_new_computation",
        )
        self.control(False)
        finished = self.wait(
            lambda: next(
                (
                    row
                    for row in self.batches()
                    if row["id"] not in baseline_ids
                    and row["state"] == "completed"
                    and row["investigation_ids"]
                ),
                None,
            ),
            "automatic calculation and investigation did not finish",
        )
        self.check(finished["run_id"] is not None, "source_write_started_existing_run_queue")
        run = self.call(self.radar, "GET", "/api/engine/runs/" + finished["run_id"], auth=self.user).json()
        self.check(run["state"] == "completed", "real_worker_completed_calculation")
        investigation_id = finished["investigation_ids"][0]
        investigation = self.call(
            self.radar, "GET", "/api/engine/investigations/" + investigation_id, auth=self.user
        ).json()
        entries = investigation["entries"]
        materials = [entry for entry in entries if entry["kind"] == "material"]
        self.check(
            materials
            and all(entry["payload"].get("automation", {}).get("source") == "monitor" for entry in materials),
            "automatic_materials_keep_program_provenance",
        )
        self.check(
            all(entry["payload"]["stance"] == "context" for entry in materials)
            and investigation["judgment_count"] == 0,
            "program_does_not_forge_human_stance_or_judgment",
        )
        receipts = []
        for entry in entries:
            text = entry["payload"].get("text", "")
            if text.startswith("自动补查回执") and "{" in text:
                receipts.append(json.loads(text[text.index("{") :]))
        self.check(
            any(
                item.get("status") == "read" and item.get("returned", 0) > 0 and item.get("snapshot_id")
                for item in receipts
            ),
            "automatic_digging_performed_an_actual_read_and_saved_a_snapshot",
        )
        self.check(
            any(
                item["payload"]["support"]["record_ref"]["adopted_version"] == record["version_id"]
                for item in materials
            ),
            "investigation_refers_to_exact_automatically_published_version",
        )
        before_duplicate = {row["id"] for row in self.batches()}
        duplicate = self.upload(self.prefix + "-same", content)
        self.check(duplicate == uuid, "repeated_upload_keeps_document_identity")
        time.sleep(max(8, status["limits"]["poll_seconds"] * len(status["sources"]) + 2))
        self.check(
            {row["id"] for row in self.batches()} == before_duplicate,
            "identical_source_write_does_not_launch_another_run",
        )
        self.control(True)
        second_uuid = self.upload(
            self.prefix + "-two",
            f"# Second explicit teaching record {self.prefix}\n\n"
            "photon hydrogel macrophage targeting. "
            "A second synthetic source revision requires checking the material conditions.\n",
        )
        self.wait(
            lambda: next(
                (
                    row
                    for row in self.current_records()
                    if row["source_record_id"] == "legacy:nmpa:" + second_uuid
                ),
                None,
            ),
            "second source record was not automatically published",
        )
        self.check(
            {row["id"] for row in self.batches()} == before_duplicate,
            "fabric_publication_continues_while_radar_is_paused",
        )
        self.control(False)
        second = self.wait(
            lambda: next(
                (
                    row
                    for row in self.batches()
                    if row["id"] not in before_duplicate and row["state"] == "completed"
                ),
                None,
            ),
            "resuming did not automatically consume the next committed source record",
        )
        self.check(
            second["next_after"] > finished["next_after"],
            "durable_source_cursor_advances_after_a_second_completed_batch",
        )
        return {
            "checks": self.checks,
            "prefix": self.prefix,
            "source_record_id": native,
            "version_id": record["version_id"],
            "batch_ids": [finished["id"], second["id"]],
            "investigation_id": investigation_id,
            "automatic_read_receipts": len(receipts),
            "limitation": "Synthetic engineering exercise; not a real scientific discovery.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fabric-url", required=True)
    parser.add_argument("--radar-url", required=True)
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    exercise = MonitorExercise(args.fabric_url, args.radar_url)
    result = exercise.run(args.source_database, args.namespace)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checks_passed": len(result["checks"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
