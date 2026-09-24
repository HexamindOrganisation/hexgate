#!/usr/bin/env bash
# Preflight for running the hexgate platform locally.
#   preflight.sh          # light mode: platform-api (SQLite) + dashboard
#   preflight.sh --full   # full mode: + Postgres, Redpanda, ClickHouse, collector, enricher
# Prints one line per check and exits 1 if anything required is missing.
set -u
full=0; [ "${1:-}" = "--full" ] && full=1
fail=0
ok()   { printf '  ok    %s\n' "$1"; }
miss() { printf '  MISS  %s  (%s)\n' "$1" "$2"; fail=1; }
warn() { printf '  warn  %s  (%s)\n' "$1" "$2"; }

need() { command -v "$1" >/dev/null 2>&1 && ok "$1" || miss "$1" "$2"; }

echo "tools:"
need uv   "curl -LsSf https://astral.sh/uv/install.sh | sh"
need pnpm "brew install pnpm"
command -v opa >/dev/null 2>&1 && ok opa || warn opa "optional: brew install opa (WASM policy engine)"
if [ $full = 1 ]; then
  need go "brew install go (to build the collector)"
  if ! command -v docker >/dev/null 2>&1; then
    miss docker "install Docker Desktop"
  elif ! docker info >/dev/null 2>&1; then
    miss "docker daemon" "start Docker Desktop"
  else
    ok "docker (running)"
  fi
fi

# biscuit-python has no Python 3.14 wheel and PyO3 can't build it there.
if command -v uv >/dev/null 2>&1 && ! uv python find 3.13 >/dev/null 2>&1; then
  warn "python 3.13" "uv python install 3.13, then run with UV_PYTHON=3.13"
fi

echo "ports:"
ports="8000 5173"; [ $full = 1 ] && ports="$ports 4317 4318"
for p in $ports; do
  who=$(lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1" pid "$2}')
  [ -z "$who" ] && ok "$p free" || warn "$p busy" "$who; stop it or reuse that server"
done

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
echo "state:"
if [ -f "$root/platform/api/hexgate.db" ]; then
  warn "SQLite db exists" "already seeded, so no admin password will print; see SKILL.md"
else
  ok "fresh SQLite db (admin password prints on first boot)"
fi

exit $fail
