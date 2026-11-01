"""Exercise real semantic analysis through HTTP and existing RQ in isolated databases.

The first analysis sees only today's official NCT03758417 registration. The JCO
paper is fetched and adopted later, triggering a child analysis by material_update.
A separate real WHO topic exercises the automatic topic path. No internal tick,
model answer, synthetic source record, or human judgment is inserted by this script.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from bioradar.adapters.fabric_history import FabricHistoryReader
from bioradar.adapters.fabric_monitor import build_snapshot, project_record
from bioradar.api.config import Settings
from bioradar.research.engine.auth import DevelopmentAccess
from history_e2e import canonical
from topic_e2e import TopicExercise

from tests.query_policy_assertions import assert_query_policy

TERMINAL_ANALYSIS = {"completed", "needs_review", "blocked", "failed"}


class SemanticExercise(TopicExercise):
    def __init__(self, fabric_url, radar_url, output, *, existing_only=False, policy=None):
        self.existing_only = existing_only
        self.policy = policy
        if not existing_only and (urlsplit(fabric_url).port != 18513 or urlsplit(radar_url).port != 18510):
            raise ValueError("Use the designated isolated semantic service ports")
        super().__init__(fabric_url, radar_url, read_only_sources=existing_only, policy=policy)
        self.prefix = "SEMANTIC-ACCEPTANCE-" + uuid4().hex[:12]
        self.output = output
        self.created_topics = []
        self.phase = "guard"
        self.receipt = {
            "prefix": self.prefix,
            "started_at": datetime.now(UTC).isoformat(),
            "phase": self.phase,
            "status": "running",
            "checks": self.checks,
            "writes": [],
            "sources": {},
            "analyses": {},
            "semantic_review": "pending_independent_review",
            "test_limits": {
                "manual_analyses_per_hour": Settings().analysis_manual_per_hour,
                "qualification": (
                    "Root explicitly approved 3/h only in this isolated database: "
                    "preserved failed attempt, explicit retry, later-material child. Production remains 2/h."
                    if Settings().analysis_manual_per_hour == 3
                    else (
                        "Root explicitly approved 4/h only in this isolated database: two preserved "
                        "quote failures, passage-ID retry, later-material child. Production remains 2/h."
                        if Settings().analysis_manual_per_hour == 4
                        else "Configured isolated admission limit; no counter is reset."
                    )
                ),
            },
        }
        # Create the private receipt before the first HTTP write. An existing
        # receipt means a previous run must be reconciled, never silently replayed.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        self.save()

    def save(self):
        self.receipt["phase"] = self.phase
        self.receipt["checks"] = self.checks
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(self.receipt, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.output)

    def check(self, condition, name):
        super().check(condition, name)
        self.save()

    def call(self, client, method, path, *, auth, body=None, params=None, key=None, expected=(200, 201)):
        if self.existing_only and client is self.fabric and method not in {"GET", "HEAD"}:
            raise AssertionError("existing-source mode only permits read-only Fabric requests")
        headers = dict(auth)
        if key:
            headers["Idempotency-Key"] = key
        write = None
        if method not in {"GET", "HEAD"}:
            write = {"method": method, "path": path, "key": key, "status": "sent"}
            self.receipt["writes"].append(write)
            self.save()
        try:
            response = client.request(method, path, json=body, params=params, headers=headers)
        except httpx.HTTPError:
            if write is not None:
                write["status"] = "unconfirmed"
                self.save()
            raise RuntimeError(
                "HTTP result unconfirmed; inspect the private receipt, do not resend"
            ) from None
        if write is not None:
            write.update(status="received", http_status=response.status_code)
            self.save()
        if response.status_code not in expected:
            # Response bodies can contain source text or request data.
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}")
        return response

    def guard(self, *, initial=True, expect_paper_empty=True):
        state = self.call(self.fabric, "GET", "/api/history/publication/status", auth=self.read).json()
        self.check(
            state["is_demo"] is False
            and state["namespace"] == "datafabric.semantic.acceptance"
            and state["source_database"] == "fabric_source_semantic_acceptance",
            "server_confirms_new_isolated_non_demo_source_database",
        )
        settings = Settings()
        database = urlsplit(settings.database_url.get_secret_value())
        redis = urlsplit(settings.redis_url.get_secret_value())
        self.check(
            database.hostname == "127.0.0.1"
            and database.port == 18436
            and database.path == "/bioradar_semantic_acceptance"
            and redis.port == 16479
            and redis.path == "/7",
            "client_settings_confirm_new_semantic_radar_database_and_redis",
        )
        monitor = self.call(self.radar, "GET", "/api/engine/monitor/status", auth=self.user).json()
        self.check(monitor["enabled"] and not monitor["paused"], "actual_background_monitor_is_running")
        if expect_paper_empty:
            self.check(not self.records("europe_pmc"), "paper_source_is_empty_before_the_first_analysis")
        if initial:
            self.check(not self.records("clinicaltrials"), "registration_source_is_empty_before_first_fetch")
        self.check(not self.records("who_don"), "who_source_is_empty_before_the_automatic_topic_case")

    def records(self, source, *, run_id=None):
        params = {
            "source_id": source,
            "is_demo": "false",
            "mode": "current",
            "cutoff": datetime.now(UTC).isoformat(),
            "limit": 10,
        }
        if run_id:
            params["run_id"] = run_id
        return self.call(self.fabric, "GET", "/api/history/records", auth=self.read, params=params).json()[
            "items"
        ]

    def fetch(self, source, query, suffix, *, ids=(), count=1, window=False):
        body = {
            "source_id": source,
            "query": query,
            "source_record_ids": list(ids),
            "max_records": count,
            "max_pages": 1,
            "deadline_seconds": 45,
        }
        if window:
            now = datetime.now(UTC)
            body.update(updated_since=(now - timedelta(days=30)).isoformat(), updated_until=now.isoformat())
        run = self.call(
            self.fabric,
            "POST",
            "/api/history/fetch",
            auth=self.admin,
            body=body,
            key=self.prefix + suffix,
        ).json()
        self.receipt["sources"][suffix] = {"run_id": run["id"], "source_id": source}
        self.save()

        def completed():
            view = self.call(self.fabric, "GET", "/api/history/runs/" + run["id"], auth=self.read).json()
            if view["state"] not in {"queued", "running"}:
                return view
            return None

        run = self.wait(completed, "public source RQ fetch did not finish", seconds=240)
        self.receipt["sources"][suffix].update(
            state=run["state"],
            coverage=run["coverage"],
            version_ids=run["version_ids"],
            error=run.get("error"),
        )
        self.save()
        self.check(
            run["is_demo"] is False and run["state"] in {"completed", "partial"} and bool(run["version_ids"]),
            suffix + "_actual_public_fetch_finished_without_synthetic_records",
        )
        items = self.records(source, run_id=run["id"])
        self.check(
            bool(items) and all(not r["is_demo"] for r in items), suffix + "_frozen_versions_are_readable"
        )
        return items

    def adopt(self, record, suffix):
        settings = Settings()
        with httpx.Client(
            base_url=str(self.fabric.base_url), headers=self.read, timeout=30, trust_env=False
        ) as client:
            reader = FabricHistoryReader(
                client,
                authorization_ref=DevelopmentAccess(settings).actor().grant_ref,
            )
            now = datetime.now(UTC)
            item = project_record(reader, record, as_of=now)
            snapshot = build_snapshot([item], is_demo=False, as_of=now)
        view = self.call(
            self.radar,
            "POST",
            "/api/engine/snapshots",
            auth=self.user,
            key=self.prefix + suffix,
            body=snapshot.model_dump(mode="json"),
        ).json()
        self.check(not view["is_demo"], suffix + "_radar_adoption_preserves_real_domain")
        return view["id"], item.record_ref.model_dump(mode="json")

    def investigation(self, identifier):
        return self.call(self.radar, "GET", "/api/engine/investigations/" + identifier, auth=self.user).json()

    def append_material(self, investigation, record, suffix):
        snapshot_id, ref = self.adopt(record, suffix + "-snapshot")
        return self.call(
            self.radar,
            "POST",
            f"/api/engine/investigations/{investigation['id']}/entries",
            auth=self.user,
            key=self.prefix + suffix,
            body={
                "kind": "material",
                "expected_revision": investigation["revision"],
                "snapshot_id": snapshot_id,
                "record_ref": ref,
                "stance": "context",
                "text": "公开来源按本次实际取得时间采用；仅作分析材料，不预先给出判断。",
            },
        ).json()

    def analysis_list(self, investigation_id):
        return self.call(
            self.radar,
            "GET",
            f"/api/engine/investigations/{investigation_id}/analyses",
            auth=self.user,
        ).json()

    def analysis(self, investigation_id, identifier):
        return self.call(
            self.radar,
            "GET",
            f"/api/engine/investigations/{investigation_id}/analyses/{identifier}",
            auth=self.user,
        ).json()

    def wait_analysis(self, investigation_id, identifier):
        view = self.wait(
            lambda: (
                v
                if (v := self.analysis(investigation_id, identifier))["state"] in TERMINAL_ANALYSIS
                else None
            ),
            "RQ machine analysis did not reach a terminal state",
            seconds=max(600, 6 * Settings().semantic_llm_timeout_seconds + 90),
        )
        self.receipt["analyses"][identifier] = {
            "investigation_id": investigation_id,
            "state": view["state"],
            "trigger": view["trigger"],
            "error_code": view["error_code"],
        }
        self.save_private_view(view)
        self.save()
        self.check(
            view["state"] in {"completed", "needs_review"} and view["result"] is not None,
            identifier + "_actual_analysis_has_a_saved_structured_result",
        )
        return view

    def save_private_view(self, view):
        path = self.output.parent / (self.prefix + "-analysis-" + view["id"] + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(view, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        self.receipt["analyses"].setdefault(view["id"], {})["private_view"] = str(path)

    def verify_analysis(self, view, label, *, require_queries=True):
        calls = view["calls"]
        step_trace = (view.get("query_policy") or {}).get("execution_version") == "query-step-v1"
        if step_trace or self.policy:
            summary = assert_query_policy(view, expected_policy=self.policy)
            self.receipt.setdefault("policy_traces", {})[view["id"]] = summary
            self.check(True, label + "_actual_policy_actions_use_observed_receipts")
        normal_stages = ["read_plan", "synthesize", "check"]
        allowed_stages = [normal_stages]
        if view["version"] in {
            "bioradar-investigation-analysis-v0.4",
            "bioradar-investigation-analysis-v0.4.1",
            "bioradar-investigation-analysis-v0.4.2",
            "bioradar-investigation-analysis-v0.4.3",
            "bioradar-investigation-analysis-v0.4.4",
            "bioradar-investigation-analysis-v0.4.5",
            "bioradar-investigation-analysis-v0.4.6",
            "bioradar-investigation-analysis-v0.4.7",
        }:
            allowed_stages.append([*normal_stages, "revise", "recheck"])
        if view["version"] in {
            "bioradar-investigation-analysis-v0.4.4",
            "bioradar-investigation-analysis-v0.4.5",
            "bioradar-investigation-analysis-v0.4.6",
            "bioradar-investigation-analysis-v0.4.7",
        }:
            allowed_stages.extend(
                [
                    [*normal_stages, "compare"],
                    [*normal_stages, "revise", "recheck", "compare"],
                ]
            )
        self.check(
            (step_trace or [c["stage"] for c in calls] in allowed_stages)
            and all(
                c["status"] == "received"
                and c["receipt"]["status"] == "completed"
                and c["receipt"]["http_status"] == 200
                and c["receipt"]["provider_response_id"]
                and c["receipt"]["finish_reason"] == "stop"
                for c in calls
            ),
            label + "_bounded_real_model_stages_have_provider_receipts",
        )
        self.check(
            not view["is_demo"]
            and view["professional_status"] == "not_reviewed"
            and view["result"]["professional_status"] == "not_reviewed",
            label + "_machine_draft_does_not_become_a_professional_or_human_approval",
        )
        originals = {}
        for evidence in view["evidence"]:
            read = self.call(
                self.radar,
                "GET",
                f"/api/engine/investigations/{view['investigation_id']}/analyses/{view['id']}"
                f"/evidence/{evidence['key']}",
                auth=self.user,
            ).json()
            text = read["normalized_text"]
            self.check(
                hashlib.sha256(text.encode()).hexdigest() == evidence["text_sha256"]
                and text[evidence["start"] : evidence["end"]] == evidence["text"],
                label + "_frozen_evidence_" + evidence["key"] + "_hash_and_window_match",
            )
            originals[evidence["key"]] = text
        links = [link for claim in view["result"]["claims"] for link in claim["evidence_links"]]
        self.check(
            bool(links)
            and all(
                link["evidence_key"] in originals
                and originals[link["evidence_key"]][link["start"] : link["end"]] == link["quote"]
                for link in links
            ),
            label + "_every_saved_claim_quote_matches_its_exact_immutable_source",
        )
        self.check(
            (not require_queries or any(q["status"] in {"read", "empty"} for q in view["queries"]))
            and all(q["status"] in {"read", "empty", "failed"} for q in view["queries"])
            and all(q["plan"]["source_id"] in view["search_scope"] for q in view["queries"]),
            label + "_model_gap_plan_executed_only_in_the_declared_adopted_source_scope",
        )
        exported = self.call(
            self.radar,
            "GET",
            f"/api/engine/investigations/{view['investigation_id']}/analyses/{view['id']}/export",
            auth=self.user,
        ).json()["analysis"]
        self.check(
            exported["input_hash"] == view["input_hash"]
            and exported["result"] == view["result"]
            and (not step_trace or exported.get("query_policy") == view["query_policy"]),
            label + "_export_reads_the_frozen_analysis",
        )
        self.receipt["analyses"][view["id"]].update(
            input_hash=view["input_hash"],
            result_sha256=hashlib.sha256(canonical(view["result"]).encode()).hexdigest(),
            search_scope=view["search_scope"],
            calls=[{"stage": c["stage"], "receipt": c["receipt"]} for c in calls],
            queries=view["queries"],
        )
        self.save()

    def existing_pair(self, trial_version, paper_version, trial_record, paper_record):
        self.guard_existing()

        def original(source, record_id, version):
            value = self.call(
                self.fabric,
                "GET",
                "/api/history/versions/" + version,
                auth=self.read,
                params={
                    "source_id": source,
                    "source_record_id": record_id,
                    "is_demo": "false",
                    "mode": "current",
                    "cutoff": datetime.now(UTC).isoformat(),
                },
            ).json()
            value = value.get("record", value)
            self.check(
                value["version_id"] == version and not value["is_demo"],
                source + "_explicit_existing_real_version_is_readable",
            )
            return value

        self.existing_paper_record = paper_record
        trial = original("clinicaltrials", trial_record, trial_version)
        paper = original("europe_pmc", paper_record, paper_version)
        self.phase = "existing_registration_first"
        investigation = self.call(
            self.radar,
            "POST",
            "/api/engine/investigations",
            auth=self.user,
            key=self.prefix + "-existing-investigation",
            body={
                "title": "已存真实资料的证据更新 " + self.prefix,
                "is_demo": False,
                "question": "这份临床登记已公开什么、哪些仍是计划？请比较后到论文的人群、结果和限制，"
                "区分原文陈述与推断，说明新采用证据怎样维持或修订判断。",
            },
        ).json()
        identifier = investigation["id"]
        self.receipt["case_investigation_id"] = identifier
        investigation = self.append_material(investigation, trial, "-existing-trial")
        started = self.call(
            self.radar,
            "POST",
            f"/api/engine/investigations/{identifier}/analyses",
            auth=self.user,
            key=self.prefix + "-existing-first",
            body={
                "expected_revision": investigation["revision"],
                "source_ids": ["clinicaltrials"],
                **({"query_policy": self.policy} if self.policy else {}),
            },
            expected=(200, 202),
        ).json()
        first = self.wait_analysis(identifier, started["id"])
        self.verify_analysis(first, "existing_registration", require_queries=False)
        self.check(
            all(e["history_binding"]["version_id"] != paper_version for e in first["evidence"]),
            "first_analysis_excludes_the_later_adopted_paper_under_explicit_source_scope",
        )
        second = self.finish_case(first, identifier, paper=paper)
        self.receipt["existing_source_qualification"] = (
            "The paper already existed in Fabric but was adopted into this investigation later; "
            "this exercises accumulation, not its first public disclosure or first corpus arrival. "
            "Later adoption does not establish a new independent trial."
        )
        self.receipt["scientific_quality_status"] = "pending_independent_review"
        self.check(
            bool(second["result"]["update"]["reasons"]),
            "new_evidence_has_an_explicit_update_or_unable_to_compare_reason",
        )

    def case(self):
        self.phase = "registration_first"
        trial = self.fetch("clinicaltrials", "myeloma", "-trial", ids=["NCT03758417"])[0]
        raw = json.loads(trial["raw_content"])
        stopped = raw["protocolSection"]["statusModule"]["whyStopped"]
        self.check(
            trial["facts"]["trial_id"] == "NCT03758417" and bool(stopped),
            "official_registration_contains_the_termination_statement",
        )
        question = (
            "围绕登记试验 NCT03758417，终止原因究竟能说明什么、不能说明什么？"
            "请区分登记方对终止原因的陈述与疗效、安全性结论，比较可获得的论文证据、"
            "样本范围和随访时点；缺口应驱动追加查找，后来新材料到达时明确哪些理由维持、"
            "哪些需要修订。"
        )
        view = self.call(
            self.radar,
            "POST",
            "/api/engine/investigations",
            auth=self.user,
            key=self.prefix + "-investigation",
            body={"title": "真实病例语义验收 " + self.prefix, "question": question, "is_demo": False},
        ).json()
        identifier = view["id"]
        self.receipt["case_investigation_id"] = identifier
        self.save()
        view = self.append_material(view, trial, "-trial-material")
        configuration = self.analysis_list(identifier)
        self.check(
            configuration["enabled"] and configuration["configured"],
            "analysis_endpoint_reports_an_enabled_configured_model",
        )
        initial = self.call(
            self.radar,
            "POST",
            f"/api/engine/investigations/{identifier}/analyses",
            auth=self.user,
            key=self.prefix + "-first-analysis",
            body={
                "expected_revision": view["revision"],
                "material_entry_ids": [],
                **({"query_policy": self.policy} if self.policy else {}),
            },
            expected=(200, 202),
        ).json()
        first = self.wait_analysis(identifier, initial["id"])
        self.verify_analysis(first, "registration_only")
        self.check(
            any(
                e["text_basis"] == "source_raw_content" and stopped in e["text"]
                for e in first["evidence"]
                if e["source_id"] == "clinicaltrials"
            ),
            "the_model_really_received_the_full_original_why_stopped_statement",
        )
        self.check(
            not self.records("europe_pmc") and all(e["source_id"] != "europe_pmc" for e in first["evidence"]),
            "first_analysis_froze_before_the_paper_was_available_to_retrieval",
        )
        self.finish_case(first, identifier)

    def finish_case(self, first, identifier, *, paper=None):
        first_result_hash = hashlib.sha256(canonical(first["result"]).encode()).hexdigest()
        self.phase = "later_paper"
        if paper is None:
            paper = self.fetch("europe_pmc", "EXT_ID:36269898 AND SRC:MED", "-paper")[0]
        proof = paper.get("availability_proof") or {}
        self.check(
            paper["source_record_id"]
            == (self.existing_paper_record if self.existing_only else "MED:36269898")
            and bool(paper["facts"].get("abstract"))
            and (
                self.existing_only
                or (
                    proof.get("kind") == "unverified"
                    and proof.get("available_at") is None
                    and proof.get("verified_by") is None
                )
            ),
            (
                "explicit_existing_paper_keeps_its_original_metadata_and_abstract"
                if self.existing_only
                else "later_paper_is_current_public_metadata_and_abstract_without_historical_proof"
            ),
        )
        self.receipt["paper_adopted_at"] = paper["first_seen_at"]
        self.save()
        self.append_material(self.investigation(identifier), paper, "-paper-material")

        def child_started():
            return next(
                (
                    a
                    for a in self.analysis_list(identifier)["items"]
                    if a["parent_analysis_id"] == first["id"] and a["trigger"] == "material_update"
                ),
                None,
            )

        child = self.wait(
            child_started, "external material did not automatically create a child", seconds=180
        )
        second = self.wait_analysis(identifier, child["id"])
        self.verify_analysis(second, "later_paper")
        self.check(
            second["parent_analysis_id"] == first["id"]
            and any(
                e["source_id"] == "europe_pmc" and e["history_binding"]["version_id"] == paper["version_id"]
                for e in second["evidence"]
            ),
            "the_child_uses_the_actual_later_paper_and_preserves_parent_lineage",
        )
        old = self.analysis(identifier, first["id"])
        self.check(
            hashlib.sha256(canonical(old["result"]).encode()).hexdigest() == first_result_hash,
            "the_first_analysis_result_is_unchanged_after_the_child",
        )
        investigation = self.investigation(identifier)
        self.check(
            investigation["judgment_count"] == 0
            and any(
                e["payload"].get("machine_analysis_id") == second["id"] and e["kind"] == "note"
                for e in investigation["entries"]
            ),
            "actual_machine_notes_are_saved_without_inserting_a_human_judgment",
        )
        self.receipt["case_analysis_ids"] = [first["id"], second["id"]]
        self.save()
        return second

    def resume_failed_case(self, previous_path):
        previous = json.loads(previous_path.read_text())
        self.check(previous["status"] == "failed", "resume_only_from_a_preserved_failed_receipt")
        self.receipt["previous_receipt"] = {
            "path": str(previous_path),
            "sha256": hashlib.sha256(previous_path.read_bytes()).hexdigest(),
        }
        self.guard(initial=False)
        identifier = previous["case_investigation_id"]
        prior_ids = list(previous["analyses"])
        self.check(len(prior_ids) == 1, "failed_receipt_identifies_one_prior_analysis")
        failed = self.analysis(identifier, prior_ids[0])
        self.check(
            failed["state"] in {"blocked", "failed"}
            and failed["result"] is None
            and all(c["status"] in {"received", "failed"} for c in failed["calls"]),
            "failed_model_call_outcomes_are_known_before_an_explicit_new_attempt",
        )
        self.receipt["case_investigation_id"] = identifier
        self.receipt["retry_of_analysis_id"] = failed["id"]
        self.phase = "explicit_new_attempt"
        self.save()
        view = self.investigation(identifier)
        response = self.call(
            self.radar,
            "POST",
            f"/api/engine/investigations/{identifier}/analyses",
            auth=self.user,
            key=self.prefix + "-explicit-retry",
            body={
                "expected_revision": view["revision"],
                "material_entry_ids": [],
                "retry_of_analysis_id": failed["id"],
                **({"query_policy": self.policy} if self.policy else {}),
            },
            expected=(200, 202),
        ).json()
        self.check(response["id"] != failed["id"], "explicit_retry_creates_a_new_auditable_analysis")
        first = self.wait_analysis(identifier, response["id"])
        self.verify_analysis(first, "registration_only")
        trials = self.records("clinicaltrials")
        self.check(
            len(trials) == 1 and trials[0]["facts"]["trial_id"] == "NCT03758417",
            "retry_reuses_the_single_actual_registration_without_refetching",
        )
        stopped = json.loads(trials[0]["raw_content"])["protocolSection"]["statusModule"]["whyStopped"]
        self.check(
            any(
                e["text_basis"] == "source_raw_content" and stopped in e["text"]
                for e in first["evidence"]
                if e["source_id"] == "clinicaltrials"
            ),
            "the_model_really_received_the_full_original_why_stopped_statement",
        )
        self.check(
            not self.records("europe_pmc") and all(e["source_id"] != "europe_pmc" for e in first["evidence"]),
            "first_successful_analysis_froze_before_the_paper_was_available",
        )
        self.finish_case(first, identifier)

    def who(self):
        self.phase = "who_automatic"
        templates = self.call(self.radar, "GET", "/api/engine/topics/templates", auth=self.user).json()
        template = next(t for t in templates["items"] if t["id"] == "public_health")
        definition = copy.deepcopy(template["definition"])
        definition.update(
            title="真实WHO自动研判验收 " + self.prefix,
            source_ids=["who_don"],
            source_instance="datafabric-test",
            terms_all=[],
            terms_any=[],
        )
        self.check(
            definition["profiles"] == template["definition"]["profiles"]
            and definition["source_sites"] == template["definition"]["source_sites"],
            "real_who_case_keeps_the_public_health_template_profiles_and_sites",
        )
        topic = self.call(
            self.radar,
            "POST",
            "/api/engine/topics",
            auth=self.user,
            key=self.prefix + "-who-topic",
            body={"is_demo": False, "definition": definition},
        ).json()
        self.created_topics.append(topic["id"])
        self.receipt["who_topic_id"] = topic["id"]
        self.save()
        self.wait(
            lambda: next(
                (
                    e
                    for e in self.evaluations(topic["id"])
                    if e["coverage"]["seed_complete"] and e["coverage"]["active_records"] == 0
                ),
                None,
            ),
            "empty WHO baseline was not saved before the official notices arrived",
            seconds=180,
        )
        notices = self.fetch("who_don", "*", "-who", count=10, window=True)

        def followed():
            return next(
                (
                    e
                    for e in self.evaluations(topic["id"])
                    if e["investigation_id"] and e["automation_status"] == "saved"
                ),
                None,
            )

        evaluation = self.wait(
            followed, "WHO notices did not automatically start an investigation", seconds=600
        )
        identifier = evaluation["investigation_id"]

        def started():
            return next((a for a in self.analysis_list(identifier)["items"] if a["trigger"] == "topic"), None)

        automatic = self.wait(
            started, "WHO topic did not automatically enqueue semantic analysis", seconds=180
        )
        analysis = self.wait_analysis(identifier, automatic["id"])
        self.verify_analysis(analysis, "who_automatic", require_queries=False)
        self.check(
            bool(notices)
            and not analysis["is_demo"]
            and any(e["source_id"] == "who_don" for e in analysis["evidence"])
            and not evaluation["metric_series"],
            "real_who_announcement_evidence_does_not_invent_incidence_metrics",
        )
        self.check(
            any(
                s["kind"] == "source_event" and s["details"]["event_type"] == "outbreak_notice_published"
                for s in evaluation["signals"]
            ),
            "the_automatic_trigger_is_an_attributed_announcement_not_a_case_count",
        )
        self.check(
            self.investigation(identifier)["judgment_count"] == 0,
            "the_automatic_who_path_does_not_sign_a_human_judgment",
        )
        self.receipt["who"] = {
            "topic_id": topic["id"],
            "evaluation_id": evaluation["id"],
            "investigation_id": identifier,
            "analysis_id": analysis["id"],
            "notice_version_ids": [
                e["history_binding"]["version_id"]
                for e in analysis["evidence"]
                if e["source_id"] == "who_don"
            ],
            "input_fetch_version_ids": [n["version_id"] for n in notices],
            "qualification": "Selected official DON notices; not complete event surveillance or incidence.",
        }
        self.save()

    def run(
        self,
        resume_from=None,
        *,
        trial_version=None,
        paper_version=None,
        trial_record="NCT03758417",
        paper_record="MED:36269898",
    ):
        if self.existing_only:
            self.existing_pair(trial_version, paper_version, trial_record, paper_record)
        elif resume_from is None:
            self.guard()
            self.case()
        else:
            self.resume_failed_case(resume_from)
        if not self.existing_only:
            self.who()
        self.receipt.update(
            status="technical_checks_passed",
            completed_at=datetime.now(UTC).isoformat(),
            qualification=(
                "Real public current evidence and real model receipts; "
                "independent semantic review remains required."
            ),
        )
        self.save()
        return self.receipt

    def pause_own_topics(self):
        for identifier in self.created_topics:
            if not self.topic(identifier)["paused"]:
                self.change_control(identifier, True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fabric-url", required=True)
    parser.add_argument("--radar-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Preserved failed receipt; admits a new explicit retry_of analysis, never replays a write",
    )
    parser.add_argument("--policy", choices=("baseline", "query_planner"))
    parser.add_argument("--existing-only", action="store_true")
    parser.add_argument("--trial-version")
    parser.add_argument("--paper-version")
    parser.add_argument("--trial-record", default="NCT03758417")
    parser.add_argument("--paper-record", default="MED:36269898")
    args = parser.parse_args()
    if args.existing_only and (not args.trial_version or not args.paper_version or args.resume_from):
        parser.error("--existing-only requires both fixed version IDs and excludes --resume-from")
    exercise = SemanticExercise(
        args.fabric_url, args.radar_url, args.output, existing_only=args.existing_only, policy=args.policy
    )
    try:
        exercise.run(
            resume_from=args.resume_from,
            trial_version=args.trial_version,
            paper_version=args.paper_version,
            trial_record=args.trial_record,
            paper_record=args.paper_record,
        )
    except Exception as exc:
        exercise.receipt.update(status="failed", error_type=type(exc).__name__)
        exercise.save()
        raise
    finally:
        try:
            exercise.pause_own_topics()
        finally:
            exercise.close()
    print(
        json.dumps(
            {
                "status": exercise.receipt["status"],
                "checks_passed": len(exercise.checks),
                "private_report": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
