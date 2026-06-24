# Hexgate live-demo container

One **disposable, self-contained** Hexgate world per visitor: a marimo notebook
where you define an agent and chat with it through the live dashboard playground.
Everything runs in a single container on a **fresh SQLite** — when the container
goes away, the org/project/agents evaporate. **No shared database, nothing to
garbage-collect.** Runs locally (`make demo-notebook`) and per-visitor in
**GitHub Codespaces** (see `.devcontainer/`).

```
ONE CONTAINER (per visitor — fresh SQLite, dies with the session)
   ├─ Platform API (:8000)  ─ serves the built dashboard same-origin + /v1/demo-login
   └─ marimo notebook (:2718) ─ the landing UI; runs the serve loop IN-KERNEL
        ├─ visitor pastes their OWN OpenAI key (BYOK) ─┐
        ├─ define tools + agent in cells + "Start" ────┤→ (re)starts in-kernel serve
        │     serve loop (bound to the live `agent` object) → dials :8000/v1/serve
        │     └─ LLM calls use the visitor's key → OpenAI
        └─ "Open playground" → <dashboard-url>/v1/demo-login → signed-in /playground
```

**Keys: bring-your-own (BYOK).** The visitor enters their own OpenAI key in a
notebook cell; it's set into the process env and used for LLM calls. Their key,
their spend, in their own throwaway container — so no provider key of yours is
ever exposed, and there's nothing to operate. The key is never written to disk.

**Live edit:** the visitor defines tools and the agent as real Python in cells —
the kernel holds an actual `agent` object — then clicks **Start**. `serve_manager`
runs the `hexgate serve` loop **in-kernel** bound to that exact object (a
background thread), so what the dashboard talks to *is* the object you defined —
no subprocess, no source-string round-trip. Edit the cells, click Start again,
and the loop restarts with the new object; the dashboard picks it up on its next
`hello`.

## What's in here

| File | Role |
|------|------|
| `boot.py` | Process orchestrator: start API → mint token (→ key file) → start marimo → block |
| `provision.py` | Mints a `HEXGATE_KEY` for the seeded project (shares the container's SQLite + keystore) |
| `serve_manager.py` | Runs the `hexgate serve` loop in-kernel, bound to the live `agent` object; `apply()` on Start |
| `demo_notebook.py` | The marimo notebook: define tools + agent in real cells, Start, playground link |
| `smoke_test.py` + `_mock_llm.py` | Full-path smoke test with a mock LLM (`make demo-smoke`) — no real key |

Plus the API-side glue (committed in `platform/api/`):
- `platform/api/demo.py` — serves the dashboard `dist/` same-origin + `GET /v1/demo-login` (auto-login)
- `platform/api/main.py` — wires `enable_demo(app)` when `HEXGATE_DEMO=1` (off by default)

The `.devcontainer/` at the repo root makes this run per-visitor in Codespaces
(`setup.sh` builds it, `start.sh` launches it, ports 2718/8000 are forwarded).

## Run it in your browser (Codespaces)

The repo ships a devcontainer, so anyone can launch the full demo in one click:
`https://codespaces.new/HexamindOrganisation/hexgate`. Each visitor gets their
own isolated container on their own free quota — ports are forwarded directly
(marimo's live-edit WebSocket works), notebook on 2718, dashboard on 8000.

## Run locally

From `asianf/`:

```bash
make demo-notebook-build    # one-time: deps + marimo + dashboard build
make demo-notebook          # launch the whole world (one process)
```

Then open <http://localhost:2718>, define your tools/agent, enter your OpenAI
key, click **Start**, and open the playground.

`make demo-notebook` runs `deploy/boot.py` with `HEXGATE_DEMO=1` +
`HEXGATE_COOKIE_SECURE=0` (the latter is required over local HTTP — the browser
drops a `Secure` cookie over plain HTTP; it's `1` over HTTPS, e.g. Codespaces).

```bash
make demo-smoke             # verify the whole path with a mock LLM (no real key)
```

## ⚠️ Security

`HEXGATE_DEMO=1` makes `/v1/demo-login` grant a **passwordless** session for the
seeded admin (and serves an SPA catch-all). That's intentional for a throwaway,
single-user container — but it would be an account-takeover hole on any
persistent/real deployment that ran the default seed. **Only ever set
`HEXGATE_DEMO` on an ephemeral demo container.** The API logs a loud warning at
startup when it's on.

## Knobs (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `HEXGATE_DEMO` | off | Must be `1` to serve the dashboard + demo-login |
| `HEXGATE_COOKIE_SECURE` | off (`1` over HTTPS) | `Secure` flag on the session cookie |
| `HEXGATE_API_PORT` / `HEXGATE_MARIMO_PORT` | 8000 / 2718 | Ports |
| `HEXGATE_DASHBOARD_DIST` | `../dashboard/dist` | Override the built-SPA location |
| `HEXGATE_DASH_URL` | local API origin | Public dashboard URL for the notebook's "Open playground" link (Codespaces sets it) |

## Verified

- `/v1/demo-login` → 303 → `/playground` + sets `hexgate_session`; SPA serves; `/v1` wins over the catch-all.
- `provision.py` mints a signature-valid token against the API's keystore.
- **Live edit (in-kernel serve):** `serve_manager.apply(agent)` runs the serve loop bound to the live `agent` object — it connects to the API, auto-registers, goes `agent_online`, and a chat message round-trips back as streamed events. `make demo-smoke` exercises this end-to-end against a mock LLM (passes with `LINKUP`/`TAVILY` unset).
