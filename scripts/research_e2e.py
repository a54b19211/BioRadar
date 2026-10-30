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
from query_policy_assertions import assert_query_policy

TERMINAL = {"completed", "needs_review", "failed", "blocked"}
DEFAULT_QUESTION = (
    "Elranatamab 治疗复发/难治性多发性骨髓瘤，作用机制和疗效的现有证据分别到哪一步？"
    "请比较临床登记与公开论文，说明哪些材料支持判断，哪些仍不足以支持管线布局决策。"
)
DEFAULT_LAYOUT_QUESTION = (
    "复发/难治性多发性骨髓瘤的多靶标抗体及免疫治疗管线应怎样布局？"
    "请从实际资料发现候选方向，比较支持与反证、竞争项目、类似技术的失败经验和专利缺口，"
    "说明哪些已查范围与未查资料会改变布局判断。"
)
DEFAULT_LAYOUT_FOLLOWUP = (
    "从本版候选中选出证据最薄弱但可能改变布局的一项，继续查反例或竞争解释。"
    "请区分登记停止原因、研究人群、可比结果和缺失资料，并说明为何维持或修改上一版。"
)
DEFAULT_FOLLOWUP = (
    "这些材料是否足以证明 Elranatamab 已在一线治疗中优于现有方案？"
    "请核对研究人群、对照、研究阶段和结果是否已经公布，不要把预计完成日期当成疗效结论。"
)


def expected_report_markdown(view):
    """System scope is outside the unchanged verified body; v1 stays byte-for-byte exact."""
    body = view["result"]["report"]["final_text"]
    scope = (view.get("progress") or {}).get("material_scope")
    if scope is None:
        return body
    assert isinstance(scope, dict), "invalid material scope"
    assert all(
        type(scope.get(key)) is int and scope[key] >= 0
        for key in ("fixed_version_count", "read_version_count")
    ), "invalid material scope counts"
    assert scope["read_version_count"] <= scope["fixed_version_count"], "read count exceeds fixed scope"
    assert scope.get("read_scope") == "selected_passages_not_full_documents", "unknown reading scope"
    assert isinstance(scope.get("unread_versions"), list), "missing unread versions"
    assert isinstance(scope.get("source_failures"), list), "missing source failures"
    lines = [
        "## 本轮资料范围（系统记录）",
        "",
        f"固定资料 {scope['fixed_version_count']} 个版本；"
        f"研判实际读取其中 {scope['read_version_count']} 个版本的选定片段。",
        "这些数字不是全文阅读量、独立研究数或全领域覆盖率。",
    ]
    if scope["unread_versions"]:
        labels = []
        for item in scope["unread_versions"]:
            assert all(
                isinstance(item.get(key), str) and item[key]
                for key in ("source_id", "source_record_id", "version_id")
            ), "invalid unread version identity"
            labels.append(item["source_id"] + ":" + item["source_record_id"])
        lines.append("未进入本轮原文片段：" + "；".join(labels) + "。")
    unread_hits = [item for item in scope["unread_versions"] if item.get("query_hit")]
    if unread_hits:
        labels = []
        for item in unread_hits:
            label = item["source_id"] + ":" + item["source_record_id"]
            reason = item.get("adoption_stop_reason")
            if reason:
                description = "本轮原文片段容量已满" if reason == "analysis_evidence_budget" else reason
                label += "（" + description + "；" + reason + "）"
            labels.append(label)
        lines.append("成功补查命中但未读：" + "；".join(labels) + "。")
    if scope["source_failures"]:
        labels = []
        for item in scope["source_failures"]:
            assert isinstance(item.get("status"), str) and item["status"], "invalid source failure"
            labels.append(
                (item.get("source_id") or item.get("provider") or "")
                + ":"
                + (item.get("error_code") or item["status"])
            )
        lines.append("补查未完成：" + "；".join(labels) + "。")
    return "\n".join(lines) + "\n\n---\n\n" + body


def read_resume(path):
    prior = json.loads(path.read_text())
    created = prior.get("created", {}) if isinstance(prior, dict) else {}
    if not all(
        isinstance(created.get(key), str) and created[key] for key in ("investigation_id", "analysis_id")
    ):
        raise ValueError("Resume artifact must contain the accepted investigation_id and analysis_id")
    return prior


def run_inputs(args, prior=None):
    """Keep a resumed first question and source scope tied to the accepted record."""
    prior = prior or {}
    saved = prior.get("request", {})
    created = prior.get("created", {})
    first = {
        **(created.get("analysis") or {}),
        **(prior.get("analyses", {}).get(created.get("analysis_id")) or {}),
    }

    def fixed(name, explicit, previous, default):
        if explicit is not None and previous is not None and explicit != previous:
            raise ValueError(f"Resume cannot change the original {name}")
        return previous if previous is not None else explicit if explicit is not None else default

    profile = fixed("case profile", args.case_profile, prior.get("case_profile"), "research")
    question = fixed(
        "question",
        args.question,
        saved.get("question", first.get("question")),
        DEFAULT_LAYOUT_QUESTION if profile == "pipeline_layout" else DEFAULT_QUESTION,
    )
    previous_sources = saved.get("source_ids", first.get("search_scope"))
    sources = fixed(
        "source scope",
        sorted(args.source) if args.source else None,
        sorted(previous_sources) if previous_sources is not None else None,
        ["clinicaltrials", "europe_pmc"],
    )
    policy = fixed(
        "query policy",
        args.policy,
        prior.get("requested_policy") or (first.get("query_policy") or {}).get("requested"),
        None,
    )
    mechanism = fixed(
        "mechanism requirement",
        args.require_mechanism,
        saved.get("require_mechanism"),
        False,
    )
    return {
        "question": question,
        "followup": args.followup
        or saved.get("followup")
        or (DEFAULT_LAYOUT_FOLLOWUP if profile == "pipeline_layout" else DEFAULT_FOLLOWUP),
        "sources": sources,
        "policy": policy,
        "case_profile": profile,
        "require_mechanism": mechanism,
        "require_mechanism_basis": "prior_request"
        if "require_mechanism" in saved
        else "explicit_cli"
        if args.require_mechanism is not None
        else "fixed_default_false",
    }


class ResearchExercise:
    def __init__(
        self,
        url: str,
        output: Path,
        *,
        policy=None,
        case_profile="research",
        resume_from=None,
        prior=None,
    ):
        settings = Settings()
        database = urlsplit(settings.database_url.get_secret_value()).path
        if settings.environment != "test" or not database.startswith(
            ("/bioradar_research_", "/bioradar_query_", "/bioradar_kdd_")
        ):
            raise ValueError(
                "Use an isolated bioradar_research_*, bioradar_query_* or bioradar_kdd_* acceptance database"
            )
        if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("Use the local acceptance API")
        self.prior_state = prior
        if resume_from is not None and output.resolve() == resume_from.resolve():
            raise ValueError("Resume output must be a new artifact; the original stays immutable")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.open("x").close()
        output.chmod(0o600)
        self.output = output
        self.policy = policy
        self.case_profile = case_profile
        self.expected_database = database.removeprefix("/")
        self.expected_workspace = settings.engine_workspace_id
        self.prefix = "research-" + uuid4().hex[:12]
        self.client = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + settings.engine_dev_credential.get_secret_value()},
            timeout=60,
        )
        self.state = {
            "run": self.prefix,
            "checks": [],
            "analyses": {},
            "status": "running",
            "case_profile": case_profile,
            "requested_policy": policy,
            "scientific_quality_status": "pending_independent_review",
        }
        if resume_from is not None:
            self.state.update(
                resume_from=str(resume_from.resolve()),
                resume_from_sha256=hashlib.sha256(resume_from.read_bytes()).hexdigest(),
                prior_run=prior.get("run"),
                prior_analysis_ids=sorted(set(prior.get("analyses", {})) | {prior["created"]["analysis_id"]}),
                created=prior["created"],
            )
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
                "path": path,
                "status": response.status_code,
                "body": response.json(),
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

    def check_report(self, view, *, require_search=True):
        policy_summary = assert_query_policy(view, expected_policy=self.policy)
        self.state.setdefault("policy_traces", {})[view["id"]] = policy_summary
        self.check(True, "receipt_driven_query_policy_trace_is_complete")
        self.check(bool(view["evidence"]), "retrieved_and_adopted_real_evidence")
        if require_search:
            self.check(any(query["status"] == "read" for query in view["queries"]), "real_search_completed")
        else:
            self.check(True, "fixed_material_report_does_not_require_a_new_query")
        self.check(
            any(call.get("receipt", {}).get("status") == "completed" for call in view["calls"]),
            "real_model_receipts_saved",
        )
        result = view["result"]
        report = result.get("report") or {}
        self.check(bool(report.get("final_text")), "saved_report_has_text")
        self.check(report.get("provider") == "biocheck", "report_used_shared_biocheck")
        completed_stages = {
            call["stage"] for call in view["calls"] if call.get("receipt", {}).get("status") == "completed"
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
        self.check(
            exported == expected_report_markdown(view),
            "markdown_is_exact_system_scope_and_saved_report",
        )
        exported_json = self.call("GET", path).json()["analysis"]
        self.check(exported_json["result"] == result, "json_export_is_same_saved_result")
        self.check(
            (exported_json.get("progress") or {}).get("material_scope")
            == (view.get("progress") or {}).get("material_scope"),
            "json_export_preserves_exact_material_scope",
        )
        self.check(
            exported_json.get("query_policy") == view["query_policy"], "export_preserves_exact_policy_trace"
        )
        usage, limits = view["budget"]["usage"], view["budget"]["limits"]
        self.state.setdefault("budgets", {})[view["id"]] = {"usage": usage, "limits": limits}
        self.save()

    def run(self, question, followup, sources, *, require_mechanism=False, require_mechanism_basis=None):
        self.state["request"] = {
            "question": question,
            "followup": followup,
            "source_ids": sources,
            "require_mechanism": require_mechanism,
            "require_mechanism_basis": require_mechanism_basis,
        }
        self.save()
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
        if self.prior_state is not None:
            old_runtime = self.prior_state.get("options", {}).get("runtime")
            self.check(
                old_runtime is None
                or all(
                    old_runtime.get(key) == runtime.get(key)
                    for key in ("environment", "database_name", "workspace_id")
                ),
                "resume_artifact_matches_the_expected_isolated_runtime",
            )
            started = self.prior_state["created"]
        else:
            body = {"question": question, "source_ids": sources}
            if self.policy:
                body["query_policy"] = self.policy
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
        self.check(
            first["id"] == started["analysis_id"]
            and first["investigation_id"] == started["investigation_id"],
            "checked_the_original_accepted_analysis",
        )
        self.check(first["question"] == question, "original_analysis_keeps_the_fixed_question")
        self.check_report(first)
        if require_mechanism:
            mechanism = first["result"].get("mechanism_evaluation") or {}
            stages = {
                call["stage"]
                for call in first["calls"]
                if call.get("receipt", {}).get("status") == "completed"
            }
            self.check(
                mechanism.get("status") == "completed"
                and {"mech_decomposition", "mech_local_review", "mech_mechanism_review"} <= stages,
                "shared_mechanism_method_actually_executed",
            )
        previous_followup = (self.prior_state or {}).get("followup_request")
        if previous_followup is not None:
            followup_body, followup_key = previous_followup["body"], previous_followup["key"]
            self.check(
                followup_body.get("parent_analysis_id") == first["id"]
                and followup_body.get("question") == followup
                and sorted(followup_body.get("source_ids", [])) == sorted(sources)
                and followup_body.get("query_policy") == self.policy,
                "resume_keeps_the_saved_followup_request",
            )
        else:
            self.check(
                not any(
                    item.get("parent_analysis_id") == first["id"]
                    for item in (self.prior_state or {}).get("analyses", {}).values()
                ),
                "no_unreconciled_legacy_followup_before_creating_one",
            )
            investigation = self.call(
                "GET", "/api/engine/investigations/" + started["investigation_id"]
            ).json()
            followup_body = {
                "expected_revision": investigation["revision"],
                "parent_analysis_id": first["id"],
                "question": followup,
                "auto_retrieve": True,
                "source_ids": sources,
                **({"query_policy": self.policy} if self.policy else {}),
            }
            followup_key = ((self.prior_state or {}).get("run") or self.prefix) + "-followup"
        self.state["followup_request"] = {"body": followup_body, "key": followup_key}
        self.save()
        next_view = self.call(
            "POST",
            "/api/engine/investigations/" + started["investigation_id"] + "/analyses",
            body=followup_body,
            key=followup_key,
        ).json()
        self.state["followup_created"] = next_view
        self.save()
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
            all(
                old[key] == first[key] for key in ("question", "evidence", "calls", "queries", "query_policy")
            ),
            "followup_preserves_original_question_evidence_and_calls",
        )
        quality = {view["id"]: view["result"]["report"]["status"] for view in (first, second)}
        self.state["quality_status"] = quality
        self.state["engineering_status"] = "passed"
        self.state["text_verification_status"] = (
            "passed" if all(value == "PASS" for value in quality.values()) else "needs_review"
        )
        summaries = self.state["policy_traces"]
        self.state["unchecked_scope"] = {
            identifier: item["unsearched_checks"] for identifier, item in summaries.items()
        }
        self.state["coverage_status"] = (
            "complete"
            if all(
                item["status"] == "complete" and not item["unsearched_checks"] for item in summaries.values()
            )
            else "incomplete"
        )
        self.state["status"] = (
            "completed_with_unchecked_scope"
            if self.state["coverage_status"] != "complete"
            else "completed_pending_independent_review"
        )
        if self.state["text_verification_status"] != "passed":
            self.state["status"] = "completed_with_review"
        self.save()
        print(
            json.dumps(
                {"status": self.state["status"], "checks": len(self.state["checks"]), "quality": quality}
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--radar-url", default="http://127.0.0.1:18520")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Use prior accepted IDs; never restart or resend the first ask",
    )
    parser.add_argument("--question")
    parser.add_argument("--followup")
    parser.add_argument("--policy", choices=("baseline", "query_planner"))
    parser.add_argument("--case-profile", choices=("research", "pipeline_layout"))
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--require-mechanism", action="store_true", default=None)
    args = parser.parse_args()
    prior = read_resume(args.resume_from) if args.resume_from else None
    inputs = run_inputs(args, prior)
    exercise = ResearchExercise(
        args.radar_url,
        args.output,
        policy=inputs.pop("policy"),
        case_profile=inputs.pop("case_profile"),
        resume_from=args.resume_from,
        prior=prior,
    )
    try:
        exercise.run(**inputs)
    except Exception as exc:
        exercise.state.update(status="failed", error=type(exc).__name__, message=str(exc))
        exercise.save()
        raise
    finally:
        exercise.client.close()


if __name__ == "__main__":
    main()
