# Hexgate edge image: Caddy serving the dashboard SPA + reverse-proxying the
# control-plane API, with automatic HTTPS.
#
# Build from the REPO ROOT (the dashboard source lives at platform/dashboard/):
#   docker build --platform linux/amd64 -f platform/edge/Dockerfile -t hexgate-edge .
#
# The repo-root .dockerignore excludes platform/dashboard/ (it's not part of
# the API image), so this build relies on platform/edge/Dockerfile.dockerignore
# to override it and KEEP the dashboard source in the build context.

# ---- build the SPA -------------------------------------------------------
# node 20 + pnpm 9 match CI (.github/workflows/tests.yml).
FROM node:20-bookworm-slim AS build
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable && corepack prepare pnpm@9 --activate
WORKDIR /app

# Manifest + lock first so the dependency layer caches until they change.
COPY platform/dashboard/package.json platform/dashboard/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile

# Source last so edits don't bust the install layer. `pnpm build` runs
# `tsc -b && vite build` → ./dist. The bundle hits relative /v1 paths and
# location.host for the WebSocket, so it's domain-agnostic — one build serves
# any deployment, no per-environment rebuild.
COPY platform/dashboard/ ./
RUN pnpm build

# ---- serve ---------------------------------------------------------------
FROM caddy:2-alpine
# Baked-in SPA + config → one immutable artifact, no shared volumes. Caddy's
# default CMD runs `caddy run --config /etc/caddy/Caddyfile --adapter caddyfile`.
COPY --from=build /app/dist /srv
COPY platform/edge/Caddyfile /etc/caddy/Caddyfile
EXPOSE 80 443
