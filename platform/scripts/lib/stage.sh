# Shared by migrate.sh and backfill.sh: sourced, not run. The caller sets STAGE
# first. Paths are relative to the repo root, where `make platform-*` runs them.

compose() {
  docker compose -p "hexgate-$STAGE" \
    --env-file "platform/.env.$STAGE" \
    -f platform/docker-compose.deploy.yml "$@"
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
