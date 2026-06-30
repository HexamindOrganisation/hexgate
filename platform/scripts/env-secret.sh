#!/usr/bin/env bash
#
# Pull platform/.env.<stage> from Scaleway Secret Manager.
#
#   env-secret.sh <stage>        # write platform/.env.<stage> from the secret
#
# Each stage's whole env file is stored as ONE opaque secret in Scaleway,
# addressed by path + name: <path>/<stage>, e.g. /hexgate/staging. Opaque (not
# key_value) means the payload is the file verbatim — comments, quotes, ordering
# — so this is a byte-for-byte restore of revision=latest. Creating and updating
# that secret is the admin's job (console or `scw`); this script only reads.
#
# Requires: scw CLI (authenticated) + jq. Region defaults to fr-par (matches the
# rg.fr-par.scw.cloud registry); override with SCW_DEFAULT_REGION. The secret
# folder defaults to /hexgate; override with HEXGATE_SECRET_PATH.
set -euo pipefail

die() { echo "env-secret: $*" >&2; exit 1; }

STAGE="${1:-}"
REGION="${SCW_DEFAULT_REGION:-fr-par}"
SECRET_PATH="${HEXGATE_SECRET_PATH:-/hexgate}"

[[ "$STAGE" == "prod" || "$STAGE" == "staging" ]] \
  || die "usage: env-secret.sh {prod|staging} (got '${STAGE:-}')"

# Secret is <path>/<stage> in Scaleway, e.g. /hexgate/staging.
SECRET_NAME="$STAGE"
# Resolve relative to the repo root so the target is the same whatever the cwd.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/platform/.env.${STAGE}"

command -v scw >/dev/null \
  || die "scw CLI not found — install: https://github.com/scaleway/scaleway-cli"
command -v jq >/dev/null \
  || die "jq not found — install jq"

tmp="${ENV_FILE}.tmp.$$"
trap 'rm -f "$tmp"' EXIT
# access-by-path resolves the secret by folder + name (no id needed); scw returns
# the payload base64-encoded in .data, which we decode to the raw file. A bad
# path/name/region or missing creds makes this fail before any file is touched.
scw secret version access-by-path \
    secret-path="$SECRET_PATH" secret-name="$SECRET_NAME" revision=latest region="$REGION" -o json 2>/dev/null \
  | jq -r '.data' | base64 -d > "$tmp" \
  || die "could not access secret $SECRET_PATH/$SECRET_NAME in region $REGION — check the secret exists, your creds, and SCW_DEFAULT_REGION / HEXGATE_SECRET_PATH"
[[ -s "$tmp" ]] || die "secret $SECRET_PATH/$SECRET_NAME decoded to an empty file — refusing to overwrite $ENV_FILE"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"   # atomic: a failed pull never leaves a half-written env
trap - EXIT
echo "env-secret: wrote $ENV_FILE from $SECRET_PATH/$SECRET_NAME (region $REGION)"
