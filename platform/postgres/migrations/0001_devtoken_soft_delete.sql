-- Adds the soft-delete / audit columns to devtoken (the ApiKey table).
--
-- Applied BY HAND on deployed environments, not by a runner. init_db() is
-- SQLModel.metadata.create_all (platform/api/hexgate_api/core/db.py:82), which
-- creates missing TABLES and never adds columns to a table that already
-- exists -- so a database that has ever run the control plane will not pick
-- these up on its own. `make postgres-init` replays this directory for local
-- volumes; deployed stacks apply it as described in platform/DEPLOY.md § 6.
--
-- ORDERING: apply this BEFORE deploying the code that references the columns.
-- Additive and back-compatible in that direction (a running old API never
-- selects them), and catastrophic in the other: the Collector's snapshot query
-- filters on revoked_at, so against a database without the column its first
-- load raises UndefinedColumn, start() returns "initial api-key load: ..." and
-- the container refuses to boot, taking OTLP ingest down; every
-- bearer-authenticated API route would 500 on find_token_by_secret.
--
-- revoked_by_user_id deliberately carries no REFERENCES clause. create_all
-- emits one on a fresh database (the SQLModel field declares
-- foreign_key="user.id", matching Ban.revoked_by_user_id), but the column is
-- nullable and only ever read for display, so adding a constraint to a live
-- table is not worth the lock.
--
-- Idempotent (IF NOT EXISTS on both), so re-running is a no-op.

ALTER TABLE devtoken ADD COLUMN IF NOT EXISTS revoked_at timestamptz;
ALTER TABLE devtoken ADD COLUMN IF NOT EXISTS revoked_by_user_id varchar;
