"""Bounded history research through the local API, with a durable write receipt."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import SecretStr

from bioradar.api.config import Settings


def main():
    parser = argparse.ArgumentParser(description="History research; no automatic write retries")
    parser.add_argument("action", choices=["create", "score", "show", "sources", "export", "graph"])
    parser.add_argument("--input", type=Path, help="HistoryStudyCreate or HistoryEvaluationCreate JSON")
    parser.add_argument("--study-id")
    parser.add_argument(
        "--output", type=Path, required=True, help="New private receipt/output; never overwritten"
    )
    parser.add_argument("--api", default="http://127.0.0.1:18510")
    args = parser.parse_args()
    parts = urlsplit(args.api)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.username
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        parser.error("API must be a loopback HTTP endpoint")
    if args.action in {"score", "show", "export"} and not args.study_id:
        parser.error("--study-id is required")
    if args.study_id and (
        len(args.study_id) != 36 or any(c not in "0123456789abcdef-" for c in args.study_id)
    ):
        parser.error("--study-id must be a saved UUID")
    body = None
    if args.action in {"create", "score", "graph"}:
        if args.input is None:
            parser.error("--input is required")
        if args.input.stat().st_size > 64_000:
            parser.error("request exceeds 64000 bytes")
        body = json.loads(args.input.read_text())
        from bioradar.research.history.models import HistoryEvaluationCreate, HistoryStudyCreate
        from bioradar.research.investigations.graph import GraphInvestigationCreate

        model = {
            "create": HistoryStudyCreate,
            "score": HistoryEvaluationCreate,
            "graph": GraphInvestigationCreate,
        }[args.action]
        body = model.model_validate(body).model_dump(mode="json")
    try:
        settings = Settings(_env_file=Path("ops/.env"))
    except ValueError:
        parser.error("local settings are invalid; values are not printed")
    # make init uses unprefixed compose keys; explicitly read only this known
    # local-user key, while BIORADAR_ process environment keeps precedence.
    env_path = Path("ops/.env")
    if settings.engine_dev_credential is None and env_path.exists():
        for line in env_path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "ENGINE_DEV_CREDENTIAL":
                value = value.strip().strip('"').strip("'")
                if len(value) < 24:
                    parser.error("local development-user credential is invalid")
                settings.engine_dev_credential = SecretStr(value)
                break
    if settings.engine_dev_credential is None:
        parser.error("local development-user credential is not configured")
    receipt = {
        "schema_version": "bioradar.history.cli-receipt.v1",
        "execution_key": str(uuid4()),
        "action": args.action,
        "study_id": args.study_id,
        "status": "started",
        "started_at": datetime.now(UTC).isoformat(),
        "request": body,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    path = "/api/engine/history/"
    if args.action == "graph":
        path = "/api/engine/investigations/graph"
    elif args.action == "sources":
        path += "sources"
    elif args.action == "create":
        path += "studies"
    else:
        path += "studies/" + args.study_id
        if args.action == "score":
            path += "/evaluations"
        elif args.action == "export":
            path += "/export"
    method = "POST" if body is not None else "GET"
    try:
        with httpx.Client(
            base_url=args.api,
            trust_env=False,
            timeout=150,
            headers={"Authorization": "Bearer " + settings.engine_dev_credential.get_secret_value()},
        ) as client:
            response = client.request(
                method,
                path,
                json=body if body is not None else None,
                headers={"Idempotency-Key": receipt["execution_key"]},
            )
            if not response.is_success:
                receipt.update(
                    status="rejected_or_outcome_unconfirmed",
                    http_status=response.status_code,
                    limitation="No retry performed. Inspect saved studies/receipt before another write.",
                )
            else:
                receipt.update(
                    status="received",
                    http_status=response.status_code,
                    result=response.text if args.action == "export" else response.json(),
                )
    except (httpx.HTTPError, ValueError):
        receipt.update(
            status="write_or_read_outcome_unconfirmed",
            limitation="No retry performed. Inspect saved studies using the execution key.",
        )
    with args.output.open("w") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    os.chmod(args.output, 0o600)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "output": str(args.output),
                "execution_key": receipt["execution_key"],
            }
        )
    )
    return 0 if receipt["status"] == "received" else 1


if __name__ == "__main__":
    raise SystemExit(main())
