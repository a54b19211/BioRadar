"""Exercise the unattended pipeline by writing only to an isolated Fabric source API.

The publisher, monitor and RQ worker must already be running. This script never
calls a Radar run/investigate endpoint or an internal publisher/monitor tick.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from history_e2e import Exercise

from tests.query_policy_assertions import assert_query_policy


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

    def existing(self, sources, *, require_unselected=False):
        self.guard_existing()
        status = self.status()
        selected_sources = [row for row in status["sources"] if row["source_id"] in sources]
        self.check(
            bool(selected_sources) and all(not row["is_demo"] for row in selected_sources),
            "isolated_monitor_reads_the_explicit_real_source_scope",
        )
        self.check(status["enabled"] and not status["paused"], "existing_source_monitor_is_running")
        batches = self.wait(
            lambda: [
                row
                for row in self.batches()
                if row["source_id"] in sources and not row["is_demo"] and row["state"] == "completed"
            ],
            "no completed real-source batch was produced by the existing background monitor",
        )
        selections = []
        for batch in batches:
            summary = batch.get("selection_summary")
            self.check(
                isinstance(summary, dict)
                and all(
                    isinstance(summary.get(key), int) and summary[key] >= 0
                    for key in ("new_records", "evaluated", "selected", "no_candidate", "unknown")
                ),
                "automatic_batch_preserves_its_actual_selection_and_unselected_scope",
            )
            selections.append({"batch_id": batch["id"], **summary})
        if require_unselected:
            self.check(
                any(row["no_candidate"] or row["unknown"] for row in selections),
                "real_batch_includes_new_material_not_selected_or_not_evaluable_by_current_rules",
            )
        lead_list = self.call(
            self.radar,
            "GET",
            "/api/engine/monitor/leads",
            auth=self.user,
            params={"limit": 100},
        ).json()
        batch_ids = {batch["id"] for batch in batches}
        leads = [lead for lead in lead_list["items"] if lead["batch_id"] in batch_ids]
        self.check(bool(leads), "real_source_batch_automatically_produced_an_investigation_lead")
        analyses = []
        for lead in leads:
            self.check(
                bool(lead.get("importance_reason")) and bool(lead.get("urgency_reason")),
                "automatic_importance_and_urgency_have_saved_evidence_based_reasons",
            )
            self.check(bool(lead.get("investigation_id")), "selected_real_lead_has_an_actual_investigation")
            investigation = self.call(
                self.radar,
                "GET",
                "/api/engine/investigations/" + lead["investigation_id"],
                auth=self.user,
            ).json()
            self.check(
                investigation["judgment_count"] == 0, "automatic_source_path_does_not_forge_a_human_judgment"
            )
            listing = self.wait(
                lambda: self.call(
                    self.radar,
                    "GET",
                    f"/api/engine/investigations/{lead['investigation_id']}/analyses",
                    auth=self.user,
                ).json()["items"],
                "automatic real-source lead did not enqueue any semantic analysis",
            )
            candidates = [item for item in listing if item["trigger"] == "monitor"]
            self.check(bool(candidates), "monitor_trigger_uses_the_shared_semantic_execution_path")
            analysis = self.wait_semantic(lead["investigation_id"], candidates[0]["id"])
            summary = assert_query_policy(analysis, expected_policy=self.policy)
            self.check(
                analysis["state"] in {"completed", "needs_review"} and not analysis["is_demo"],
                "automatic_query_policy_produced_a_saved_real_source_result",
            )
            for evidence in analysis["evidence"]:
                original = self.call(
                    self.radar,
                    "GET",
                    f"/api/engine/investigations/{analysis['investigation_id']}/analyses/{analysis['id']}"
                    f"/evidence/{evidence['key']}",
                    auth=self.user,
                ).json()
                self.check(
                    original["evidence"]["record_ref"] == evidence["record_ref"]
                    and original["normalized_text"][evidence["start"] : evidence["end"]] == evidence["text"],
                    "automatic_policy_keeps_the_same_frozen_original_and_passage",
                )
            analyses.append({"lead_id": lead["id"], "analysis_id": analysis["id"], "query_policy": summary})
        return {
            "checks": self.checks,
            "source_ids": sources,
            "selection_summary": selections,
            "leads": leads,
            "analyses": analyses,
            "engineering_status": "passed",
            "scientific_review": "pending_independent_review",
            "qualification": (
                "Real existing source versions, read-only Fabric; "
                "unselected means not selected by this run's rules."
            ),
        }

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
    parser.add_argument("--source-database")
    parser.add_argument("--namespace")
    parser.add_argument("--existing-only", action="store_true")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--policy", choices=("baseline", "query_planner"))
    parser.add_argument("--require-unselected", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.existing_only and not args.source:
        parser.error("--existing-only requires explicit --source scope")
    if not args.existing_only and (not args.source_database or not args.namespace):
        parser.error("synthetic upload mode requires --source-database and --namespace")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    exercise = MonitorExercise(
        args.fabric_url, args.radar_url, read_only_sources=args.existing_only, policy=args.policy
    )
    result = {"status": "running", "checks": exercise.checks, "started_at": datetime.now(UTC).isoformat()}
    try:
        result.update(
            exercise.existing(args.source, require_unselected=args.require_unselected)
            if args.existing_only
            else exercise.run(args.source_database, args.namespace)
        )
        result["status"] = (
            "technical_checks_passed" if args.existing_only else "synthetic_engineering_checks_passed"
        )
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        exercise.close()
        result["finished_at"] = datetime.now(UTC).isoformat()
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"status": result["status"], "checks_passed": len(result["checks"]), "output": str(args.output)}
        )
    )


if __name__ == "__main__":
    main()
