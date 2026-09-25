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
- Each `AGENTS.md` has a `CLAUDE.md` beside it holding one line, `@AGENTS.md`. Claude Code stops reading every `AGENTS.md` in a repo as soon as any `CLAUDE.md` exists at or above the working directory, so the pointers are what keep the nested files loading. Put Claude-specific notes under the import, not in `AGENTS.md`.
- Repo skills live in `.claude/skills/`. That path is Claude-specific and deliberate: nothing under `.agents/` is read.
