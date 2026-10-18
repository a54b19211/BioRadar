"""Real HTTP acceptance for long-term topics in an isolated, labelled teaching database.

The background processes must already be running. Only source archive imports and
ordinary topic CRUD are used; the script never calls an internal monitor tick.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from history_e2e import Exercise, archive_body, canonical, record


class TopicExercise(Exercise):
    def wait(self, predicate, message, seconds=360):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(1)
        raise AssertionError(message)

    def topic(self, identifier):
        return self.call(self.radar, "GET", "/api/engine/topics/" + identifier, auth=self.user).json()

    def evaluations(self, identifier):
        return self.call(
            self.radar,
            "GET",
            "/api/engine/topics/" + identifier + "/evaluations",
            auth=self.user,
            params={"limit": 20},
        ).json()["items"]

    def archived(self, records, suffix):
        return self.call(
            self.fabric,
            "POST",
            "/api/history/archives",
            auth=self.admin,
            body=archive_body(records, datetime.now(UTC).isoformat(), self.prefix + suffix),
            key=self.prefix + suffix,
        ).json()

    def change_control(self, identifier, paused):
        topic = self.topic(identifier)
        return self.call(
            self.radar,
            "POST",
            "/api/engine/topics/" + identifier + "/control",
            auth=self.user,
            key=self.prefix + "-" + str(time.monotonic_ns()),
            body={"expected_revision": topic["control_revision"], "paused": paused},
        ).json()

    def frozen_hash(self, evaluation):
        fields = [
            "id",
            "definition",
            "scope_revision",
            "compute_version",
            "observation_revision",
            "as_of",
            "summary",
            "buckets",
            "signals",
            "registered_states",
            "metric_series",
            "coverage",
            "evidence",
            "data_gaps",
            "next_questions",
            "draft",
        ]
        return hashlib.sha256(canonical({key: evaluation[key] for key in fields}).encode()).hexdigest()

    def run(self):
        status = self.call(self.fabric, "GET", "/api/history/publication/status", auth=self.read).json()
        self.check(
            status["is_demo"] is True
            and status["namespace"].startswith("synthetic.")
            and status["source_database"] == "fabric_source_acceptance",
            "server_confirms_isolated_teaching_source_before_writes",
        )
        monitor = self.call(self.radar, "GET", "/api/engine/monitor/status", auth=self.user).json()
        self.check(monitor["enabled"], "real_monitor_is_enabled")
        if monitor["paused"]:
            self.call(
                self.radar,
                "POST",
                "/api/engine/monitor/control",
                auth=self.user,
                key=self.prefix + "-resume-engine",
                body={"expected_revision": monitor["control_revision"], "paused": False},
            )
        documents = []
        today = datetime.now(UTC)
        # The three most recent closed months; older backfill must not look like a current trend.
        first_month = today.year * 12 + today.month - 1 - 3
        for month in range(1, 4):
            ordinal = first_month + month - 1
            year, calendar_month = ordinal // 12, ordinal % 12 + 1
            for index in range(4):
                phrase = (
                    "photon hydrogel macrophage targeting"
                    if index < (1, 2, 4)[month - 1]
                    else "carrier stability instrument"
                )
                documents.append(
                    record(
                        self.prefix + "-" + str(month) + "-" + str(index),
                        "document",
                        {
                            "title": "教学专题 " + self.prefix + " " + str(month) + "-" + str(index),
                            "summary": f"Explicit synthetic material {month}-{index}. "
                            + phrase
                            + ". Independent replication not assessed.",
                            "source_published_at": f"{year:04d}-{calendar_month:02d}-10T00:00:00Z",
                        },
                    )
                )
        self.archived(documents, "-initial")
        create = {
            "is_demo": True,
            "definition": {
                "title": "教学验收：跨月积累 " + self.prefix,
                "question": "这组教学资料中的文字变化能否持续积累，并保留未知独立性？",
                "domain": "research",
                "source_instance": "fixture",
                "source_ids": ["external_archive"],
                "source_sites": [],
                "terms_all": [],
                "terms_any": [self.prefix],
                "profiles": ["text_exposure"],
                "metric_ids": [],
                "bucket": "month",
                "lookback_buckets": 24,
                "cadence_hours": 24,
                "minimum_support": 2,
            },
        }
        topic = self.call(
            self.radar,
            "POST",
            "/api/engine/topics",
            auth=self.user,
            body=create,
            key=self.prefix + "-create",
        ).json()
        identifier = topic["id"]
        first = self.wait(
            lambda: next(
                (
                    item
                    for item in self.evaluations(identifier)
                    if item["coverage"]["seed_complete"] and item["coverage"]["active_records"] == 12
                ),
                None,
            ),
            "topic did not seed all preexisting source records automatically",
        )
        self.check(
            self.topic(identifier)["observation_count"] >= 12
            and sum(item["exposures"] for item in first["buckets"]) == 12,
            "twelve_cross_month_contributions_survive_a_ten_record_monitor_window",
        )
        self.check(
            not first["coverage"]["original_source_coverage_known"]
            and all(bucket["confirmed_study_units"] == 0 for bucket in first["buckets"]),
            "adopted_counts_do_not_forge_domain_coverage_or_independent_studies",
        )
        self.check(first["automation_status"] == "not_needed",
                   "initial_archive_builds_a_baseline_without_claiming_new_progress")
        first_hash = self.frozen_hash(first)
        evidence = first["evidence"][0]
        base = "/api/engine/topics/" + identifier + "/evaluations/"
        original = self.call(
            self.radar, "GET", base + first["id"] + "/evidence/" + evidence["key"], auth=self.user
        ).json()
        original = original.get("record", original)
        self.check(original["raw_sha256"] == evidence["raw_sha256"], "fixed_evidence_reads_original_version")

        paused = self.change_control(identifier, True)
        before = paused["observation_count"]
        later = record(
            self.prefix + "-later",
            "document",
            {
                "title": "教学专题 " + self.prefix + " later",
                "summary": "photon hydrogel macrophage targeting",
                # A newly adopted item changes the last closed period's evidence.
                # Its old public date is retained; this is not labelled a newly occurred event.
                "source_published_at": f"{year:04d}-{calendar_month:02d}-20T00:00:00Z",
            },
        )
        self.archived([later], "-while-paused")
        time.sleep(6)
        self.check(
            self.topic(identifier)["observation_count"] == before,
            "paused_topic_does_not_consume_new_source_record",
        )
        self.change_control(identifier, False)
        self.wait(
            lambda: self.topic(identifier)["observation_count"] > before,
            "topic did not catch up after resuming",
        )
        self.check(True, "resume_catches_up_from_own_durable_cursor")
        followed = self.wait(
            lambda: next((item for item in self.evaluations(identifier)
                          if item["observation_revision"] > first["observation_revision"]
                          and item["investigation_id"] and item["automation_status"] == "saved"), None),
            "new evidence after the baseline did not enter an automatic topic investigation",
        )
        investigation = self.call(
            self.radar, "GET", "/api/engine/investigations/" + followed["investigation_id"], auth=self.user
        ).json()
        self.check(
            investigation["judgment_count"] == 0
            and any(entry["payload"].get("automation", {}).get("source") == "topic"
                    for entry in investigation["entries"]),
            "slow_signal_enters_real_investigation_without_forging_expert_judgment",
        )


        updated = copy.deepcopy(create)
        current = self.topic(identifier)
        updated["definition"]["title"] = "教学验收：新范围 " + self.prefix
        updated["definition"]["terms_any"] = []
        updated["definition"]["terms_all"] = [self.prefix, "later"]
        updated["expected_revision"] = current["control_revision"]
        revised = self.call(
            self.radar,
            "PUT",
            "/api/engine/topics/" + identifier,
            auth=self.user,
            body=updated,
            key=self.prefix + "-change-scope",
        ).json()
        self.check(
            revised["id"] == identifier and revised["scope_revision"] == 2,
            "scope_change_keeps_topic_identity",
        )
        second = self.wait(
            lambda: next(
                (
                    item
                    for item in self.evaluations(identifier)
                    if item["scope_revision"] == 2
                    and item["coverage"]["seed_complete"]
                    and item["coverage"]["active_records"] == 1
                ),
                None,
            ),
            "new scope did not independently seed already acknowledged source data",
        )
        old = self.call(self.radar, "GET", base + first["id"], auth=self.user).json()
        exported = self.call(self.radar, "GET", base + first["id"] + "/export", auth=self.user).json()
        self.check(
            self.frozen_hash(old) == first_hash == self.frozen_hash(exported)
            and old["definition"]["title"] == create["definition"]["title"],
            "scope_change_preserves_old_observation_question_and_export",
        )
        self.change_control(identifier, True)
        return {
            "checks": self.checks,
            "topic_id": identifier,
            "evaluation_ids": [first["id"], second["id"]],
            "investigation_id": followed["investigation_id"],
            "teaching_prefix": self.prefix,
            "limitation": "Synthetic engineering acceptance; biomedical accuracy is not evaluated.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fabric-url", required=True)
    parser.add_argument("--radar-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    exercise = TopicExercise(args.fabric_url, args.radar_url)
    try:
        result = exercise.run()
    finally:
        exercise.close()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checks_passed": len(result["checks"]), "topic_id": result["topic_id"]}))


if __name__ == "__main__":
    main()
