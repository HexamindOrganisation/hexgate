-- Closes a fresh-vs-migrated divergence that 0002 left on every stage it ran on.
--
-- The models declare these four non-Optional, so create_all emits NOT NULL on a
-- fresh database. 0002 added them with a bare `timestamptz` and backfilled
-- values, but never issued SET NOT NULL -- so staging and prod are nullable
-- where every fresh database is not. Found by the upgrade-path test added
-- alongside this file, not by reading 0002: the defect is in what the migration
-- omits, which only executing it reveals.
--
-- Exposure is low. Every application write goes through the model's
-- default_factory, so the API cannot store a NULL. A manual INSERT or a restore
-- could, and such a row fails to hydrate -- SQLModel types the attribute
-- `datetime`, not `datetime | None`.
--
-- A new file rather than an edit to 0002: 0002 has already been applied to both
-- stages, and a migration that changes meaning after it has run somewhere is a
-- habit worth not starting. 0004 sorts after it, so the backfill it depends on
-- has always run.
--
-- The backfill is repeated before each SET NOT NULL. A row inserted since 0002
-- cannot be NULL, so this is normally a no-op -- but it costs nothing and makes
-- each block correct on its own rather than only in sequence after 0002.
--
-- SET NOT NULL is a no-op on a column that already has it, so this is idempotent
-- and correct on a fresh database too. It takes ACCESS EXCLUSIVE and scans the
-- table to validate: trivial at control-plane row counts, and it inherits
-- migrate.sh's lock_timeout on a live stack. It will FAIL LOUDLY if any row
-- actually holds a NULL, which is the right outcome -- that means something
-- inserted outside the model, and an operator should see it.
--
-- Guarded per the convention in 0002: every migration guards each table it
-- touches.

-- organization -------------------------------------------------------------
DO $$
BEGIN
  IF to_regclass('organization') IS NULL THEN
    RAISE NOTICE 'organization absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  UPDATE organization SET updated_at = created_at WHERE updated_at IS NULL;
  ALTER TABLE organization ALTER COLUMN updated_at SET NOT NULL;
END $$;

-- organization_member ------------------------------------------------------
DO $$
BEGIN
  IF to_regclass('organization_member') IS NULL THEN
    RAISE NOTICE 'organization_member absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  UPDATE organization_member SET updated_at = created_at WHERE updated_at IS NULL;
  ALTER TABLE organization_member ALTER COLUMN updated_at SET NOT NULL;
END $$;

-- project ------------------------------------------------------------------
DO $$
BEGIN
  IF to_regclass('project') IS NULL THEN
    RAISE NOTICE 'project absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  UPDATE project SET updated_at = created_at WHERE updated_at IS NULL;
  ALTER TABLE project ALTER COLUMN updated_at SET NOT NULL;
END $$;

-- role_binding -------------------------------------------------------------
-- No created_at to copy from, so old rows carry the migration timestamp --
-- matching 0002's own backfill for this column.
DO $$
BEGIN
  IF to_regclass('role_binding') IS NULL THEN
    RAISE NOTICE 'role_binding absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  UPDATE role_binding SET created_at = now() WHERE created_at IS NULL;
  ALTER TABLE role_binding ALTER COLUMN created_at SET NOT NULL;
END $$;
