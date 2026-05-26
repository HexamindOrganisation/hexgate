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

# -------- Package --------

.PHONY: build
build: ## Build sdist + wheel into dist/
	uv build

.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
