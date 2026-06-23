# Hexgate control-plane API image.
#
# Build from the REPO ROOT — the hexgate SDK is an in-repo path dependency
# (platform/api depends on `hexgate = { path = "../.." }`), so it must be in
# the build context:
#
#   docker build -f platform/api/Dockerfile -t hexgate-api .
#
# Requires the repo-root .dockerignore to keep venvs/caches/secrets out of the
# context. Target amd64 (the deploy arch); on an arm host build with
# `--platform linux/amd64`.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# OPA — a runtime dependency: the control plane shells out to `opa` to compile
# and sign policy bundles. Without it, policy saves silently yield unsigned
# bundles. Pinned to match CI (.github/workflows/tests.yml).
ADD https://openpolicyagent.org/downloads/v0.70.0/opa_linux_amd64_static /usr/local/bin/opa
RUN chmod +x /usr/local/bin/opa && opa version

WORKDIR /app

# SDK source — what setuptools needs to build the hexgate wheel.
COPY pyproject.toml README.md LICENSE ./
COPY hexgate ./hexgate

# Dependencies in their own layer, so they cache until a manifest/lock changes.
# platform/api sets `package = false`, so only deps (incl. the SDK, installed
# non-editable) land in the venv — not the API project itself, which runs from
# source below. The lock references the SDK at ../.., resolved from /app.
COPY platform/api/pyproject.toml platform/api/uv.lock ./platform/api/
WORKDIR /app/platform/api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# API source last — edits here don't invalidate the dependency layer above.
COPY platform/api ./

EXPOSE 8000

# Single worker: the WebSocket relay holds connections in process memory and
# cannot span workers (or instances). --no-sync: use the venv built above, no
# runtime re-resolution.
CMD ["uv", "run", "--no-sync", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
