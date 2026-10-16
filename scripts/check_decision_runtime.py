"""Exercise the actual local HTTP/PG/RQ decision path with an explicit demo package."""

import argparse
import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from bioradar.research.decision.models import DecisionInput, DecisionPolicy
from bioradar.research.decision.runner import evaluate_decision
from bioradar.research.mining.models import NormalizedSnapshot, canonical_hash


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("artifacts/decision-runtime-check.json"))
    parser.add_argument("--base-url", default="http://127.0.0.1:18100")
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    adopted_input = DecisionInput.model_validate(request["input"])
    policy = DecisionPolicy.model_validate(request["policy"])
    if not adopted_input.is_demo:
        raise SystemExit("This engineering probe requires an explicitly marked demo commission.")
    snapshots = tuple(NormalizedSnapshot.model_validate_json(path.read_text()) for path in args.snapshot)
    env = dict(
        line.split("=", 1)
        for line in Path("ops/.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    headers = {"Authorization": "Bearer " + env["ENGINE_DEV_CREDENTIAL"]}
    report = {"tested_at": datetime.now(UTC).isoformat(), "is_demo": True, "checks": {}}
    comparisons = []
    results = []
    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        assert client.get("/api/engine/decision-runs").status_code == 401
        report["checks"]["anonymous_denied"] = True
        identifiers = []
        for snapshot in snapshots:
            response = client.post(
                "/api/engine/snapshots",
                json=snapshot.model_dump(mode="json"),
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            response.raise_for_status()
            identifiers.append(response.json()["id"])
        for audience in ("central", "provincial", "university", "enterprise"):
            value = copy.deepcopy(request["input"])
            value["brief"]["audience"] = audience
            body = {"input": value, "policy": request["policy"], "snapshot_ids": identifiers}
            writes = {**headers, "Idempotency-Key": str(uuid4())}
            response = client.post("/api/engine/decision-runs", json=body, headers=writes)
            response.raise_for_status()
            run_id = response.json()["id"]
            replay = client.post("/api/engine/decision-runs", json=body, headers=writes)
            replay.raise_for_status()
            assert replay.json()["id"] == run_id
            different = copy.deepcopy(body)
            different["input"]["brief"]["title"] += " (different content)"
            assert client.post("/api/engine/decision-runs", json=different, headers=writes).status_code == 409
            deadline = time.monotonic() + 90
            while True:
                response = client.get(f"/api/engine/decision-runs/{run_id}", headers=headers)
                response.raise_for_status()
                run = response.json()
                if run["state"] not in {"queued", "running"}:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Decision worker did not reach a terminal state")
                time.sleep(1)
            assert run["state"] in {"completed", "partial"}, run.get("error")
            result = run["result"]
            assert result is not None and result["is_demo"]
            expected = evaluate_decision(DecisionInput.model_validate(value), policy, snapshots)
            assert result == expected.model_dump(mode="json")
            assert run["submitted_by"] and run["accepted_at"] and run["submitted_request_hash"]
            assert client.get(f"/api/engine/runs/{run_id}", headers=headers).status_code == 404
            numerical = {
                key: result[key]
                for key in (
                    "feasibility",
                    "baseline_differences",
                    "pareto_comparisons",
                    "regret",
                    "conditional_comparison_set",
                    "excluded_from_comparison",
                )
            }
            comparisons.append(canonical_hash(numerical))
            results.append(
                {
                    "audience": audience,
                    "run_id": run_id,
                    "state": run["state"],
                    "input_hash": run["input_hash"],
                    "evidence_basis_hash": result["evidence_basis_hash"],
                    "compute_version": result["compute_version"],
                    "result_hash": result["result_hash"],
                    "feasibility": {row["option_id"]: row["status"] for row in result["feasibility"]},
                    "question_count": len(result["investigation_questions"]),
                    "regret_status": result["regret"]["status"],
                }
            )
        assert len(set(comparisons)) == 1
        assert len({item["evidence_basis_hash"] for item in results}) == 1
        assert len({item["input_hash"] for item in results}) == 4
        report["checks"].update(
            {
                "request_idempotent": True,
                "same_key_different_body_denied": True,
                "worker_result_equals_pure_recomputation": True,
                "server_receipt_recorded": True,
                "decision_ids_not_readable_as_mining": True,
                "audience_does_not_change_facts_or_numbers": True,
                "audiences_have_distinct_commission_identity": True,
            }
        )
    report["runs"] = results
    report["numerical_result_hash"] = comparisons[0]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(args.output), "audiences_checked": 4, "checks_passed": True}))


if __name__ == "__main__":
    main()
