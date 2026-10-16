"""Run ENGINE tests against only the configured local BioRadar PostgreSQL.

The tests use disposable schemas. Database credentials stay out of process argv
and tool output; this does not use or modify any shared upstream database.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

root = Path(__file__).resolve().parents[1]
values = dict(
    line.split("=", 1)
    for line in (root / "ops/.env").read_text().splitlines()
    if "=" in line and not line.lstrip().startswith("#")
)
env = os.environ.copy()
env["BIORADAR_ENGINE_TEST_DATABASE_URL"] = (
    "postgresql+psycopg://bioradar:"
    + quote(values["POSTGRES_PASSWORD"], safe="")
    + f"@127.0.0.1:{int(values.get('POSTGRES_PORT', '15436'))}/bioradar"
)
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=root, env=env))
