#!/usr/bin/env bash
# Preflight for running the hexgate platform locally (macOS or Linux).
#   preflight.sh          # light mode: platform-api (SQLite) + dashboard
#   preflight.sh --full   # full mode: + Postgres, Redpanda, ClickHouse, collector, enricher
# Prints one line per check and exits 1 if anything required is missing.
set -u
full=0; [ "${1:-}" = "--full" ] && full=1
fail=0
ok()   { printf '  ok    %s\n' "$1"; }
miss() { printf '  MISS  %s  (%s)\n' "$1" "$2"; fail=1; }
warn() { printf '  warn  %s  (%s)\n' "$1" "$2"; }

# hint <macOS text> <Linux text>
mac=0; [ "$(uname -s)" = Darwin ] && mac=1
hint() { [ $mac = 1 ] && echo "$1" || echo "$2"; }

need() { command -v "$1" >/dev/null 2>&1 && ok "$1" || miss "$1" "$2"; }

# Docker first: it decides which mode is possible at all.
# Required in full mode (Postgres, Redpanda, ClickHouse), not in light mode.
echo "docker:"
docker_hint=""
if ! command -v docker >/dev/null 2>&1; then
  docker_hint="not installed: $(hint \
    'brew install --cask docker (or https://docs.docker.com/desktop/), then open Docker Desktop' \
    'https://docs.docker.com/engine/install/ (Engine plus the compose plugin)')"
else
  # `docker info` has no timeout and hangs on a daemon that accepts but never
  # answers (Docker Desktop still starting), so give it 10s.
  err=$(mktemp); docker info >/dev/null 2>"$err" & dpid=$!
  for _ in $(seq 1 20); do kill -0 $dpid 2>/dev/null || break; sleep 0.5; done
  if kill -0 $dpid 2>/dev/null; then
    { kill $dpid; wait $dpid; } 2>/dev/null   # wait: no "Terminated" line on bash 3.2
    docker_hint="the daemon doesn't answer after 10s (still starting?): wait, then rerun"
  elif ! wait $dpid; then
    case $(cat "$err") in
      *"permission denied"*) docker_hint="permission denied on the Docker socket: sudo usermod -aG docker \$USER, then log in again";;
      *) docker_hint="installed but the daemon isn't running: $(hint \
           "open -a Docker, wait for it to say running" \
           "sudo systemctl start docker")";;
    esac
  fi
  rm -f "$err"
fi
if [ -z "$docker_hint" ]; then
  ok "docker (running)"
elif [ $full = 1 ]; then
  miss docker "$docker_hint"
else
  warn docker "$docker_hint; light mode doesn't need it, full mode does"
fi

echo "tools:"
need uv   "curl -LsSf https://astral.sh/uv/install.sh | sh"
need pnpm "$(hint 'brew install pnpm' 'npm install -g pnpm (or https://pnpm.io/installation)')"
command -v opa >/dev/null 2>&1 && ok opa || warn opa "optional: $(hint 'brew install opa' 'https://www.openpolicyagent.org/docs/#running-opa') (WASM policy engine)"
[ $full = 1 ] && need go "$(hint 'brew install go' 'https://go.dev/doc/install') (to build the collector)"

# biscuit-python has no Python 3.14 wheel and PyO3 can't build it there.
if command -v uv >/dev/null 2>&1 && ! uv python find 3.13 >/dev/null 2>&1; then
  warn "python 3.13" "uv python install 3.13, then run with UV_PYTHON=3.13"
fi

# Who listens on TCP port $1: "<name> pid <pid>", "in use" when the owner is
# hidden (another user's process), or nothing when the port is free.
listener() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1" pid "$2}'
  else
    line=$(ss -ltnpH "sport = :$1" 2>/dev/null | head -1)
    [ -z "$line" ] && return
    who=$(echo "$line" | grep -o '"[^"]*",pid=[0-9]*' | head -1 | sed 's/"\(.*\)",pid=/\1 pid /')
    echo "${who:-in use}"
  fi
}

echo "ports:"
# 8000 API, 5173 dashboard; full mode adds the collector (4317/4318) and the
# host ports platform/docker-compose.yml maps: Postgres 5433, ClickHouse
# 8124/9001, Redpanda 9092.
ports="8000 5173"; [ $full = 1 ] && ports="$ports 4317 4318 5433 8124 9001 9092"
if ! command -v lsof >/dev/null 2>&1 && ! command -v ss >/dev/null 2>&1; then
  warn "ports not checked" "needs lsof or ss: $(hint 'lsof ships with macOS' 'install lsof or iproute2')"
else
  for p in $ports; do
    # A published container port shows up as Docker's own proxy process, so
    # name the container instead. hexgate's own (the container_name values in
    # platform/docker-compose.yml) are reused by make.
    ctr=""
    [ -z "$docker_hint" ] && ctr=$(docker ps --filter "publish=$p" --format '{{.Names}}' 2>/dev/null | head -1)
    case $ctr in
      hexgate-postgres|hexgate-clickhouse|hexgate-redpanda) ok "$p used by hexgate container $ctr (make reuses it)"; continue;;
      ?*) warn "$p busy" "container $ctr; stop it or free the port"; continue;;
    esac
    who=$(listener "$p")
    [ -z "$who" ] && ok "$p free" || warn "$p busy" "$who; stop it or reuse that server"
  done
fi

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
echo "state:"
if [ -f "$root/platform/api/hexgate.db" ]; then
  warn "SQLite db exists" "already seeded, so no admin password will print; see SKILL.md"
else
  ok "fresh SQLite db (admin password prints on first boot)"
fi

exit $fail
