# Hexgate MCP server image. Build from the REPO ROOT, so docs/policy/ is in the
# context (platform/mcp/Dockerfile.dockerignore keeps it), amd64-only (deploy
# arch):
#   docker build --platform linux/amd64 -f platform/mcp/Dockerfile -t hexgate-mcp .
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Deps in their own layer so they cache until the lock changes. package=false:
# the server runs from source (copied below).
COPY platform/mcp/pyproject.toml platform/mcp/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY platform/mcp/hexgate_mcp ./hexgate_mcp

# The policy reference tool serves these, from the nearest docs/policy above
# its module. Fail the build, not the first call, if the copy came up empty.
COPY docs/policy/ ./docs/policy/
RUN ls docs/policy/*.mdx > /dev/null

# Drop root. HOME=/app (owned below) keeps uv's cache writable.
ENV HOME=/app
RUN groupadd --gid 10001 hexgate \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app hexgate \
    && chown -R hexgate:hexgate /app
USER hexgate

EXPOSE 8080

# Liveness only: /health checks neither the API nor its JWKS.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=4).status==200 else 1)"]

# __main__ sets the log format and trusts the proxy's X-Forwarded-* headers.
CMD ["uv", "run", "--no-sync", "python", "-m", "hexgate_mcp"]
