---
name: run-platform
description: Spin up a local hexgate platform instance (FastAPI platform-api + React dashboard, optionally the full OTLP pipeline) — check prerequisites first (uv, pnpm, Docker, Go, free ports), start the servers in the background, and hand back the dashboard URL plus the first-boot admin email and password. Use when asked to run, start, spin up, or demo the platform/dashboard locally, or to get a login for a local instance. Local dev machine only, not staging or prod (those are deploy stacks, see platform/DEPLOY.md).
---

# Run the hexgate platform locally

**Scope: your own machine only.** This skill runs dev servers on localhost
against local SQLite or Docker databases. It does not start, stop, or log into
staging or prod. Those are deploy stacks (`make platform-up STAGE=staging|prod`,
see `platform/DEPLOY.md`). If the user asks for staging or prod, point them
there and don't run anything from this skill against them.

Goal: a running instance the user can log into. End with the URL, email, and
password, or with exactly what is missing.

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

- Any `MISS` line: stop and give the user the install hint the script printed.
- `8000 busy`: an API may already be running, but `/health` looks the same whatever database it uses, so check with `.claude/skills/run-platform/instance.sh who` before reusing it. Reuse it only when its checkout is this one and its target fits the mode you pick below: `make platform-api` for light, `make platform-api-pg` (or a Postgres `DATABASE_URL`) for full. A light-mode API under a full pipeline looks healthy, but the collector rejects every key it mints with 401.
  - This checkout, wrong mode: `kill <pid>` with the pid `who` printed, wait until `curl -sf -m 2 localhost:8000/health` fails, then start the right target in step 3.
  - Another checkout: ask the user before stopping it; it may be in use.
  - `not a hexgate API on :8000`: tell the user what holds the port.
- `5173 busy`: fine if it's another checkout's dashboard; Vite starts this one on the next free port, and step 3 finds it.
- `4317`/`4318 busy` in full mode: a collector is already running. `make collector-run` would fail on it, so find out whose it is before starting yours.

**Pick the mode.** Default to light unless the user wants audit events, agents
talking to the platform, or integration tests.

| Mode | Runs | Needs |
|---|---|---|
| light | `platform-api` (SQLite) + `dashboard` | uv, pnpm |
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

Wait until the API answers `curl -sf localhost:8000/health` and
`.claude/skills/run-platform/instance.sh dash` prints this checkout's dashboard
URL. Don't probe `localhost:5173` directly: when another checkout's dashboard
holds it, Vite quietly moves this one to the next port. Watch `api.log` for a
`Traceback` while you wait. A CSS `@import` warning in `dash.log` is harmless.

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

## 5. Report

Give the user:
- the dashboard URL that `instance.sh dash` printed (normally `http://localhost:5173`);
- the email and password, and that they should rotate the password in account settings;
- the log directory, `/tmp/hexgate-<checkout name>`;
- how to stop the instance: stop the background tasks, or run `.claude/skills/run-platform/instance.sh stop`, which stops this checkout's API, dashboard, collector and enricher and nothing else.

The password is a local dev credential, so printing it in chat is fine. Never do
this against a staging or prod stack.
