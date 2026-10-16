"""Unimplemented cards/scenarios must fail, never print a fake green result."""

import argparse
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["test", "scenario", "snapshot-import", "evaluate"])
parser.add_argument("--card", default="BOOT")
parser.add_argument("--case", default="")
parser.add_argument("--mode", default="fixture")
parser.add_argument("--manifest", default="")
args = parser.parse_args()

if args.action == "test" and args.card in {"BOOT", "ENGINE"}:
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-m", args.card]))
if args.action == "scenario" and args.case == "BOOT" and args.mode == "real":
    raise SystemExit(subprocess.call([sys.executable, "-m", "bioradar.ops.probe"]))
parser.error(f"{args.action} is not implemented for this card/case; see docs/DEVELOPMENT.md")
