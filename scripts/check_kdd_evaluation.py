"""Offline controls for the KDD acceptance observer; no real-data or science score."""

import copy
import hashlib
import inspect
from datetime import UTC, datetime

from bioradar.research.investigations.analysis import semantic_reading_state
from bioradar.research.investigations.analysis_service import AnalysisService
from bioradar.research.investigations.kdd import _professional_cost
from bioradar.research.mining.kdd import run_kdd
from bioradar.research.mining.models import (
    EngineRecord,
    KddPolicy,
    NormalizedSnapshot,
    ProviderRef,
    canonical_hash,
)
from bioradar.research.mining.prepare import snapshot_hash
from kdd_e2e import KddExercise, inspect_run, sample_unselected
from query_policy_assertions import assert_query_policy
from research_e2e import ResearchExercise, expected_report_markdown


def fixture():
    records = []
    for index, words in enumerate(
        (
            "T cells bispecific antibody cytotoxicity myeloma mechanism",
            "T cells bispecific antibody myeloma binding experiments",
            "manufacturing supply availability clinical comparison conditions",
        )
    ):
        digest = hashlib.sha256(words.encode()).hexdigest()
        ref = ProviderRef(
            provider="explicit-offline-control",
            object_type="document",
            object_id=str(index),
            adopted_version="offline-v1",
            version_source="provider",
            content_hash=digest,
            authorization_ref="offline-no-access",
        )
        records.append(
            EngineRecord(
                record_ref=ref,
                document_hash=digest,
                source_instance="explicit-offline-control",
                source_id="offline",
                observed_at=datetime(2026, 10, 1, tzinfo=UTC),
                published_at=datetime(2026, 10, 1, tzinfo=UTC),
                publication_time_verified=True,
                time_precision="day",
                ingestion_mode="fixture",
                text_status="inline_authorized",
                normalized_text=words,
                normalized_text_hash=digest,
                normalization_version="offline-v1",
            )
        )
    snapshot = NormalizedSnapshot(
        snapshot_id="offline-observer-control",
        is_demo=True,
        source_instance="explicit-offline-control",
        observed_at=datetime(2026, 10, 2, tzinfo=UTC),
        records=tuple(records),
    )
    policy = KddPolicy(kdd_direction="research", as_of=datetime(2026, 10, 3, tzinfo=UTC), topic_components=2)
    result = run_kdd(snapshot, policy)
    return (
        {
            "state": "completed",
            "original_policy": policy.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        },
        snapshot.model_dump(mode="json"),
        {"direction": "research", "role": "pattern"},
    )


def rehash_dataset(view):
    data = view["result"]["data_preparation"]
    data["dataset_hash"] = canonical_hash(
        {
            key: value
            for key, value in data.items()
            if key not in {"dataset_hash", "selection_summary", "dataset_hash_scope"}
        }
    )


def rejected(view, snapshot, case, expected):
    # Keep the mutation self-consistent at the transport hash, then inspect its business boundary.
    view["result"]["result_hash"] = canonical_hash(
        {key: value for key, value in view["result"].items() if key != "result_hash"}
    )
    try:
        inspect_run(view, snapshot, case)
    except (AssertionError, KeyError) as error:
        assert isinstance(error, KeyError) or expected in str(error), (expected, error)
        return
    raise AssertionError("Mutated control was accepted: " + expected)


def policy_fixture():
    run, _, _ = fixture()
    pattern = run["result"]
    data = {
        key: value
        for key, value in pattern["data_preparation"].items()
        if key not in {"selection_summary", "dataset_hash_scope"}
    }
    scope = {"direction": "research", "mode": "current", "cutoff": run["original_policy"]["as_of"]}
    source = {
        "id": "sources:offline",
        "version": canonical_hash(["offline-fixed-snapshot"]),
        "kind": "source_documents",
        "scope": scope,
        "snapshot_ids": ["offline-fixed-snapshot"],
    }
    dataset = {
        "id": "dataset:" + data["dataset_hash"][:24],
        "version": data["dataset_hash"],
        "kind": "analysis_dataset",
        "scope": scope,
        "source_artifact_id": source["id"],
        "source_version": source["version"],
        "qualified_count": data["qualified_count"],
    }
    result = {
        "id": "pattern:" + pattern["result_hash"][:24],
        "version": pattern["result_hash"],
        "kind": "pattern_result",
        "scope": scope,
        "source_artifact_id": source["id"],
        "source_version": source["version"],
        "dataset_hash": data["dataset_hash"],
        "candidate_keys": [row["candidate_key"] for row in pattern["candidates"]],
        "method_statuses": [row["execution_status"] for row in pattern["method_results"]],
    }
    trace = {
        "execution_version": "query-step-v1",
        "schema_version": "query-planner.external.v2",
        "requested": "query_planner",
        "effective": "query_planner",
        "version": "explicit-offline-v2",
        "training_status": "heuristic_untrained",
        "selector": "information_gain_v1",
        "stop": "rule",
        "direction": "research",
        "evidence_time": {"mode": scope["mode"], "cutoff": scope["cutoff"]},
        "status": "insufficient",
        "reason": "no_legal_action",
        "pending_action": None,
        "artifacts": [source, dataset, result],
        "artifact_results": {dataset["id"]: data, result["id"]: pattern},
        "execution": [],
        "decisions": [],
        "checks": [
            {
                "id": "kdd_dataset",
                "status": "passed",
                "evidence_ids": [],
                "artifact_refs": [{"id": dataset["id"], "version": dataset["version"]}],
            },
            {
                "id": "kdd_pattern",
                "status": "passed",
                "evidence_ids": [],
                "artifact_refs": [{"id": result["id"], "version": result["version"]}],
            },
        ],
        "budget": {
            "limits": {"tool_calls": 6, "queries": 6},
            "usage": {
                "tool_calls": 3,
                "known_tool_calls": 3,
                "unknown_tool_calls": 0,
                "reserved_tool_calls": 0,
                "budgeted_tool_calls": 3,
                "preparation_tool_calls": 1,
                "queries": 2,
            },
            "remaining": {"tool_calls": 3, "queries": 4},
        },
    }
    queries, observed = [], []
    for index, (kind, input_artifact, output_artifact) in enumerate(
        (
            ("dataset_prepare", source, dataset),
            ("analysis_run", dataset, result),
        )
    ):
        inputs = [{"id": input_artifact["id"], "version": input_artifact["version"]}]
        outputs = [{"id": output_artifact["id"], "version": output_artifact["version"]}]
        action = {
            "id": "offline-action-" + str(index),
            "kind": kind,
            "arguments": {
                "capability_id": "offline." + kind,
                "capability_version": "offline-fixed-v1",
                "input_refs": inputs,
                "scope": {"direction": "research", "mode": "current", "as_of": scope["cutoff"]},
                "parameters": {},
            },
        }
        query = {
            "action_id": action["id"],
            "provider": "radar",
            "kind": kind,
            "plan": copy.deepcopy(action["arguments"]),
            "status": "read",
            "evidence_keys": [],
            "artifact_refs": outputs,
            "cost": {"tool_calls": 1, "tokens": 0, "model_calls": 0},
        }
        receipt = {
            "action_id": action["id"],
            "query_index": index,
            "status": "read",
            "evidence_ids": [],
            "new_artifact_refs": outputs,
            "cost": copy.deepcopy(query["cost"]),
        }
        trace["decisions"].append(
            {
                "step": index,
                "decision": "continue",
                "reason": "selected_fixed_capability",
                "action": action,
                "receipt": receipt,
                "state_summary": {"observed_history": copy.deepcopy(observed)},
            }
        )
        observed.append(
            {
                "action_id": action["id"],
                "query_index": index,
                "status": "read",
                "evidence_ids": [],
                "new_artifact_refs": outputs,
            }
        )
        trace["execution"].append(
            {
                "action_id": action["id"],
                "kind": kind,
                "input_refs": inputs,
                "output_refs": outputs,
            }
        )
        queries.append(query)
    trace["decisions"].append(
        {
            "step": 2,
            "decision": "insufficient",
            "reason": "no_legal_action",
            "action": None,
            "state_summary": {"observed_history": observed},
        }
    )
    return {"query_policy": trace, "queries": queries, "evidence": [], "result": {"query_policy": trace}}


def professional_fixture():
    view = policy_fixture()
    trace = view["query_policy"]
    source = trace["artifacts"][0]
    payload = {"provider": "mech_evidence", "status": "completed", "overall": "insufficient"}
    artifact = {
        "id": "professional:offline",
        "version": canonical_hash(payload),
        "kind": "professional_assessment",
        "scope": source["scope"],
        "source_artifact_id": source["id"],
        "source_version": source["version"],
        "provider": "mech_evidence",
        "status": "completed",
    }
    trace["artifacts"].append(artifact)
    trace["artifact_results"][artifact["id"]] = payload
    inputs = [{"id": source["id"], "version": source["version"]}]
    outputs = [{"id": artifact["id"], "version": artifact["version"]}]
    action = {
        "id": "offline-professional",
        "kind": "professional_assess",
        "arguments": {
            "capability_id": "mech_evidence.evaluate",
            "capability_version": "offline-v1",
            "input_refs": inputs,
            "parameters": {},
            "scope": {"direction": "research", "mode": "current", "as_of": source["scope"]["cutoff"]},
        },
    }
    cost = {"tool_calls": 1, "tokens": 30, "model_calls": 2, "budgeted_tokens": 30}
    view["queries"].append(
        {
            "action_id": action["id"],
            "kind": action["kind"],
            "provider": "professional",
            "plan": copy.deepcopy(action["arguments"]),
            "status": "read",
            "artifact_refs": outputs,
            "evidence_keys": [],
            "cost": cost,
            "usage": {"model_call_ids": ["offline-model-1", "offline-model-2"]},
        }
    )
    view["calls"] = [
        {
            "call_id": "offline-model-" + str(index + 1),
            "action_id": action["id"],
            "stage": "offline-professional-control",
            "status": "received",
            "token_upper_bound": 100,
            "receipt": {"status": "received", "usage": {"total_tokens": tokens}},
        }
        for index, tokens in enumerate((10, 20))
    ]
    terminal = trace["decisions"].pop()
    history = copy.deepcopy(terminal["state_summary"]["observed_history"])
    trace["decisions"].append(
        {
            "step": 2,
            "decision": "continue",
            "reason": "applicable_professional_capability",
            "action": action,
            "receipt": {
                "action_id": action["id"],
                "query_index": 2,
                "status": "read",
                "new_artifact_refs": outputs,
                "evidence_ids": [],
                "cost": copy.deepcopy(cost),
            },
            "state_summary": {"observed_history": history},
        }
    )
    terminal["step"] = 3
    terminal["state_summary"]["observed_history"].append(
        {
            "action_id": action["id"],
            "query_index": 2,
            "status": "read",
            "new_artifact_refs": outputs,
            "evidence_ids": [],
        }
    )
    trace["decisions"].append(terminal)
    trace["execution"].append(
        {
            "action_id": action["id"],
            "kind": action["kind"],
            "input_refs": inputs,
            "output_refs": outputs,
        }
    )
    trace["checks"].append(
        {
            "id": "kdd_mechanism_review",
            "status": "passed",
            "evidence_ids": [],
            "artifact_refs": outputs,
        }
    )
    trace["budget"]["usage"].update(tool_calls=4, known_tool_calls=4, budgeted_tool_calls=4, queries=3)
    trace["budget"]["remaining"].update(tool_calls=2, queries=3)
    return view


def professional_controls():
    view = professional_fixture()
    assert assert_query_policy(view)["action_tool_calls"] == 3
    fixed = {
        "budget": {"max_calls": 6, "max_tokens": 1000, "max_seconds": 240, "max_queries": 6},
        "model_parameters": {"max_calls": 6, "max_input_bytes": 65536, "timeout_seconds": 60},
    }
    actual = copy.deepcopy(view["queries"][-1])
    _professional_cost(fixed, {"calls": view["calls"]}, actual)
    assert actual["cost"] == view["queries"][-1]["cost"]
    assert actual["usage"]["model_call_ids"] == view["queries"][-1]["usage"]["model_call_ids"]
    broken = copy.deepcopy(view)
    broken["queries"][-1]["cost"]["tokens"] = 0
    broken["query_policy"]["decisions"][-2]["receipt"]["cost"]["tokens"] = 0
    policy_rejected(broken, "professional action cost does not reconcile")
    broken = copy.deepcopy(view)
    broken["queries"][-1]["usage"]["model_call_ids"].reverse()
    policy_rejected(broken, "actual model call identities")
    unknown = copy.deepcopy(view)
    trace = unknown["query_policy"]
    artifact = trace["artifacts"].pop()
    trace["artifact_results"].pop(artifact["id"])
    trace["execution"].pop()
    trace["checks"][-1].update(status="unknown", artifact_refs=[])
    trace["decisions"].pop()
    decision = trace["decisions"][-1]
    query = unknown["queries"][-1]
    query.update(status="unconfirmed", artifact_refs=[])
    query["cost"].update(tokens=None, budgeted_tokens=110)
    decision["receipt"].update(status="unconfirmed", new_artifact_refs=[], cost=copy.deepcopy(query["cost"]))
    trace.update(
        status="blocked", reason="professional_model_outcome_unconfirmed", pending_action=decision["action"]
    )
    unknown["calls"][-1].update(status="unconfirmed", receipt={"status": "unconfirmed", "usage": {}})
    assert assert_query_policy(unknown)["status"] == "blocked"
    actual = copy.deepcopy(unknown["queries"][-1])
    _professional_cost(fixed, {"calls": unknown["calls"]}, actual)
    assert actual["cost"] == unknown["queries"][-1]["cost"]
    assert actual["cost"]["tokens"] is None and actual["cost"]["budgeted_tokens"] == 110
    broken = copy.deepcopy(unknown)
    broken["queries"][-1]["cost"]["tokens"] = 0
    broken["query_policy"]["decisions"][-1]["receipt"]["cost"]["tokens"] = 0
    policy_rejected(broken, "professional action cost does not reconcile")


def policy_rejected(view, expected):
    try:
        assert_query_policy(view)
    except AssertionError as error:
        assert expected in str(error), (expected, error)
        return
    raise AssertionError("Mutated v2 control accepted: " + expected)


def policy_controls():
    view = policy_fixture()
    assert assert_query_policy(view)["action_tool_calls"] == 2
    # Exercise the real checkpoint synchronizer, including an exception-path saved receipt.
    progress = {
        "queries": copy.deepcopy(view["queries"]),
        "query_policy": copy.deepcopy(view["query_policy"]),
    }
    progress["queries"][0]["check_updates"] = [{"id": "kdd_dataset", "status": "passed"}]
    for decision in progress["query_policy"]["decisions"]:
        if decision.get("action"):
            decision["receipt"] = None
    AnalysisService._sync_query_receipts(progress)
    synced = progress["query_policy"]["decisions"][0]["receipt"]
    assert synced["new_artifact_refs"] == progress["queries"][0]["artifact_refs"]
    assert synced["check_updates"] == progress["queries"][0]["check_updates"]
    saved = {**view, "queries": progress["queries"], "query_policy": progress["query_policy"]}
    saved["result"] = {"query_policy": progress["query_policy"]}
    assert assert_query_policy(saved)["action_tool_calls"] == 2
    fixed = {
        "execution_version": "query-step-v1",
        "query_policy": {
            key: copy.deepcopy(progress["query_policy"][key])
            for key in (
                "schema_version",
                "requested",
                "effective",
                "version",
                "training_status",
                "selector",
                "stop",
                "direction",
            )
        },
        "evidence_time": copy.deepcopy(progress["query_policy"]["evidence_time"]),
        "budget": {"max_tool_calls": 6, "max_queries": 6},
    }
    progress.update(
        tool_usage={"tool_calls": 3, "preparation_tool_calls": 1},
        kdd_artifacts=progress["query_policy"]["artifacts"],
        kdd_results=progress["query_policy"]["artifact_results"],
        kdd_execution=progress["query_policy"]["execution"],
        kdd_source_artifact_id=progress["query_policy"]["artifacts"][0]["id"],
    )
    # Check the real public view constructor, not a separate hand-built transport shape.
    published = AnalysisService._policy_view(fixed, progress)
    saved.update(query_policy=published, result={"query_policy": published})
    assert assert_query_policy(saved)["action_tool_calls"] == 2
    context = AnalysisService._reading_context(progress)
    assert context["analysis_results"], "actual calculation was omitted from the model context"
    assert semantic_reading_state([], context)["analysis_results"] == context["analysis_results"]
    broken = copy.deepcopy(view)
    broken["query_policy"]["schema_version"] = "query-planner.external.future"
    policy_rejected(broken, "unsupported policy schema")
    broken = copy.deepcopy(view)
    broken["query_policy"]["schema_version"] = "query-planner.external.v1"
    policy_rejected(broken, "actual provider differs")
    broken = copy.deepcopy(view)
    broken["query_policy"]["decisions"][1]["state_summary"]["observed_history"][0].pop("new_artifact_refs")
    policy_rejected(broken, "next decision did not observe actual yielded artifacts")
    broken = copy.deepcopy(view)
    artifact = broken["query_policy"]["artifacts"][1]
    broken["query_policy"]["artifact_results"][artifact["id"]]["qualified_count"] += 1
    policy_rejected(broken, "saved dataset content differs")
    broken = copy.deepcopy(view)
    trace = broken["query_policy"]
    future = [{"id": trace["artifacts"][2]["id"], "version": trace["artifacts"][2]["version"]}]
    trace["decisions"][0]["action"]["arguments"]["input_refs"] = future
    broken["queries"][0]["plan"]["input_refs"] = copy.deepcopy(future)
    trace["execution"][0]["input_refs"] = copy.deepcopy(future)
    policy_rejected(broken, "absent or future artifact")
    broken = copy.deepcopy(view)
    broken["queries"][0]["cost"]["tool_calls"] = 0
    broken["query_policy"]["decisions"][0]["receipt"]["cost"]["tool_calls"] = 0
    policy_rejected(broken, "local computation must count one tool action")
    broken = copy.deepcopy(view)
    broken["query_policy"]["artifacts"][1]["source_version"] = "different-source"
    policy_rejected(broken, "not bound to the fixed source version")
    broken = copy.deepcopy(view)
    broken["query_policy"]["checks"][0]["id"] = "support"
    policy_rejected(broken, "ordinary evidence coverage was replaced")


def report_export_controls():
    from bioradar.research.investigations.kdd import scope_markdown

    body = "SYNTHETIC unchanged checked body\n精确正文\n"
    view = {"result": {"report": {"final_text": body}}}
    assert expected_report_markdown(view) == body
    scope = {
        "fixed_version_count": 8,
        "read_version_count": 5,
        "read_scope": "selected_passages_not_full_documents",
        "unread_versions": [
            {"source_id": "who_don", "source_record_id": "unread-" + str(i), "version_id": str(i) * 64}
            for i in range(3)
        ],
        "source_failures": [
            {
                "provider": "fabric",
                "source_id": "who_don",
                "status": "unavailable",
                "error_code": "history_source_unavailable",
            },
            {"provider": "weknora", "source_id": None, "status": "unconfirmed", "error_code": None},
        ],
    }
    scope["unread_versions"][0].update(query_hit=True, adoption_stop_reason="analysis_evidence_budget")
    view["progress"] = {"material_scope": scope}
    actual = scope_markdown(scope) + body
    expected = expected_report_markdown(view)
    assert actual == expected
    prefix_length = len(expected) - len(body)
    assert actual[prefix_length:] == body
    for changed in (
        actual.replace("固定资料 8", "固定资料 9"),
        actual.replace("history_source_unavailable", "masked"),
        actual.replace("unread-0", "different-version"),
        actual[:-1],
        "unexpected prefix\n" + actual,
    ):
        assert changed != expected, "Changed system metadata/body must not match exact export"
    assert body != expected, "Scope must not disappear from an otherwise unchanged body"
    legacy = {"result": {"report": {"final_text": body}}}
    assert actual != expected_report_markdown(legacy), "Legacy exports stay exact without a prefix"


def main():
    report_export_controls()
    policy_controls()
    professional_controls()
    view, snapshot, case = fixture()
    assert inspect_run(view, snapshot, case)["analysis_units"] > 0
    broken = copy.deepcopy(view)
    broken["result"]["data_preparation"]["units"][0]["members"][0]["record_ref"]["adopted_version"] = (
        "unseen-version"
    )
    rehash_dataset(broken)
    rejected(broken, snapshot, case, "member_original")
    broken = copy.deepcopy(view)
    assert broken["result"]["pattern_evaluations"], "Control must generate a real algorithm candidate"
    broken["result"]["pattern_evaluations"].pop()
    rejected(broken, snapshot, case, "generated_candidates_not_fully_retained")
    broken = copy.deepcopy(view)
    key = broken["result"]["selection_decisions"][0]["candidate_key"]
    decision = broken["result"]["selection_decisions"][0]
    decision["decision"] = "capacity_deferred"
    broken["result"]["candidates"] = [
        row for row in broken["result"]["candidates"] if row["candidate_key"] != key
    ]
    for evaluation in broken["result"]["pattern_evaluations"]:
        if evaluation["candidate_key"] == key:
            evaluation["eligible"] = False
    rejected(broken, snapshot, case, "value_and_capacity_conflated")
    broken = copy.deepcopy(view)
    broken["result"]["data_preparation"]["denominator"]["population_rate_permitted"] = True
    rehash_dataset(broken)
    rejected(broken, snapshot, case, "unqualified_population_rate")
    broken = copy.deepcopy(view)
    future_snapshot = copy.deepcopy(snapshot)
    member = broken["result"]["data_preparation"]["units"][0]["members"][0]
    original = next(row for row in future_snapshot["records"] if row["record_ref"] == member["record_ref"])
    original["available_at"] = member["available_at"] = "2027-01-01T00:00:00Z"
    broken["original_policy"]["time_mode"] = "availability_replay"
    broken["result"]["data_preparation"]["time_mode"] = "availability_replay"
    broken["result"]["policy_hash"] = canonical_hash(broken["original_policy"])
    broken["result"]["snapshot_hash"] = snapshot_hash(NormalizedSnapshot.model_validate(future_snapshot))
    rehash_dataset(broken)
    rejected(broken, future_snapshot, case, "future_available_input")
    try:
        KddExercise.call(object.__new__(KddExercise), "POST", "/not-sent")
    except ValueError as error:
        assert "GET required" in str(error)
    else:
        raise AssertionError("Observer accepted a mutating request")
    assert inspect.signature(ResearchExercise.check_report).parameters["require_search"].default is True
    samples = sample_unselected(
        [
            {
                "case_id": "offline-input-gap",
                "excluded_records": [
                    {"record_ref": row["record_ref"], "reason": "missing_fields"}
                    for row in snapshot["records"][:2]
                ],
            }
        ]
    )
    assert len(samples) == 2
    assert all(
        row["qualification"] == "input_ineligible_or_unknown_not_a_validated_negative" for row in samples
    )
    print(
        "KDD observer controls passed: actual pure computation, 5 rejected boundary mutations, "
        "GET-only guard, v2 real synchronizer/artifacts, 8 rejected trace mutations, "
        "professional known/unknown ledger controls; synthetic/offline only."
    )


if __name__ == "__main__":
    main()
