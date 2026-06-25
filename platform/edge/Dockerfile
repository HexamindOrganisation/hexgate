# Edge image: Caddy serving the dashboard SPA + reverse-proxying the API.
# Build from the REPO ROOT:
#   docker build --platform linux/amd64 -f platform/edge/Dockerfile -t hexgate-edge .
# Dockerfile.dockerignore overrides the repo-root .dockerignore to keep the
# dashboard source (which the root one excludes) in the build context.

# ---- build the SPA -------------------------------------------------------
# node 20 + pnpm 9 match CI.
FROM node:20-bookworm-slim AS build
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable && corepack prepare pnpm@9 --activate
WORKDIR /app

# Manifest + lock first so the dep layer caches independently of source.
COPY platform/dashboard/package.json platform/dashboard/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile

# Source last. `pnpm build` (tsc + vite) → ./dist. The bundle uses relative /v1
# + location.host, so one build is domain-agnostic.
COPY platform/dashboard/ ./
RUN pnpm build

# ---- serve ---------------------------------------------------------------
FROM caddy:2-alpine
# Baked-in SPA + config = one immutable artifact. Caddy auto-loads the Caddyfile.
COPY --from=build /app/dist /srv
COPY platform/edge/Caddyfile /etc/caddy/Caddyfile
EXPOSE 80 443
