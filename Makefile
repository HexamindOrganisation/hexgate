# Fortify SDK — dev/test/build helpers.
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
	@awk 'BEGIN{FS=":.*##"; printf "\nFortify SDK targets:\n\n"} /^[a-zA-Z0-9_.-]+:.*##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

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
	$(UV) ruff check fortify tests

.PHONY: lint-fix
lint-fix: ## Apply ruff autofixes
	$(UV) ruff check --fix fortify tests

.PHONY: fmt
fmt: ## Format with ruff
	$(UV) ruff format fortify tests

.PHONY: fmt-check
fmt-check: ## Check formatting without writing changes
	$(UV) ruff format --check fortify tests

.PHONY: check
check: lint fmt-check test ## All static + dynamic checks (CI parity)

# -------- Policy / M2 demo helpers --------

.PHONY: policy-build
policy-build: ## Compile examples/example_agent/policy.yaml to a bundle under /tmp/m2-bundle
	$(UV) fortify policy build examples/example_agent/policy.yaml --out /tmp/m2-bundle

.PHONY: policy-test-wasm
policy-test-wasm: ## Smoke a wasm-engine decision on the example policy
	$(UV) fortify policy test examples/example_agent/policy.yaml \
	    --role default --tool web_search --args '{}' --engine wasm

.PHONY: demo-override
demo-override: ## Build a deny-everything bundle + chat with FORTIFY_LOCAL_POLICY set
	@echo "→ Writing a deny-everything override policy…"
	@printf 'version: 1\nroles:\n  default:\n    tools:\n      web_search: { mode: deny }\n      fetch: { mode: deny }\n' > /tmp/m2-deny-policy.yaml
	$(UV) fortify policy build /tmp/m2-deny-policy.yaml --out /tmp/m2-deny-bundle
	@echo ""
	@echo "→ Starting chat with FORTIFY_LOCAL_POLICY=/tmp/m2-deny-bundle"
	@echo "  Try a prompt that would trigger web_search; expect a wasm-engine deny."
	@echo ""
	FORTIFY_LOCAL_POLICY=/tmp/m2-deny-bundle $(UV) fortify chat --agent researcher --approval-mode auto-deny

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
	docker exec -it fortify-clickhouse clickhouse-client \
	    --user fortify --password fortify-dev-password --database fortify_audit

.PHONY: clickhouse-reset
clickhouse-reset: ## Wipe the data volume and re-run init scripts
	$(COMPOSE) down -v
	$(COMPOSE) up -d clickhouse

# -------- Platform API (FastAPI control plane) --------
#
# The platform API is a separate uv project under platform/api/ with its
# own pyproject.toml. We invoke uv from there directly so it uses the
# platform's venv, not the SDK's.

.PHONY: platform-api-install
platform-api-install: ## Install platform API deps (first time)
	cd platform/api && uv sync --extra dev

.PHONY: platform-api
platform-api: ## Run the platform API dev server (FastAPI on :8000)
	cd platform/api && uv run uvicorn main:app --reload --port 8000

.PHONY: platform-api-test
platform-api-test: ## Run the platform API test suite
	cd platform/api && uv run pytest tests/

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

# -------- SDK → platform bridge --------

.PHONY: serve
serve: ## Run `fortify serve` — bridges this SDK to the platform's relay
# Reads FORTIFY_KEY from asianf/.env via dotenv at startup (not from the
# shell env), so we don't pre-check here — `fortify serve` will fail
# loudly if the key is missing. Mint one at http://localhost:5173/tokens.
	$(UV) fortify serve

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
	@echo "      2. Add to asianf/.env:  FORTIFY_KEY=fty_test_..."
	@echo "      3. make serve"
	@echo ""
	@echo "Then chat with the live agent at  http://localhost:5173/playground"
	@echo ""
	@echo "First-time setup (run once):"
	@echo "      make platform-api-install"
	@echo "      make dashboard-install"
	@echo ""

# -------- Package --------

.PHONY: build
build: ## Build sdist + wheel into dist/
	uv build

.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
