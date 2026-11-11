# Hexgate control-plane API image. Build from the REPO ROOT (platform/api has
# an in-repo path dep on the hexgate SDK), amd64-only (deploy arch):
#   docker build --platform linux/amd64 -f platform/api/Dockerfile -t hexgate-api .
# Serves the dashboard SPA same-origin (no separate edge container) — the build
# stage below bakes platform/dashboard/dist into the image. The build context is
# the allowlist in platform/api/Dockerfile.dockerignore, which BuildKit uses
# instead of the repo-root .dockerignore.

# ---- build the dashboard SPA ---------------------------------------------
# node 22 matches CI; pnpm is the exact version package.json's packageManager
# pins, hash included, so corepack refuses a tampered download. The bundle uses
# relative /v1 + location.host, so one build is domain-agnostic and served
# same-origin by the API below.
# Base images are pinned by digest (tag kept for readability), so a re-pushed
# tag can't change what we publish; Dependabot proposes the bumps.
FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS dashboard
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable
WORKDIR /app
# Manifest + lock first so the dep layer caches independently of source.
COPY platform/dashboard/package.json platform/dashboard/pnpm-lock.yaml ./
RUN corepack install
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile
COPY platform/dashboard/ ./
# Vite inlines VITE_* into the bundle at build time, so the preview banner is a
# BUILD ARG, not a runtime env. Off unless the build passes it (compose sources
# it from .env.<env>). Empty default keeps local/dev builds clean.
ARG VITE_PREVIEW_BANNER=
ENV VITE_PREVIEW_BANNER=$VITE_PREVIEW_BANNER
RUN pnpm build

# ---- control-plane API ---------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

# OPA: the control plane shells out to it to sign policy bundles. Match CI.
# Pin by SHA256 so a corrupted/tampered/MITM'd download fails the build (this
# binary signs production policy bundles). Update the checksum with the version.
ADD --checksum=sha256:2ab571f367f293bb19523b76f92f1945bbf245003392c7f392903f95c2b9b630 \
    https://openpolicyagent.org/downloads/v1.21.4/opa_linux_amd64_static /usr/local/bin/opa
RUN chmod +x /usr/local/bin/opa && opa version

# WeasyPrint's native stack: the AI Act evidence report renders through it.
# Pango does the text layout, so it and its font machinery are runtime
# dependencies of the API process, not build tooling. Liberation and DejaVu
# are the document's two faces (see render/report.css) — pinning them in the
# image is what makes a re-render of an old report look like the original,
# rather than like whatever host it landed on.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
      libharfbuzz-subset0 libfribidi0 \
      fonts-liberation2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SDK source (needed to build the hexgate wheel).
COPY pyproject.toml README.md LICENSE ./
COPY hexgate ./hexgate

# Deps in their own layer so they cache until the lock changes. package=false +
# --no-editable: only deps (incl. the SDK) land in the venv; the API runs from
# source (copied below).
COPY platform/api/pyproject.toml platform/api/uv.lock ./platform/api/
WORKDIR /app/platform/api
# --refresh-package is load-bearing, not redundant: uv keys the built wheel on
# the mtime of the path dep's pyproject.toml (/app/pyproject.toml), NOT on the
# source it builds from -- so any release that changed hexgate/ without touching
# that file gets a wheel built from stale source off the cache mount. A version
# bump only works as a side effect of rewriting that file. `--no-cache` does not
# help -- it invalidates layers, not cache mounts.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --refresh-package hexgate

# Guards the flag above: fail the build, not the deploy, if the installed wheel
# drifts from the source. `uv run` because the venv is not on PATH here. Compared
# both ways and across every file type, so a module missing from the wheel, one
# deleted from the source but retained in it, and drifted package data all fail
# the build. Markdown (AGENTS.md, CLAUDE.md) is in the source tree but not packaged.
RUN INSTALLED="$(uv run --no-sync python -c 'import hexgate, os; print(os.path.dirname(hexgate.__file__))')" \
    && if ! diff -r -x '*.md' -x '__pycache__' /app/hexgate "$INSTALLED"; then \
         echo 'ERROR: the installed hexgate SDK differs from the source in this image'; \
         echo 'Either the uv cache served a stale wheel (see --refresh-package above),'; \
         echo 'or a source file is not packaged (see [tool.setuptools] in pyproject.toml).'; \
         exit 1; fi \
    && echo 'SDK wheel matches the source in this image'

# API source last so edits don't bust the deps layer. The package only, not
# platform/api/: nothing else in that directory belongs in the image.
COPY platform/api/hexgate_api ./hexgate_api

# Built dashboard, served same-origin by the API (spa.mount_spa reads this path
# via HEXGATE_DASHBOARD_DIST). Under /app so the chown below covers it.
COPY --from=dashboard /app/dist /app/static
ENV HEXGATE_DASHBOARD_DIST=/app/static

# Drop root: a compromised API process must not run as uid 0 with write access
# to the /keys signing-key volume. The named hexgate-keys volume inherits the
# ownership of /keys from the image on first mount, so create it pre-owned by
# the unprivileged user. HOME=/app (owned below) keeps uv's cache writable.
ENV HOME=/app
RUN groupadd --gid 10001 hexgate \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app hexgate \
    && mkdir -p /keys \
    && chown -R hexgate:hexgate /app /keys
USER hexgate

EXPOSE 8000

# Liveness only: /health has no dependency checks (unlike /ready, which 503s on
# unreachable ClickHouse) so a degraded-but-running container isn't killed.
# urllib over curl/wget — neither is in the slim base, but python is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else 1)"]

# Single worker: the WS relay holds connections in-process, can't span workers.
# --no-sync: use the venv built above, no runtime re-resolution.
# --proxy-headers: trust X-Forwarded-Proto/For from the box's reverse proxy
# (TLS terminates upstream), so request.url is https for OAuth callbacks + link
# generation. The proxy MUST set X-Forwarded-Proto: https. Trusted only from
# the addresses in FORWARDED_ALLOW_IPS (uvicorn reads it; loopback when unset):
# the published image may run with no proxy in front, where trusting everyone
# would let any client forge its scheme and address.
CMD ["uv", "run", "--no-sync", "uvicorn", "hexgate_api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers"]
