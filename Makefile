UV ?= .tools/bootstrap/bin/uv
COMPOSE = docker compose --env-file ops/.env -f ops/compose.yaml
NODE = docker run --rm --network host --user $$(id -u):$$(id -g) -e npm_config_cache=/tmp/npm-cache -e HTTP_PROXY -e HTTPS_PROXY -v "$$(pwd):/app" -w /app/apps/web node:22-bookworm-slim
CARD ?= BOOT
CASE ?= BOOT
MODE ?= real
MANIFEST ?=

.PHONY: help init bootstrap dev stop status migrate test history-test history-cli graph-test graph-cli monitor-test topics-test engine-integration contract-check engine-contract web-install web-check web-races decision-probes example smoke scenario snapshot-import evaluate lint
help:
	@echo 'make init && make dev       start isolated local framework'
	@echo 'make bootstrap             install Python development dependencies with uv'
	@echo 'make test CARD=BOOT         framework/adapter tests'
	@echo 'make test CARD=ENGINE       mining/runtime/adapter failure tests'
	@echo 'make contract-check        validate existing frozen contracts'
	@echo 'make web-check             generated types, TypeScript and production build'
	@echo 'make web-races             offline Vue delayed-response regression probes'
	@echo 'make decision-probes       independent decision semantic and Vue race probes'
	@echo 'make example               create a teaching investigation through the real API'
	@echo 'make smoke                 actual PostgreSQL/RQ/worker round trip'
	@echo 'make stop                  stop only BioRadar; preserve volumes'
init:
	python3 scripts/init_dev.py
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
	$(UV) run --frozen python scripts/task_entry.py test --card '$(CARD)'
engine-integration:
	$(UV) run --frozen python scripts/check_postgres_tests.py -m ENGINE
contract-check:
	$(UV) run --frozen python scripts/validate_contracts.py --output artifacts/contract-validation.json
engine-contract:
	$(UV) run --frozen python scripts/export_engine_contract.py
web-install:
	$(NODE) npm ci
web-check: web-install
	$(NODE) sh -c 'npm run generate && npm run check && npm run build'
web-races:
	docker run --rm --network none --user $$(id -u):$$(id -g) -v "$$(pwd):/app:ro" -w /app node:22-bookworm-slim node /app/scripts/check_engine_ui_races.cjs
	docker run --rm --network none --user $$(id -u):$$(id -g) -v "$$(pwd):/app:ro" -w /app node:22-bookworm-slim node /app/scripts/check_topic_ui_races.cjs
	docker run --rm --network none --user $$(id -u):$$(id -g) -v "$$(pwd):/app:ro" -w /app node:22-bookworm-slim node /app/scripts/check_analysis_ui_races.cjs
decision-probes:
	$(UV) run --frozen python scripts/check_decision_semantics.py
	docker run --rm --network none --user $$(id -u):$$(id -g) -v "$$(pwd):/app:ro" -w /app node:22-bookworm-slim node /app/scripts/check_decision_ui_races.cjs
example:
	$(UV) run --frozen python scripts/example.py
lint:
	$(UV) run --frozen ruff check services tests db/migrations scripts/init_dev.py scripts/task_entry.py scripts/export_engine_contract.py scripts/check_engine_runtime.py scripts/check_decision_runtime.py scripts/check_postgres_tests.py scripts/check_decision_semantics.py scripts/example.py scripts/history_e2e.py scripts/graph_e2e.py scripts/monitor_e2e.py scripts/topic_e2e.py scripts/topic_sources_e2e.py scripts/semantic_e2e.py
smoke:
	$(COMPOSE) exec -T api python -m bioradar.ops.probe
scenario:
	$(UV) run --frozen python scripts/task_entry.py scenario --case '$(CASE)' --mode '$(MODE)'
snapshot-import:
	$(UV) run --frozen python scripts/task_entry.py snapshot-import --manifest '$(MANIFEST)'
evaluate:
	$(UV) run --frozen python scripts/task_entry.py evaluate --manifest '$(MANIFEST)'

history-test:
	$(UV) run --frozen python scripts/check_postgres_tests.py tests/test_history.py
history-cli:
	$(UV) run --frozen python -m bioradar.ops.history_cli --help

graph-test:
	$(UV) run --frozen python scripts/check_postgres_tests.py tests/test_graph.py
graph-cli:
	$(UV) run --frozen python -m bioradar.ops.history_cli graph --help

monitor-test:
	$(UV) run --frozen python scripts/check_postgres_tests.py tests/test_monitor.py

topics-test:
	$(UV) run --frozen python scripts/check_postgres_tests.py tests/test_topics.py
