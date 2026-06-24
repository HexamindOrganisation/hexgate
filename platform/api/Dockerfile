# Hexgate control-plane API image. Build from the REPO ROOT (platform/api has
# an in-repo path dep on the hexgate SDK), amd64-only (deploy arch):
#   docker build --platform linux/amd64 -f platform/api/Dockerfile -t hexgate-api .
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# OPA: the control plane shells out to it to sign policy bundles. Match CI.
ADD https://openpolicyagent.org/downloads/v0.70.0/opa_linux_amd64_static /usr/local/bin/opa
RUN chmod +x /usr/local/bin/opa && opa version

WORKDIR /app

# SDK source (needed to build the hexgate wheel).
COPY pyproject.toml README.md LICENSE ./
COPY hexgate ./hexgate

# Deps in their own layer so they cache until the lock changes. package=false +
# --no-editable: only deps (incl. the SDK) land in the venv; the API runs from
# source (copied below).
COPY platform/api/pyproject.toml platform/api/uv.lock ./platform/api/
WORKDIR /app/platform/api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# API source last so edits don't bust the deps layer.
COPY platform/api ./

EXPOSE 8000

# Liveness only: /health has no dependency checks (unlike /ready, which 503s on
# unreachable ClickHouse) so a degraded-but-running container isn't killed.
# urllib over curl/wget — neither is in the slim base, but python is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else 1)"]

# Single worker: the WS relay holds connections in-process, can't span workers.
# --no-sync: use the venv built above, no runtime re-resolution.
CMD ["uv", "run", "--no-sync", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
