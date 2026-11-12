# Hexgate MCP server image. Build from the REPO ROOT, so docs/policy/ is in the
# context (platform/mcp/Dockerfile.dockerignore keeps it), for linux/amd64 (the
# box) and linux/arm64 (published images):
#   docker build --platform linux/amd64 -f platform/mcp/Dockerfile -t hexgate-mcp .
# Images pinned by digest (tag kept for readability); Dependabot proposes the
# bumps.

# The uv binary only, for the image below.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# The official Python image, not uv's python image: it is rebuilt with each
# Debian point release, where uv's lagged and shipped fixable openssl/gnutls
# CVEs. Same base as platform/api/Dockerfile.
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

COPY --from=uv /uv /usr/local/bin/uv
# uv runs on this interpreter and never downloads its own. pip and setuptools
# come with the image but nothing here uses them (uv installs everything), and
# their vendored copies carry their own CVEs: removed.
ENV UV_PYTHON_DOWNLOADS=never
RUN python -m pip uninstall -y -q pip setuptools wheel

WORKDIR /app

# Deps in their own layer so they cache until the lock changes. package=false:
# the server runs from source (copied below). Bytecode is compiled at build
# time, for the venv and the source: the runtime user cannot write __pycache__
# into the root-owned /app, so it would otherwise recompile on every start.
COPY platform/mcp/pyproject.toml platform/mcp/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev

COPY platform/mcp/hexgate_mcp ./hexgate_mcp
RUN uv run --no-sync python -m compileall -q hexgate_mcp

# The policy reference tool serves these, from the nearest docs/policy above
# its module. Fail the build, not the first call, if the copy came up empty.
COPY docs/policy/ ./docs/policy/
RUN ls docs/policy/*.mdx > /dev/null

# Drop root. /app stays root-owned, so a compromised process cannot rewrite the
# code it runs; its own HOME keeps uv's cache writable. HOME is set after the
# RUN, so no root step can leave a root-owned dir in it.
RUN groupadd --gid 10001 hexgate \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/hexgate hexgate
ENV HOME=/home/hexgate
USER hexgate

EXPOSE 8080

# Liveness only: /health checks neither the API nor its JWKS.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=4).status==200 else 1)"]

# __main__ sets the log format and trusts the proxy's X-Forwarded-* headers.
CMD ["uv", "run", "--no-sync", "python", "-m", "hexgate_mcp"]
