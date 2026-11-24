#!/usr/bin/env bash
# Runs platform/postgres/assistant_db.sql against a Postgres the caller names
# with the libpq variables (PGHOST, PGPORT, PGUSER as a superuser, PGPASSWORD,
# PGDATABASE=hexgate) and checks what it promises:
#   1. It runs twice without error (it runs on every `up`).
#   2. The login connects to hexgate_assistant with its password, and only
#      with it (a trust rule would let a wrong one in).
#   3. The login creates a table there: it owns the database, which on
#      Postgres 15+ is what grants CREATE on its public schema.
#   4. The login is refused on hexgate.
# psql runs from the postgres:16 image (the script needs psql 15+ for
# \getenv), on the host network, so PGHOST must not be a loopback address
# that the server trusts without a password. CI's platform-api-postgres job
# runs it.
set -euo pipefail

: "${PGHOST:?}" "${PGUSER:?}" "${PGPASSWORD:?}" "${PGDATABASE:?}"
export PGPORT="${PGPORT:-5432}"
export HEXGATE_ASSISTANT_POSTGRES_PASSWORD="assistant-db-check-$$"
sql_dir="$(cd "$(dirname "$0")/../postgres" && pwd)"

psql_() {
    docker run --rm -i --network host \
        -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE \
        -e HEXGATE_ASSISTANT_POSTGRES_PASSWORD \
        -v "$sql_dir:/sql:ro" postgres:16 psql -X -q -v ON_ERROR_STOP=1 "$@"
}

fail() { echo "$*" >&2; exit 1; }

# As the assistant's login: password $1, database $2, SQL $3.
as_assistant() {
    PGUSER=hexgate_assistant PGPASSWORD="$1" PGDATABASE="$2" psql_ -c "$3"
}

for _ in 1 2; do psql_ -f /sql/assistant_db.sql; done
echo "assistant_db.sql ran twice"

pw="$HEXGATE_ASSISTANT_POSTGRES_PASSWORD"
as_assistant "$pw" hexgate_assistant 'CREATE TABLE assistant_db_check (x int); DROP TABLE assistant_db_check;'
echo "the login connects to hexgate_assistant and creates a table there"

as_assistant wrong-password hexgate_assistant 'SELECT 1' 2>/dev/null \
    && fail "the login connected with a wrong password: the check is not testing passwords"

out="$(as_assistant "$pw" hexgate 'SELECT 1' 2>&1)" && fail "the login opened hexgate"
grep -q 'permission denied for database "hexgate"' <<<"$out" \
    || fail "the login failed on hexgate for another reason: $out"
echo "the login is refused on hexgate"
