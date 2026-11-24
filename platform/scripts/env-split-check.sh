#!/usr/bin/env bash
# Refuses a stage's main env file that holds one of the policy assistant's
# secrets. The api, api-init and enricher load all of .env.<stage>
# (env_file), so such a key reaches all three while the assistant keeps
# working, and nothing else would say so. Those keys belong in
# .env.<stage>.assistant (the /hexgate/<stage>-assistant secret).
# HEXGATE_ASSISTANT_PORT and HEXGATE_ASSISTANT_ENABLED are not secret and
# stay in the main file.
#   env-split-check.sh <main env file>
set -euo pipefail

file="${1:?usage: env-split-check.sh <main env file>}"
[[ -r "$file" ]] || { echo "env-split-check: cannot read $file" >&2; exit 1; }
# Every spelling compose's env-file parser accepts: `KEY=v`, `KEY = v`,
# `KEY: v`, with or without a leading `export`.
if keys="$(grep -oE '^[[:space:]]*(export[[:space:]]+)?HEXGATE_ASSISTANT_(POSTGRES_PASSWORD|MODEL|OPENROUTER_KEY)[[:space:]]*[=:]' "$file")"; then
    echo "$file holds the assistant's secrets:" $(sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/[[:space:]]*[=:]$//' <<<"$keys") >&2
    echo "move them to the /hexgate/<stage>-assistant secret, remove them from /hexgate/<stage>, then make platform-env-pull STAGE=<stage>" >&2
    exit 1
fi
