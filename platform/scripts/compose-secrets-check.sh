#!/usr/bin/env bash
# Checks which deploy services receive which secrets, by rendering
# platform/docker-compose.deploy.yml with marker values (`docker compose
# config`: parse and interpolate only, nothing runs):
#   1. The policy assistant's secrets (.env.<stage>.assistant) reach the
#      assistant and assistant-db-init and no other service. The api, api-init
#      and enricher load all of .env.<stage> through env_file, which is why
#      those secrets live in a second file.
#   2. The assistant itself receives no secret of .env.<stage>: no main
#      database password, no ClickHouse password.
# Needs docker compose and jq; without them the check is skipped with a
# notice, unless HEXGATE_COMPOSE_CHECK_REQUIRED is set (the CI job sets it).
set -euo pipefail

if ! docker compose version >/dev/null 2>&1 || ! command -v jq >/dev/null; then
    if [[ -n "${HEXGATE_COMPOSE_CHECK_REQUIRED:-}" ]]; then
        echo "docker compose or jq not found, and HEXGATE_COMPOSE_CHECK_REQUIRED is set" >&2
        exit 1
    fi
    echo "docker compose or jq not found, skipping compose-check (CI runs it)"
    exit 0
fi

here="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The services' env_file is .env.${HEXGATE_ENV}, next to the compose file, so
# the compose is rendered from a copy beside the marker files.
cp "$here/docker-compose.deploy.yml" "$tmp/"
# The ports and the URL only satisfy the compose's `:?` guards.
cat >"$tmp/.env.check" <<'EOF'
HEXGATE_ENV=check
HEXGATE_HTTP_PORT=7900
HEXGATE_OTLP_PORT=7901
HEXGATE_MCP_PORT=7902
HEXGATE_ASSISTANT_PORT=7903
HEXGATE_PUBLIC_URL=https://check.invalid
HEXGATE_POSTGRES_PASSWORD=main-secret-marker
HEXGATE_CLICKHOUSE_PASSWORD=main-secret-marker
EOF
cat >"$tmp/.env.check.assistant" <<'EOF'
HEXGATE_ASSISTANT_POSTGRES_PASSWORD=assistant-secret-marker
HEXGATE_ASSISTANT_MODEL=assistant-secret-marker
HEXGATE_ASSISTANT_OPENROUTER_KEY=assistant-secret-marker
EOF
# Compose prefers the shell's own variables to --env-file, so a key exported
# here would replace its marker; unset each key the marker files set.
# shellcheck disable=SC2046
unset $(cut -d= -f1 "$tmp/.env.check" "$tmp/.env.check.assistant")
docker compose -p check --env-file "$tmp/.env.check" --env-file "$tmp/.env.check.assistant" \
    -f "$tmp/docker-compose.deploy.yml" config --format json >"$tmp/config.json"

# Services whose environment holds a value containing marker $1.
holders() {
    jq -r --arg m "$1" '.services | to_entries[]
        | select([.value.environment // {} | .[] | strings | contains($m)] | any)
        | .key' "$tmp/config.json" | sort | paste -sd, -
}

fail=0
assistant_holders="$(holders assistant-secret-marker)"
if [[ "$assistant_holders" != "assistant,assistant-db-init" ]]; then
    echo "the assistant's secrets reach '$assistant_holders' (want: assistant,assistant-db-init)" >&2
    fail=1
fi
if [[ ",$(holders main-secret-marker)," == *,assistant,* ]]; then
    echo "the assistant receives a secret of .env.<stage>" >&2
    fail=1
fi
# The marker reaching the assistant at all proves each key is passed through,
# not only that none leaks.
for key in HEXGATE_ASSISTANT_POSTGRES_PASSWORD HEXGATE_ASSISTANT_MODEL HEXGATE_ASSISTANT_OPENROUTER_KEY; do
    if [[ "$(jq -r --arg k "$key" '.services.assistant.environment[$k] // ""' "$tmp/config.json")" != assistant-secret-marker ]]; then
        echo "the assistant does not receive $key" >&2
        fail=1
    fi
done
(( fail == 0 )) || exit 1
echo "compose: the assistant's secrets reach only assistant and assistant-db-init, and none of .env.<stage> reaches the assistant"
