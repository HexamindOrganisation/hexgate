# Hexgate — Agent Instructions

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

## Agent Instruction Files
- These files follow [AGENTS.md](https://agents.md): the nearest one wins, so `platform/api/AGENTS.md` applies under `platform/api/` and this one everywhere else.
- **Claude Code reads them only while no `CLAUDE.md` exists here or above.** Adding one — including a personal `CLAUDE.local.md` — makes Claude read that instead and ignore every `AGENTS.md` in the repo. Keep your local notes in `~/.claude/CLAUDE.md`, which does not count, or set **Project instructions** to `claude-md-and-agents-md` in `/config`.
- Repo skills live in `.claude/skills/`. That path is Claude-specific and deliberate: nothing under `.agents/` is read.
