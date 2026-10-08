#!/usr/bin/env bash
# Run one one-time ClickHouse backfill against a deploy stage.
#
# Backfills are not idempotent the way migrations are, which is why they live in
# platform/clickhouse/backfills/ and not in migrations/ (migrate.sh replays that
# whole directory on every upgrade). Each file guards its own re-run.
#
# Called by `make platform-backfill STAGE=<stage> FILE=<name>`; see
# platform/DEPLOY.md § 6.
set -euo pipefail

STAGE="${1:?usage: backfill.sh <stage> <file>}"
FILE="${2:?usage: backfill.sh <stage> <file>}"

# shellcheck source=lib/stage.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/stage.sh"

run_clickhouse "$FILE"

echo "backfill($STAGE): $FILE ok"
