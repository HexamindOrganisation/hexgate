#!/usr/bin/env bash
# Checks which deploy services receive which secrets, by rendering
# platform/docker-compose.deploy.yml with marker values (`docker compose
# config`: parse and interpolate only, nothing runs):
#   1. The policy assistant's secrets (.env.<stage>.assistant) reach the
#      assistant and assistant-db-init and no other service. The api, api-init
#      and enricher load all of .env.<stage> through env_file, which is why
#      those secrets live in a second file.
#   2. The assistant receives exactly its own environment (an allowlist of
#      keys), no env_file and no volume, so no secret of .env.<stage> and no
#      signing key reaches it.
#   3. The deploy targets that start or change containers, and
#      platform-env-pull, refuse a .env.<stage> that holds one of the
#      assistant's secrets (env-split-check.sh); down and logs do not.
#      Compose cannot tell where an interpolated value came from, so that is
#      checked on the file.
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

# Services whose definition (environment, labels, command, build args...)
# contains marker $1.
holders() {
    jq -r --arg m "$1" '.services | to_entries[]
        | select(.value | tostring | contains($m))
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

# The assistant's whole environment, by name. A key added to the service has
# to be added here, which is the point: each one is a decision about what the
# assistant may hold.
want_env="FORWARDED_ALLOW_IPS,HEXGATE_ASSISTANT_API_URL,HEXGATE_ASSISTANT_MCP_URL,\
HEXGATE_ASSISTANT_MODEL,HEXGATE_ASSISTANT_OPENROUTER_KEY,HEXGATE_ASSISTANT_POSTGRES_HOST,\
HEXGATE_ASSISTANT_POSTGRES_PASSWORD,HEXGATE_ASSISTANT_POSTGRES_PORT,\
HEXGATE_ASSISTANT_RESOURCE_URL,HEXGATE_MCP_RESOURCE_URL,HEXGATE_PUBLIC_URL"
got_env="$(jq -r '.services.assistant.environment // {} | keys | join(",")' "$tmp/config.json")"
if [[ "$got_env" != "$want_env" ]]; then
    echo "the assistant's environment is '$got_env' (want: '$want_env')" >&2
    fail=1
fi
# No env_file check: `config` merges an env_file into `environment`, which
# the allowlist above already pins.
if [[ "$(jq -r '.services.assistant.volumes // empty | length' "$tmp/config.json")" != "" ]]; then
    echo "the assistant has volumes; it must have none" >&2
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

# The guard against an assistant secret in the main file: it passes the
# marker file, refuses it with a secret added, and the deploy targets run it.
split_check="$here/scripts/env-split-check.sh"
if ! bash "$split_check" "$tmp/.env.check" 2>/dev/null; then
    echo "env-split-check.sh refuses a main file with no assistant secret" >&2
    fail=1
fi
for line in HEXGATE_ASSISTANT_POSTGRES_PASSWORD=m HEXGATE_ASSISTANT_MODEL=m HEXGATE_ASSISTANT_OPENROUTER_KEY=m \
        "HEXGATE_ASSISTANT_OPENROUTER_KEY = m" "HEXGATE_ASSISTANT_OPENROUTER_KEY: m" "  export HEXGATE_ASSISTANT_MODEL=m"; do
    { cat "$tmp/.env.check"; echo "$line"; } >"$tmp/.env.leak"
    if bash "$split_check" "$tmp/.env.leak" 2>/dev/null; then
        echo "env-split-check.sh accepts a main file holding '$line'" >&2
        fail=1
    fi
done
# Which targets run it: every one that starts or changes containers, and the
# pull; never down or logs, which must work during an incident.
# Prints yes or no, and fails when make prints no recipe for the target (a
# renamed target, or one left behind only as .PHONY), so the "must not" half
# cannot pass by default.
runs_guard() {
    # Captured first: `grep -q` exiting early would SIGPIPE make under pipefail.
    local recipe
    recipe="$(make -s -n -C "$here/.." "$1" STAGE=staging FILE=x)" || return 1
    grep -q 'docker compose\|env-secret.sh' <<<"$recipe" || return 1
    if grep -qF 'platform/scripts/env-split-check.sh platform/.env.staging' <<<"$recipe"; then
        echo yes
    else
        echo no
    fi
}
for target in platform-stop-writers platform-migrate platform-backfill platform-up platform-env-pull \
        platform-down platform-logs; do
    case "$target" in platform-down | platform-logs) want=no ;; *) want=yes ;; esac
    if ! got="$(runs_guard "$target")"; then
        echo "make -n $target failed" >&2
        fail=1
    elif [[ "$got" != "$want" ]]; then
        echo "make $target runs env-split-check.sh: $got (want: $want)" >&2
        fail=1
    fi
done

(( fail == 0 )) || exit 1
echo "compose: the assistant's secrets reach only assistant and assistant-db-init, the assistant holds only its allowlist, and the deploy targets refuse them in .env.<stage>"
