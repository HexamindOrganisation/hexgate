#!/usr/bin/env bash
# Inspect or stop a local hexgate platform instance (macOS or Linux).
#   instance.sh who    # checkout, make target and exported DATABASE_URL of the API on :8000
#   instance.sh stop   # stop this checkout's API, dashboard, collector and enricher
set -u

if ! command -v lsof >/dev/null 2>&1 && ! command -v ss >/dev/null 2>&1; then
  echo "instance.sh: needs lsof or ss to find listening processes" >&2; exit 1
fi

# PIDs listening on TCP port $1.
listeners() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"$1" -sTCP:LISTEN 2>/dev/null
  else
    ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -un
  fi
}

cwd_of() {
  if [ -d /proc/"$1" ]; then readlink /proc/"$1"/cwd
  else lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'; fi
}

# Every hexgate process runs from <checkout>/platform/<component>; anything
# else on these ports (Docker's port proxy, another project's Vite) is not ours.
checkout_of() {
  case $(cwd_of "$1") in
    */platform/api|*/platform/dashboard|*/platform/collector) dirname "$(dirname "$(cwd_of "$1")")";;
  esac
}

case "${1:-}" in
who)
  # With --reload both the reloader and its worker hold :8000. Pick the
  # reloader (the listener whose parent isn't one): killing only the worker
  # leaves the reloader holding a port nothing answers on.
  all=" $(echo $(listeners 8000)) "
  pid=""
  for p in $all; do
    [ -n "$(checkout_of "$p")" ] || continue
    case $all in *" $(ps -o ppid= -p "$p" | tr -d ' ') "*) continue;; esac
    pid=$p; break
  done
  if [ -z "$pid" ]; then
    other=$(listeners 8000 | head -1)
    [ -n "$other" ] && echo "not a hexgate API on :8000: $(ps -o command= -p "$other")" \
                    || echo "nothing listens on :8000"
    exit 1
  fi
  if [ -d /proc/"$pid" ]; then
    db=$(tr '\0' '\n' < /proc/"$pid"/environ 2>/dev/null | grep '^DATABASE_URL=')
  else
    # macOS only shows env as one space-separated line, so a value containing a
    # space is cut at the space.
    db=$(ps -wwE -o command= -p "$pid" | tr ' ' '\n' | grep '^DATABASE_URL=')
  fi
  target=""
  p=$pid
  while [ -n "$p" ] && [ "$p" != 1 ] && [ "$p" != 0 ]; do
    c=$(ps -o command= -p "$p")
    case $c in *make*platform-api*) target=$c; break;; esac
    p=$(ps -o ppid= -p "$p" | tr -d ' ')
  done
  echo "pid:          $pid"
  echo "checkout:     $(checkout_of "$pid")   (run make from here)"
  echo "target:       ${target:-unknown (not started through make)}"
  echo "DATABASE_URL: ${db#DATABASE_URL=}"
  [ -z "${db#DATABASE_URL=}" ] && echo "              (none exported: SQLite, or whatever platform/api/.env sets)"
  exit 0
  ;;
stop)
  root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "instance.sh: run it inside a hexgate checkout" >&2; exit 1; }
  # API, dashboard (Vite moves up from 5173 when it is taken), collector,
  # enricher (matched on its -m module, not on a file path an editor could hold).
  cands=$( { for port in 8000 5173 5174 5175 4317 4318; do listeners "$port"; done
             pgrep -f -- '-m hexgate_api\.jobs\.enricher'; } 2>/dev/null | sort -u)
  pids=""
  for p in $cands; do
    co=$(checkout_of "$p")
    if [ "$co" = "$root" ]; then pids="$pids $p"
    elif [ -n "$co" ]; then echo "skipping pid $p: it belongs to another checkout, $co"
    fi
  done
  [ -z "$pids" ] && { echo "nothing of this checkout is running"; exit 0; }
  echo "stopping:$pids"
  kill $pids 2>/dev/null
  # Wait for them to exit, so an immediate restart doesn't find the port taken.
  for _ in $(seq 1 20); do
    alive=""; for p in $pids; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
    [ -z "$alive" ] && { echo "stopped"; exit 0; }
    sleep 0.5
  done
  echo "still running after 10s:$alive (kill -9 them if they don't exit)"; exit 1
  ;;
*)
  echo "usage: instance.sh who|stop" >&2; exit 2
  ;;
esac
