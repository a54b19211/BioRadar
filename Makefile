UV ?= .tools/bootstrap/bin/uv
COMPOSE = docker compose --env-file ops/.env -f ops/compose.yaml
NODE = docker run --rm --network host --user $$(id -u):$$(id -g) -e npm_config_cache=/tmp/npm-cache -e HTTP_PROXY -e HTTPS_PROXY -v "$$(pwd):/app" -w /app/apps/web node:22-bookworm-slim

.PHONY: help init bootstrap dev stop status migrate test lint metrics web-check check check-install check-migrations
help:
	@echo 'make bootstrap   install Python dependencies with uv'
	@echo 'make init        create ops/.env with a random database password and login'
	@echo 'make dev         start postgres, redis, api, worker, monitor and web in Docker'
	@echo 'make check       run the checks: test, lint, metrics, web-check'
	@echo 'make check-install  fresh venv from the declared pins; import every bioradar module'
	@echo 'make check-migrations  upgrade / downgrade / upgrade on a throwaway PostgreSQL 17 container'
init:
	test -f ops/.env || python3 -c "import secrets,pathlib; t=pathlib.Path('ops/env.example').read_text(); t=t.replace('replace-with-generated-local-password', secrets.token_urlsafe(18)).replace('replace-with-generated-login', secrets.token_urlsafe(12)).replace('replace-with-generated-session-key', secrets.token_urlsafe(32)); p=pathlib.Path('ops/.env'); p.write_text(t); p.chmod(0o600)"
bootstrap:
	python3 -m venv .tools/bootstrap
	.tools/bootstrap/bin/python -m pip install uv==0.13.0
	$(UV) sync --frozen --python 3.12
dev:
	$(COMPOSE) up -d --build
stop:
	$(COMPOSE) down
status:
	$(COMPOSE) ps
migrate:
	$(COMPOSE) run --rm migrate
test:
	$(UV) run --frozen pytest -q
lint:
	$(UV) run --frozen ruff check services tests db/migrations scripts
metrics:
	$(UV) run --frozen python scripts/code_metrics.py --check
web-check:
	$(NODE) sh -c 'npm ci && npm run check && npm run build'
check: test lint metrics web-check
check-install:
	UV=$(UV) sh scripts/check_install.sh
check-migrations:
	PYTHON="$(UV) run --frozen python" sh scripts/check_migrations.sh
