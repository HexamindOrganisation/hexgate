# Hexgate assistant image. Build from the REPO ROOT (the context every platform
# image shares; platform/assistant/Dockerfile.dockerignore allowlists what is
# sent), for linux/amd64 (the box) and linux/arm64 (published images):
#   docker build --platform linux/amd64 -f platform/assistant/Dockerfile -t hexgate-assistant .
# Images pinned by digest (tag kept for readability); Dependabot proposes the
# bumps.

# The uv binary only, for the image below.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

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
# the assistant runs from source (copied below). Bytecode is compiled at build
# time, for the venv and the source: the runtime user cannot write __pycache__
# into the root-owned /app, so it would otherwise recompile on every start.
COPY platform/assistant/pyproject.toml platform/assistant/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev

COPY platform/assistant/hexgate_assistant ./hexgate_assistant
RUN uv run --no-sync python -m compileall -q hexgate_assistant

# Drop root. /app stays root-owned, so a compromised process cannot rewrite the
# code it runs; its own HOME keeps uv's cache writable. HOME is set after the
# RUN, so no root step can leave a root-owned dir in it.
RUN groupadd --gid 10001 hexgate \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/hexgate hexgate
ENV HOME=/home/hexgate
USER hexgate

# All interfaces: the container is reached through the compose network and a
# published loopback port.
ENV HEXGATE_ASSISTANT_BIND_HOST=0.0.0.0

EXPOSE 8090

# Liveness only: /v1/assistant/health checks no dependency once served, but the
# process exits at start-up, before serving it, when its database is unreachable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8090/v1/assistant/health', timeout=4).status==200 else 1)"]

# __main__ sets the log format and trusts the proxy's X-Forwarded-* headers.
CMD ["uv", "run", "--no-sync", "python", "-m", "hexgate_assistant"]
