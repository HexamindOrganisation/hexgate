#!/usr/bin/env bash
# Start the local hexgate stack for the bank-assistant demo video.
#
#   API + dashboard  http://localhost:8000   (demo mode: /v1/demo-login, no password)
#   MCP server       http://localhost:8080/mcp
#   collector        :4318  + enricher         (only if the collector binary exists)
#
# Infra (Postgres :5433, Redpanda, ClickHouse :8124) runs in Docker. Logs go to
# $LOGDIR. Stop everything with demo/scripts/down.sh.
#
# DASHBOARD_DIST: a built dashboard (vite build output). Defaults to
# platform/dashboard/dist; point it elsewhere when the working tree doesn't build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOGDIR="${LOGDIR:-/tmp/hexgate-demo}"
PIDS="$LOGDIR/pids"
DASHBOARD_DIST="${DASHBOARD_DIST:-$ROOT/platform/dashboard/dist}"
POSTGRES_DSN="postgresql+asyncpg://hexgate:hexgate-dev-password@localhost:5433/hexgate"
mkdir -p "$LOGDIR"
: > "$PIDS"

cd "$ROOT"

echo "== infra (Postgres, Redpanda, ClickHouse) =="
make -s postgres-init redpanda-topics clickhouse-migrate >> "$LOGDIR/infra.log" 2>&1

[ -f "$DASHBOARD_DIST/index.html" ] || {
  echo "no built dashboard at $DASHBOARD_DIST (run: cd platform/dashboard && pnpm build)"; exit 1; }

start() {  # start <name> <dir> <cmd...>: run in the background, log, remember the pid
  local name=$1 dir=$2; shift 2
  ( cd "$dir" && exec "$@" ) >> "$LOGDIR/$name.log" 2>&1 &
  echo "$name $!" >> "$PIDS"
}

wait_for() {  # wait_for <name> <url> [seconds]
  for _ in $(seq 1 "${3:-60}"); do
    curl -sf -m 2 -o /dev/null "$2" && { echo "   $1 up"; return 0; }
    sleep 1
  done
  echo "   $1 did not come up, see $LOGDIR/$1.log"; return 1
}

echo "== API + dashboard on :8000 (demo mode) =="
start api "$ROOT/platform/api" env \
  DATABASE_URL="$POSTGRES_DSN" \
  HEXGATE_DEMO=1 \
  HEXGATE_COOKIE_SECURE=0 \
  HEXGATE_SEED_AGENTS=skip \
  HEXGATE_DASHBOARD_URL=http://localhost:8000 \
  HEXGATE_DASHBOARD_DIST="$DASHBOARD_DIST" \
  .venv/bin/uvicorn hexgate_api.main:app --port 8000
wait_for api http://localhost:8000/health 90

echo "== MCP server on :8080 =="
start mcp "$ROOT/platform/mcp" uv run python -m hexgate_mcp
wait_for mcp http://localhost:8080/health 120

COLLECTOR="$ROOT/platform/collector/hexgate-collector"
if [ -x "$COLLECTOR" ]; then
  echo "== collector :4318 + enricher =="
  start collector "$ROOT" make -s collector-run
  start enricher "$ROOT" make -s enricher-run
  wait_for collector http://localhost:4318 60 || true
else
  echo "== collector not built: decisions won't reach the audit log (needs Go) =="
fi

echo
echo "Dashboard   http://localhost:8000/v1/demo-login   (signs in, no password)"
echo "MCP         http://localhost:8080/mcp"
echo "Logs        $LOGDIR"
