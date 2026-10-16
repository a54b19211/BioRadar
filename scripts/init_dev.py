"""Create additive local settings without copying any upstream credentials."""

import os
import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "ops/.env"
defaults = {
    "COMPOSE_PROJECT_NAME": os.environ.get("COMPOSE_PROJECT_NAME", "bioradar-dev"),
    "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
    "POSTGRES_PORT": "15436",
    "REDIS_PORT": "16379",
    "API_PORT": "18100",
    "WEB_PORT": "15176",
    "ENGINE_DEV_CREDENTIAL": secrets.token_urlsafe(32),
    "ENGINE_SOURCE_ALLOWLIST": '["fixture","datafabric-test"]',
    "ENGINE_SOURCE_ID_ALLOWLIST": '{"fixture":["fixture-source"],"datafabric-test":["clinicaltrials"]}',
}
existing = path.read_text() if path.exists() else ""
keys = {
    line.split("=", 1)[0]
    for line in existing.splitlines()
    if "=" in line and not line.lstrip().startswith("#")
}
additions = {key: value for key, value in defaults.items() if key not in keys}
if additions:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        for key, value in additions.items():
            handle.write(f"{key}={value}\n")
    path.chmod(0o600)
    print(f"Added {len(additions)} missing local settings; existing values unchanged; secrets not printed")
else:
    print("ops/.env already contains required local settings; unchanged")
