"""Exercise Fabric → HTTP → Radar → PostgreSQL with a labelled historical example.

Run only against disposable test databases. Credentials are read from environment.
The synthetic cohort checks engineering and arithmetic, never real predictive value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from tests.query_policy_assertions import assert_query_policy


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def archive_body(records, available_at, scope, *, reviewed=True):
    ordered = sorted(records, key=lambda record: record["source_record_id"])
    manifest = [{"source_record_id": r["source_record_id"], "raw_content": r["raw_content"]} for r in ordered]
    mapping_ids = [r["source_record_id"] for r in records if r["record_kind"] == "program"]
    return {
        "source_id": "external_archive",
        "is_demo": True,
        "scope_description": scope,
        "declared_count": len(records),
        "declared_complete": True,
        "records": records,
        "proof": {
            "available_at": {
                "value": available_at,
                "precision": "instant",
                "meaning": "synthetic_archive_available_at",
            },
            "uri": "fixture://bioradar-history-e2e/" + scope,
            "sha256": hashlib.sha256(canonical(manifest).encode()).hexdigest(),
            "reviewed": reviewed,
            "review_note": "明确合成材料；只检验工程与计算，不是实际历史档案。",
        },
        "verified_mapping_ids": mapping_ids if reviewed else [],
        "mapping_review_basis": "合成项目、适应证及法域逐项固定；不对应现实药物。" if reviewed else "",
    }


def record(identifier, kind, facts):
    raw = {"record_kind": kind, "title": "教学材料 " + identifier, **facts}
    return {"source_record_id": identifier, "record_kind": kind, "raw_content": canonical(raw)}


def historical_fixture(prefix):
    """Two predeclared dated cohorts, not today's winner list copied into the past."""
    old = [prefix + "-OLD-" + str(i) for i in range(5)]
    new = [prefix + "-NEW-" + str(i) for i in range(4)]

    def program(identifier, date, status="ACTIVE"):
        return record(
            identifier,
            "program",
            {
                "program_id": identifier,
                "lineage_id": identifier + "-LINEAGE",
                "indication": "教学适应证",
                "jurisdiction": "DEMO",
                "mapping_available_at": date,
                "phase": ["PHASE2"],
                "status": status,
            },
        )

    def outcome(identifier, suffix, positive, public, event=None, end="2017-01-02T00:00:00Z"):
        return record(
            identifier + suffix,
            "outcome",
            {
                "event_type": "regulatory_approval",
                "unit_kind": "program",
                "unit_id": identifier,
                "event_observed": positive,
                "event_at": event,
                "public_available_at": public,
                "observation_start": "2016-01-01T00:00:00Z" if identifier in old else "2018-01-01T00:00:00Z",
                "observation_end": end,
            },
        )

    initial = [program(i, "2015-12-31T00:00:00Z") for i in old]
    known = [
        outcome(old[0], "-OUTCOME", True, "2016-06-01T00:00:00Z", "2016-06-01T00:00:00Z"),
        outcome(old[1], "-OUTCOME", True, "2016-07-01T00:00:00Z", "2016-07-01T00:00:00Z"),
        outcome(old[2], "-OUTCOME", False, "2017-02-01T00:00:00Z"),
        outcome(old[3], "-OUTCOME", False, "2017-02-01T00:00:00Z"),
    ]
    later = [
        program(i, "2015-12-31T00:00:00Z", "APPROVED" if n < 2 else "DISCONTINUED") for n, i in enumerate(old)
    ]
    later += [program(i, "2017-12-31T00:00:00Z") for i in new]
    late_training = [outcome(old[4], "-LATE", True, "2018-03-01T00:00:00Z", "2016-07-01T00:00:00Z")]
    early = [
        outcome(
            new[0], "-OUTCOME", True, "2018-06-01T00:00:00Z", "2018-06-01T00:00:00Z", "2018-06-01T00:00:00Z"
        ),
        program(new[0], "2017-12-31T00:00:00Z", "APPROVED"),
    ]
    final = [outcome(new[1], "-OUTCOME", False, "2019-02-01T00:00:00Z", end="2019-01-02T00:00:00Z")]
    delayed = [outcome(new[2], "-OUTCOME", False, "2020-02-01T00:00:00Z", end="2019-01-02T00:00:00Z")]
    return {
        "old": old,
        "new": new,
        "batches": [
            ("training_scope", "2015-12-31T00:00:00Z", initial),
            ("training_outcomes", "2017-02-01T00:00:00Z", known),
            ("prediction_scope", "2017-12-31T00:00:00Z", later),
            ("late_training_label", "2018-03-01T00:00:00Z", late_training),
            ("early_outcome", "2018-06-01T00:00:00Z", early),
            ("mature_outcome", "2019-02-01T00:00:00Z", final),
            ("later_evaluation", "2020-02-01T00:00:00Z", delayed),
        ],
    }


class Exercise:
    def __init__(self, fabric_url, radar_url, *, read_only_sources=False, policy=None, forbidden_versions=()):
        for url in (fabric_url, radar_url):
            if urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Use loopback test services; this script writes demonstration data")
        self.read_only_sources = read_only_sources
        self.policy = policy
        self.forbidden_versions = set(forbidden_versions)
        self.fabric = httpx.Client(base_url=fabric_url, timeout=150, trust_env=False)
        self.radar = httpx.Client(base_url=radar_url, timeout=150, trust_env=False)
        self.read = {"Authorization": "Bearer " + os.environ["DATAFABRIC_HISTORY_READ_TOKEN"]}
        self.admin = (
            {"Authorization": "Bearer " + os.environ["DATAFABRIC_HISTORY_ADMIN_TOKEN"]}
            if not read_only_sources
            else {}
        )
        self.user = {"Authorization": "Bearer " + os.environ["BIORADAR_ENGINE_DEV_CREDENTIAL"]}
        self.prefix = "DEMO-HISTORY-" + uuid4().hex[:12]
        self.checks = []

    def call(self, client, method, path, *, auth, body=None, params=None, key=None, expected=(200, 201)):
        if self.read_only_sources and client is self.fabric and method not in {"GET", "HEAD"}:
            raise AssertionError("existing-source mode only permits read-only Fabric requests")
        headers = dict(auth)
        if key:
            headers["Idempotency-Key"] = key
        response = client.request(method, path, json=body, params=params, headers=headers)
        if response.status_code not in expected:
            # Response bodies may contain source content; keep routine failure output bounded.
            detail = response.text[:1200]
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}: {detail}")
        return response

    def check(self, condition, name):
        if not condition:
            raise AssertionError(name)
        self.checks.append(name)

    def guard_existing(self):
        from bioradar.api.config import Settings

        settings = Settings()
        database = urlsplit(settings.database_url.get_secret_value()).path.removeprefix("/")
        self.check(
            settings.environment == "test"
            and database.startswith(("bioradar_query_", "bioradar_research_", "bioradar_semantic_")),
            "client_uses_an_isolated_research_database",
        )
        runtime = (
            self.call(
                self.radar,
                "GET",
                "/api/engine/investigations/research-options",
                auth=self.user,
            )
            .json()
            .get("runtime", {})
        )
        self.check(
            runtime.get("environment") == "test"
            and runtime.get("database_name") == database
            and runtime.get("workspace_id") == settings.engine_workspace_id,
            "api_confirms_the_same_isolated_database_and_workspace_before_writes",
        )

    def wait_semantic(self, investigation_id, analysis_id):
        deadline = time.monotonic() + 1900
        while time.monotonic() < deadline:
            view = self.call(
                self.radar,
                "GET",
                f"/api/engine/investigations/{investigation_id}/analyses/{analysis_id}",
                auth=self.user,
            ).json()
            if view["state"] in {"completed", "needs_review", "blocked", "failed"}:
                return view
            time.sleep(3)
        raise TimeoutError("Inspect the existing analysis receipt before another attempt")

    def study(self, body, suffix):
        return self.call(
            self.radar,
            "POST",
            "/api/engine/history/studies",
            auth=self.user,
            body=body,
            key=self.prefix + suffix,
        ).json()

    def evaluate(self, study, cutoff, suffix):
        return self.call(
            self.radar,
            "POST",
            f"/api/engine/history/studies/{study['id']}/evaluations",
            auth=self.user,
            body={"cutoff": cutoff},
            key=self.prefix + suffix,
        ).json()

    def demo(self):
        fixture = historical_fixture(self.prefix)
        runs = {}
        for name, date, records in fixture["batches"]:
            body = archive_body(records, date, self.prefix + "-" + name)
            runs[name] = self.call(
                self.fabric,
                "POST",
                "/api/history/archives",
                auth=self.admin,
                body=body,
                key=self.prefix + "-" + name,
            ).json()
        train_run, cohort_run = runs["training_scope"]["id"], runs["prediction_scope"]["id"]
        params = {
            "source_id": "external_archive",
            "is_demo": "true",
            "mode": "public_history_reconstruction",
            "cutoff": "2018-01-01T00:00:00Z",
            "run_id": cohort_run,
            "limit": 2,
        }
        collected, cursor = [], None
        while True:
            page = self.call(
                self.fabric,
                "GET",
                "/api/history/records",
                auth=self.read,
                params={**params, **({"cursor": cursor} if cursor else {})},
            ).json()
            collected.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.check(len(collected) == 9, "frozen_manifest_pagination_returns_nine_at_cutoff")
        first_new = next(r for r in collected if r["source_record_id"] == fixture["new"][0])
        self.check(first_new["facts"]["status"] == "ACTIVE", "future_program_version_excluded")
        proof_time = first_new["availability_proof"]["available_at"]["value"]
        self.check(proof_time.startswith("2017"), "reviewed_version_uses_its_own_history_proof")
        version_path = "/api/history/versions/" + first_new["version_id"]
        binding = {k: params[k] for k in ("source_id", "is_demo", "mode", "cutoff")}
        binding["source_record_id"] = first_new["source_record_id"]
        original = self.call(self.fabric, "GET", version_path, auth=self.read, params=binding).json()
        original = original.get("record", original)
        self.check(original["raw_content"] == first_new["raw_content"], "adopted_old_original_readable")
        body = {
            "title": "教学：按2018年资料估计12个月内获批披露",
            "question": "用2016年独立项目作为基准，哪些项目值得进一步核查？",
            "source_id": "external_archive",
            "is_demo": True,
            "mode": "public_history_reconstruction",
            "cutoff": "2018-01-01T00:00:00Z",
            "target": "regulatory_approval",
            "horizon_months": 12,
            "training_runs": [{"cutoff": "2016-01-01T00:00:00Z", "run_id": train_run}],
            "run_id": cohort_run,
            "minimum_training_units": 3,
            "review_budget": 2,
            "followup_queries": 2,
        }
        study = self.study(body, "-study")
        self.check(study["state"] in {"predicted", "partial"}, "study_completed_with_supported_baseline")
        predictions = [p for p in study["predictions"] if p["probability"] is not None]
        self.check(
            {p["unit"]["unit_id"] for p in predictions} == set(fixture["new"]),
            "only_four_new_at_risk_programs_predicted",
        )
        self.check(
            all(
                p["probability"] == 0.5 and p["training_units"] == 4 and p["training_events"] == 2
                for p in predictions
            ),
            "two_of_four_mature_training_events_late_label_excluded",
        )
        repeated = self.study(body, "-study")
        self.check(repeated["id"] == study["id"], "same_request_reuses_saved_prediction")
        self.call(
            self.radar,
            "POST",
            "/api/engine/history/studies",
            auth=self.user,
            body={**body, "horizon_months": 24},
            key=self.prefix + "-study",
            expected=(409,),
        )
        self.check(True, "same_key_changed_request_rejected")
        early = self.evaluate(study, "2018-08-01T00:00:00Z", "-early")
        self.check(
            early["metrics"]["evaluable_count"] == 0 and early["metrics"]["brier_score"] is None,
            "immature_positive_not_scored_selectively",
        )
        mature = self.evaluate(study, "2019-03-01T00:00:00Z", "-mature")
        metrics = mature["metrics"]
        self.check(
            metrics["evaluable_count"] == 2 and metrics["events"] == 1 and metrics["non_events"] == 1,
            "explicit_positive_and_covered_negative_only",
        )
        self.check(
            metrics["brier_score"] == 0.25 and metrics["evaluation_coverage"] == 0.5,
            "brier_one_quarter_coverage_one_half",
        )
        later = self.evaluate(study, "2020-03-01T00:00:00Z", "-later")
        self.check(later["metrics"]["evaluable_count"] == 3, "later_disclosure_appends_new_evaluation")
        reread = self.call(
            self.radar, "GET", f"/api/engine/history/studies/{study['id']}", auth=self.user
        ).json()
        self.check(
            reread["prediction_hash"] == study["prediction_hash"]
            and reread["predictions"] == study["predictions"],
            "predictions_unchanged_after_evaluation",
        )
        old_eval = self.call(
            self.radar,
            "GET",
            f"/api/engine/history/studies/{study['id']}/evaluations/{mature['id']}",
            auth=self.user,
        ).json()
        self.check(old_eval["metrics"] == metrics, "earlier_evaluation_preserved")
        exported = self.call(
            self.radar, "GET", f"/api/engine/history/studies/{study['id']}/export", auth=self.user
        ).text
        self.check(
            bool(exported.strip()) and fixture["new"][0] in exported,
            "saved_research_export_contains_actual_program",
        )
        limited = self.study({**body, "max_records": 2, "followup_queries": 0}, "-limited")
        self.check(
            not any(p["probability"] is not None for p in limited["predictions"]),
            "truncated_cohort_cannot_return_complete_prediction",
        )
        forged = archive_body(
            [record(self.prefix + "-FORGED", "document", {"text": "教学"})],
            "2017-01-01T00:00:00Z",
            self.prefix + "-forged",
        )
        forged["records"][0]["first_seen_at"] = "2010-01-01T00:00:00Z"
        self.call(
            self.fabric,
            "POST",
            "/api/history/archives",
            auth=self.admin,
            body=forged,
            key=self.prefix + "-forged",
            expected=(422,),
        )
        self.check(True, "caller_cannot_backdate_first_seen")
        return {
            "is_demo": True,
            "study_id": study["id"],
            "investigation_id": study["investigation_id"],
            "prediction_hash": study["prediction_hash"],
            "metrics": metrics,
            "evaluations": [early["id"], mature["id"], later["id"]],
            "scope": "合成历史队列，只验证跨服务行为与已知算术，不评价真实预测能力。",
        }

    def public(self):
        output = []
        for source, query, ids in [
            ("clinicaltrials", "NCT03758417", ["NCT03758417"]),
            ("fda_crl", "*", []),
            ("europe_pmc", "EXT_ID:32511943 AND SRC:MED", []),
        ]:
            run = self.call(
                self.fabric,
                "POST",
                "/api/history/fetch",
                auth=self.admin,
                body={
                    "source_id": source,
                    "query": query,
                    "source_record_ids": ids,
                    "max_records": 2,
                    "max_pages": 1,
                    "deadline_seconds": 40,
                },
                key=self.prefix + "-fetch-" + source,
                expected=(200, 201, 202),
            ).json()
            until = time.monotonic() + 80
            while run["state"] in {"queued", "running"} and time.monotonic() < until:
                time.sleep(0.5)
                run = self.call(self.fabric, "GET", "/api/history/runs/" + run["id"], auth=self.read).json()
            self.check(
                run["state"] in {"completed", "partial"} and bool(run["version_ids"]),
                source + "_actual_public_fetch_persisted",
            )
            cutoff = datetime.now(UTC).isoformat()
            study = self.study(
                {
                    "title": "公开资料联调：" + source,
                    "question": "核对来源内容，继续查找关联资料和缺口。",
                    "source_id": source,
                    "is_demo": False,
                    "mode": "current",
                    "cutoff": cutoff,
                    "target": "trial_stoppage",
                    "horizon_months": 12,
                    "training_cutoffs": [],
                    "run_id": run["id"],
                    "followup_queries": 1,
                },
                "-" + source,
            )
            self.check(
                study["state"] != "failed" and bool(study["investigation_id"]) and bool(study["sources"]),
                source + "_actual_adoption_investigation_saved",
            )
            self.check(
                not any(p["probability"] is not None for p in study["predictions"]),
                source + "_no_invented_historical_prediction",
            )
            exported = self.call(
                self.radar,
                "GET",
                f"/api/engine/history/studies/{study['id']}/export",
                auth=self.user,
            ).text
            rows = (study.get("fact_summary") or {}).get("rows", [])
            self.check(
                bool(rows) and all(row["fields"] for row in rows), source + "_readable_source_facts_returned"
            )
            first = rows[0]
            self.check(
                first["source_record_id"] in exported and first["version_id"] in exported,
                source + "_export_contains_actual_source_and_version",
            )
            original = self.call(
                self.radar,
                "GET",
                f"/api/engine/history/studies/{study['id']}/evidence/{first['version_id']}",
                auth=self.user,
            ).json()
            self.check(
                hashlib.sha256(original["raw_content"].encode()).hexdigest() == first["raw_sha256"],
                source + "_saved_original_matches_adopted_content",
            )
            for mode in ("operational_replay", "public_history_reconstruction"):
                historical = self.call(
                    self.fabric,
                    "GET",
                    "/api/history/records",
                    auth=self.read,
                    params={
                        "source_id": source,
                        "is_demo": "false",
                        "mode": mode,
                        "cutoff": "2018-01-01T00:00:00Z",
                        "run_id": run["id"],
                    },
                ).json()
                self.check(not historical["items"], source + "_" + mode + "_cannot_backdate_current_body")
            output.append(
                {
                    "source": source,
                    "run_id": run["id"],
                    "run_state": run["state"],
                    "versions": len(run["version_ids"]),
                    "study_id": study["id"],
                    "investigation_id": study["investigation_id"],
                    "state": study["state"],
                    "coverage": run["coverage"],
                }
            )
        return output

    def existing(self, request):
        self.guard_existing()
        self.check(request.get("is_demo") is False, "existing_source_case_uses_real_records")
        study = self.study({**request, "followup_queries": 0}, "-existing-study")
        self.check(bool(study.get("investigation_id")), "fixed_study_saved_an_investigation")
        prediction = {"prediction_hash": study["prediction_hash"], "predictions": study["predictions"]}
        started = self.call(
            self.radar,
            "POST",
            f"/api/engine/history/studies/{study['id']}/analyses",
            auth=self.user,
            key=self.prefix + "-existing-analysis",
            body={"query_policy": self.policy},
            expected=(200, 202),
        ).json()
        view = self.wait_semantic(started["investigation_id"], started["id"])
        summary = assert_query_policy(view, expected_policy=self.policy)
        self.check(
            view["state"] in {"completed", "needs_review"}, "historical_semantic_analysis_saved_a_result"
        )
        scope = view["query_policy"].get("evidence_time", {})
        self.check(
            scope.get("mode") == study["request"]["mode"]
            and datetime.fromisoformat(scope["cutoff"].replace("Z", "+00:00"))
            == datetime.fromisoformat(study["cutoff"].replace("Z", "+00:00"))
            and scope.get("study_id") == study["id"]
            and scope.get("run_id") == study["request"].get("run_id"),
            "policy_uses_the_frozen_study_mode_cutoff_and_manifest",
        )
        if view.get("parent_analysis_id"):
            parent = self.call(
                self.radar,
                "GET",
                f"/api/engine/investigations/{view['investigation_id']}/analyses/{view['parent_analysis_id']}",
                auth=self.user,
            ).json()
            self.check(
                (parent.get("query_policy") or {}).get("evidence_time") == scope,
                "as_of_analysis_does_not_import_an_incompatible_current_parent",
            )
        originals = []
        for evidence in view["evidence"]:
            binding = evidence["history_binding"]
            self.check(bool(binding), "historical_semantic_evidence_has_a_fixed_original")
            record = self.call(
                self.fabric,
                "GET",
                "/api/history/versions/" + binding["version_id"],
                auth=self.read,
                params={
                    "source_id": binding["source_id"],
                    "source_record_id": binding["source_record_id"],
                    "is_demo": str(binding["is_demo"]).lower(),
                    "mode": scope["mode"],
                    "cutoff": scope["cutoff"],
                },
            ).json()
            record = record.get("record", record)
            self.check(record["record_kind"] != "outcome", "prediction_inputs_exclude_outcome_records")
            self.check(
                binding["version_id"] not in self.forbidden_versions, "known_future_version_was_not_used"
            )
            self.check(
                record["version_id"] == binding["version_id"]
                and hashlib.sha256(record["raw_content"].encode()).hexdigest() == binding["raw_sha256"],
                "each_semantic_original_is_readable_under_the_same_as_of_boundary",
            )
            originals.append(binding["version_id"])
        reread = self.call(
            self.radar,
            "GET",
            f"/api/engine/history/studies/{study['id']}",
            auth=self.user,
        ).json()
        self.check(
            all(reread[key] == value for key, value in prediction.items()),
            "modern_semantic_analysis_preserves_the_fixed_historical_prediction",
        )
        self.check(
            any(item["id"] == view["id"] for item in reread.get("semantic_analyses", [])),
            "study_links_to_the_actual_saved_semantic_analysis",
        )
        exported = self.call(
            self.radar,
            "GET",
            f"/api/engine/investigations/{view['investigation_id']}/analyses/{view['id']}/export",
            auth=self.user,
        ).json()["analysis"]
        self.check(
            exported["query_policy"] == view["query_policy"] and exported["result"] == view["result"],
            "historical_semantic_export_keeps_the_exact_saved_trace_and_result",
        )
        return {
            "study_id": study["id"],
            "analysis_id": view["id"],
            "query_policy": summary,
            "evidence_time": scope,
            "version_ids": originals,
            "scientific_review": "pending_independent_review",
            "qualification": (
                "Modern model with as-of source inputs; not a model deployed at the historical date."
            ),
        }

    def close(self):
        self.fabric.close()
        self.radar.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fabric-url", required=True)
    parser.add_argument("--radar-url", required=True)
    parser.add_argument("--mode", choices=("demo", "public", "all", "existing"), default="all")
    parser.add_argument("--output", type=Path, default=Path("artifacts/history-e2e.json"))
    parser.add_argument(
        "--request-file",
        type=Path,
        help="Existing source study request; never imports or fetches Fabric data",
    )
    parser.add_argument("--policy", choices=("baseline", "query_planner"))
    parser.add_argument("--forbidden-version", action="append", default=[])
    args = parser.parse_args()
    if args.mode == "existing" and not args.request_file:
        parser.error("--mode existing requires --request-file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    exercise = Exercise(
        args.fabric_url,
        args.radar_url,
        read_only_sources=args.mode == "existing",
        policy=args.policy,
        forbidden_versions=args.forbidden_version,
    )
    result = {"started_at": datetime.now(UTC).isoformat(), "status": "running", "checks": exercise.checks}
    try:
        if args.mode == "existing":
            result["existing"] = exercise.existing(json.loads(args.request_file.read_text()))
        if args.mode in {"demo", "all"}:
            result["demo"] = exercise.demo()
        if args.mode in {"public", "all"}:
            result["public"] = exercise.public()
        result["status"] = "technical_checks_passed" if args.mode == "existing" else "passed"
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        exercise.close()
        if result["status"] == "running":
            result["status"] = "failed"
        result["finished_at"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"status": result["status"], "checks_passed": len(exercise.checks), "output": str(args.output)}
        )
    )


if __name__ == "__main__":
    main()
