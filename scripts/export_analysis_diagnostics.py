"""Export source-free diagnostics from one saved JSON file; never calls research services."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from bioradar.delivery.analysis_diagnostics import build_analysis_diagnostics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    saved = json.loads(args.input.read_text())
    analysis = saved.get("analysis", saved)
    value = build_analysis_diagnostics(
        analysis,
        progress=saved.get("progress", analysis.get("progress")),
        fixed=saved.get("fixed_input"),
    )
    with args.output.open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "analysis_id": value["analysis_id"]}))


if __name__ == "__main__":
    main()
