# Claude Code's sandbox for the policy-writing eval (`agent=claude` in task.py), built
# by compose.yaml from the REPO ROOT: the agent's `hexgate` is the scorer's. No token here.

# OPA 0.70.0, the platform API's pin, so `hexgate policy test --engine wasm` runs.
# One stage per architecture (Apple-silicon Docker builds arm64), each pinned by
# the SHA256 OPA publishes beside the binary, e.g.
# https://openpolicyagent.org/downloads/v0.70.0/opa_linux_arm64_static.sha256
ARG TARGETARCH
FROM scratch AS opa-amd64
ADD --checksum=sha256:00d114b94fdb1606a48cccdfc73c9ccdc62c38721150131ae578d5ff3df5c084 \
    https://openpolicyagent.org/downloads/v0.70.0/opa_linux_amd64_static /opa
FROM scratch AS opa-arm64
ADD --checksum=sha256:48061407a2d7b0b59440fc3a257e7bb251e9ec62f6ce7b1e45c142263ae24413 \
    https://openpolicyagent.org/downloads/v0.70.0/opa_linux_arm64_static /opa
FROM opa-${TARGETARCH} AS opa

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Node LTS from its official image, then Claude Code pinned: it is the agent under test.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
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
