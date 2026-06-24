# HexaGate SDK — dev/test/build helpers.
#
# Most targets shell out to `uv`. The default flow assumes a uv-managed
# virtualenv (created by `make install-dev`). If you're driving uv with
# a pre-existing conda/micromamba environment, export the env path
# before invoking make:
#
#     export UV_PROJECT_ENVIRONMENT=$HOME/micromamba/envs/hexanlp-demo
#     make test
#
# `uv` picks up that variable and runs against your existing env
# instead of bootstrapping its own.

UV ?= uv run --active
TESTS ?= tests/

.DEFAULT_GOAL := help

# -------- Meta --------

.PHONY: help
help: ## Show this help
	@awk 'BEGIN{FS=":.*##"; printf "\nHexaGate SDK targets:\n\n"} /^[a-zA-Z0-9_.-]+:.*##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# -------- Setup --------

.PHONY: install
install: ## Install runtime deps via uv (creates .venv if needed)
	uv sync

.PHONY: install-dev
install-dev: ## Install with dev extras (pytest, ruff)
	uv sync --extra dev

# -------- Dev loop --------

.PHONY: test
test: ## Run the full test suite quietly
	$(UV) pytest $(TESTS) -q

.PHONY: test-verbose
test-verbose: ## Run tests with -v output
	$(UV) pytest $(TESTS) -v

.PHONY: test-failed
test-failed: ## Re-run only the tests that failed last time
	$(UV) pytest $(TESTS) --lf -v

.PHONY: test-one
test-one: ## Run one test path: make test-one T=tests/security/test_bundle.py
	@test -n "$(T)" || (echo "Set T=<path>, e.g. make test-one T=tests/security/test_bundle.py" && exit 1)
	$(UV) pytest $(T) -v

.PHONY: lint
lint: ## Static check via ruff
	$(UV) ruff check hexgate tests

.PHONY: lint-fix
lint-fix: ## Apply ruff autofixes
	$(UV) ruff check --fix hexgate tests

.PHONY: fmt
fmt: ## Format with ruff (SDK + platform/api)
	$(UV) ruff format hexgate tests platform/api

.PHONY: fmt-check
fmt-check: ## Check formatting without writing changes
	$(UV) ruff format --check hexgate tests platform/api

.PHONY: check
check: lint fmt-check test ## Python CI parity: lint + fmt-check + test

.PHONY: check-all
check-all: check dashboard-lint dashboard-typecheck dashboard-fmt-check ## Full stack check: Python + dashboard lint, typecheck, fmt, tests
	cd platform/dashboard && pnpm test --run

# -------- Policy / M2 demo helpers --------

.PHONY: policy-build
policy-build: ## Compile examples/example_agent/policy.yaml to a bundle under /tmp/m2-bundle
	$(UV) hexgate policy build examples/example_agent/policy.yaml --out /tmp/m2-bundle

.PHONY: policy-test-wasm
policy-test-wasm: ## Smoke a wasm-engine decision on the example policy
	$(UV) hexgate policy test examples/example_agent/policy.yaml \
	    --role default --tool web_search --args '{}' --engine wasm

.PHONY: demo-override
demo-override: ## Build a deny-everything bundle + chat with HEXGATE_LOCAL_POLICY set
	@echo "→ Writing a deny-everything override policy…"
	@printf 'version: 1\nroles:\n  default:\n    tools:\n      web_search: { mode: deny }\n      fetch: { mode: deny }\n' > /tmp/m2-deny-policy.yaml
	$(UV) hexgate policy build /tmp/m2-deny-policy.yaml --out /tmp/m2-deny-bundle
	@echo ""
	@echo "→ Starting chat with HEXGATE_LOCAL_POLICY=/tmp/m2-deny-bundle"
	@echo "  Try a prompt that would trigger web_search; expect a wasm-engine deny."
	@echo ""
	HEXGATE_LOCAL_POLICY=/tmp/m2-deny-bundle $(UV) hexgate chat --agent researcher --approval-mode auto-deny

# -------- Platform infra (ClickHouse audit log) --------
#
# Docker Compose service definition lives in platform/docker-compose.yml.
# First `make clickhouse-up` on an empty volume runs the init scripts in
# platform/clickhouse/init/ and creates the policy_decision table.
# Subsequent schema changes don't auto-apply — use `make clickhouse-reset`
# (wipes data) or apply by hand via `make clickhouse-cli`.

COMPOSE := docker compose -f platform/docker-compose.yml

.PHONY: clickhouse-up
clickhouse-up: ## Start the local ClickHouse server (creates schema on first run)
	$(COMPOSE) up -d clickhouse

.PHONY: clickhouse-down
clickhouse-down: ## Stop ClickHouse (keeps the data volume)
	$(COMPOSE) down

.PHONY: clickhouse-logs
clickhouse-logs: ## Tail ClickHouse server logs
	$(COMPOSE) logs -f clickhouse

.PHONY: clickhouse-cli
clickhouse-cli: ## Open an interactive SQL shell against the local ClickHouse
	docker exec -it hexgate-clickhouse clickhouse-client \
	    --user hexgate --password hexgate-dev-password --database hexgate_audit

.PHONY: clickhouse-reset
clickhouse-reset: ## Wipe the data volume and re-run init scripts
	$(COMPOSE) down -v
	$(COMPOSE) up -d clickhouse

# -------- Platform infra (Postgres control-plane DB) --------
#
# Service lives in platform/docker-compose.yml. NOTE: clickhouse-reset runs
# `down` on the whole compose (stops Postgres too) — use postgres-* targets.

# DSN for the local postgres service (host port 5433, dev creds).
POSTGRES_DSN ?= postgresql+asyncpg://hexgate:hexgate-dev-password@localhost:5433/hexgate

.PHONY: postgres-up
postgres-up: ## Start local Postgres and wait until healthy
	$(COMPOSE) up -d --wait postgres

.PHONY: postgres-stop
postgres-stop: ## Stop Postgres (keeps the data volume)
	$(COMPOSE) stop postgres

.PHONY: postgres-psql
postgres-psql: ## Open a psql shell against local Postgres
	docker exec -it hexgate-postgres psql -U hexgate -d hexgate

.PHONY: postgres-reset
postgres-reset: ## Wipe ONLY the Postgres data volume and restart
	$(COMPOSE) rm -sf postgres
	-docker volume rm platform_postgres-data
	$(COMPOSE) up -d --wait postgres

# -------- Platform API (FastAPI control plane) --------
#
# The platform API is a separate uv project under platform/api/ with its
# own pyproject.toml. We invoke uv from there directly so it uses the
# platform's venv, not the SDK's.

.PHONY: platform-api-install
platform-api-install: ## Install platform API deps (first time)
	cd platform/api && uv sync --group dev

.PHONY: platform-api
platform-api: ## Run the platform API dev server (FastAPI on :8000, SQLite)
	cd platform/api && uv run uvicorn main:app --reload --port 8000

.PHONY: platform-api-pg
platform-api-pg: postgres-up ## Run the platform API against local Postgres (starts PG first)
	cd platform/api && DATABASE_URL=$(POSTGRES_DSN) uv run uvicorn main:app --reload --port 8000

.PHONY: platform-api-test
platform-api-test: ## Run the platform API test suite
	cd platform/api && uv run pytest tests/

# -------- Platform API image (Docker) --------
#
# Build from repo root (in-repo SDK path dep). amd64-only (deploy arch + OPA
# binary); emulated on arm.

API_IMAGE ?= hexgate-api

.PHONY: platform-api-image
platform-api-image: ## Build the control-plane API image (amd64, from repo root)
	docker build --platform linux/amd64 -f platform/api/Dockerfile -t $(API_IMAGE) .

.PHONY: platform-api-docker
platform-api-docker: postgres-up ## Run the API image against local Postgres (:8000)
# Joins the compose network to reach `postgres`; keystore on a named volume to
# persist across restarts. ClickHouse unreachable here (→ /ready 503); audit is
# validated in the prod compose.
	docker run --rm -it --name $(API_IMAGE) \
	    --network platform_default \
	    -p 8000:8000 \
	    -e DATABASE_URL=postgresql+asyncpg://hexgate:hexgate-dev-password@postgres:5432/hexgate \
	    -e HEXGATE_KEYSTORE_PATH=/keys \
	    -v hexgate-api-keys:/keys \
	    $(API_IMAGE)

.PHONY: seed-audit
seed-audit: ## Seed ClickHouse with audit test data (anomaly detection)
	cd platform/api && uv run python ../scripts/seed_audit.py

.PHONY: seed-audit-clear
seed-audit-clear: ## Clear seeded audit test data
	cd platform/api && uv run python ../scripts/seed_audit.py --clear

# -------- Dashboard (Vite + React) --------
#
# Uses pnpm. `pnpm dev` runs Vite on :5173 and proxies /v1/* to :8000,
# so the dashboard needs the platform-api target running in another
# terminal.

.PHONY: dashboard-install
dashboard-install: ## Install dashboard JS deps (first time)
	cd platform/dashboard && pnpm install

.PHONY: dashboard
dashboard: ## Run the dashboard dev server (Vite on :5173)
	cd platform/dashboard && pnpm dev

.PHONY: dashboard-fmt
dashboard-fmt: ## Format dashboard TypeScript with prettier
	cd platform/dashboard && pnpm format

.PHONY: dashboard-fmt-check
dashboard-fmt-check: ## Check dashboard TypeScript formatting (prettier)
	cd platform/dashboard && pnpm format:check

.PHONY: dashboard-lint
dashboard-lint: ## Lint dashboard TypeScript with eslint
	cd platform/dashboard && pnpm lint

.PHONY: dashboard-typecheck
dashboard-typecheck: ## Typecheck dashboard TypeScript
	cd platform/dashboard && pnpm typecheck

# -------- SDK → platform bridge --------

# Make's rule parser treats colons specially, so a positional
# `make serve examples.foo:bar` won't work — the colon makes Make
# read it as a target+prerequisite. Two clean ways to pick a
# different agent:
#
#   make serve AGENT_SPEC=examples.foo:bar        # variable form
#   uv run hexgate serve examples.foo:bar         # skip make entirely
#
# Bare `make serve` defaults to the customer_bot demo for the
# hexgate-canonical workflow.
AGENT_SPEC ?= examples.customer_bot:agent

.PHONY: serve
serve: ## Run `hexgate serve` on the customer_bot demo (override with AGENT_SPEC=)
# Reads HEXGATE_KEY from asianf/.env at startup. Uvicorn-style spec —
# the agent name + tools come from the loaded object, no env vars to
# keep in sync.
	$(UV) hexgate serve $(AGENT_SPEC)

# -------- Full platform demo (multi-terminal) --------

.PHONY: demo-platform
demo-platform: ## Print 3-terminal instructions for the full platform demo
	@echo ""
	@echo "Platform demo — open three terminals in this directory (asianf/):"
	@echo ""
	@echo "  Terminal 1 — FastAPI backend (control plane):"
	@echo "      make platform-api"
	@echo ""
	@echo "  Terminal 2 — dashboard (Vite + React, http://localhost:5173):"
	@echo "      make dashboard"
	@echo ""
	@echo "  Terminal 3 — your local agent bridged to the platform:"
	@echo "      1. Open  http://localhost:5173/tokens  and mint a dev token"
	@echo "      2. Add to asianf/.env:  HEXGATE_KEY=fty_live_..."
	@echo "      3. make serve  (or: hexgate serve <your.module:agent>)"
	@echo ""
	@echo "Then chat with the live agent at  http://localhost:5173/playground"
	@echo ""
	@echo "First-time setup (run once):"
	@echo "      make platform-api-install"
	@echo "      make dashboard-install"
	@echo ""

# -------- Bundled notebook demo (one process locally / per-container on Modal) --------
#
# Unlike `demo-platform` (3 terminals, manual login + token), this bundles the
# whole thing into one process: the API serves the built dashboard same-origin,
# auto-seeds + auto-logs-in, and a marimo notebook owns `hexgate serve`. The
# visitor brings their own OpenAI key (BYOK). This is also what runs per visitor
# in GitHub Codespaces (see .devcontainer/). See deploy/README.md.

.PHONY: demo-notebook-build
demo-notebook-build: platform-api-install dashboard-install ## One-time setup for `make demo-notebook` (deps + marimo + dashboard build)
	uv pip install --python platform/api/.venv marimo
	cd platform/dashboard && pnpm build

.PHONY: demo-notebook
demo-notebook: ## Run the bundled BYOK demo locally (one process). Open http://localhost:2718
	PATH="$(CURDIR)/platform/api/.venv/bin:$$PATH" \
	  HEXGATE_DEMO=1 HEXGATE_COOKIE_SECURE=0 \
	  python deploy/boot.py

.PHONY: demo-smoke
demo-smoke: ## Smoke-test the bundled demo with a mock LLM (no real key)
	cd platform/api && uv run python "$(CURDIR)/deploy/smoke_test.py"

# -------- Package --------

.PHONY: build
build: ## Build sdist + wheel into dist/
	uv build

.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
