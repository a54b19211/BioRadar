"""Read-only acceptance of real saved KDD runs and explicitly approved report IDs.

Manifest: {"schema_version":"bioradar.kdd.acceptance.v1","fixed_sources":[
{"source_id":"...","source_record_id":"...","version_id":"...","raw_sha256":"...",
 "is_demo":false}],"cases":[{"id":"...","direction":"research","role":"pattern",
 "run_id":"...","analysis_id":"...","investigation_id":"..."}]}.
Roles are pattern, false_positive and missing_fields. Review labels stay here,
outside algorithm/model inputs. This script sends GET only and never starts a model.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.api.config import Settings
from bioradar.research.mining.models import NormalizedSnapshot, canonical_hash
from bioradar.research.mining.prepare import snapshot_hash
from research_e2e import ResearchExercise

DIRECTIONS = ("research", "pipeline", "business", "public_health")
ROLES = {"pattern", "false_positive", "missing_fields"}
DECISIONS = {"selected", "value_excluded", "capacity_deferred", "redundancy_deferred"}


def identity(ref):
    return canonical_hash(ref)


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def representation_scope(result):
    methods = {row["method_id"]: row for row in result["method_results"]}
    tfidf = (methods.get("research_tfidf_topics") or {}).get("statistics", {})
    embedding = (methods.get("research_embedding_clusters") or {}).get("statistics", {})
    text_ids = tfidf.get("processed_unit_ids")
    spaces = embedding.get("model_spaces")
    vector_ids = (
        {item for space in spaces for item in space["processed_unit_ids"]}
        if isinstance(spaces, list) and all("processed_unit_ids" in space for space in spaces)
        else None
    )
    text_ids = set(text_ids) if isinstance(text_ids, list) else None
    known = text_ids is not None and vector_ids is not None
    return {
        "same_saved_dataset": True,
        "tfidf_processed_units": sorted(text_ids) if text_ids is not None else None,
        "embedding_processed_units": sorted(vector_ids) if vector_ids is not None else None,
        "same_processed_units": text_ids == vector_ids if known else None,
        "shared_unit_count": len(text_ids & vector_ids) if known else None,
        "tfidf_fit_units": tfidf.get("fit_unit_ids"),
        "tfidf_fit_scope": tfidf.get("fit_scope"),
        "qualification": "Method eligibility subsets may differ; no performance improvement is inferred",
    }


def inspect_run(view, snapshot, case):
    """Validate saved boundaries, without rerunning the method being evaluated."""
    assert view["state"] in {"completed", "partial"}, "run_not_finished"
    result = view["result"]
    assert result["compute_version"] == "bioradar-mining-compute-v0.3", "not_a_kdd_run"
    assert result["result_hash"] == canonical_hash(
        {key: value for key, value in result.items() if key != "result_hash"}
    ), "saved_result_hash_mismatch"
    assert view["original_policy"]["kdd_direction"] == case["direction"], "wrong_direction"
    assert result["policy_hash"] == canonical_hash(view["original_policy"]), "policy_hash_mismatch"
    assert result["snapshot_hash"] == snapshot_hash(NormalizedSnapshot.model_validate(snapshot)), (
        "snapshot_hash_mismatch"
    )
    data = result["data_preparation"]
    assert data["direction"] == case["direction"], "dataset_direction_mismatch"
    cutoff = instant(view["original_policy"]["as_of"])
    assert instant(data["as_of"]) == cutoff, "dataset_cutoff_mismatch"
    assert data["time_mode"] == view["original_policy"]["time_mode"], "dataset_time_mode_mismatch"
    assert data["input_count"] == len(snapshot["records"]), "input_count_mismatch"
    assert data["qualified_count"] == len(data["units"]), "analysis_unit_count_mismatch"
    assert data["qualified_count_unit"] == "analysis_units_not_documents_or_population", (
        "ambiguous_count_unit"
    )
    unhashed = {"dataset_hash", "selection_summary", "dataset_hash_scope"}
    assert data["dataset_hash"] == canonical_hash({k: v for k, v in data.items() if k not in unhashed}), (
        "dataset_hash_mismatch"
    )
    assert data["denominator"]["population_total"] is None, "invented_population_total"
    assert data["denominator"]["population_rate_permitted"] is False, "unqualified_population_rate"
    records = {identity(record["record_ref"]): record for record in snapshot["records"]}
    units = set()
    for unit in data["units"]:
        assert unit["unit_id"] not in units, "duplicate_analysis_unit"
        units.add(unit["unit_id"])
        assert unit["members"], "unit_without_original_members"
        for member in unit["members"]:
            record = records[identity(member["record_ref"])]
            assert member["text_hash"] == record["normalized_text_hash"], "member_text_hash_mismatch"
            assert member["source_id"] == record["source_id"], "member_source_mismatch"
            assert member["source_instance"] == record["source_instance"], "member_instance_mismatch"
            assert instant(member["observed_at"]) == instant(record["observed_at"]), (
                "member_observed_time_mismatch"
            )
            assert instant(member.get("available_at")) == instant(record.get("available_at")), (
                "member_available_time_mismatch"
            )
            if data["time_mode"] == "availability_replay":
                assert instant(member.get("available_at") or member["observed_at"]) <= cutoff, (
                    "future_available_input"
                )
            elif record.get("publication_time_verified") and record.get("published_at"):
                assert instant(record["published_at"]) <= cutoff, "future_publication_input"
    for excluded in data["excluded_records"]:
        assert identity(excluded["record_ref"]) in records, "excluded_record_outside_snapshot"
        assert excluded["reason"], "exclusion_without_reason"
    generated = {}
    for method in result["method_results"]:
        for candidate in method["candidates"]:
            key = candidate["candidate_key"]
            assert key not in generated or generated[key] == candidate, "conflicting_candidate_identity"
            generated[key] = candidate
            for support in candidate["supports"]:
                assert identity(support["record_ref"]) in records, "candidate_support_outside_snapshot"
    evaluations = {row["candidate_key"]: row for row in result["pattern_evaluations"]}
    decisions = {row["candidate_key"]: row for row in result["selection_decisions"]}
    assert len(evaluations) == len(result["pattern_evaluations"]), "duplicate_value_evaluation"
    assert len(decisions) == len(result["selection_decisions"]), "duplicate_selection_decision"
    assert generated.keys() == evaluations.keys() == decisions.keys(), (
        "generated_candidates_not_fully_retained"
    )
    for evaluation in evaluations.values():
        assert evaluation["uncertainty"]["statistical_significance"] == "not_established", (
            "unvalidated_statistical_claim"
        )
    selected = {candidate["candidate_key"] for candidate in result["candidates"]}
    assert selected == {key for key, row in decisions.items() if row["decision"] == "selected"}, (
        "selected_set_mismatch"
    )
    for candidate in result["candidates"]:
        assert candidate == generated[candidate["candidate_key"]], "selected_candidate_payload_changed"
    for key, row in decisions.items():
        assert row["decision"] in DECISIONS and row["reasons"], "selection_reason_missing"
        assert evaluations[key]["eligible"] is (row["decision"] != "value_excluded"), (
            "value_and_capacity_conflated"
        )
    summary = data["selection_summary"]
    counts = Counter(row["decision"] for row in decisions.values())
    assert summary["generated_count"] == len(generated), "generated_count_mismatch"
    for state in DECISIONS:
        assert summary[state + "_count"] == counts[state], "selection_count_mismatch"
    assert summary["all_generated_candidates_retained"] is True, "candidate_retention_not_declared"
    return {
        "state": view["state"],
        "case_id": case.get("id"),
        "run_id": view.get("id"),
        "direction": case["direction"],
        "role": case["role"],
        "dataset_hash": data["dataset_hash"],
        "unit_contract": data["unit_contract"],
        "input_records": data["input_count"],
        "analysis_units": data["qualified_count"],
        "excluded_records": data["excluded_records"],
        "unknown_reasons": data["unknown_reasons"],
        "denominator": data["denominator"],
        "coverage": data["coverage"],
        "method_results": [
            {
                key: method.get(key)
                for key in (
                    "method_id",
                    "method_version",
                    "execution_status",
                    "readiness",
                    "statistics",
                    "truncated_count",
                    "limitations",
                )
            }
            for method in result["method_results"]
        ],
        "generated_candidates": len(generated),
        "selection_summary": summary,
        "unselected": [
            {"candidate": generated[key], "value_evaluation": evaluations[key], "selection": row}
            for key, row in decisions.items()
            if row["decision"] != "selected"
        ],
        "representation_comparison": representation_scope(result)
        if case["direction"] == "research"
        else None,
        "scientific_quality": "not_established_by_engineering_checks",
    }


def reviewed_candidates(case, result):
    review = case.get("independent_review") or {}
    labels = review.get("candidate_labels") or []
    if not labels:
        return {"status": "not_scored", "reason": "independent_candidate_labels_not_provided"}
    assert review.get("reviewer") and review.get("reviewer_type"), "review_provenance_missing"
    decisions = {row["candidate_key"]: row["decision"] for row in result["selection_decisions"]}
    counts = Counter()
    for label in labels:
        key, verdict = label["candidate_key"], label["verdict"]
        assert key in decisions, "review_label_has_no_saved_candidate"
        assert verdict in {"useful", "not_useful", "unknown"}, "unknown_review_verdict"
        selected = decisions[key] == "selected"
        counts["reviewed_candidates"] += 1
        if verdict == "unknown":
            counts["unknown"] += 1
        elif selected:
            counts["selected_useful" if verdict == "useful" else "selected_not_useful"] += 1
        elif verdict == "useful":
            counts["unselected_useful_generated_candidate"] += 1
    return {
        "status": "bounded_independent_labels_scored",
        "reviewer": review["reviewer"],
        "reviewer_type": review["reviewer_type"],
        "counts": dict(counts),
        "scope": "reviewed_generated_candidates_only_not_population_false_negative_rate",
        "not_scored": ["important_patterns_never_generated", "unreviewed_records_and_candidates"],
        "review": review,
    }


def sample_unselected(cases):
    candidates, excluded = {}, {}
    for case in cases:
        for row in case.get("unselected", []):
            candidates.setdefault(
                row["candidate"]["candidate_key"],
                {
                    "item_type": "generated_candidate",
                    "case_id": case.get("case_id"),
                    **row,
                },
            )
        for row in case.get("excluded_records", []):
            excluded.setdefault(
                identity(row["record_ref"]),
                {
                    "item_type": "input_record_not_admitted",
                    "case_id": case.get("case_id"),
                    "record_ref": row["record_ref"],
                    "reason": row["reason"],
                    "qualification": "input_ineligible_or_unknown_not_a_validated_negative",
                },
            )
    chosen = {}
    for states in ({"value_excluded"}, {"capacity_deferred", "redundancy_deferred"}):
        for key in sorted(candidates):
            if candidates[key]["selection"]["decision"] in states:
                chosen[key] = candidates[key]
                break
    for key in sorted(candidates):
        if len(chosen) >= 2:
            break
        chosen.setdefault(key, candidates[key])
    samples = list(chosen.values())
    samples.extend(excluded[key] for key in sorted(excluded)[: max(0, 2 - len(samples))])
    return samples


class KddExercise(ResearchExercise):
    def __init__(self, url, output, manifest):
        settings = Settings()
        if not settings.data_fabric_url or not settings.data_fabric_history_read_token:
            raise ValueError("Fixed original read access must be configured")
        if manifest.get("fabric_url") and manifest["fabric_url"].rstrip(
            "/"
        ) != settings.data_fabric_url.rstrip("/"):
            raise ValueError("Manifest and configured Fabric instances differ")
        super().__init__(url, output, case_profile="kdd")
        self.manifest = manifest
        self.state.update(
            acceptance="read_only_real_kdd_runs",
            model_requests_started=0,
            mutating_requests=0,
            cases={},
            runs={},
            snapshots={},
            original_readbacks={},
        )
        self.fabric_client = httpx.Client(
            base_url=settings.data_fabric_url,
            headers={"Authorization": "Bearer " + settings.data_fabric_history_read_token.get_secret_value()},
            timeout=60,
        )
        self.reader = FabricHistoryReader(
            self.fabric_client,
            authorization_ref="read-only-kdd-acceptance",
            max_records=10000,
            max_bytes=64_000_000,
            max_seconds=600,
        )
        self.bindings = {(row["source_id"], row["version_id"]): row for row in manifest["fixed_sources"]}
        self.original_cache = {}
        self.save()

    def call(self, method, path, **kwargs):
        if method != "GET":
            raise ValueError("KDD acceptance only observes approved work; GET required")
        return super().call(method, path, **kwargs)

    def originals(self, snapshot, cutoff, mode):
        for record in snapshot["records"]:
            ref = record["record_ref"]
            assert record["ingestion_mode"] != "fixture", "fixture_input_not_real_acceptance"
            binding = self.bindings[(record["source_id"], ref["adopted_version"])]
            assert binding["is_demo"] is False, "demo_original_not_real_acceptance"
            if binding.get("record_ref"):
                assert binding["record_ref"] == ref, "manifest_original_identity_mismatch"
            cache_key = (record["source_id"], ref["adopted_version"], mode, cutoff.isoformat())
            if cache_key not in self.original_cache:
                original = self.reader.read_version(binding, mode=mode, cutoff=cutoff)
                assert original["raw_sha256"] == binding["raw_sha256"] == ref["content_hash"], (
                    "fixed_original_raw_hash_mismatch"
                )
                assert original["source_record_id"] == binding["source_record_id"], (
                    "native_original_identity_mismatch"
                )
                self.original_cache[cache_key] = original
                self.state["original_readbacks"][canonical_hash(list(cache_key))] = {
                    key: original.get(key)
                    for key in (
                        "source_id",
                        "source_record_id",
                        "version_id",
                        "raw_sha256",
                        "first_seen_at",
                        "published_at",
                        "available_at",
                    )
                }
            if record.get("normalized_text") is not None:
                assert (
                    hashlib.sha256(record["normalized_text"].encode()).hexdigest()
                    == record["normalized_text_hash"]
                ), "normalized_snapshot_text_hash_mismatch"

    def report(self, case, run):
        investigation_id, analysis_id = case.get("investigation_id"), case.get("analysis_id")
        if not investigation_id or not analysis_id:
            return {"status": "not_provided", "model_started_by_acceptance": False}
        investigation = self.call("GET", "/api/engine/investigations/" + investigation_id).json()
        origin = investigation.get("origin") or {}
        assert origin.get("run_id") == run["id"], "report_investigation_not_bound_to_run"
        assert origin.get("result_hash") == run["result"]["result_hash"], "report_origin_hash_mismatch"
        assert origin.get("candidate_key") in {row["candidate_key"] for row in run["result"]["candidates"]}, (
            "report_not_from_selected_candidate"
        )
        path = f"/api/engine/investigations/{investigation_id}/analyses/{analysis_id}"
        view = self.call("GET", path).json()
        self.state["analyses"][analysis_id] = view
        if view["state"] not in {"completed", "needs_review"}:
            return {
                "status": "not_completed",
                "analysis_id": analysis_id,
                "state": view["state"],
                "error_code": view.get("error_code"),
                "budget": view.get("budget"),
                "progress": view.get("progress"),
            }
        self.check_report(view, require_search=False)
        for evidence in view["evidence"][1:]:
            original = self.call("GET", path + "/evidence/" + evidence["key"]).json()
            assert original["evidence"]["record_ref"] == evidence["record_ref"], (
                "report_original_identity_changed"
            )
            assert evidence["text"] in original["normalized_text"], "report_passage_not_in_original"
        report = view["result"]["report"]
        return {
            "status": "passed_body_and_receipt_integrity",
            "analysis_id": analysis_id,
            "analysis_state": view["state"],
            "report_status": report["status"],
            "support_audit_status": (report.get("support_audit") or {}).get("status"),
            "professional_status": view["professional_status"],
            "body_sha256": report["text_sha256"],
            "query_count": len(view["queries"]),
            "model_calls": len(view["calls"]),
            "mechanism": (view["result"].get("mechanism_evaluation") or {}).get("status"),
            "qualification": "Body integrity and actual verdicts; no automatic scientific approval",
        }

    def version_changes(self):
        changes = []
        for change in self.manifest.get("updates", []):
            observed = {
                "id": change["id"],
                "direction": change["direction"],
                "kind": change["kind"],
                "status": "reading",
            }
            changes.append(observed)
            try:
                assert change["kind"] in {"new", "revised", "retracted"}, "unsupported_update_kind"
                before = self.call("GET", "/api/engine/runs/" + change["before_run_id"]).json()
                after = self.call("GET", "/api/engine/runs/" + change["after_run_id"]).json()
                assert before["result"]["result_hash"] == change["before_result_hash"], (
                    "old_frozen_result_hash_changed"
                )
                assert before["id"] != after["id"], "update_reused_old_run"
                assert (
                    before["original_policy"]["kdd_direction"]
                    == after["original_policy"]["kdd_direction"]
                    == change["direction"]
                ), "update_direction_mismatch"
                old_detail = self.call("GET", "/api/engine/snapshots/" + before["snapshot_id"]).json()
                new_detail = self.call("GET", "/api/engine/snapshots/" + after["snapshot_id"]).json()
                old_snapshot, new_snapshot = old_detail["snapshot"], new_detail["snapshot"]
                old_versions = {record["record_ref"]["adopted_version"] for record in old_snapshot["records"]}
                new_versions = {record["record_ref"]["adopted_version"] for record in new_snapshot["records"]}
                assert change["new_version_id"] in new_versions - old_versions, (
                    "no_new_fixed_version_in_update"
                )
                cutoff = datetime.fromisoformat(after["original_policy"]["as_of"].replace("Z", "+00:00"))
                self.originals(new_snapshot, cutoff, change.get("evidence_mode", "current"))
                if change["kind"] != "new":
                    assert change["old_version_id"] in old_versions, "old_fixed_version_missing"
                    old_binding = self.bindings[(change["source_id"], change["old_version_id"])]
                    new_binding = self.bindings[(change["source_id"], change["new_version_id"])]
                    assert old_binding["source_record_id"] == new_binding["source_record_id"], (
                        "different_native_record_is_not_a_revision"
                    )
                    cutoff = datetime.fromisoformat(before["original_policy"]["as_of"].replace("Z", "+00:00"))
                    self.originals(old_snapshot, cutoff, change.get("evidence_mode", "current"))
                reread = self.call("GET", "/api/engine/runs/" + before["id"]).json()
                assert reread["result"] == before["result"], "old_result_rewritten_by_update"
                observed.update(
                    status="passed_fixed_version_update_integrity",
                    before_run_id=before["id"],
                    after_run_id=after["id"],
                    before_result_hash=before["result"]["result_hash"],
                    after_result_hash=after["result"]["result_hash"],
                    old_version_id=change.get("old_version_id"),
                    new_version_id=change["new_version_id"],
                    qualification=(
                        "Saved compute-version change and old-result preservation; "
                        "no semantic report-update claim"
                    ),
                )
            except (AssertionError, KeyError, TypeError, ValueError, httpx.HTTPError) as error:
                observed.update(status="failed", error=type(error).__name__, reason=str(error))
            self.state["version_changes"] = changes
            self.save()
        return changes

    def observe(self):
        options = self.call("GET", "/api/engine/investigations/research-options").json()
        runtime = options.get("runtime", {})
        self.check(
            runtime.get("environment") == "test"
            and runtime.get("database_name") == self.expected_database
            and runtime.get("workspace_id") == self.expected_workspace,
            "api_confirms_the_expected_isolated_database_and_workspace",
        )
        self.state["options"] = options
        for case in self.manifest["cases"]:
            self.state["cases"][case["id"]] = {
                "status": "reading",
                "direction": case["direction"],
                "role": case["role"],
            }
            self.save()
            try:
                run = self.call("GET", "/api/engine/runs/" + case["run_id"]).json()
                self.state["runs"][run["id"]] = run
                detail = self.call("GET", "/api/engine/snapshots/" + run["snapshot_id"]).json()
                snapshot = detail["snapshot"]
                self.state["snapshots"][run["snapshot_id"]] = detail
                assert snapshot["is_demo"] is False, "demo_snapshot_not_real_acceptance"
                assert not case.get("snapshot_id") or case["snapshot_id"] == run["snapshot_id"], (
                    "wrong_snapshot"
                )
                cutoff = datetime.fromisoformat(run["original_policy"]["as_of"].replace("Z", "+00:00"))
                self.originals(snapshot, cutoff, case.get("evidence_mode", "current"))
                observed = inspect_run(run, snapshot, case)
                observed["independent_review"] = reviewed_candidates(case, run["result"])
                self.state["cases"][case["id"]] = observed
                observed["report"] = self.report(case, run)
                observed["status"] = "passed_saved_run_integrity"
                self.state["cases"][case["id"]] = observed
            except (AssertionError, KeyError, TypeError, ValueError, httpx.HTTPError) as error:
                self.state["cases"][case["id"]].update(
                    status="failed", error=type(error).__name__, reason=str(error)
                )
            self.save()
        changes = self.version_changes()
        self.state["directions"] = {}
        for direction in DIRECTIONS:
            cases = [value for value in self.state["cases"].values() if value["direction"] == direction]
            samples = sample_unselected(cases)
            reports = [case.get("report", {}) for case in cases if case["role"] == "pattern"]
            self.state["directions"][direction] = {
                "case_roles": sorted({case["role"] for case in cases}),
                "three_real_cases_verified": len(cases) >= 3
                and {case["role"] for case in cases} >= ROLES
                and all(case["status"] == "passed_saved_run_integrity" for case in cases),
                "unselected_samples": samples,
                "two_unselected_items_available": len(samples) == 2,
                "positive_report_integrity_verified": any(
                    report.get("status") == "passed_body_and_receipt_integrity" for report in reports
                ),
                "actual_report_statuses": reports,
                "incremental_versions_verified": any(
                    item["direction"] == direction
                    and item["status"] == "passed_fixed_version_update_integrity"
                    for item in changes
                ),
            }
        for run_id, frozen in self.state["runs"].items():
            fresh = self.call("GET", "/api/engine/runs/" + run_id).json()
            for key in ("input_hash", "original_policy", "result"):
                assert fresh[key] == frozen[key], "original_run_changed_during_acceptance"
        complete = all(
            item["three_real_cases_verified"]
            and item["two_unselected_items_available"]
            and item["positive_report_integrity_verified"]
            and item["incremental_versions_verified"]
            for item in self.state["directions"].values()
        ) and any(
            item["kind"] in {"revised", "retracted"}
            and item["status"] == "passed_fixed_version_update_integrity"
            for item in changes
        )
        self.state.update(
            status="engineering_readback_complete_scientific_results_separate"
            if complete
            else "incomplete_real_acceptance",
            engineering_status="passed" if complete else "partial",
            scientific_quality_status="actual_machine_verdicts_and_bounded_external_reviews_only",
            source_read_requests=self.reader.requests_made,
            update_coverage=(
                "Four directions require actual new versions; one actual revision/retraction "
                "must preserve its prior saved result"
            ),
        )
        self.save()
        return self.state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radar-url", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("schema_version") != "bioradar.kdd.acceptance.v1":
        parser.error("Use a frozen bioradar.kdd.acceptance.v1 manifest")
    ids = []
    for case in manifest["cases"]:
        if case["direction"] not in DIRECTIONS or case["role"] not in ROLES:
            parser.error("Each case requires an explicit supported direction and role")
        ids.append(case["id"])
    if len(ids) != len(set(ids)):
        parser.error("Case IDs must be unique")
    exercise = KddExercise(args.radar_url, args.output, manifest)
    exercise.state.update(
        manifest_path=str(args.manifest.resolve()),
        manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        frozen_manifest=manifest,
    )
    try:
        exercise.observe()
    except Exception as error:
        exercise.state.update(status="failed", error=type(error).__name__, reason=str(error))
        exercise.save()
        raise
    finally:
        exercise.client.close()
        exercise.fabric_client.close()
    print(
        json.dumps(
            {
                key: exercise.state[key]
                for key in ("status", "engineering_status", "scientific_quality_status")
            },
            ensure_ascii=False,
        )
    )

    if exercise.state["engineering_status"] != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
