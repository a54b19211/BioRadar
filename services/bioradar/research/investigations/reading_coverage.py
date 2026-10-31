"""Expose the actual input of a retained reading, without refreshing its conclusion."""

import copy


def retained_reading_coverage(progress, original):
    """Called only after retained_policy_reading validates the received input and output."""
    payload = original["payload"]
    requested = {item["evidence_key"] for item in payload["evidence"]}

    def evidence(item):
        return {
            "evidence_key": item["key"],
            **{
                key: copy.deepcopy(item[key])
                for key in ("record_ref", "source_id", "selection_hash", "text_sha256", "start", "end")
                if key in item
            },
        }

    history = payload.get("query_history")
    indices = {query["index"] for query in history or [] if type(query.get("index")) is int}
    actions = {query.get("action_id") for query in history or [] if query.get("action_id")}
    steps = [
        query["decision_step"]
        for query in progress.get("queries", [])
        if query.get("action_id") in actions and type(query.get("decision_step")) is int
    ]
    inputs = {}
    for analysis in payload.get("analysis_results", []):
        ref = ((analysis.get("output") or {}).get("report_projection") or {}).get("full_result_ref")
        if isinstance(ref, dict) and isinstance(ref.get("id"), str) and isinstance(ref.get("version"), str):
            inputs[(ref["id"], ref["version"])] = {"id": ref["id"], "version": ref["version"]}
    missing = []
    for artifact in progress.get("kdd_artifacts", []):
        if artifact["kind"] not in {"analysis_dataset", "pattern_result", "professional_assessment"}:
            continue
        ref = (artifact["id"], artifact["version"])
        if ref in inputs:
            continue
        row = {
            **{
                key: copy.deepcopy(artifact[key])
                for key in ("id", "version", "kind", "provider")
                if key in artifact
            },
            "status": artifact.get("status"),
        }
        receipt = next(
            (
                query
                for query in progress.get("queries", [])
                if any(
                    (item.get("id"), item.get("version")) == ref for item in query.get("artifact_refs", [])
                )
            ),
            None,
        )
        if receipt is not None:
            row.update(
                query_index=receipt["index"],
                decision_step=receipt.get("decision_step"),
                receipt_status=receipt["status"],
            )
        missing.append(row)
    return {
        "coverage_version": "retained-reading-coverage-v1",
        "cannot_use_as_current_conclusion": True,
        "call_index": next(index for index, call in enumerate(progress["calls"]) if call is original),
        "after_query_index": max(indices) if indices else None,
        "after_decision_step": max(steps) if steps else None,
        "input_evidence": [evidence(item) for item in progress["evidence"] if item["key"] in requested],
        "input_artifact_refs": [inputs[key] for key in sorted(inputs)],
        "not_incorporated": {
            "evidence": [evidence(item) for item in progress["evidence"] if item["key"] not in requested],
            "artifacts": missing,
        },
    }
