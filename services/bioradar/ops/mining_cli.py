"""Evaluate an explicitly frozen local snapshot, without upstream or queue calls."""

import argparse
import json
from pathlib import Path

from bioradar.research.mining.models import MiningPolicy, NormalizedSnapshot
from bioradar.research.mining.runner import run_mining


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshot = NormalizedSnapshot.model_validate_json(args.snapshot.read_bytes())
    policy = MiningPolicy.model_validate_json(args.policy.read_bytes())
    result = run_mining(snapshot, policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "result_hash": result.result_hash,
                "candidates": len(result.candidates),
                "is_demo": snapshot.is_demo,
            }
        )
    )


if __name__ == "__main__":
    main()
