#!/usr/bin/env bash
#
# Pull platform/.env.<stage> from the Scaleway secret /hexgate/<stage> (opaque,
# admin-maintained — read only). Needs scw (authenticated) + jq.
# Override region via SCW_DEFAULT_REGION, secret folder via HEXGATE_SECRET_PATH.
set -euo pipefail

die() { echo "env-secret: $*" >&2; exit 1; }

STAGE="${1:-}"
REGION="${SCW_DEFAULT_REGION:-fr-par}"
SECRET_PATH="${HEXGATE_SECRET_PATH:-/hexgate}"

[[ "$STAGE" == "prod" || "$STAGE" == "staging" ]] \
  || die "usage: env-secret.sh {prod|staging} (got '${STAGE:-}')"

SECRET_NAME="$STAGE"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/platform/.env.${STAGE}"

command -v scw >/dev/null || die "scw CLI not found — https://github.com/scaleway/scaleway-cli"
command -v jq >/dev/null || die "jq not found"

tmp="${ENV_FILE}.tmp.$$"
trap 'rm -f "$tmp"' EXIT
# .data is base64; decode to the raw file. Fails before touching $ENV_FILE.
scw secret version access-by-path \
    secret-path="$SECRET_PATH" secret-name="$SECRET_NAME" revision=latest region="$REGION" -o json 2>/dev/null \
  | jq -r '.data' | base64 -d > "$tmp" \
  || die "cannot access $SECRET_PATH/$SECRET_NAME in $REGION — check it exists, creds, SCW_DEFAULT_REGION / HEXGATE_SECRET_PATH"
[[ -s "$tmp" ]] || die "$SECRET_PATH/$SECRET_NAME is empty — refusing to overwrite $ENV_FILE"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"   # atomic
trap - EXIT
echo "env-secret: wrote $ENV_FILE from $SECRET_PATH/$SECRET_NAME (region $REGION)"
