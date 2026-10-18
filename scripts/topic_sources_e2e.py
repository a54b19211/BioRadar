"""Isolated HTTP/RQ domain acceptance: declared versus bound metrics, trials and real WHO.

Only topic CRUD and Fabric archive HTTP writes are used. The already-running
publisher/worker/monitor performs the work; this script never calls internal tick.
Teaching material and real public WHO notices remain separate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from history_e2e import canonical
from topic_e2e import TopicExercise


class SourceExercise(TopicExercise):
    def __init__(self, fabric_url, radar_url):
        if urlsplit(fabric_url).port != 18513 or urlsplit(radar_url).port != 18510:
            raise ValueError("Use the designated isolated topic-acceptance service ports")
        super().__init__(fabric_url, radar_url)
        self.phase = "guard"
        self.created_topics = []
        self.domain_results = {}

    def guard(self):
        state = self.call(self.fabric, "GET", "/api/history/publication/status", auth=self.read).json()
        self.check(
            state["is_demo"] is True
            and state["namespace"].startswith("synthetic.")
            and state["source_database"] == "fabric_source_acceptance",
            "server_confirms_isolated_teaching_database_before_writes",
        )
        monitor = self.call(self.radar, "GET", "/api/engine/monitor/status", auth=self.user).json()
        self.check(monitor["enabled"] and not monitor["paused"], "real_background_monitor_is_running")
        self.templates = {
            item["id"]: item
            for item in self.call(self.radar, "GET", "/api/engine/topics/templates", auth=self.user).json()[
                "items"
            ]
        }

    def create_from_template(self, domain, marker, *, demo, sources):
        definition = copy.deepcopy(self.templates[domain]["definition"])
        original_profiles, original_sites = list(definition["profiles"]), list(definition["source_sites"])
        definition.update(
            title=("教学领域验收 " if demo else "真实WHO公告验收 ") + self.prefix + " " + domain,
            source_instance="fixture" if demo else "datafabric-test",
            source_ids=sources,
            terms_all=[self.prefix, marker] if marker else [],
            terms_any=[],
        )
        self.check(
            definition["profiles"] == original_profiles and definition["source_sites"] == original_sites,
            domain + ("_demo" if demo else "_real") + "_keeps_template_profiles_and_sites",
        )
        value = self.call(
            self.radar,
            "POST",
            "/api/engine/topics",
            auth=self.user,
            key=self.prefix + "-topic-" + domain + ("-demo" if demo else "-real"),
            body={"is_demo": demo, "definition": definition},
        ).json()
        self.created_topics.append(value["id"])
        return value["id"]

    def import_rows(self, source, rows, suffix, *, expected=(200, 201)):
        body = {
            "source_id": source,
            "is_demo": True,
            "scope_description": "Explicit synthetic domain acceptance " + self.prefix,
            "declared_count": len(rows),
            "declared_complete": False,
            "records": rows,
        }
        return self.call(
            self.fabric,
            "POST",
            "/api/history/archives",
            auth=self.admin,
            key=self.prefix + suffix,
            body=body,
            expected=expected,
        )

    def raw_row(self, identifier, value, *, kind="document"):
        return {"source_record_id": identifier, "record_kind": kind, "raw_content": canonical(value)}

    def read_version(self, run, source, native, *, demo=True):
        return self.call(
            self.fabric,
            "GET",
            "/api/history/versions/" + run["version_ids"][0],
            auth=self.read,
            params={
                "source_id": source,
                "source_record_id": native,
                "is_demo": str(demo).lower(),
                "mode": "current",
                "cutoff": datetime.now(UTC).isoformat(),
            },
        ).json()

    def metric_pair(self, marker, index, start, end, amount, *, denominator=None):
        native = self.prefix + "-" + marker + "-native-" + str(index)
        subject = self.prefix + "-" + marker + "-unit"
        unit = "USD" if marker == "business" else "reported_count"
        value = {
            "record_kind": "document",
            "title": self.prefix + " " + marker + " original " + str(index),
            "subject_id": subject,
            "metric_id": marker + "_reported_value",
            "metric_version": "SYNTHETIC-monthly-v1",
            "amount": amount,
            "unit": unit,
            "population": "SYNTHETIC fixed reporting scope",
            "region": "SYNTHETIC REGION",
            "start": start,
            "end": end,
            "measurement_basis": "reported",
            "status": "active",
        }
        if denominator is not None:
            value.update(denominator=denominator, denominator_unit="SYNTHETIC tested specimens")
        run = self.import_rows(
            "external_archive", [self.raw_row(native, value)], "-original-" + marker + str(index)
        ).json()
        original = self.read_version(run, "external_archive", native)
        observation = {
            "schema_version": "datafabric.observation.v1",
            "kind": "metric",
            "title": self.prefix + " " + marker + " bound observation " + str(index),
            "subject": {"namespace": "external_archive", "id": subject},
            "metric_id": value["metric_id"],
            "metric_version": value["metric_version"],
            "value": {"lower": str(amount), "upper": str(amount)},
            "unit": unit,
            "population": value["population"],
            "region": value["region"],
            "period_start": start,
            "period_end": end,
            "measurement_basis": "reported",
            "status": "active",
            "basis": {
                "kind": "source_bound",
                "ref": {
                    key: original[key]
                    for key in ("source_id", "source_record_id", "version_id", "raw_sha256")
                },
                "field_pointers": {
                    "subject.id": "/subject_id",
                    "metric_id": "/metric_id",
                    "metric_version": "/metric_version",
                    "value.lower": "/amount",
                    "value.upper": "/amount",
                    "unit": "/unit",
                    "population": "/population",
                    "region": "/region",
                    "period_start": "/start",
                    "period_end": "/end",
                    "measurement_basis": "/measurement_basis",
                    "status": "/status",
                },
            },
        }
        if denominator is not None:
            observation.update(
                denominator={"lower": str(denominator), "upper": str(denominator)},
                denominator_unit=value["denominator_unit"],
            )
            observation["basis"]["field_pointers"].update(
                {
                    "denominator.lower": "/denominator",
                    "denominator.upper": "/denominator",
                    "denominator_unit": "/denominator_unit",
                }
            )
        return self.raw_row(self.prefix + "-" + marker + "-metric-" + str(index), observation), original

    def wait_evaluation(self, topic, predicate, message):
        return self.wait(
            lambda: next(
                (
                    item
                    for item in self.evaluations(topic)
                    if item["coverage"]["seed_complete"] and predicate(item)
                ),
                None,
            ),
            message,
            seconds=1200,
        )

    def wait_investigation(self, topic, predicate=None):
        evaluation = self.wait_evaluation(
            topic,
            lambda item: (
                bool(item["investigation_id"])
                and item["automation_status"] == "saved"
                and (predicate is None or predicate(item))
            ),
            "background topic follow-up did not save an investigation",
        )
        view = self.call(
            self.radar, "GET", "/api/engine/investigations/" + evaluation["investigation_id"], auth=self.user
        ).json()
        materials = [entry for entry in view["entries"] if entry["kind"] == "material"]
        notes = [entry for entry in view["entries"] if entry["kind"] == "note"]
        self.check(bool(materials) and bool(notes), topic + "_has_materials_and_readable_notes")
        self.check(
            not any(entry["kind"] == "judgment" for entry in view["entries"]),
            topic + "_automation_does_not_write_human_judgment",
        )
        return evaluation, view

    def business(self):
        self.phase = "business"
        rows = []
        original = None
        for index, (start, end) in enumerate(
            [("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01"), ("2026-03-01", "2026-04-01")], 1
        ):
            row, original = self.metric_pair("business", index, start, end, index * 100)
            rows.append(row)
        forged = copy.deepcopy(rows[0])
        fake = json.loads(forged["raw_content"])
        fake["value"]["lower"] = fake["value"]["upper"] = "999"
        forged["source_record_id"] += "-forged"
        forged["raw_content"] = canonical(fake)
        rejected = self.import_rows("external_archive", [forged], "-wrong-value", expected=(422,)).json()
        self.check(
            "observation_binding_mismatch" in canonical(rejected), "wrong_metric_value_rejected_by_fabric"
        )
        # Two points form the baseline; a later third point must cause the inquiry.
        self.import_rows("external_archive", rows[:2], "-business-bound-baseline")
        # Identical arithmetic as a declaration must not gain source-bound qualification.
        declared = []
        for index, row in enumerate(rows):
            value = json.loads(row["raw_content"])
            value["title"] += " declared"
            value["subject"]["id"] += "-declared"
            value["basis"] = {"kind": "declared", "note": "Explicit unverified teaching declaration"}
            declared.append(self.raw_row(row["source_record_id"] + "-declared", value))
        self.import_rows("external_archive", declared, "-business-declared")
        topic = self.create_from_template("business", "business", demo=True, sources=["external_archive"])
        first = self.wait_evaluation(
            topic,
            lambda item: len(item["metric_series"]) >= 2,
            "business template did not compute typed original-bound metrics",
        )
        groups = {item["subject"]["id"]: item for item in first["metric_series"]}
        self.check(
            len(groups[self.prefix + "-business-unit"]["points"]) == 2,
            "two_bound_months_are_saved_before_the_new_arrival",
        )
        self.check(
            all(not p["qualified"] for p in groups[self.prefix + "-business-unit-declared"]["points"]),
            "declaration_never_becomes_source_bound",
        )
        frozen = self.frozen_hash(first)
        self.import_rows("external_archive", rows[2:], "-business-later")
        evaluation, view = self.wait_investigation(
            topic,
            lambda item: any(e["source_record_id"].endswith("-metric-3") for e in item["evidence"]),
        )
        bound = next(
            s for s in evaluation["metric_series"] if s["subject"]["id"] == self.prefix + "-business-unit"
        )
        self.check(
            bound["descriptive_slope"] is not None and len(bound["points"]) == 3,
            "three_comparable_reported_months_have_descriptive_slope",
        )
        self.check(
            all(point["qualified"] for point in bound["points"]), "bound_fields_are_qualified_literally"
        )
        self.check(
            any(s["kind"] == "metric_change" for s in evaluation["signals"]),
            "new_same_scope_metric_runs_automatic_followup",
        )
        evidence = next(e for e in evaluation["evidence"] if e["source_record_id"].endswith("-metric-3"))
        adopted = self.call(
            self.radar,
            "GET",
            f"/api/engine/topics/{topic}/evaluations/{evaluation['id']}/evidence/{evidence['key']}",
            auth=self.user,
        ).json()
        self.check(
            hashlib.sha256(adopted["raw_content"].encode()).hexdigest() == evidence["raw_sha256"],
            "metric_evidence_reads_exact_adopted_original",
        )
        old = self.call(
            self.radar, "GET", f"/api/engine/topics/{topic}/evaluations/{first['id']}", auth=self.user
        ).json()
        self.check(self.frozen_hash(old) == frozen, "later_numeric_data_does_not_rewrite_old_evaluation")
        self.change_control(topic, True)
        return {
            "topic_id": topic,
            "evaluation_id": evaluation["id"],
            "investigation_id": view["id"],
            "qualification": "synthetic literal-scope arithmetic only; no real company result",
        }

    def public_health(self):
        self.phase = "public_health_teaching"
        rows = []
        for index, (start, end) in enumerate(
            [("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01"), ("2026-03-01", "2026-04-01")], 1
        ):
            row, _ = self.metric_pair(
                "epidemic", index, start, end, index * 10, denominator=100 if index < 3 else 1000
            )
            rows.append(row)
        self.import_rows("external_archive", rows, "-health-bound")
        topic = self.create_from_template(
            "public_health", "epidemic", demo=True, sources=["external_archive"]
        )
        evaluation = self.wait_evaluation(
            topic, lambda item: bool(item["metric_series"]), "public-health template did not consume metrics"
        )
        self.check(
            evaluation["metric_series"][0]["descriptive_slope"] is None
            and not any(s["kind"] == "metric_change" for s in evaluation["signals"]),
            "changed_testing_denominator_prevents_comparable_trend",
        )
        self.check(
            "metric_denominator_changed_requires_review" in evaluation["data_gaps"],
            "denominator_drift_is_an_explicit_gap_not_incidence",
        )
        self.check(
            not any("incidence" in series["metric_id"] for series in evaluation["metric_series"]),
            "reported_counts_not_renamed_to_population_incidence",
        )
        self.change_control(topic, True)
        return {
            "topic_id": topic,
            "evaluation_id": evaluation["id"],
            "qualification": "synthetic reporting scope and denominator; no real outbreak/case rate",
        }

    def pipeline(self):
        self.phase = "pipeline_teaching"
        native = "NCT9" + str(int(hashlib.sha256(self.prefix.encode()).hexdigest(), 16) % 10000000).zfill(7)

        def trial(status):
            return {
                "protocolSection": {
                    "identificationModule": {
                        "nctId": native,
                        "briefTitle": self.prefix + " pipeline SYNTHETIC trial",
                    },
                    "descriptionModule": {
                        "briefSummary": self.prefix + " pipeline explicit teaching registry"
                    },
                    "statusModule": {
                        "overallStatus": status,
                        "studyFirstPostDateStruct": {"date": "2026-01-10", "type": "ACTUAL"},
                        "lastUpdatePostDateStruct": {"date": "2026-10-18", "type": "ACTUAL"},
                        **(
                            {"whyStopped": "SYNTHETIC administrative reason, not drug failure"}
                            if status == "TERMINATED"
                            else {}
                        ),
                    },
                    "designModule": {"phases": ["PHASE2"]},
                }
            }

        self.import_rows(
            "clinicaltrials", [self.raw_row(native, trial("RECRUITING"), kind="trial")], "-trial-first"
        )
        topic = self.create_from_template("pipeline", "pipeline", demo=True, sources=["clinicaltrials"])
        self.wait_evaluation(
            topic,
            lambda item: any(s["unit_id"] == native for s in item["registered_states"]),
            "pipeline template did not retain native registry identity",
        )
        self.import_rows(
            "clinicaltrials", [self.raw_row(native, trial("TERMINATED"), kind="trial")], "-trial-stopped"
        )
        evaluation, view = self.wait_investigation(
            topic,
            lambda item: any(
                s["unit_id"] == native and s["status"] == "TERMINATED" for s in item["registered_states"]
            ),
        )
        self.check(
            any(s["kind"] == "state_change" for s in evaluation["signals"]),
            "native_trial_state_change_creates_ordinary_investigation",
        )
        self.check(
            any(
                s["unit_id"] == native and s["status"] == "TERMINATED"
                for s in evaluation["registered_states"]
            ),
            "termination_is_registration_status_not_drug_failure",
        )
        self.change_control(topic, True)
        return {
            "topic_id": topic,
            "trial_id": native,
            "evaluation_id": evaluation["id"],
            "investigation_id": view["id"],
            "qualification": "synthetic native registry fields; no drug-program mapping or efficacy judgment",
        }

    def who(self):
        self.phase = "real_who"
        page = self.call(
            self.fabric,
            "GET",
            "/api/history/records",
            auth=self.read,
            params={
                "source_id": "who_don",
                "is_demo": "false",
                "mode": "current",
                "cutoff": datetime.now(UTC).isoformat(),
                "limit": 100,
            },
        ).json()
        self.check(
            bool(page["items"]) and all(item["is_demo"] is False for item in page["items"]),
            "real_who_notices_remain_non_demo",
        )
        topic = self.create_from_template("public_health", None, demo=False, sources=["who_don"])
        evaluation, view = self.wait_investigation(topic)
        self.check(
            any(
                s["kind"] == "source_event" and s["details"]["event_type"] == "outbreak_notice_published"
                for s in evaluation["signals"]
            ),
            "real_who_notice_publication_triggers_followup",
        )
        self.check(not evaluation["metric_series"], "who_announcements_do_not_invent_case_metrics")
        evidence = next(e for e in evaluation["evidence"] if e["source_id"] == "who_don")
        adopted = self.call(
            self.radar,
            "GET",
            f"/api/engine/topics/{topic}/evaluations/{evaluation['id']}/evidence/{evidence['key']}",
            auth=self.user,
        ).json()
        original = json.loads(adopted["raw_content"])
        self.check(
            adopted["is_demo"] is False
            and original["Id"] == evidence["source_record_id"]
            and hashlib.sha256(adopted["raw_content"].encode()).hexdigest() == evidence["raw_sha256"],
            "real_who_evidence_keeps_native_id_and_frozen_hash",
        )
        notes = [entry["payload"].get("text", "") for entry in view["entries"] if entry["kind"] == "note"]
        self.check(
            any("追加查找" in text for text in notes),
            "real_who_investigation_keeps_actual_bounded_query_receipt",
        )
        self.check(
            not any("追加查找：outbreak_notice_published" in text for text in notes),
            "followup_does_not_search_an_internal_enum_as_if_it_were_source_text",
        )
        self.change_control(topic, True)
        return {
            "topic_id": topic,
            "evaluation_id": evaluation["id"],
            "investigation_id": view["id"],
            "native_notice_id": original["Id"],
            "version_id": evidence["version_id"],
            "qualification": "real selected WHO notices; no incidence or complete global coverage",
        }

    def run(self):
        self.guard()
        result = self.domain_results
        for name in ("business", "public_health", "pipeline", "who"):
            result[name] = getattr(self, name)()
            print("completed domain", name, flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fabric-url", default="http://127.0.0.1:18513")
    parser.add_argument("--radar-url", default="http://127.0.0.1:18510")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    exercise = SourceExercise(args.fabric_url, args.radar_url)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "checks": exercise.checks,
        "teaching_prefix": exercise.prefix,
    }
    failed = False
    try:
        report["domains"] = exercise.run()
        report["status"] = "passed"
    except Exception as exc:
        failed = True
        report.update(status="failed", phase=exercise.phase, error_type=type(exc).__name__)
    finally:
        report["topic_ids"] = exercise.created_topics
        report["domains"] = exercise.domain_results
        report["finished_at"] = datetime.now(UTC).isoformat()
        cleanup_errors = []
        for identifier in exercise.created_topics:
            try:
                if not exercise.topic(identifier)["paused"]:
                    exercise.change_control(identifier, True)
            except Exception as exc:
                cleanup_errors.append({"topic_id": identifier, "error_type": type(exc).__name__})
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
            report["status"] = "failed"
            failed = True
        exercise.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if failed:
        raise SystemExit("Domain source acceptance failed; inspect the private report and service status")
    print(f"PASS domain sources: {len(exercise.checks)} checks; report {args.output}")


if __name__ == "__main__":
    main()
