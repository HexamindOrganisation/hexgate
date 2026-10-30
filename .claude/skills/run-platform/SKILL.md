---
name: run-platform
description: Spin up a local hexgate platform instance (FastAPI platform-api + React dashboard + MCP server, optionally the full OTLP pipeline) — check prerequisites first (uv, pnpm, Docker, Go, free ports), start the servers in the background, and hand back the dashboard URL, the first-boot admin email and password, and register the local MCP server with Claude Code (`claude mcp add`) so the user only opens a new session and authenticates. Use when asked to run, start, spin up, or demo the platform/dashboard/MCP server locally, or to get a login for a local instance; "without MCP" or "dashboard only" skips the MCP server. Local dev machine only, not staging or prod (those are deploy stacks, see platform/DEPLOY.md).
---

# Run the hexgate platform locally

**Scope: your own machine only.** This skill runs dev servers on localhost
against local SQLite or Docker databases. It does not start, stop, or log into
staging or prod. Those are deploy stacks (`make platform-up STAGE=staging|prod`,
see `platform/DEPLOY.md`). If the user asks for staging or prod, point them
there and don't run anything from this skill against them.

Goal: a running instance the user can log into and connect Claude Code to.
End with the URL, email, password and the MCP server registered in Claude
Code, or with exactly what is missing.

The MCP server is part of the platform, so start it every time. Skip it (and
its port, step and report line) only when the user asks for the platform
"without MCP" or "dashboard only".

Works on macOS and Linux. The paths and `make` targets below are relative to
the repo root, and each command runs in a fresh shell, so start **every**
command with this line (it also sets the log directory used from step 3 on):
```bash
cd "$(git rev-parse --show-toplevel)" && LOGDIR="/tmp/hexgate-$(basename "$PWD")" && mkdir -p "$LOGDIR"
```

## 1. Preflight

```bash
.claude/skills/run-platform/preflight.sh          # light mode
.claude/skills/run-platform/preflight.sh --full   # full pipeline
```
Add `--no-mcp` (to either) when the user opted out of the MCP server.

- Any `MISS` line: stop and give the user the install hint the script printed.
- `8000 busy`: an API may already be running, but `/health` looks the same whatever database it uses, so check with `.claude/skills/run-platform/instance.sh who` before reusing it. Reuse it only when its checkout is this one and its target fits the mode you pick below: `make platform-api` for light, `make platform-api-pg` (or a Postgres `DATABASE_URL`) for full. A light-mode API under a full pipeline looks healthy, but the collector rejects every key it mints with 401.
  - This checkout, wrong mode: `kill <pid>` with the pid `who` printed, wait until `curl -sf -m 2 localhost:8000/health` fails, then start the right target in step 3.
  - Another checkout: ask the user before stopping it; it may be in use.
  - `not a hexgate API on :8000`: tell the user what holds the port.
- `5173 busy`: fine if it's another checkout's dashboard; Vite starts this one on the next free port, and step 3 finds it.
- `8080 busy`: an MCP server may already be running. Check its checkout with `lsof -a -p <pid> -d cwd`: reuse it if that is `<this checkout>/platform/mcp`, otherwise ask the user before stopping it; `make mcp-run` would fail on the taken port.
- `4317`/`4318 busy` in full mode: a collector is already running. `make collector-run` would fail on it, so find out whose it is before starting yours.

**Pick the mode.** Default to light unless the user wants audit events, agents
talking to the platform, or integration tests.

| Mode | Runs | Needs |
|---|---|---|
| light | `platform-api` (SQLite) + `dashboard` + `mcp-run` | uv, pnpm |
| full | + Postgres, Redpanda, ClickHouse, collector, enricher (`make demo-platform` prints the recipe) | + Docker, Go |

**No Docker?** Explain the options to the user instead of failing:
- Light mode needs no Docker. The API uses SQLite and the dashboard is plain Vite. You can log in, manage agents and policies, and mint tokens. But there is no audit trail, so agent decisions don't show up on the audit page.
- Full mode needs Docker, because Postgres, Redpanda and ClickHouse run as containers. The preflight's hint differs between macOS (Docker Desktop) and Linux (Docker Engine, `systemctl`, the `docker` group).
  - Installed but not running, on macOS: run `open -a Docker` yourself, then rerun the preflight until it prints `docker (running)` (don't poll `docker info` directly: it hangs while Docker is starting). Run `open -a Docker` once only. If Docker isn't running after about 2 minutes, stop and ask the user to check the Docker Desktop window: a first launch waits for them to accept the terms.
  - Not running on Linux, or not installed anywhere: give the user the hint the preflight printed. Never do these yourself: starting it on Linux needs `sudo`, and installing needs the user's approval.
  - Then rerun the preflight.

## 2. Install (first time, idempotent)

```bash
UV_PYTHON=3.13 make platform-api-install
make dashboard-install
```

`UV_PYTHON=3.13` is needed because `biscuit-python` fails to build on Python 3.14.

## 3. Start (each as a background command, logs to files)

Logs go to `$LOGDIR` (set by the line at the top), one fixed directory per
checkout, so a later command or a later session can find them again. Always
append (`>>`): a restart must not erase the one-time password block.

Light mode:
```bash
UV_PYTHON=3.13 make platform-api >> "$LOGDIR/api.log" 2>&1       # :8000
make dashboard >> "$LOGDIR/dash.log" 2>&1                         # :5173
make mcp-run >> "$LOGDIR/mcp.log" 2>&1                            # :8080
```

Full mode, in this order:
1. Bring up the infrastructure first, in one foreground command, so the two
   background targets below don't race to create the same containers:
   `make postgres-init redpanda-topics clickhouse-migrate`. `clickhouse-migrate`
   also brings an old ClickHouse schema up to date; the enricher exits on a
   stale one.
2. Start the API with `make platform-api-pg`, not `platform-api` (keys minted
   on SQLite get a 401 from the collector), and wait for its `/health`: the
   collector needs `platform/api/data/hexgate.pub`, which the API writes on its
   first boot.
3. Build the collector, every time (the binary is gitignored, so an old one
   would silently stay): `cd platform/collector && go build -o hexgate-collector .`.
   `collector-run` runs the binary and doesn't build it.
4. Start `make collector-run` and `make enricher-run` as background commands,
   appending to `$LOGDIR/collector.log` and `$LOGDIR/enricher.log`.
5. Start the dashboard and `make mcp-run` as in light mode.

The MCP server's defaults already fit this setup, so export nothing:
`HEXGATE_MCP_API_URL` is `http://localhost:8000` and `HEXGATE_MCP_RESOURCE_URL`
is `http://localhost:8080/mcp`. It doesn't read `platform/api/.env`. So if the
API runs with a non-default `HEXGATE_PUBLIC_URL`, export the same value to
`make mcp-run`, or every token gets refused for its issuer.

Wait until the API answers `curl -sf localhost:8000/health` and
`.claude/skills/run-platform/instance.sh dash` prints this checkout's dashboard
URL. Don't probe `localhost:5173` directly: when another checkout's dashboard
holds it, Vite quietly moves this one to the next port. Watch `api.log` for a
`Traceback` while you wait. A CSS `@import` warning in `dash.log` is harmless.
Then check the MCP server answers (any HTTP code, a `401` is normal before
login): `curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/mcp`.
If it prints `000`, read `mcp.log`: an address-in-use error means another MCP
server holds :8080 (see the preflight).

The OAuth login ends on a consent page on the API's `HEXGATE_DASHBOARD_URL`,
which defaults to `http://localhost:5173`, and the consent form is refused
unless its origin matches that URL exactly. So check the URL `instance.sh dash`
printed:
- `http://localhost:5173`: nothing more to do.
- Another port: Vite moved off a 5173 that another checkout holds. Restart the
  API             with `HEXGATE_DASHBOARD_URL=<that URL>` (it reads its settings only at
  startup). Otherwise consent opens the other checkout's dashboard, which may
  not have the consent page.

In full mode the pipeline must be up too, or the audit page stays empty while
everything else looks fine. Before reporting, check that:
- the collector answers: `curl -s -o /dev/null localhost:4318 && echo up`;
- the collector and enricher background tasks are still running, and their logs
  have no `Traceback` or error exit. On a schema error, run
  `make clickhouse-migrate` and start the enricher again.

## 4. Credentials

On first boot against an **empty** database, the API seeds `admin@hexgate.dev`
and prints its password **once** to stderr:

```bash
grep -A3 "FIRST-BOOT" "$LOGDIR/api.log" | tail -4   # the newest block, if there are several
```

Check it works (expect `204`):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/v1/auth/cookie/login \
    -d "username=admin@hexgate.dev&password=<password>"
```

A dashboard login answering `400 LOGIN_BAD_CREDENTIALS` while the curl check
gives 204 means the password didn't paste exactly: a trailing space, or `0`
mistaken for `O`. Give it to the user in a code block.

If the curl check itself fails, the block is stale (the password was rotated
or reset since, or a different database is running): treat it as no block.

**No FIRST-BOOT block** means the database was already seeded, and the
password can't be printed again. Offer these options and let the user choose:
- use the password they saved earlier;
- reset the password, which keeps the database. This needs the API's stderr, so
  it only works on an API you started. If it was started elsewhere, restart it
  with its log going to a file first, from the same checkout and with the same
  target, or it opens a different database: the reset and login then succeed,
  but on the wrong one. Find its checkout, its target, and any exported
  `DATABASE_URL`:
  ```bash
  .claude/skills/run-platform/instance.sh who
  ```
  Stop only the API, not the whole instance: `kill <pid>` with the pid `who`
  printed, and wait until `curl -sf -m 2 localhost:8000/health` fails. Then restart
  from that checkout with that target, and the same `DATABASE_URL` if one was
  exported. If the checkout or the target is missing, ask the user. When it is
  another checkout, keep this checkout's `$LOGDIR` for that restart (don't rerun
  the top line there), so the token lookup below reads the right log; a later
  `instance.sh stop` must then run from that checkout.
  Either database survives a restart. If both `RESEND_API_KEY` and
  `HEXGATE_EMAIL_FROM` are set (in the shell or `platform/api/.env`), the email
  is really sent instead of printed, so no token reaches the log:
  ```bash
  n=$(wc -l < "$LOGDIR/api.log")   # only read what this request logs
  curl -s -w ' %{http_code}\n' -X POST localhost:8000/v1/auth/forgot-password \
      -H 'content-type: application/json' -d '{"email":"admin@hexgate.dev"}'   # 202
  TOKEN=$(tail -n +$((n + 1)) "$LOGDIR/api.log" | grep -o 'reset-password/[^ ]*' | tail -1 | cut -d/ -f2)
  curl -s -w ' %{http_code}\n' -X POST localhost:8000/v1/auth/reset-password \
      -H 'content-type: application/json' \
      -d "{\"token\":\"$TOKEN\",\"password\":\"<letters-digits-dashes>\"}"             # 200
  ```
  Pick a password of letters, digits and `-`: the login check sends a form body,
  so `+`, `&` or `%` would make it fail even though the reset worked.
  The token expires after an hour. Then run the login check above with the new password;
- start from a fresh database. For SQLite, stop the API and move `platform/api/hexgate.db` aside (don't delete it). For Postgres, run `make postgres-reset`, which wipes the one Postgres volume every checkout shares, so ask first; then restart the API, which creates the tables and seeds (printing a new block) only at startup;
- light mode only: run in a fresh git worktree, which gets its own SQLite database. Full mode gets nothing fresh from a new worktree, because all checkouts share the same Postgres.

## 5. Connect Claude Code

Skip this when the user opted out of the MCP server.

Register the server yourself; it is a shell command that writes
`~/.claude.json`. Check first:
```bash
claude mcp get hexgate
```
- It shows `http://localhost:8080/mcp`: already registered, add nothing.
- It says there is no server named `hexgate`: register it.
  ```bash
  claude mcp add -s user --transport http hexgate http://localhost:8080/mcp
  ```
- It shows another URL: ask the user before replacing it
  (`claude mcp remove hexgate -s user`, then the add above), since it may be
  their connection to another instance.

Remember whether you added it or found it; the report says which.

Two things only the user can do, in their own session:
1. Open a **new** Claude Code session in another terminal, and keep this one
   open: the servers are this session's background commands, so quitting it
   stops them. A session reads its MCP servers when it starts, so one that
   started before the registration doesn't see the server. If the
   registration was already there before this session started, this session
   sees it already, so skip to 2 here.
2. In that session, open `/mcp`, choose `hexgate`, then **Authenticate**.
   The browser opens the dashboard by itself, nothing to paste: log in with the
   step 4 credentials, then approve the consent page. Then a first prompt:
   "List my Hexgate projects".

`HEXGATE_OAUTH_ALLOW_INSECURE_CLIENT_IDS` is **not** needed for Claude Code.
Its client id is https://claude.ai/oauth/claude-code-client-metadata, an https
URL the API fetches as usual. Set it, on the API, only for a client whose
client id is an `http://localhost` URL, such as a metadata document you serve
yourself. The API refuses to start with it when its public or dashboard URL is
not local.

Failure modes:
- **"No Hexgate MCP server connected"** (or the session has no hexgate
  tools): the session started before the server was registered. Open a new
  session in another terminal.
- **hexgate shows as failed in `/mcp`**: if :8000, :5173 and :8080 are free,
  the session that started the servers was quit, which stopped them; run
  `/run-platform` again and keep that session open. If they are up, the
  session loaded hexgate before :8080 answered; choose Reconnect, then
  Authenticate.
- **"Safari can't connect to the server localhost:5173"** after Authenticate:
  the API's `HEXGATE_DASHBOARD_URL` has no dashboard behind it. Either the
  dashboard isn't running (start it, step 3), or it runs on another port (set
  the variable as in step 3 and restart the API). Then authenticate again.
- **`401` on `POST /mcp`** in `mcp.log`: this is normal before login. That's
  how the server tells the client to authenticate. It's only a problem if it
  keeps happening after the consent page has been approved.

## 6. Report

Reply with exactly this block, filled in, and nothing else: no sentence
before or after it.

```
Hexgate platform · <light|full> mode · logs <dir>
  Dashboard       http://localhost:5173    admin@hexgate.dev / <password or "unchanged">
  API             http://localhost:8000    (<SQLite|Postgres>)
  OTLP pipeline   collector :4317/:4318 · enricher running        ← full mode only
  MCP             http://localhost:8080/mcp   registered as `hexgate` (<added|found>)
Next  open a NEW terminal: claude → /mcp → hexgate → Authenticate → "List my Hexgate projects"
Stop  .claude/skills/run-platform/instance.sh stop
Notes <one line per anomaly, e.g. "enricher restarted once (Postgres init deadlock)">; omit the line when there is none
```

Rules:
- Name the mode once, in the header line.
- `Dashboard` is the URL `instance.sh dash` printed. The password is the one the
  login check accepted, or `unchanged` when the database was already seeded and
  the user keeps their saved one.
- Leave out the `OTLP pipeline` line in light mode, and the `MCP` and `Next` lines
  when the user opted out of the MCP server.
- Add `(keep this session open, it owns the servers)` to the `Next` line only
  when this run started any server (rather than reusing ones already running):
  quitting this session stops them.
- Don't mention stale FIRST-BOOT blocks unless the password line depends on
  them.

The password is a local dev credential, so printing it in chat is fine. Never do
this against a staging or prod stack.
