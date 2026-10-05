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

compose() {
  docker compose -p "hexgate-$STAGE" \
    --env-file "platform/.env.$STAGE" \
    -f platform/docker-compose.deploy.yml "$@"
}

# Same client invocation as migrate.sh's run_clickhouse (see there for why
# --database is passed).
compose exec -T clickhouse sh -c \
  'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
    --database "$CLICKHOUSE_DB" --multiquery' \
  <"$FILE"

echo "backfill($STAGE): $FILE ok"
