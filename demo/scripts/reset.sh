#!/usr/bin/env bash
# Put the demo back to the start of scene 1, between takes:
#   - bank_agent.py back to the unsafe original
#   - the project's policy files deleted (back to "no policy.yaml")
#   - the bank_assistant registration deleted
#   - bank_assistant's audit rows deleted from ClickHouse
# The rest of the platform (other audit data, the API key in .env, Claude's MCP
# login) is kept. Needs the stack from demo/scripts/up.sh.
set -euo pipefail

DEMO="$(cd "$(dirname "$0")/.." && pwd)"
API="${HEXGATE_API_URL:-http://localhost:8000}"
PROJECT="00000000-0000-0000-0000-000000000003"
AGENT="bank_assistant"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT

echo "== agent code"
cp "$DEMO/reference/bank_agent_original.py" "$DEMO/bank/bank_agent.py"

echo "== policy files"
curl -sf -c "$JAR" -o /dev/null "$API/v1/demo-login"
FILES=$(curl -sf -b "$JAR" "$API/v1/projects/$PROJECT/policy-files" |
  python3 -c 'import json,sys
names = [f["name"] for f in json.load(sys.stdin)]
# policy.yaml first: an imported file cannot go while something imports it.
print("\n".join(sorted(names, key=lambda n: n != "policy.yaml")))')
for f in $FILES; do
  code=$(curl -s -b "$JAR" -X DELETE -o /dev/null -w '%{http_code}' "$API/v1/projects/$PROJECT/policy-files/$f")
  echo "   delete $f → $code"
done

echo "== registration"
docker exec -i hexgate-postgres psql -q -v ON_ERROR_STOP=1 -U hexgate -d hexgate <<SQL
BEGIN;
CREATE TEMP TABLE gone AS
  SELECT id FROM agent WHERE name = '$AGENT' AND project_id = '$PROJECT';
DELETE FROM tool  WHERE agent_version_id IN (SELECT id FROM agent_version WHERE agent_id IN (SELECT id FROM gone));
DELETE FROM skill WHERE agent_version_id IN (SELECT id FROM agent_version WHERE agent_id IN (SELECT id FROM gone));
DELETE FROM agent_version        WHERE agent_id IN (SELECT id FROM gone);
DELETE FROM agent_classification WHERE agent_id IN (SELECT id FROM gone);
DELETE FROM agent                WHERE id IN (SELECT id FROM gone);
COMMIT;
SQL

echo "== audit rows"
for t in policy_decision llm_message agent_run llm_invocation ban_enforcement usage_minute; do
  docker exec hexgate-clickhouse clickhouse-client -q \
    "ALTER TABLE hexgate_audit.$t DELETE WHERE agent_name = '$AGENT' SETTINGS mutations_sync = 1"
done

# Anything else Claude left in demo/bank/ during the take (not deleted, just listed).
extra=$(cd "$DEMO/bank" && ls -A | grep -v -x -E '\.env|\.venv|__pycache__|_display\.py|bank_agent\.py|pyproject\.toml|uv\.lock|CLAUDE\.md|\.claude' || true)
[ -z "$extra" ] || { echo "== left over in demo/bank/ (check and remove by hand):"; echo "$extra" | sed 's/^/   /'; }
echo "reset done"
