"""Read an explicit UUID manifest through the bounded Data-Fabric adapter.

The service root comes from server/operator configuration, never a Web request.
This command freezes an observation; it is not a full-source or CDC export.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

import httpx

from bioradar.adapters.data_fabric_snapshot import DataFabricSnapshotReader
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-instance", default="datafabric-test")
    parser.add_argument(
        "--manifest", required=True, type=Path, help="JSON array of at most 20 document UUIDs"
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    settings = Settings()
    actor = DevelopmentAccess(settings).actor()
    source_ids = settings.engine_source_id_allowlist.get(args.source_instance, [])
    root_url = os.environ.get("BIORADAR_DATA_FABRIC_URL")
    if not root_url:
        parser.error("Configure BIORADAR_DATA_FABRIC_URL with the reviewed service root")
    if (
        args.source_instance not in actor.source_allowlist
        or args.source_instance == "fixture"
        or not source_ids
    ):
        parser.error("The source instance and source IDs require server authorization")
    try:
        identifiers = json.loads(args.manifest.read_text())
        if not isinstance(identifiers, list) or not all(isinstance(item, str) for item in identifiers):
            parser.error("The manifest must be a JSON array of UUID strings")
        with httpx.Client(base_url=root_url, trust_env=False) as client:
            snapshot = DataFabricSnapshotReader(
                client,
                source_instance=args.source_instance,
                authorization_ref=actor.grant_ref,
                allowed_source_sites=set(source_ids),
            ).read_snapshot(identifiers)
    except DomainError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from None
    except Exception as exc:
        raise SystemExit(f"Snapshot input/read failed ({type(exc).__name__}); no package adopted") from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=args.output.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(snapshot.model_dump_json(indent=2) + "\n")
    try:
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "snapshot_id": snapshot.snapshot_id,
                "records": len(snapshot.records),
                "coverage": snapshot.coverage_mode,
                "text_available": sum(r.text_status == "inline_authorized" for r in snapshot.records),
            }
        )
    )


if __name__ == "__main__":
    main()
