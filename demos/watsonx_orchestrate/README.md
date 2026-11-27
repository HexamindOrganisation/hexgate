# Demo: Hexgate-governed agent inside IBM watsonx Orchestrate

This demo runs an **OpenAI Agents SDK agent, wrapped by Hexgate**, as a
collaborator inside **IBM watsonx Orchestrate**. The agent's tool calls go
through Hexgate policy. An allowed action runs; a forbidden one comes back as
`[policy_denied]`, and Orchestrate relays that to the user.

We ran it end to end on 2026-09-28: a SaaS Orchestrate trial, with the bridge
running on a laptop behind ngrok. [What we observed](#what-we-observed) lists
the results.

## How it works

Orchestrate can't import OpenAI SDK agents directly. It can call any **external
agent** that exposes an OpenAI-style `POST /chat/completions` endpoint (its
`external_chat` provider). External agents can only be *collaborators*, so users
talk to a native Orchestrate agent that delegates to ours.

```
user ─▶ Orchestrate native agent (hexgate_router)
            │ delegates to its collaborator
            ▼
        POST https://<public-url>/chat/completions     Authorization: Bearer <token>
            ▼
        app.py (FastAPI) ─▶ HexgateRunner(orchestrate_devops_agent) ─▶ policy-gated tools
            ▼
        SSE back to Orchestrate: tool_calls / tool_response steps, answer text, [DONE]
```

| File | Purpose |
|---|---|
| `agent.py` | The OpenAI agent: stub DevOps tools from `examples/devops_openai.py` (`read_logs`, `restart_service`, `scale_deployment`) |
| `app.py` | FastAPI bridge: token check, `POST /chat/completions` (streaming and non-streaming), `GET /health` |
| `sse.py` | Maps Orchestrate messages to agent input, and Hexgate `StreamEvent`s to Orchestrate SSE frames |
| `orchestrate/external_agent.yaml` | The external agent definition (CLI import path) |
| `orchestrate/router_agent.yaml` | The native router agent (CLI import path) |

The policy is `examples/devops_policy.yaml`. Every call runs as
`HEXGATE_ORCHESTRATE_USER_ROLES` (default `operator`), which may:
- read logs anywhere;
- restart or scale in `dev` and `staging`, up to 10 replicas;
- **not** touch `prod`.

## Prerequisites

- This repo, synced (`uv sync`), and an `OPENAI_API_KEY`.
- A watsonx Orchestrate account. The [30-day trial](https://www.ibm.com/products/watsonx-orchestrate) is enough.
- A way to give the bridge a **public HTTPS URL** (IBM's cloud calls it). We used
  [ngrok](https://ngrok.com)'s free tier: `brew install ngrok`, sign up, then
  `ngrok config add-authtoken <token>`.
  - Avoid Cloudflare *quick* tunnels (`*.trycloudflare.com`): they buffer
    server-sent events, which breaks streaming. A named Cloudflare tunnel, or
    Caddy on a server with a public IP, both work.

## 1. Start the bridge

From the repo root:

```bash
export OPENAI_API_KEY=...
export HEXGATE_ORCHESTRATE_TOKEN=$(openssl rand -hex 32)   # shared secret; we generate it, not IBM
echo "$HEXGATE_ORCHESTRATE_TOKEN"                           # you'll paste it into Orchestrate

# Policy source, pick one:
#  a) local file, no platform needed (what we used for the demo)
export HEXGATE_LOCAL_POLICY=examples/devops_policy.yaml HEXGATE_API_KEY=
#  b) Hexgate platform: register once, then paste examples/devops_policy.yaml
#     into the policy editor for orchestrate_devops_agent
#     export HEXGATE_API_KEY=...
#     uv run hexgate register --agent demos.watsonx_orchestrate.agent:agent

# fastapi/uvicorn aren't SDK dependencies; --with pulls them in for this run
uv run --with fastapi --with uvicorn \
  uvicorn demos.watsonx_orchestrate.app:app --port 8080
```

`HEXGATE_API_KEY=` in (a) is deliberate. The bridge loads `./.env`, but a
variable already set in the shell wins, so this stops a platform key in `.env`
from being picked up.

Local smoke test, in a second terminal:

```bash
curl -sN localhost:8080/chat/completions \
  -H "Authorization: Bearer $HEXGATE_ORCHESTRATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"x","stream":true,"messages":[{"role":"user","content":"Restart web-checkout in prod."}]}'
# → a tool_calls step, a tool_response carrying [policy_denied], the answer, then [DONE]
```

## 2. Expose it over HTTPS

```bash
ngrok http 8080        # prints https://<name>.ngrok-free.dev
curl -s https://<name>.ngrok-free.dev/health   # {"status":"ok","agent":"orchestrate_devops_agent"}
```

Watch incoming requests at http://localhost:4040 (the ngrok inspector). This is
where you see exactly what Orchestrate sends.

## 3. Add the bridge to Orchestrate (web UI)

You don't need the ADK or CLI for this; the portal is enough.

1. **Import the external agent.** In the agent builder, go to **Agents → Add
   agents + → Import → External agent**, and choose the chat-completions type.
   - **URL:** `https://<name>.ngrok-free.dev/chat/completions`
   - **Auth:** Bearer token = your `HEXGATE_ORCHESTRATE_TOKEN`
   - **Display name:** `hexgate_devops_agent`
   - **Description** (the router reads this to decide when to delegate):
     ```
     DevOps assistant for a Kubernetes platform: reads service logs, restarts
     services, and scales deployments in dev, staging, or prod. Every action is
     checked against the Hexgate policy before it runs.
     ```
2. **Create the router.** Go to **Agents → Add agents + → Create** and make a
   native agent named `hexgate_router`.
   - **Instructions:**
     ```
     For any request about service logs, restarting services, or scaling
     deployments, delegate to hexgate_devops_agent and relay its answer verbatim,
     including any message saying an action was blocked by policy.
     ```
   - **Collaborators:** add `hexgate_devops_agent`.
3. **Test in Preview** with:
   ```
   Scale web-checkout to 5 replicas in staging, then restart web-checkout in prod.
   ```
   Expected: the scale succeeds and the restart is blocked by policy.
   - `Read the logs for api-gateway in dev.` should be allowed.
   - `Scale web-checkout to 50 replicas in staging.` should be denied (the 10-replica cap).

**CLI alternative.** Install the ADK with `pip install ibm-watsonx-orchestrate`,
then `orchestrate env activate <env>`. Fill in `api_url` and `token` in
`orchestrate/external_agent.yaml`, but **don't commit the filled file**. Then:
```bash
orchestrate agents import -f demos/watsonx_orchestrate/orchestrate/external_agent.yaml
orchestrate agents import -f demos/watsonx_orchestrate/orchestrate/router_agent.yaml
```

**Local Developer Edition.** With `orchestrate server start` on the same machine,
skip ngrok and use `http://host.docker.internal:8080/chat/completions`. This
needs an Orchestrate license (the trial works), and the VM takes 16 GB of RAM by
default.

## What we observed

The test query above was sent from the Orchestrate Preview on the SaaS trial.
Orchestrate called the bridge from AWS; the call took 5.4 s end to end:

```
tool_calls     scale_deployment {service: web-checkout, replicas: 5, env: staging}
tool_calls     restart_service  {service: web-checkout, env: prod}
tool_response  (stub) scaled web-checkout@staging to 5 replicas
tool_response  [policy_denied] Tool 'restart_service' is denied by the agent policy:
               Policy denied tool "restart_service": args.env in ["dev", "staging"] ...
text           The scaling of web-checkout to 5 replicas in staging was successful.
               However, the request to restart web-checkout in prod was blocked by policy ...
[DONE]
```

What Orchestrate sends on each call:

| | Observed |
|---|---|
| Headers | `Authorization` (our token), `X-Ibm-Thread-Id`, `Traceparent` (W3C trace context), `X-Global-Transaction-Id`, `Accept: text/event-stream`, and a `python-requests` user agent |
| Body | `messages`, `stream: true`, `store: true`; no `model` field |
| `messages` | Orchestrate's own `system` prompt ("You are hexgate_devops_agent_<suffix>, a helpful, ethical… assistant"), then the user turn |
| User identity | **None.** No user ID, roles or context variables. |

What this means for Hexgate:
- **Roles are fixed per deployment** (`HEXGATE_ORCHESTRATE_USER_ROLES`) until we
  find a way to forward the end user's identity. Orchestrate context variables
  are the next thing to try.
- **Orchestrate's system prompt reaches the agent** on top of our own
  instructions. It was harmless here. Dropping `system` messages from Orchestrate
  is a likely follow-up.
- **`Traceparent` is available**, so an Orchestrate trace could be linked to
  Hexgate's audit and spans for the same run.

The bridge logs one line per call with the body keys and header names (never
their values), e.g. `orchestrate call thread=... body_keys=[...] headers=[...]`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Bridge won't start: `HEXGATE_ORCHESTRATE_TOKEN is not set` | Export the token before starting uvicorn. |
| Bridge won't start: `HEXGATE_API_KEY is not set` | Set a platform key, or `HEXGATE_LOCAL_POLICY` (option a). |
| `401 Unauthorized` in the bridge log | The token in Orchestrate doesn't match `HEXGATE_ORCHESTRATE_TOKEN`. |
| The answer arrives all at once, or not at all | Your tunnel buffers SSE (e.g. a Cloudflare quick tunnel). Switch tunnels or set `stream: false`. |
| Nothing in the bridge log after chatting | The router didn't delegate. Check that the external agent is added as a collaborator and that the description matches the request. |
| `Failed to export span batch code: 401` | Langfuse credentials in `.env` are for another instance. Unrelated to the bridge. |
