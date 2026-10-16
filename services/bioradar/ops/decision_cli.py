"""Recompute a frozen current appraisal locally, without HTTP, queues or upstream access."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from bioradar.research.decision.models import DecisionInput, DecisionPolicy
from bioradar.research.decision.runner import evaluate_decision
from bioradar.research.mining.models import NormalizedSnapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True, help="JSON with input and policy")
    parser.add_argument("--snapshot", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text())
        result = evaluate_decision(
            DecisionInput.model_validate(request["input"]),
            DecisionPolicy.model_validate(request["policy"]),
            tuple(NormalizedSnapshot.model_validate_json(path.read_text()) for path in args.snapshot),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=args.output.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(result.model_dump_json(indent=2) + "\n")
            temporary.replace(args.output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except Exception as exc:
        raise SystemExit(
            f"Decision appraisal did not complete ({type(exc).__name__}); no output accepted."
        ) from None
    print(
        json.dumps(
            {"status": result.execution_status, "is_demo": result.is_demo, "result_hash": result.result_hash}
        )
    )


if __name__ == "__main__":
    main()
