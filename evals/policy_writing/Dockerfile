# Claude Code's sandbox for the policy-writing eval (`agent=claude` in task.py),
# built by compose.yaml from the REPO ROOT so the agent's `hexgate policy …`
# dry-runs use the scorer's SDK. No token in here: compose passes it at run time.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Node LTS from its official image, then Claude Code pinned: it is the agent under test.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @anthropic-ai/claude-code@2.1.296

# Hexgate from this checkout, pinned by the lock: dependencies in their own
# layer, then the SDK. Its venv goes first on PATH so both `hexgate …` and
# `uv run hexgate …` (the agent's allow-list) resolve to it from the workspace.
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
