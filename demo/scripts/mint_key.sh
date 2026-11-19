#!/usr/bin/env bash
# Mint a hexgate API key for the default project and write demo/bank/.env.
# Signs in through the demo-only /v1/demo-login (no password), so it only works
# against a stack started with demo/scripts/up.sh. Keeps OPENAI_API_KEY if
# demo/bank/.env already has one.
set -euo pipefail

DEMO="$(cd "$(dirname "$0")/.." && pwd)"
API="${HEXGATE_API_URL:-http://localhost:8000}"
PROJECT="00000000-0000-0000-0000-000000000003"   # the seeded default project
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT

curl -sf -c "$JAR" -o /dev/null "$API/v1/demo-login"
# The seeded project is called "support-bot"; give it the bank's name on camera.
curl -sf -b "$JAR" -X PATCH "$API/v1/projects/$PROJECT" \
  -H 'content-type: application/json' -d '{"name":"hexa-bank"}' -o /dev/null
KEY=$(curl -sf -b "$JAR" -X POST "$API/v1/projects/$PROJECT/tokens" \
  -H 'content-type: application/json' \
  -d '{"name":"bank-demo","env":"live"}' |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["full"])')

OPENAI=""
[ -f "$DEMO/bank/.env" ] && OPENAI=$(sed -n 's/^OPENAI_API_KEY=//p' "$DEMO/bank/.env" | tail -1)

cat > "$DEMO/bank/.env" <<EOF
OPENAI_API_KEY=${OPENAI}
HEXGATE_API_KEY=${KEY}
HEXGATE_API_URL=${API}
HEXGATE_OTLP_ENDPOINT=http://localhost:4318/v1/traces
# The agents SDK's own tracing goes to OpenAI; not needed here.
OPENAI_AGENTS_DISABLE_TRACING=1
EOF
echo "wrote $DEMO/bank/.env (key …${KEY: -6})"
[ -n "$OPENAI" ] || echo "add your OPENAI_API_KEY to $DEMO/bank/.env"
