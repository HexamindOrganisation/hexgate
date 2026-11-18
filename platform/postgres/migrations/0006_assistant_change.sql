-- Adds the two columns the policy assistant needs on tables that already exist:
-- the org's assistant setting, and the change that last wrote a policy file.
-- The assistant_change table itself is new, so create_all builds it whole and
-- it needs nothing here.
--
-- Same conventions as 0002: guarded per table, idempotent, no REFERENCES
-- clause. Apply BEFORE deploying the code that reads them. Without them, on a
-- deploy stack (HEXGATE_SEED=skip) the API boots and every route that loads an
-- organization or a policy_file returns 500 on UndefinedColumn; where the seed
-- runs (local, or self-hosted without HEXGATE_SEED=skip) the API does not boot,
-- because the seed reads the default organization.
--
-- Safe to run early. The old code never selects either column, and
-- assistant_mode carries a DEFAULT, so an org the old code inserts after this
-- has run still gets a value.

-- organization -------------------------------------------------------------
-- NOT NULL with a constant DEFAULT: Postgres 11+ stores the default in the
-- catalogue and fills existing rows without rewriting the table. Every org
-- predating this starts at read_write, the same as a new one.
DO $$
BEGIN
  IF to_regclass('organization') IS NULL THEN
    RAISE NOTICE 'organization absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  ALTER TABLE organization ADD COLUMN IF NOT EXISTS assistant_mode varchar
    NOT NULL DEFAULT 'read_write';
END $$;

-- policy_file --------------------------------------------------------------
-- NULL for every file written before this: no change wrote it.
DO $$
BEGIN
  IF to_regclass('policy_file') IS NULL THEN
    RAISE NOTICE 'policy_file absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  ALTER TABLE policy_file ADD COLUMN IF NOT EXISTS updated_via_change_id varchar;
  CREATE INDEX IF NOT EXISTS ix_policy_file_updated_via_change_id
    ON policy_file (updated_via_change_id);
END $$;
