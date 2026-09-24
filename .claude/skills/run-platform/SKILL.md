---
name: run-platform
description: Spin up a local hexgate platform instance (FastAPI platform-api + React dashboard, optionally the full OTLP pipeline) — check prerequisites first (uv, pnpm, Docker, Go, free ports), start the servers in the background, and hand back the dashboard URL plus the first-boot admin email and password. Use when asked to run, start, spin up, or demo the platform/dashboard locally, or to get a login for a local instance.
---

# Run the hexgate platform locally

Goal: a running instance the user can log into. End with the URL, email, and
password, or with exactly what is missing.

## 1. Preflight

```bash
.claude/skills/run-platform/preflight.sh          # light mode
.claude/skills/run-platform/preflight.sh --full   # full pipeline
```

- Any `MISS` line: stop and give the user the install hint the script printed.
- A busy port: an instance may already be running. Check `curl -sf localhost:8000/health` and reuse the instance rather than starting a second one.

**Pick the mode.** Default to light unless the user wants audit events, agents
talking to the platform, or integration tests.

| Mode | Runs | Needs |
|---|---|---|
| light | `platform-api` (SQLite) + `dashboard` | uv, pnpm |
| full | + Postgres, Redpanda, ClickHouse, collector, enricher (`make demo-platform` prints the recipe) | + Docker, Go |

**No Docker?** Explain the options to the user instead of failing:
- Light mode needs no Docker. The API uses SQLite and the dashboard is plain Vite. You can log in, manage agents and policies, and mint tokens. But there is no audit trail, so agent decisions don't show up on the audit page.
- Full mode needs Docker, because Postgres, Redpanda and ClickHouse run as containers. If Docker isn't installed, the fix is `brew install --cask docker` (or https://docs.docker.com/desktop/). If it's installed but not running, run `open -a Docker` and wait until it reports running. Then rerun the preflight.
- Never try to install Docker yourself. It needs the user's approval and a GUI step.

## 2. Install (first time, idempotent)

```bash
UV_PYTHON=3.13 make platform-api-install
make dashboard-install
```

`UV_PYTHON=3.13` is needed because `biscuit-python` fails to build on Python 3.14.

## 3. Start (each as a background command, logs to files)

Light mode:
```bash
UV_PYTHON=3.13 make platform-api > "$LOGDIR/api.log" 2>&1        # :8000
make dashboard > "$LOGDIR/dash.log" 2>&1                          # :5173
```

Full mode is different:
- Use `make platform-api-pg`, not `platform-api`. Keys minted on SQLite get a 401 from the collector.
- Also run `make collector-run` and `make enricher-run`.
- Build the collector once: `cd platform/collector && go build -o hexgate-collector .`

Wait until `curl -sf localhost:8000/health` and `curl -sf localhost:5173` both
succeed. Watch `api.log` for a `Traceback` while you wait. A CSS `@import`
warning in `dash.log` is harmless.

## 4. Credentials

On first boot against an **empty** database, the API seeds `admin@hexgate.dev`
and prints its password **once** to stderr:

```bash
grep -A3 "FIRST-BOOT" "$LOGDIR/api.log"
```

Check it works (expect `204`):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/v1/auth/cookie/login \
    -d "username=admin@hexgate.dev&password=<password>"
```

A dashboard login answering `400 LOGIN_BAD_CREDENTIALS` while the curl check
gives 204 means the password didn't paste exactly: a trailing space, or `0`
mistaken for `O`. Give it to the user in a code block.

**No FIRST-BOOT block** means the database was already seeded, and the
password can't be printed again. Offer these options and let the user choose:
- use the password they saved earlier;
- start from a fresh database. For SQLite, stop the API and move `platform/api/hexgate.db` aside (don't delete it). For Postgres, run `make postgres-reset`, which wipes the local volume, so ask first;
- run in a fresh git worktree, which gets its own SQLite database.

## 5. Report

Give the user:
- the dashboard URL, `http://localhost:5173`;
- the email and password, and that they should rotate the password in account settings;
- the log paths;
- how to stop the instance: stop the background tasks, or `lsof -ti:8000,5173 | xargs kill`.

The password is a local dev credential, so printing it in chat is fine. Never do
this against a staging or prod stack.
