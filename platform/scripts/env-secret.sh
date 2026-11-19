#!/usr/bin/env bash
#
# Pull a stage's env files from Scaleway secrets (opaque, admin-maintained —
# read only): platform/.env.<stage> from /hexgate/<stage>, and
# platform/.env.<stage>.assistant from /hexgate/<stage>-assistant (the policy
# assistant's secrets; why apart: DEPLOY.md section 3).
#   env-secret.sh <stage>             # both
#   env-secret.sh <stage> main        # only .env.<stage>
#   env-secret.sh <stage> assistant   # only .env.<stage>.assistant
# Needs scw (authenticated) + jq.
# Override region via SCW_DEFAULT_REGION, secret folder via HEXGATE_SECRET_PATH.
set -euo pipefail

die() { echo "env-secret: $*" >&2; exit 1; }

STAGE="${1:-}"
REGION="${SCW_DEFAULT_REGION:-fr-par}"
SECRET_PATH="${HEXGATE_SECRET_PATH:-/hexgate}"

WHICH="${2:-all}"

case "$STAGE/$WHICH" in
  prod/all | prod/main | prod/assistant | staging/all | staging/main | staging/assistant) ;;
  *) die "usage: env-secret.sh {prod|staging} [main|assistant] (got '$*')" ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

command -v scw >/dev/null || die "scw CLI not found — https://github.com/scaleway/scaleway-cli"
command -v jq >/dev/null || die "jq not found"

# pull <secret name> <env file>
pull() {
  local secret_name="$1" env_file="$2"
  tmp="${env_file}.tmp.$$"   # global: the EXIT trap reads it
  trap 'rm -f "$tmp"' EXIT
  # .data is base64; decode to the raw file. Fails before touching $env_file.
  # jq -e fails the pipeline if .data is missing/null, so a malformed response
  # can't slip through as decoded garbage (pipefail surfaces it as the die below).
  scw secret version access-by-path \
      secret-path="$SECRET_PATH" secret-name="$secret_name" revision=latest region="$REGION" -o json 2>/dev/null \
    | jq -e -r '.data' | base64 -d > "$tmp" \
    || die "cannot access $SECRET_PATH/$secret_name in $REGION — check it exists, creds, SCW_DEFAULT_REGION / HEXGATE_SECRET_PATH"
  [[ -s "$tmp" ]] || die "$SECRET_PATH/$secret_name is empty — refusing to overwrite $env_file"
  chmod 600 "$tmp"
  mv "$tmp" "$env_file"   # atomic
  trap - EXIT
  echo "env-secret: wrote $env_file from $SECRET_PATH/$secret_name (region $REGION)"
}

[[ "$WHICH" == "assistant" ]] || pull "$STAGE" "${REPO_ROOT}/platform/.env.${STAGE}"
[[ "$WHICH" == "main" ]] || pull "${STAGE}-assistant" "${REPO_ROOT}/platform/.env.${STAGE}.assistant"
