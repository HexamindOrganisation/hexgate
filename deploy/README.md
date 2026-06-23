# Hexgate live-demo container

One **disposable, self-contained** Hexgate world per visitor: a marimo notebook
that shows the agent definition, and a one-click link into the live dashboard
playground. Everything runs in a single container on a **fresh SQLite** — when
the container scales down, the org/project/agents evaporate. **No shared
database, nothing to garbage-collect.**

```
ONE CONTAINER (dies after idle → takes the whole world with it)
  fresh SQLite
   ├─ Platform API (:8000)  ─ serves the built dashboard same-origin + /v1/demo-login
   └─ marimo notebook (:2718) ─ the landing UI; runs the serve loop IN-KERNEL
        ├─ visitor pastes their OWN OpenAI key (BYOK) ─┐
        ├─ define tools + agent in cells + "Apply" ────┤→ (re)starts in-kernel serve
        │     serve loop (bound to the live `agent` object) → dials :8000/v1/serve
        │     └─ LLM calls use the visitor's key → OpenAI
        └─ "Open playground" → <dashboard-url>/v1/demo-login → signed-in /playground
```

**Keys: bring-your-own (BYOK).** The visitor pastes their own OpenAI key into a
notebook cell; it's set into the process env and used for LLM calls. It's their
key, their spend, in their own throwaway container (one visitor per container,
dies after idle) — so no provider key of yours is ever exposed, and there's no
gateway to operate. The key is never logged.

**Live edit:** the visitor defines tools and the agent as real Python in cells —
the kernel holds an actual `agent` object — then clicks **Apply & reload**.
`serve_manager` runs the `hexgate serve` loop **in-kernel** bound to that exact
object (a background thread), so what the dashboard talks to *is* the object you
defined — no subprocess, no source-string round-trip. Edit the cells, Apply, and
the loop restarts with the new object; the dashboard picks it up on its next
`hello`.

## What's in here

| File | Role |
|------|------|
| `boot.py` | Process orchestrator: start API → mint token (→ key file) → start marimo → block |
| `provision.py` | Mints a `HEXGATE_KEY` for the seeded project (shares the container's SQLite + keystore) |
| `serve_manager.py` | Runs the `hexgate serve` loop in-kernel, bound to the live `agent` object; `apply()` on Apply |
| `demo_notebook.py` | The marimo notebook: define tools + agent in real cells, Apply&reload, playground link |
| `modal_app.py` | Modal wrapper: image build, per-session container, tunnels marimo + dashboard |
| `gateway/app.py` | *Optional* LLM gateway (powerless session tokens) — only if you ever want a no-BYOK public demo |

Plus the API-side glue (committed in `platform/api/`):
- `platform/api/demo.py` — serves the dashboard `dist/` same-origin + `GET /v1/demo-login`
- `platform/api/main.py` — wires `enable_demo(app)` when `HEXGATE_DEMO=1`

## Run locally (no Modal)

From `asianf/`:

```bash
make demo-notebook-build    # one-time: deps + marimo + dashboard build
make demo-notebook          # launch the whole world (one process)
```

Then open <http://localhost:2718> (notebook), paste your OpenAI key, **Apply &
reload**, and click **Open the live playground**.

`make demo-notebook` runs `deploy/boot.py` with `HEXGATE_DEMO=1` +
`HEXGATE_COOKIE_SECURE=0` (the latter is required over local HTTP — the browser
drops a `Secure` cookie over plain HTTP; in the container it's `1` because the
tunnel is HTTPS). The raw form, if you're not using make:

```bash
PATH="$PWD/platform/api/.venv/bin:$PATH" \
  HEXGATE_DEMO=1 HEXGATE_COOKIE_SECURE=0 python deploy/boot.py
```

## Deploy on Modal

First time: `uv pip install modal && modal token new`. Then, from `asianf/`:

```bash
make demo-modal            # → modal deploy deploy/modal_app.py
# iterating? raw form for hot-reload:  modal serve deploy/modal_app.py
```

- **marimo** is the stable entry URL (`@modal.web_server(2718)`).
- the **dashboard** is exposed at runtime via `modal.forward(8000)`; its public
  URL is written to `/tmp/hexgate_dash_url` and rendered in the notebook.
- `scaledown_window=600` → the container dies 10 min after the last request.
- `@modal.concurrent(max_inputs=1)` → one visitor per container (isolation).
- `max_containers=50` → cost guardrail on concurrent live sessions.

## Knobs (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `HEXGATE_DEMO` | off | Must be `1` to serve the dashboard + demo-login |
| `HEXGATE_COOKIE_SECURE` | off (`1` in container) | `Secure` flag on the session cookie (required over HTTPS) |
| `HEXGATE_API_PORT` / `HEXGATE_MARIMO_PORT` | 8000 / 2718 | Ports |
| `HEXGATE_DASHBOARD_DIST` | `../dashboard/dist` | Override the built-SPA location |

## The LLM key — BYOK (default)

The visitor enters their own OpenAI key in the notebook. `serve_manager.apply()`
sets it as `OPENAI_API_KEY` in the process env, and the agent's model client
uses it when the in-kernel serve loop runs. Their key, their spend, isolated in
their own container — nothing of yours is exposed and there's nothing to run or
maintain. The notebook gates "Apply & reload" on a key being present.

### Optional: a no-BYOK public demo (`gateway/app.py`)

If you ever want visitors to run the demo *without* pasting a key, the included
gateway holds your real key **outside** the container and hands each session a
**powerless token** (short TTL, request cap, model allowlist). You'd re-add the
boot→gateway minting + env injection (kept in git history). Only worth the
operational cost if friction-free public access matters more than simplicity —
for true $-budgets, point it at **LiteLLM proxy** (already a hexgate dep)
instead. **Default demo doesn't need this.**

## Verified

- `/v1/demo-login` → 303 → `/playground` + sets `hexgate_session`; SPA serves; `/v1` wins over the catch-all.
- `provision.py` mints a signature-valid token against the API's keystore.
- **Live edit (in-kernel serve):** `serve_manager.apply(agent, key)` runs the serve loop bound to the live `agent` object — it connects to the API, auto-registers, goes `agent_online`, and a chat message round-trips back as streamed events (full dry run against a mock LLM passes; only the real provider call is unexercised).
- **Gateway (optional path):** mint requires the master key; session tokens are model-allowlisted + TTL'd + request-capped; unknown/expired/disallowed rejected (unit-tested, but not used by the default BYOK demo).
