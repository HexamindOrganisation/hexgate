#!/usr/bin/env bash
# One-time build for the Codespaces demo: install deps + build the dashboard.
# Runs as postCreateCommand. Mirrors `make demo-notebook-build`.
set -euo pipefail

echo "==> Installing uv + pnpm"
pip install --quiet uv
npm install -g pnpm@9

echo "==> Python deps (platform API + hexgate + marimo) into platform/api/.venv"
(cd platform/api && uv sync)
uv pip install --python platform/api/.venv marimo

echo "==> Building the dashboard SPA (served same-origin by the API)"
(cd platform/dashboard && pnpm install --frozen-lockfile && pnpm build)

echo "==> Setup complete — the demo will start when you attach."
