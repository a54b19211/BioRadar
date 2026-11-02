UV ?= .tools/bootstrap/bin/uv
COMPOSE = docker compose --env-file ops/.env -f ops/compose.yaml
NODE = docker run --rm --network host --user $$(id -u):$$(id -g) -e npm_config_cache=/tmp/npm-cache -e HTTP_PROXY -e HTTPS_PROXY -v "$$(pwd):/app" -w /app/apps/web node:22-bookworm-slim

.PHONY: help init bootstrap dev stop status migrate test lint web-check check
help:
	@echo 'make bootstrap   install Python dependencies with uv'
	@echo 'make init        create ops/.env with a random database password and login'
	@echo 'make dev         start postgres, redis, api, worker, monitor and web in Docker'
	@echo 'make check       run the three checks: test, lint, web-check'
init:
	test -f ops/.env || python3 -c "import secrets,pathlib; t=pathlib.Path('ops/env.example').read_text(); t=t.replace('replace-with-generated-local-password', secrets.token_urlsafe(18)).replace('replace-with-generated-login', secrets.token_urlsafe(12)); p=pathlib.Path('ops/.env'); p.write_text(t); p.chmod(0o600)"
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
web-check:
	$(NODE) sh -c 'npm ci && npm run check && npm run build'
check: test lint web-check
