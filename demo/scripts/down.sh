#!/usr/bin/env bash
# Stop what demo/scripts/up.sh started. Docker infra keeps running (and keeps its
# data); stop it with `make postgres-stop redpanda-stop clickhouse-down`.
set -uo pipefail
LOGDIR="${LOGDIR:-/tmp/hexgate-demo}"

kill_tree() {  # the pid and every descendant (make and uv run spawn children)
  local child
  for child in $(pgrep -P "$1"); do kill_tree "$child"; done
  kill "$1" 2>/dev/null
}

[ -f "$LOGDIR/pids" ] || { echo "nothing to stop"; exit 0; }
while read -r name pid; do
  kill_tree "$pid" && echo "stopped $name"
done < "$LOGDIR/pids"
: > "$LOGDIR/pids"
