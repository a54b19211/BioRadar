import argparse
import json
from pathlib import Path

import httpx

from bioradar.adapters.data_fabric import DataFabricReader
from bioradar.api.errors import DomainError


def main() -> None:
    parser = argparse.ArgumentParser(description="只读核对现役 Data-Fabric 统计，不触发抽取和审核")
    parser.add_argument("--url", required=True, help="Server URL supplied by the operator, not a browser")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        with httpx.Client(base_url=args.url, timeout=20, trust_env=False, follow_redirects=False) as client:
            result = DataFabricReader(client).inspect_pipeline()
    except DomainError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Saved read-only pipeline statistics to {args.output}; not an intelligence evidence package")


if __name__ == "__main__":
    main()
