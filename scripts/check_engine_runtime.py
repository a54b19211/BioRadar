"""Actual local HTTP -> PostgreSQL -> RQ -> worker acceptance probe.

Requires an explicitly supplied adopted snapshot. Does not call upstream services
or print credentials/text. This is engineering verification, not a quality score.
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument(
        "--policy", type=Path, help="Optional fixed policy, e.g. the synthetic fixture policy"
    )
    parser.add_argument("--output", default="artifacts/engine-runtime-check.json", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18100")
    args = parser.parse_args()
    settings = dict(
        line.split("=", 1)
        for line in Path("ops/.env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    snapshot = json.loads(args.snapshot.read_text())
    headers = {"Authorization": "Bearer " + settings["ENGINE_DEV_CREDENTIAL"]}
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "is_demo": snapshot["is_demo"],
        "record_count": len(snapshot["records"]),
        "checks": {},
    }
    with httpx.Client(base_url=args.base_url, timeout=20, trust_env=False) as client:
        assert client.get("/api/engine/snapshots").status_code == 401
        report["checks"]["anonymous_denied"] = True
        adopted = client.post(
            "/api/engine/snapshots", json=snapshot, headers={**headers, "Idempotency-Key": str(uuid4())}
        )
        adopted.raise_for_status()
        snapshot_id = adopted.json()["id"]
        report["snapshot_id"] = snapshot_id
        policy = (
            json.loads(args.policy.read_text())
            if args.policy
            else {
                "as_of": datetime.now(UTC).isoformat(),
                "time_mode": "publication_retrospective",
                "discovery_mode": "stock_and_incremental",
            }
        )
        body = {"snapshot_id": snapshot_id, "policy": policy}
        key = str(uuid4())
        run_headers = {**headers, "Idempotency-Key": key}
        accepted = client.post("/api/engine/runs", json=body, headers=run_headers)
        accepted.raise_for_status()
        run_id = accepted.json()["id"]
        replay = client.post("/api/engine/runs", json=body, headers=run_headers)
        replay.raise_for_status()
        assert replay.json()["id"] == run_id
        report["checks"]["request_idempotent"] = True
        different = {"snapshot_id": snapshot_id, "policy": {**policy, "minimum_support": 3}}
        assert client.post("/api/engine/runs", json=different, headers=run_headers).status_code == 409
        report["checks"]["same_key_different_body_rejected"] = True
        deadline = time.monotonic() + 90
        while True:
            response = client.get(f"/api/engine/runs/{run_id}", headers=headers)
            response.raise_for_status()
            run = response.json()
            if run["state"] not in {"queued", "running"}:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Run did not complete: {run['state']}")
            time.sleep(1)
        assert run["state"] in {"completed", "partial"}, run.get("error")
        assert run["result"] is not None
        result = run["result"]
        records = {
            (
                r["record_ref"]["provider"],
                r["record_ref"]["object_type"],
                r["record_ref"]["object_id"],
                r["record_ref"]["adopted_version"],
            ): r
            for r in snapshot["records"]
        }
        quotes = 0
        for method in result["method_results"]:
            for candidate in method["candidates"]:
                for support in candidate["supports"]:
                    ref = support["record_ref"]
                    record = records[
                        (ref["provider"], ref["object_type"], ref["object_id"], ref["adopted_version"])
                    ]
                    if support["quote"] is not None:
                        assert (
                            record["normalized_text"][support["normalized_start"] : support["normalized_end"]]
                            == support["quote"]
                        )
                        quotes += 1
        report.update(
            {
                "compute_version": result["compute_version"],
                "snapshot_hash": result["snapshot_hash"],
                "policy_hash": result["policy_hash"],
                "input_hash": run["input_hash"],
                "run_id": run_id,
                "state": run["state"],
                "result_hash": result["result_hash"],
                "candidate_count": len(result["candidates"]),
                "verified_quote_locations": quotes,
                "methods": [
                    {k: m[k] for k in ("method_id", "readiness", "execution_status")}
                    for m in result["method_results"]
                ],
            }
        )
        report["checks"]["persisted_worker_result"] = True
        report["checks"]["all_returned_quotes_match_frozen_text"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": str(args.output),
                "state": report["state"],
                "candidate_count": report["candidate_count"],
            }
        )
    )


if __name__ == "__main__":
    main()
