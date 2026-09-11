-- Adds the control-plane actor trail (issue #160): who created a row, who last
-- updated it, and -- on devtoken -- whose key it is.
--
-- Applied BY HAND (or by `make platform-migrate STAGE=...`). init_db() is
-- create_all, which adds missing TABLES but never columns, so a database that
-- has ever run the control plane will not pick these up on its own.
-- `make postgres-init` replays this directory for local volumes.
--
-- ORDERING: apply BEFORE deploying the code that reads these columns. The old
-- API never selects them; the new one 500s on UndefinedColumn across all nine
-- tables, reads included. Unlike 0001 the Collector is unaffected.
--
-- No REFERENCES "user"(id), as in 0001: create_all emits them on a fresh
-- database, but these are nullable and display-only, so the lock is not worth
-- it. The divergence is permanent -- a fresh database's FKs are ON DELETE SET
-- NULL (models.actor_fk_column), while here a deleted user's id simply stays
-- and resolves to no email. Same display either way.
--
-- NULL means "no human actor": a seed row, a key with no owner, or a row
-- predating this migration. No sentinel, and no backfill -- the information
-- never existed.
--
-- Idempotent (IF NOT EXISTS, backfills guarded on IS NULL).

-- organization -------------------------------------------------------------
ALTER TABLE organization ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE organization ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE organization ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;

-- organization_member ------------------------------------------------------
ALTER TABLE organization_member ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE organization_member ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE organization_member ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;

-- invitation ---------------------------------------------------------------
-- invited_by_user_id already exists; only the revoker was missing, so the
-- revoke trail now matches Ban and devtoken.
ALTER TABLE invitation ADD COLUMN IF NOT EXISTS revoked_by_user_id varchar;

-- project ------------------------------------------------------------------
ALTER TABLE project ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE project ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE project ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;

-- devtoken -----------------------------------------------------------------
-- created_by_user_id minted the key; owner_user_id owns it. They differ when
-- an admin mints for a teammate -- the case the offboarding sweep exists for.
ALTER TABLE devtoken ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE devtoken ADD COLUMN IF NOT EXISTS owner_user_id varchar;
-- The only indexed actor column: remove_member queries by it. The name matches
-- SQLAlchemy's ix_<table>_<column> default so migrated and fresh agree.
CREATE INDEX IF NOT EXISTS ix_devtoken_owner_user_id ON devtoken (owner_user_id);

-- agent --------------------------------------------------------------------
-- updated_at already exists (models.py:274).
ALTER TABLE agent ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE agent ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;

-- agent_version ------------------------------------------------------------
-- Immutable snapshot: creator only, no update trail.
ALTER TABLE agent_version ADD COLUMN IF NOT EXISTS created_by_user_id varchar;

-- policy_module ------------------------------------------------------------
-- updated_at already exists (models.py:383).
ALTER TABLE policy_module ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE policy_module ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;

-- role_binding -------------------------------------------------------------
-- No timestamps today. set_roles replaces every row on each write, so a row's
-- creation IS its last write and an updated_* pair would duplicate it.
ALTER TABLE role_binding ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE role_binding ADD COLUMN IF NOT EXISTS created_by_user_id varchar;

-- Backfills ----------------------------------------------------------------
-- The model declares updated_at non-Optional, so every row needs a value, and
-- created_at is the true one for a row never updated since.
UPDATE organization        SET updated_at = created_at WHERE updated_at IS NULL;
UPDATE organization_member SET updated_at = created_at WHERE updated_at IS NULL;
UPDATE project             SET updated_at = created_at WHERE updated_at IS NULL;
-- role_binding has no created_at to copy, so old rows carry the migration
-- timestamp. Wrong-but-bounded, unlike a nullable column the model calls
-- non-null.
UPDATE role_binding SET created_at = now() WHERE created_at IS NULL;
