#!/bin/sh
# Clean-install check: build a throwaway venv from the declared pins only, then import every bioradar module.
#   1. `uv sync --locked` fails when uv.lock does not match pyproject.toml (a stale pin), and installs the
#      project non-editable, so a package missing from the build is caught too;
#   2. the shared contract wheel is installed again by its sha256 from requirements-contract.txt (uv.lock does
#      not record a local wheel's hash);
#   3. pyproject.toml, uv.lock, requirements-contract.txt and the installed contract must name one version;
#   4. every module under bioradar is imported from the venv (isolated mode, outside the checkout).
# Usage: sh scripts/check_install.sh   (UV=<uv binary>, EXTRACTION_CONTRACT_WHEELS=<wheel dir> optional)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
UV=${UV:-uv}
WHEELS=${EXTRACTION_CONTRACT_WHEELS:-$ROOT/../extraction-contract/dist}
WORK=$(mktemp -d /tmp/bioradar-check-install.XXXXXX)
# the non-editable build runs setuptools in the checkout; what it leaves there is removed again
LEFT=""
for d in "$ROOT/build" "$ROOT/services/bioradar.egg-info"; do [ -e "$d" ] || LEFT="$LEFT $d"; done
trap 'rm -rf "$WORK" $LEFT' EXIT INT TERM
VENV="$WORK/venv"

cd "$ROOT"
# --reinstall-package: uv caches a local project build by pyproject.toml alone; this checkout is built afresh
UV_PROJECT_ENVIRONMENT="$VENV" "$UV" sync --locked --no-dev --no-editable --reinstall-package bioradar --python 3.12 --quiet
"$UV" pip install --quiet --python "$VENV/bin/python" --no-index --find-links "$WHEELS" --require-hashes \
  -r requirements-contract.txt

cd "$WORK"
"$VENV/bin/python" -I - "$ROOT" <<'EOF'
import importlib
import importlib.metadata
import pathlib
import pkgutil
import re
import sys
import tomllib

root = pathlib.Path(sys.argv[1])
project = tomllib.loads((root / "pyproject.toml").read_text())
declared = next(d for d in project["project"]["dependencies"] if d.startswith("extraction-contract"))
lock = tomllib.loads((root / "uv.lock").read_text())
locked = next(p["version"] for p in lock["package"] if p["name"] == "extraction-contract")
hashed = re.search(r"^extraction-contract==(\S+)", (root / "requirements-contract.txt").read_text(), re.M)
installed = importlib.metadata.version("extraction-contract")
versions = {
    "pyproject.toml": declared.split("==", 1)[1].strip(),
    "uv.lock": locked,
    "requirements-contract.txt": hashed.group(1) if hashed else None,
    "installed": installed,
}
if len(set(versions.values())) != 1:
    sys.exit(f"extraction-contract pins disagree: {versions}")

import bioradar

if str(root) in str(pathlib.Path(bioradar.__file__).resolve()):
    sys.exit(f"bioradar was imported from the checkout, not the venv: {bioradar.__file__}")
names = [m.name for m in pkgutil.walk_packages(bioradar.__path__, "bioradar.")]
checkout = sorted(
    "bioradar." + ".".join(f.relative_to(root / "services" / "bioradar").with_suffix("").parts)
    for f in (root / "services" / "bioradar").rglob("*.py")
)
checkout = sorted(n.removesuffix(".__init__") for n in checkout if n != "bioradar.__init__")
if sorted(names) != checkout:
    sys.exit(f"the installed package is not this checkout: {sorted(set(checkout) ^ set(names))}")
failed = []
for name in names:
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - every failure is reported, the check fails below
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
if failed:
    sys.exit("modules that do not import from a clean install:\n" + "\n".join(failed))
print(f"clean install ok: extraction-contract {installed}, {len(names)} bioradar modules imported")
EOF
