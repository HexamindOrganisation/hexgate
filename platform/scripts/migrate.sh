#!/usr/bin/env bash
# Replay the SQL migrations for one deploy stage: Postgres first, then ClickHouse.
#
# Deliberately NOT `set -e`. The two stores are independent, and the failure this
# script exists to prevent is a Postgres error silently skipping every ClickHouse
# migration -- how a stale llm_message table reached both stages. Within a store
# the loop still stops at the first failure: 0003 may depend on 0002 having landed.
#
# Called by `make platform-migrate STAGE=<stage>`; see platform/DEPLOY.md § 6.
set -uo pipefail

STAGE="${1:?usage: migrate.sh <stage>}"

# ADD COLUMN takes ACCESS EXCLUSIVE and CREATE INDEX takes SHARE, so one session
# idle in a transaction blocks the run indefinitely. Fail in seconds with a
# nameable error instead; the operator stops the writers and re-runs. Set to 0 to
# wait forever, which is what a quiesced stack running a long index build wants.
LOCK_TIMEOUT_MS="${HEXGATE_MIGRATE_LOCK_TIMEOUT_MS:-10000}"

compose() {
  docker compose -p "hexgate-$STAGE" \
    --env-file "platform/.env.$STAGE" \
    -f platform/docker-compose.deploy.yml "$@"
}

run_postgres() {
  compose exec -T -e PGOPTIONS="-c lock_timeout=$LOCK_TIMEOUT_MS" postgres \
    psql -v ON_ERROR_STOP=1 -U hexgate -d hexgate <"$1"
}

# --database is not redundant: CLICKHOUSE_DB creates the database but does NOT
# make it the user's default, so without this the session lands on `default`.
# Every migration today is fully qualified (hexgate_audit.llm_message, ...) so
# nothing depends on it yet -- but `make clickhouse-migrate` passes --database,
# and an unqualified ALTER would then pass locally and fail here with
# `Code: 60 ... UNKNOWN_TABLE`. Read from the container's own env, as the
# credentials above are.
run_clickhouse() {
  compose exec -T clickhouse sh -c \
    'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
      --database "$CLICKHOUSE_DB" --multiquery' \
    <"$1"
}

# Applies every file in a directory through `runner`, stopping at the first
# failure. One line per file AFTER it succeeds, so an interrupted run shows what
# completed rather than what it was about to attempt.
apply_dir() {
  local label="$1" dir="$2" runner="$3"
  local applied=0 f
  for f in "$dir"/*.sql; do
    if [ ! -e "$f" ]; then
      echo "$label: no .sql files in $dir" >&2
      return 1
    fi
    if "$runner" "$f"; then
      applied=$((applied + 1))
      echo "$label: applied $f"
    else
      echo "$label: FAILED $f" >&2
      echo "$label: stopped after $applied file(s) -- later files may depend on it" >&2
      return 1
    fi
  done
  echo "$label: $applied file(s) applied"
}

apply_dir postgres platform/postgres/migrations run_postgres
pg_rc=$?
apply_dir clickhouse platform/clickhouse/migrations run_clickhouse
ch_rc=$?

status() { [ "$1" -eq 0 ] && echo ok || echo FAILED; }

echo
echo "migrate($STAGE): postgres=$(status $pg_rc) clickhouse=$(status $ch_rc)"

if [ "$pg_rc" -ne 0 ] || [ "$ch_rc" -ne 0 ]; then
  echo "migrate($STAGE): at least one store did not complete -- see above" >&2
  exit 1
fi
