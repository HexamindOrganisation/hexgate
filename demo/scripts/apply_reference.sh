#!/usr/bin/env bash
# Do scene 2 without Claude: wrap the agent, register it, and save the
# reference policy (reference/policy/) to the platform. For rehearsing scenes
# 3-5, or as a fallback if a Claude take goes wrong. Undo with reset.sh.
set -euo pipefail

DEMO="$(cd "$(dirname "$0")/.." && pwd)"
API="${HEXGATE_API_URL:-http://localhost:8000}"
PROJECT="00000000-0000-0000-0000-000000000003"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
cd "$DEMO/bank"

echo "== wrap";     cp ../reference/bank_agent_secured.py bank_agent.py
echo "== register"; (set -a; . ./.env; set +a; uv run hexgate register --agent bank_agent:agent)

echo "== policy"
curl -sf -c "$JAR" -o /dev/null "$API/v1/demo-login"
# Imported files first: policy.yaml fails to link until what it imports exists.
for f in $(cd ../reference/policy && find caps -name '*.yaml' | sort) policy.yaml; do
  python3 -c 'import json,sys; print(json.dumps({"content": open(sys.argv[1]).read()}))' \
    "../reference/policy/$f" |
    curl -s -b "$JAR" -X PUT "$API/v1/projects/$PROJECT/policy-files/$f" \
      -H 'content-type: application/json' -d @- -o /dev/null -w "   $f → %{http_code}\n"
done
