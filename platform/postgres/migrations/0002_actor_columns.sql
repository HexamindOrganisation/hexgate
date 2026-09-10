-- Adds the control-plane actor trail (issue #160): who created a row, who last
-- updated it, and -- on devtoken -- whose key it is.
--
-- Applied BY HAND (or by `make platform-migrate STAGE=...`) on deployed
-- environments, not by a runner. init_db() is SQLModel.metadata.create_all
-- (platform/api/hexgate_api/core/db.py:81), which creates missing TABLES and
-- never adds columns to a table that already exists -- so a database that has
-- ever run the control plane will not pick these up on its own.
-- `make postgres-init` replays this directory for local volumes.
--
-- ORDERING: apply this BEFORE deploying the code that references the columns.
-- Additive and back-compatible in that direction (a running old API never
-- selects them). In the other direction every route that touches one of these
-- nine tables 500s on an UndefinedColumn -- reads included, so the dashboard is
-- blank, not just degraded. Unlike 0001 this does NOT affect the Collector: its
-- snapshot query selects id/project_id and filters revoked_at only.
--
-- No REFERENCES "user"(id) clauses, for the same reason as 0001: create_all
-- emits them on a fresh database (every field below declares
-- foreign_key="user.id", matching Ban.created_by_user_id), but the columns are
-- nullable and only ever read for display, so adding constraints to live tables
-- is not worth the lock. The divergence is confined to pre-existing volumes.
--
-- NULL means "no human actor" -- a first-boot seed row, an SDK write from a key
-- with no recorded owner, or a row that predates this migration. There is no
-- sentinel actor: these columns are FKs to "user" on a fresh database, so a
-- sentinel string could not be stored. No historical actor can be backfilled
-- either; the information never existed.
--
-- Idempotent (IF NOT EXISTS throughout, and the backfills are guarded on
-- IS NULL), so re-running is a no-op.

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
-- created_by_user_id is the minter; owner_user_id is whose key it is. They
-- differ when an admin mints on a teammate's behalf, which is the whole point
-- of the offboarding sweep (features/members/service.py:remove_member).
ALTER TABLE devtoken ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE devtoken ADD COLUMN IF NOT EXISTS owner_user_id varchar;
-- The only actor column with an index: remove_member queries by it on every
-- member removal. Name matches SQLAlchemy's ix_<table>_<column> default so a
-- fresh create_all database and a migrated one agree.
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
-- No timestamps at all today. set_roles replaces the whole row set on every
-- write (features/policy_modules/service.py:set_roles), so a row's creation IS
-- its last write -- created_at + created_by_user_id carry the full trail and an
-- updated_* pair would always duplicate them.
ALTER TABLE role_binding ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE role_binding ADD COLUMN IF NOT EXISTS created_by_user_id varchar;

-- Backfills ----------------------------------------------------------------
-- The model declares updated_at non-Optional (matching Agent), so every row
-- needs a value. created_at is the semantically true one: a row never updated
-- since creation was last written when it was created.
UPDATE organization        SET updated_at = created_at WHERE updated_at IS NULL;
UPDATE organization_member SET updated_at = created_at WHERE updated_at IS NULL;
UPDATE project             SET updated_at = created_at WHERE updated_at IS NULL;
-- role_binding has no created_at to copy, so pre-existing rows carry the
-- migration timestamp rather than their true creation time. Stated rather than
-- hidden: it is wrong-but-bounded, and the alternative (a nullable column the
-- model says is non-null) is wrong-and-unbounded.
UPDATE role_binding SET created_at = now() WHERE created_at IS NULL;
