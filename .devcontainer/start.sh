#!/usr/bin/env bash
# Launch the bundled demo (API + dashboard + marimo notebook) in the background.
# Runs as postAttachCommand on every attach — guarded so a reconnect doesn't
# start a second copy.
set -uo pipefail

if pgrep -f "deploy/boot.py" >/dev/null 2>&1; then
  echo "▶ Hexgate demo already running. Open the forwarded port 2718 (Notebook)."
  exit 0
fi

export PATH="$PWD/platform/api/.venv/bin:$HOME/.local/bin:$PATH"
export HEXGATE_DEMO=1
export HEXGATE_COOKIE_SECURE=1   # Codespaces forwarded ports are HTTPS

# Point the notebook's "Open playground" link at *this* codespace's forwarded
# dashboard URL (port 8000), so it opens in the visitor's browser rather than an
# unreachable localhost.
if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
  export HEXGATE_DASH_URL="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
fi

nohup python deploy/boot.py > /tmp/hexgate-demo.log 2>&1 &

echo ""
echo "▶ Hexgate demo starting (~30-60s on first run)."
echo "  When port 2718 forwards, the Notebook opens automatically."
echo "  Logs:  tail -f /tmp/hexgate-demo.log"
echo ""
