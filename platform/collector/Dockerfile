# hexgate-collector deploy image (service `collector` in
# platform/docker-compose.deploy.yml; local dev keeps running the binary
# natively via `make collector-run`). The Go source here is committed ocb
# output (`make collector-generate`), so this is a plain `go build` — no
# builder tool at image-build time.

# Match go.mod's directive; bump together. Base images are pinned by digest
# (tag kept for readability); Dependabot proposes the bumps.
# Runs on the BUILD platform and cross-compiles for the target (Go needs no
# emulation for that), so a multi-arch build compiles natively.
FROM --platform=$BUILDPLATFORM golang:1.26-bookworm@sha256:dc40a13b61304647ed7e5d581d246b86413dfc29d63c6d018310feb93cd98fc0 AS build
ARG TARGETOS TARGETARCH

WORKDIR /src

# Manifests first so the module download caches until a dependency changes.
# builder-config.yaml `replaces` the biscuit-auth extension to its in-tree
# path, so its manifest must exist before `go mod download` parses go.mod.
COPY go.mod go.sum ./
COPY extension/hexgatebiscuitauth/go.mod extension/hexgatebiscuitauth/go.sum ./extension/hexgatebiscuitauth/
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
# CGO off: nothing here needs cgo, and a static binary keeps the runtime
# image's libc out of the compatibility equation.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -o /out/hexgate-collector .

# Slim-not-distroless on purpose: the compose healthcheck is a bash /dev/tcp
# probe (the ocb manifest builds no health extension into the collector, so
# there is no HTTP endpoint to hit).
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

COPY --from=build /out/hexgate-collector /usr/local/bin/hexgate-collector
# The image ships the same config.yaml dev runs; every deploy-varying value in
# it (bind addresses, brokers, DSN, public key path) is an ${env:...} lookup,
# set by the compose service.
COPY config.yaml /etc/hexgate/config.yaml

# The same gate `make collector-check` ends with, because no CI job runs it:
# building this image is the only automated step that touches config.yaml.
# Here, not in the build stage, because it runs the TARGET binary (emulated
# for a foreign architecture; ~1s either way), which is also a smoke test of
# the binary that ships.
# `validate` is a dry run: it resolves the ${env:...:-default} lookups, runs
# each component's Validate() and builds the pipeline graph, but never reads
# the public key file or opens Postgres/Kafka, so it needs no env here (the
# defaults are the dev ones, whose localhost DSN passes the dev-password
# tripwire). A typo in config.yaml therefore fails the build in ~1s instead of
# crash-looping the container while api/enricher come up healthy. It cannot
# catch bad deploy-time values (malformed DSN, wrong key path) — those still
# surface only at Start().
RUN ["/usr/local/bin/hexgate-collector", "validate", "--config=/etc/hexgate/config.yaml"]

# Non-root, and a DIFFERENT uid than the api image's hexgate user (10001): on
# the shared /keys volume this user can read the 0644 hexgate.pub but not the
# 0600 hexgate.priv — the collector must never be able to sign keys.
RUN groupadd --gid 10002 collector \
    && useradd --uid 10002 --gid 10002 --no-create-home --home-dir /nonexistent collector
USER collector

EXPOSE 4317 4318

ENTRYPOINT ["/usr/local/bin/hexgate-collector"]
CMD ["--config=/etc/hexgate/config.yaml"]
