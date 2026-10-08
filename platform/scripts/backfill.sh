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

# An INSERT ... SELECT commits block by block, so a failed run can leave rows
# behind, and the file's own re-run guard then turns a plain rerun into a no-op.
if ! run_clickhouse "$FILE"; then
  echo "backfill($STAGE): FAILED $FILE. Rows may be partly committed, and a plain" \
    "rerun would skip them: with the writers still stopped, follow the recovery" \
    "steps in the header of $FILE." >&2
  exit 1
fi

echo "backfill($STAGE): $FILE ok"
