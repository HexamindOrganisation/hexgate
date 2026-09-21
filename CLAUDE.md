# Hexgate — Claude Code Instructions

## Tech Stack
- Python ≥ 3.13 (`uv`), FastAPI (`platform/api/`), ClickHouse (Docker), Redpanda (Docker, Kafka-protocol-compatible; dev broker in `platform/docker-compose.yml`, deployed as the OTLP span buffer in `platform/docker-compose.deploy.yml`)
- React, Vite, pnpm (`platform/dashboard/`)
- Ruff (Python). WASM via `wasmtime`.

## Cross-package
After you make changes across multiple packages, run: `make check-all`  # all packages

## Repo Layout
hexgate/              # SDK source
platform/api/         # FastAPI control plane (separate uv project)
platform/api/tests/   # API tests
platform/collector/   # OTLP ingestion Collector (Go)
platform/dashboard/   # React/Vite frontend
tests/                # hexgate package tests (agents, cli, security, tracing, streaming…)

## AI Constraints (CRITICAL)
- NEVER fabricate code examples, config snippets, or file contents. If unknown, read the file first.
- Verify third-party or config behavior against actual files in this repo before generating code.

## Rules & Constants
- **Branches:** `{initials}/{type}/{short_description}` (e.g., `vl/feat/web_search`)
- **Commits:** `type(scope): description` (lowercase, imperative, no period). Scopes: `platform-api`, `platform-scripts`, `dashboard`, `sdk`, `cli`, `clickhouse`, `redpanda`, `collector`. Types: `feat`, `fix`, `docs`, `build`, `refactor`, `test`.
- **Envs:** All prefixed with `HEXGATE_`. Never commit private keys.

## Worktrees
`EnterWorktree` names the branch `worktree-<name>` (slashes in the name are sanitized to `+`), which violates the branch convention above. After entering a worktree, immediately rename the branch:

    git branch -m {initials}/{type}/{short_description}

`ExitWorktree` still tracks the pre-rename name, so do not rely on its `remove` action to delete a renamed branch.

A fresh worktree is source-only — no `.venv`, no `node_modules`. Before any `make check` / `make check-all`:

    make install-dev        # uv sync --extra dev: pytest + ruff, required by `make check`
    make dashboard-install  # only when touching platform/dashboard/

Worktrees branch from `origin/main`, not from the local `main`. Two consequences:
- Uncommitted work in the main checkout is invisible inside a worktree.
- Untracked directories (notably `plans/`) do not exist in a worktree. Read them from the main checkout by absolute path — the launch prompt must pass that path explicitly.
