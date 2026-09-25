# Hexgate × watsonx Orchestrate bridge

An experiment to see whether a **Hexgate-wrapped OpenAI Agents SDK agent** can run
inside IBM watsonx Orchestrate.

Orchestrate can't import OpenAI SDK agents directly. It can call any **external
agent** that exposes an OpenAI-style `POST /chat/completions` endpoint (the
`external_chat` provider). This bridge is that endpoint:

```
user ─▶ Orchestrate native agent (hexgate_router)
            │ delegates (external agents are collaborators only)
            ▼
        POST /chat/completions  ── Bearer <HEXGATE_ORCHESTRATE_TOKEN>
            ▼
        app.py ─▶ HexgateRunner(orchestrate_devops_agent) ─▶ policy-gated tools
            ▼
        Orchestrate SSE: answer text + tool_calls / tool_response steps
```

| File | Purpose |
|---|---|
| `agent.py` | The OpenAI agent (stub DevOps tools, same as `examples/devops_openai.py`) |
| `sse.py` | Maps Orchestrate messages → agent input, and Hexgate `StreamEvent`s → Orchestrate SSE frames |
| `app.py` | FastAPI app: token check, `/chat/completions` (streaming and non-streaming), `/health` |
| `orchestrate/external_agent.yaml` | Registers the bridge in Orchestrate |
| `orchestrate/router_agent.yaml` | Native agent that delegates to it |

## 1. Run the bridge

From the repo root:

```bash
uv sync

export OPENAI_API_KEY=...
export HEXGATE_API_KEY=...                # Hexgate platform key
export HEXGATE_ORCHESTRATE_TOKEN=$(openssl rand -hex 32)   # our own secret, not IBM's
export HEXGATE_ORCHESTRATE_USER_ROLES=operator             # roles every call runs as

uv run hexgate register --agent examples.watsonx_orchestrate.agent:agent
# then paste examples/devops_policy.yaml into the policy editor for orchestrate_devops_agent

# fastapi/uvicorn aren't SDK dependencies; --with pulls them in for this run
uv run --with fastapi --with uvicorn \
  uvicorn examples.watsonx_orchestrate.app:app --host 0.0.0.0 --port 8080
```

To try it without a platform, use the local policy file instead of
`HEXGATE_API_KEY` + `register`:

```bash
export HEXGATE_LOCAL_MODE=1 HEXGATE_LOCAL_POLICY=examples/devops_policy.yaml
```

Smoke test:

```bash
curl -sN localhost:8080/chat/completions \
  -H "Authorization: Bearer $HEXGATE_ORCHESTRATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"x","stream":true,"messages":[{"role":"user","content":"Restart web-checkout in prod."}]}'
# → a tool_calls step, a tool_response carrying [policy_denied], then the answer and [DONE]
```

With `operator`, scaling in dev or staging (≤ 10 replicas) is allowed, and any
restart or scale in prod is denied.

## 2. Expose it over HTTPS

SaaS Orchestrate calls the bridge from IBM's cloud and sends the token with every
request, so the bridge needs a public HTTPS URL. uvicorn stays on plain HTTP
behind a TLS terminator, for example Caddy:

```
orchestrate-bridge.example.com {
    reverse_proxy localhost:8080
}
```

You can skip this step with the local **Developer Edition** (`orchestrate server
start`). There, use `http://host.docker.internal:8080/chat/completions` as the
`api_url`.

## 3. Import into Orchestrate

```bash
pip install ibm-watsonx-orchestrate
orchestrate env activate <env>            # SaaS/trial env, or `local` for Developer Edition

# fill api_url + token (= HEXGATE_ORCHESTRATE_TOKEN) first; don't commit the filled file
orchestrate agents import -f examples/watsonx_orchestrate/orchestrate/external_agent.yaml
orchestrate agents import -f examples/watsonx_orchestrate/orchestrate/router_agent.yaml
```

Chat with `hexgate_router` in the Orchestrate UI (on Developer Edition, `orchestrate chat
start` opens it at `http://localhost:3000`) and ask it to restart something in prod.

## What to look for

- **Does the delegation happen?** The bridge logs one line per call, e.g.
  `orchestrate call thread=... stream=True body_keys=[...] headers=[...]`.
- **Which fields does Orchestrate send?** The same line lists the request's body
  keys and header names (never their values). This is how we find out whether
  context variables or user identity reach us, which we'd need to map to
  `HexgateContext` roles. Until then, every call runs as
  `HEXGATE_ORCHESTRATE_USER_ROLES`.
- **Does the chain of thought render?** With `enable_cot: true`, the tool calls
  and the `[policy_denied]` responses should show up as steps in the Orchestrate UI.
