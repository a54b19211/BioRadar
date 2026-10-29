"""Exercise question -> retrieval -> shared quality -> report -> follow-up over real HTTP/RQ.

Run against an isolated Radar acceptance database and an existing read-only Fabric
service. No source records, model outputs or expert judgments are inserted here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from bioradar.api.config import Settings

TERMINAL = {"completed", "needs_review", "failed", "blocked"}
DEFAULT_QUESTION = (
    "Elranatamab 治疗复发/难治性多发性骨髓瘤，作用机制和疗效的现有证据分别到哪一步？"
    "请比较临床登记与公开论文，说明哪些材料支持判断，哪些仍不足以支持管线布局决策。"
)
DEFAULT_FOLLOWUP = (
    "这些材料是否足以证明 Elranatamab 已在一线治疗中优于现有方案？"
    "请核对研究人群、对照、研究阶段和结果是否已经公布，不要把预计完成日期当成疗效结论。"
)


class ResearchExercise:
    def __init__(self, url: str, output: Path):
        settings = Settings()
        database = urlsplit(settings.database_url.get_secret_value()).path
        if settings.environment != "test" or not database.startswith("/bioradar_research_"):
            raise ValueError("Use the isolated bioradar_research_* acceptance database")
        if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("Use the local acceptance API")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.open("x").close()
        output.chmod(0o600)
        self.output = output
        self.expected_database = database.removeprefix("/")
        self.expected_workspace = settings.engine_workspace_id
        self.prefix = "research-" + uuid4().hex[:12]
        self.client = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + settings.engine_dev_credential.get_secret_value()},
            timeout=60,
        )
        self.state = {"run": self.prefix, "checks": [], "analyses": {}, "status": "running"}
        self.save()

    def save(self):
        self.output.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def check(self, condition, name):
        if not condition:
            raise AssertionError(name)
        self.state["checks"].append(name)
        self.save()

    def call(self, method, path, *, body=None, key=None):
        response = self.client.request(
            method, path, json=body, headers={"Idempotency-Key": key} if key else {}
        )
        if response.status_code >= 400:
            self.state["http_failure"] = {
                "path": path, "status": response.status_code, "body": response.json()
            }
            self.save()
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}")
        return response

    def wait(self, investigation_id, analysis_id):
        path = f"/api/engine/investigations/{investigation_id}/analyses/{analysis_id}"
        deadline = time.monotonic() + 1900
        previous = None
        while time.monotonic() < deadline:
            view = self.call("GET", path).json()
            self.state["analyses"][analysis_id] = view
            self.save()
            progress = (view["state"], view.get("progress", {}).get("stage"), len(view["calls"]))
            if progress != previous:
                print(json.dumps({"analysis_id": analysis_id, "progress": progress}), flush=True)
                previous = progress
            if view["state"] in TERMINAL:
                self.check(view["state"] in {"completed", "needs_review"}, "analysis_reached_a_report")
                return view
            time.sleep(3)
        raise TimeoutError("Read the saved analysis before attempting another run")

    def check_report(self, view):
        self.check(bool(view["evidence"]), "retrieved_and_adopted_real_evidence")
        self.check(any(query["status"] == "read" for query in view["queries"]), "real_search_completed")
        self.check(
            any(call.get("receipt", {}).get("status") == "completed" for call in view["calls"]),
            "real_model_receipts_saved",
        )
        result = view["result"]
        report = result.get("report") or {}
        self.check(bool(report.get("final_text")), "saved_report_has_text")
        self.check(report.get("provider") == "biocheck", "report_used_shared_biocheck")
        completed_stages = {
            call["stage"] for call in view["calls"]
            if call.get("receipt", {}).get("status") == "completed"
        }
        self.check(
            {"biocheck_plan", "biocheck_write", "biocheck_check"} <= completed_stages,
            "shared_generation_and_verification_actually_executed",
        )
        verification = report.get("verification") or {}
        self.check(
            verification.get("draft") == report["final_text"]
            and verification.get("status") == report["status"]
            and report["status"] in {"PASS", "REVIEW", "FAIL"},
            "quality_result_belongs_to_exact_delivered_text",
        )
        evidence = view["evidence"][0]
        original = self.call(
            "GET",
            f"/api/engine/investigations/{view['investigation_id']}/analyses/"
            f"{view['id']}/evidence/{evidence['key']}",
        ).json()
        self.check(
            original["evidence"]["record_ref"] == evidence["record_ref"]
            and evidence["text"] in original["normalized_text"],
            "fixed_original_can_be_read_back",
        )
        self.check(
            hashlib.sha256(report["final_text"].encode()).hexdigest() == report.get("text_sha256"),
            "report_version_matches_its_saved_text",
        )
        path = f"/api/engine/investigations/{view['investigation_id']}/analyses/{view['id']}/export"
        exported = self.call("GET", path + "?format=markdown").text
        self.check(exported == report["final_text"], "markdown_is_exact_saved_report")
        exported_json = self.call("GET", path).json()["analysis"]
        self.check(exported_json["result"] == result, "json_export_is_same_saved_result")
        usage, limits = view["budget"]["usage"], view["budget"]["limits"]
        self.state.setdefault("budgets", {})[view["id"]] = {"usage": usage, "limits": limits}
        self.save()

    def run(self, question, followup, sources, *, require_mechanism=False):
        options = self.call("GET", "/api/engine/investigations/research-options").json()
        self.state["options"] = options
        self.save()
        runtime = options.get("runtime", {})
        self.check(
            runtime.get("environment") == "test"
            and runtime.get("database_name") == self.expected_database
            and runtime.get("workspace_id") == self.expected_workspace,
            "remote_api_uses_the_expected_isolated_database_before_writes",
        )
        body = {"question": question, "source_ids": sources}
        started = self.call(
            "POST", "/api/engine/investigations/ask", body=body, key=self.prefix + "-ask"
        ).json()
        self.state["created"] = started
        self.save()
        repeated = self.call(
            "POST", "/api/engine/investigations/ask", body=body, key=self.prefix + "-ask"
        ).json()
        self.check(
            repeated["analysis_id"] == started["analysis_id"]
            and repeated["investigation_id"] == started["investigation_id"],
            "same_question_submission_does_not_create_a_second_job",
        )
        first = self.wait(started["investigation_id"], started["analysis_id"])
        self.check_report(first)
        if require_mechanism:
            mechanism = first["result"].get("mechanism_evaluation") or {}
            stages = {
                call["stage"] for call in first["calls"]
                if call.get("receipt", {}).get("status") == "completed"
            }
            self.check(
                mechanism.get("status") == "completed"
                and {"mech_decomposition", "mech_local_review", "mech_mechanism_review"} <= stages,
                "shared_mechanism_method_actually_executed",
            )
        investigation = self.call(
            "GET", "/api/engine/investigations/" + started["investigation_id"]
        ).json()
        next_view = self.call(
            "POST",
            "/api/engine/investigations/" + started["investigation_id"] + "/analyses",
            body={
                "expected_revision": investigation["revision"],
                "parent_analysis_id": first["id"],
                "question": followup,
                "auto_retrieve": True,
                "source_ids": sources,
            },
            key=self.prefix + "-followup",
        ).json()
        second = self.wait(started["investigation_id"], next_view["id"])
        self.check_report(second)
        self.check(second["question"] == followup, "followup_keeps_the_new_question")
        self.check(second["parent_analysis_id"] == first["id"], "followup_links_to_previous_analysis")
        old = self.call(
            "GET",
            f"/api/engine/investigations/{started['investigation_id']}/analyses/{first['id']}",
        ).json()
        self.check(old["result"] == first["result"], "followup_preserves_the_old_report")
        self.check(
            all(old[key] == first[key] for key in ("question", "evidence", "calls")),
            "followup_preserves_original_question_evidence_and_calls",
        )
        quality = {view["id"]: view["result"]["report"]["status"] for view in (first, second)}
        self.state["quality_status"] = quality
        self.state["engineering_status"] = "passed"
        self.state["status"] = (
            "passed" if all(value == "PASS" for value in quality.values()) else "completed_with_review"
        )
        self.save()
        print(json.dumps({
            "status": self.state["status"], "checks": len(self.state["checks"]), "quality": quality
        }), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--radar-url", default="http://127.0.0.1:18520")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--followup", default=DEFAULT_FOLLOWUP)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--require-mechanism", action="store_true")
    args = parser.parse_args()
    exercise = ResearchExercise(args.radar_url, args.output)
    try:
        exercise.run(
            args.question, args.followup, args.source or ["clinicaltrials", "europe_pmc"],
            require_mechanism=args.require_mechanism,
        )
    except Exception as exc:
        exercise.state.update(status="failed", error=type(exc).__name__, message=str(exc))
        exercise.save()
        raise
    finally:
        exercise.client.close()


if __name__ == "__main__":
    main()
