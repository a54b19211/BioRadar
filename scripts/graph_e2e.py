"""Run published graph → Radar investigation using real HTTP services.

Synthetic reviewed relations and actual PrimeKG context are kept in separate studies.
Use disposable test databases. Existing upstream clinical records are read-only.
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from history_e2e import Exercise, canonical, record


class GraphExercise(Exercise):
    def publish(self, source_id, native_id, kind, raw, *, is_demo, suffix):
        body = {
            "source_id": source_id,
            "is_demo": is_demo,
            "scope_description": "Graph integration: explicitly selected versions",
            "declared_count": 1,
            "declared_complete": False,
            "records": [{"source_record_id": native_id, "record_kind": kind, "raw_content": raw}],
        }
        run = self.call(
            self.fabric, "POST", "/api/history/archives", auth=self.admin, body=body, key=self.prefix + suffix
        ).json()
        assert len(run["version_ids"]) == 1
        cutoff = datetime.now(UTC).isoformat()
        version = self.call(
            self.fabric,
            "GET",
            "/api/history/versions/" + run["version_ids"][0],
            auth=self.read,
            params={
                "source_id": source_id,
                "source_record_id": native_id,
                "is_demo": str(is_demo).lower(),
                "mode": "current",
                "cutoff": cutoff,
            },
        ).json()
        return version

    def graph_investigation(self, body, suffix):
        return self.call(
            self.radar,
            "POST",
            "/api/engine/investigations/graph",
            auth=self.user,
            body=body,
            key=self.prefix + suffix,
        ).json()

    def demo(self):
        quote = "教学研究团队甲与教学研究团队乙及教学研究团队丙开展合作。"
        raw = record(self.prefix + "-document", "document", {"summary": quote})
        document = self.publish(
            "external_archive",
            raw["source_record_id"],
            "document",
            raw["raw_content"],
            is_demo=True,
            suffix="-document",
        )
        start = document["clean_text"].index(quote)
        ref = {key: document[key] for key in ("source_id", "source_record_id", "version_id", "raw_sha256")}
        evidence = {
            **ref,
            "quote": quote,
            "locator": {"basis": "clean_text", "start": start, "end": start + len(quote)},
        }
        namespace = "demo.graph." + uuid4().hex[:8]
        review = {
            "status": "approved",
            "reviewed_by": "explicit-demo-reviewer",
            "reviewed_at": datetime.now(UTC).isoformat(),
        }
        nodes = [
            {
                "namespace": namespace,
                "id": letter,
                "entity_type": "Company",
                "name": name,
                "aliases": ["教学共同别名"],
                "external_ids": {},
                "identity_status": "confirmed",
                "identity_review_basis": "Explicit synthetic identities",
                "review": review,
                "mention_ids": [],
                "evidence": [evidence],
            }
            for letter, name in [("a", "教学研究团队甲"), ("b", "教学研究团队乙"), ("c", "教学研究团队丙")]
        ]
        edge = {
            "id": self.prefix + "-edge-1",
            "source": {"namespace": namespace, "id": "a"},
            "target": {"namespace": namespace, "id": "b"},
            "relation_type": "collaborated",
            "directed": True,
            "relation_props": {},
            "review": review,
            "binding_status": "bound",
            "valid_from": {"value": "2026-01-01", "precision": "day", "meaning": "synthetic_event"},
            "evidence": [evidence],
        }
        bundle = {
            "schema_version": "datafabric.graph.bundle.v1",
            "graph_kind": "reviewed",
            "namespace": namespace,
            "is_demo": True,
            "title": "教学合作图",
            "origin": "explicit_demo",
            "scope_description": "Synthetic selected graph, no real efficacy claim",
            "dependency_source_ids": ["external_archive"],
            "document_refs": [ref],
            "nodes": nodes,
            "edges": [edge],
            "redirects": [],
            "limitations": ["Explicit synthetic fixture"],
        }
        native_id = self.prefix + "-graph"
        first = self.publish(
            "reviewed_graph", native_id, "graph", canonical(bundle), is_demo=True, suffix="-graph-first"
        )
        params = {
            "source_id": "reviewed_graph",
            "source_record_id": native_id,
            "is_demo": "true",
            "mode": "current",
            "cutoff": datetime.now(UTC).isoformat(),
            "seed_ids": namespace + "|b",
            "direction": "out",
            "max_depth": 1,
        }
        subgraph = self.call(
            self.fabric,
            "GET",
            f"/api/history/graph/{first['version_id']}/neighborhood",
            auth=self.read,
            params=params,
        ).json()
        self.check(not subgraph["edges"], "outgoing_query_does_not_reverse_a_directed_edge")
        inward = self.call(
            self.fabric,
            "GET",
            f"/api/history/graph/{first['version_id']}/neighborhood",
            auth=self.read,
            params={**params, "direction": "in"},
        ).json()
        self.check(
            [item["id"] for item in inward["edges"]] == [edge["id"]],
            "incoming_query_preserves_original_direction",
        )
        body = {
            "title": "教学图谱调查",
            "question": "核对合作关系及原始材料。",
            "source_id": "reviewed_graph",
            "source_record_id": native_id,
            "graph_version_id": first["version_id"],
            "is_demo": True,
            "seed_ids": [namespace + "|a"],
            "search_clinicaltrials": False,
            "max_followups": 2,
        }
        resolution = self.call(
            self.fabric,
            "GET",
            f"/api/history/graph/{first['version_id']}/resolve",
            auth=self.read,
            params={
                key: value
                for key, value in params.items()
                if key not in {"seed_ids", "direction", "max_depth"}
            }
            | {"name": "教学共同别名"},
        ).json()
        self.check(
            resolution["status"] == "ambiguous" and len(resolution["candidates"]) == 3,
            "same_name_is_not_automatically_merged",
        )
        ambiguous_body = {key: value for key, value in body.items() if key != "seed_ids"}
        pending = self.graph_investigation({**ambiguous_body, "seed_names": ["教学共同别名"]}, "-ambiguous")
        self.check(
            pending["completion_status"] == "awaiting_seed_selection" and not pending["followups"],
            "ambiguous_seed_requires_selection_before_digging",
        )
        result = self.graph_investigation(body, "-investigate")
        self.check(result["completion_status"] == "bounded_context_saved", "bound_graph_investigation_saved")
        view = result["investigation"]
        self.check(
            bool(result["followups"]) and bool(result["reads"]),
            "actual_graph_question_and_document_read_receipts",
        )
        self.call(
            self.radar,
            "POST",
            "/api/engine/investigations/graph",
            auth=self.user,
            body=body,
            key=self.prefix + "-investigate",
            expected=(409,),
        )
        repeated = self.call(
            self.radar, "GET", f"/api/engine/investigations/{view['id']}", auth=self.user
        ).json()
        self.check(repeated["revision"] == view["revision"], "repeated_graph_request_does_not_repeat_work")
        material_ids = [entry["id"] for entry in view["entries"] if entry["kind"] == "material"]
        judged = self.call(
            self.radar,
            "POST",
            f"/api/engine/investigations/{view['id']}/entries",
            auth=self.user,
            body={
                "expected_revision": view["revision"],
                "kind": "judgment",
                "text": "教学判断：继续核对合作关系。",
                "conditions": "Only synthetic input",
                "basis_entry_ids": material_ids,
                "assessment": "provisional",
            },
            key=self.prefix + "-judgment",
        ).json()
        self.check(not judged["needs_review"], "judgment_covers_actual_materials")
        second_bundle = copy.deepcopy(bundle)
        second_bundle["edges"].append(
            {
                **copy.deepcopy(edge),
                "id": self.prefix + "-edge-2",
                "target": {"namespace": namespace, "id": "c"},
            }
        )
        second = self.publish(
            "reviewed_graph",
            native_id,
            "graph",
            canonical(second_bundle),
            is_demo=True,
            suffix="-graph-second",
        )
        updated = self.graph_investigation(
            {
                **body,
                "graph_version_id": second["version_id"],
                "compare_version_id": first["version_id"],
                "investigation_id": view["id"],
                "expected_revision": judged["revision"],
            },
            "-append",
        )
        self.check(
            updated["investigation"]["needs_review"], "new_graph_context_marks_old_judgment_for_review"
        )
        old_judgments = [item for item in updated["investigation"]["entries"] if item["kind"] == "judgment"]
        self.check(len(old_judgments) == 1, "new_graph_does_not_rewrite_or_invent_a_judgment")
        self.check(bool(updated.get("comparison")), "two_actual_graph_versions_compared")
        self.check(
            updated["mining_snapshot_id"] is not None, "qualified_relations_project_to_existing_mining_input"
        )
        run = self.call(
            self.radar,
            "POST",
            "/api/engine/runs",
            auth=self.user,
            body={
                "snapshot_id": updated["mining_snapshot_id"],
                "policy": {"as_of": datetime.now(UTC).isoformat(), "method_ids": ["research_network"]},
            },
            key=self.prefix + "-network",
            expected=(200, 201, 202),
        ).json()
        deadline = time.monotonic() + 40
        while True:
            run = self.call(self.radar, "GET", f"/api/engine/runs/{run['id']}", auth=self.user).json()
            if run["state"] not in {"queued", "running"}:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("graph network worker did not finish")
            time.sleep(0.25)
        self.check(
            run["state"] == "completed"
            and run["result"]["method_results"][0]["execution_status"] == "success",
            "published_graph_runs_existing_network_kernel_through_real_queue",
        )
        candidates = run["result"]["candidates"]
        self.check(
            bool(candidates) and all(item["kind"] == "hypothesis" for item in candidates),
            "network_candidate_is_a_hypothesis_not_a_confirmed_relation",
        )
        self.call(
            self.fabric,
            "GET",
            f"/api/history/versions/{first['version_id']}",
            auth=self.read,
            params={
                "source_id": "reviewed_graph",
                "source_record_id": native_id,
                "is_demo": "true",
                "mode": "operational_replay",
                "cutoff": "2018-01-01T00:00:00Z",
            },
            expected=(404, 422),
        )
        self.check(True, "current_graph_cannot_be_read_as_2018_knowledge")
        bad = copy.deepcopy(bundle)
        bad["edges"][0]["evidence"][0]["quote"] = "This quotation is absent from the source."
        self.call(
            self.fabric,
            "POST",
            "/api/history/archives",
            auth=self.admin,
            body={
                "source_id": "reviewed_graph",
                "is_demo": True,
                "scope_description": "invalid quote",
                "declared_count": 1,
                "records": [
                    {
                        "source_record_id": self.prefix + "-bad-quote",
                        "record_kind": "graph",
                        "raw_content": canonical(bad),
                    }
                ],
            },
            key=self.prefix + "-bad-quote",
            expected=(422,),
        )
        self.check(True, "bound_edge_with_fabricated_quote_rejected")
        original = self.call(
            self.fabric,
            "GET",
            f"/api/history/versions/{first['version_id']}",
            auth=self.read,
            params={
                "source_id": "reviewed_graph",
                "source_record_id": native_id,
                "is_demo": "true",
                "mode": "current",
                "cutoff": datetime.now(UTC).isoformat(),
            },
        ).json()
        self.check(original["raw_content"] == first["raw_content"], "original_graph_version_unchanged")
        return {
            "is_demo": True,
            "investigation_id": view["id"],
            "source_record_id": native_id,
            "graph_snapshot_id": result["graph_snapshot_id"],
            "network_run_id": run["id"],
            "network_candidate_count": len(candidates),
            "first_version": first["version_id"],
            "second_version": second["version_id"],
            "scope": "Synthetic reviewed graph and exact source quotations; not real professional validation",
        }

    def primekg(self, path):
        bundle = json.loads(path.read_text())
        assert bundle["graph_kind"] == "background" and bundle["is_demo"] is False
        native_id = self.prefix + "-primekg"
        version = self.publish(
            "primekg_graph", native_id, "graph", canonical(bundle), is_demo=False, suffix="-primekg"
        )
        matches = [node for node in bundle["nodes"] if node["name"] == "EGFR"]
        assert len(matches) == 1
        seed = matches[0]
        body = {
            "title": "EGFR 图谱引导的实际临床资料调查",
            "question": "从现有 EGFR 关联疾病出发，核对已有临床登记和证据缺口。",
            "source_id": "primekg_graph",
            "source_record_id": native_id,
            "graph_version_id": version["version_id"],
            "is_demo": False,
            "seed_names": [seed["name"]],
            "topic_terms": ["EGFR"],
            "search_clinicaltrials": True,
            "max_followups": 2,
        }
        broad = self.graph_investigation(body, "-primekg-broad")
        broad_queries = [read for read in broad["reads"] if read.get("kind") == "clinicaltrials_title_query"]
        self.check(
            bool(broad_queries) and all(read["status"] in {"read", "empty"} for read in broad_queries),
            "broad_graph_queries_record_actual_hits_or_empty_results",
        )
        disease = [node for node in bundle["nodes"] if node["name"] == "lung adenocarcinoma"]
        assert len(disease) == 1
        focused_body = {
            **body,
            "title": "肺腺癌与 EGFR 的实际临床资料调查",
            "question": "从肺腺癌的图谱关联出发，核对 EGFR 的临床登记及缺口。",
            "seed_names": [disease[0]["name"]],
            "topic_terms": [disease[0]["name"]],
        }
        result = self.graph_investigation(focused_body, "-primekg-focused")
        self.check(
            result["completion_status"] in {"bounded_context_saved", "partial"},
            "real_primekg_context_investigation_saved",
        )
        self.check(bool(result["followups"]), "real_neighbors_generate_explicit_questions")
        self.check(
            all(follow["binding_status"] == "background" for follow in result["followups"]),
            "primekg_associations_not_claimed_as_bound_document_evidence",
        )
        neighbors = [
            (follow["neighbor"]["namespace"], follow["neighbor"]["id"]) for follow in result["followups"]
        ]
        self.check(len(neighbors) == len(set(neighbors)), "reciprocal_edges_not_duplicate_questions")
        view = result["investigation"]
        self.check(
            not any(entry["kind"] == "judgment" for entry in view["entries"]),
            "automatic_graph_search_does_not_sign_a_professional_judgment",
        )
        queries = [read for read in result["reads"] if read.get("kind") == "clinicaltrials_title_query"]
        self.check(
            bool(queries) and all(read["status"] in {"read", "empty"} for read in queries),
            "actual_clinical_queries_complete",
        )
        self.check(sum(read.get("adopted", 0) for read in queries) > 0, "actual_clinical_materials_adopted")
        detail = self.call(
            self.radar, "GET", f"/api/engine/investigations/{view['id']}", auth=self.user
        ).json()
        self.check(detail["revision"] == view["revision"], "graph_investigation_persists_after_request")
        received_cutoff = datetime.now(UTC).isoformat()
        replay = self.graph_investigation(
            {**focused_body, "mode": "operational_replay", "cutoff": received_cutoff},
            "-primekg-received-replay",
        )
        self.check(
            replay["completion_status"] in {"bounded_context_saved", "partial"}
            and replay["graph_snapshot_id"] is not None,
            "received_primekg_background_can_be_replayed_after_receipt",
        )
        self.check(
            not any(row["kind"].startswith("clinicaltrials") for row in replay["reads"])
            and replay["mining_snapshot_id"] is None,
            "replay_never_fetches_current_clinical_records_or_upgrades_background",
        )
        version_params = {
            "source_id": "primekg_graph",
            "source_record_id": native_id,
            "is_demo": "false",
        }
        for mode, cutoff, label in [
            ("operational_replay", "2018-01-01T00:00:00Z", "primekg_unavailable_before_actual_receipt"),
            ("public_history_reconstruction", received_cutoff, "primekg_not_an_external_historical_archive"),
        ]:
            self.call(
                self.fabric,
                "GET",
                f"/api/history/versions/{version['version_id']}",
                auth=self.read,
                params={**version_params, "mode": mode, "cutoff": cutoff},
                expected=(404, 422),
            )
            self.check(True, label)
        return {
            "is_demo": False,
            "graph_version_id": version["version_id"],
            "source_record_id": native_id,
            "graph_snapshot_id": result["graph_snapshot_id"],
            "investigation_id": view["id"],
            "completion_status": result["completion_status"],
            "followups": result["followups"],
            "reads": result["reads"],
            "qualification_gaps": result["qualification_gaps"],
            "received_replay": {
                "investigation_id": replay["investigation"]["id"],
                "cutoff": received_cutoff,
                "completion_status": replay["completion_status"],
                "current_clinical_queries": 0,
            },
            "broad_discovery": {
                "investigation_id": broad["investigation"]["id"],
                "completion_status": broad["completion_status"],
                "followups": broad["followups"],
                "queries": broad_queries,
                "adopted": sum(read.get("adopted", 0) for read in broad_queries),
                "scope": "Unfocused EGFR questions; empty hits are not changed into evidence.",
            },
            "scope": "Actual PrimeKG subset and live clinical lookup; no causal or historical claim",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fabric-url", required=True)
    parser.add_argument("--radar-url", required=True)
    parser.add_argument("--real-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/graph-e2e.json"))
    args = parser.parse_args()
    exercise = GraphExercise(args.fabric_url, args.radar_url)
    result = {"started_at": datetime.now(UTC).isoformat(), "status": "running", "checks": exercise.checks}
    try:
        result["demo"] = exercise.demo()
        result["real"] = exercise.primekg(args.real_bundle)
        result["status"] = "passed"
    finally:
        exercise.close()
        if result["status"] == "running":
            result["status"] = "failed"
        result["finished_at"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"PASS graph E2E: {len(exercise.checks)} checks; report {args.output}")


if __name__ == "__main__":
    main()
