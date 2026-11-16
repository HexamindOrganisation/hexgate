# Claude Code's sandbox for the policy-writing eval (`agent=claude` in task.py), built
# by compose.yaml from the REPO ROOT: the agent's `hexgate` is the scorer's. No token here.

# OPA 1.21.4, the platform API's pin, so `hexgate policy test --engine wasm` runs.
# One stage per architecture (Apple-silicon Docker builds arm64), each pinned by
# the SHA256 OPA publishes beside the binary, e.g.
# https://openpolicyagent.org/downloads/v1.21.4/opa_linux_arm64_static.sha256
ARG TARGETARCH
FROM scratch AS opa-amd64
ADD --checksum=sha256:2ab571f367f293bb19523b76f92f1945bbf245003392c7f392903f95c2b9b630 \
    https://openpolicyagent.org/downloads/v1.21.4/opa_linux_amd64_static /opa
FROM scratch AS opa-arm64
ADD --checksum=sha256:810aa96e44f1f852d044888cf830324e44eafd0f510345339011347ab4deaab1 \
    https://openpolicyagent.org/downloads/v1.21.4/opa_linux_arm64_static /opa
FROM opa-${TARGETARCH} AS opa

# Base images pinned by digest (tag kept for readability), as the platform's are.
FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS node

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

# Node LTS from its official image, then Claude Code pinned: it is the agent under test.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @anthropic-ai/claude-code@2.1.296
COPY --from=opa --chmod=755 /opa /usr/local/bin/opa

# Hexgate from this checkout, pinned by the lock: dependencies in their own
# layer, then the SDK. Its venv goes first on PATH so both `hexgate …` and
# `uv run hexgate …` resolve to it from the workspace.
WORKDIR /opt/hexgate
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY hexgate ./hexgate
RUN uv sync --frozen --no-dev
ENV VIRTUAL_ENV=/opt/hexgate/.venv PATH=/opt/hexgate/.venv/bin:$PATH

# The agent runs unprivileged, in an empty workspace Inspect fills per sample.
RUN useradd --create-home agent && mkdir /workspace && chown agent:agent /workspace
USER agent
WORKDIR /workspace
